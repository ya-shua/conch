"""面板：网页面 + 只读接口 + 少量写接口（配置、人设）。

安全按文档来：只监听回环、校验 Host、非 GET 要 JSON、令牌定长比较。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .. import log
from ..config import DEFAULT_STRATEGY

log_ = log.get("panel")

STATIC = Path(__file__).parent / "static"


def _host_ok(request: web.Request) -> bool:
    host = request.headers.get("Host", "")
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return name in ("127.0.0.1", "localhost", "[::1]")


def _token_ok(request: web.Request, expected: str) -> bool:
    given = request.headers.get("x-console-token") or request.query.get("token") or ""
    if not given or not expected:
        return False
    return hmac.compare_digest(given, expected)


AUTH_FILE = "panel_auth.json"
SESSION_COOKIE = "conch_session"
SESSION_DAYS = 30
OPEN_PATHS = ("/", "/api/login", "/api/auth")
_PBKDF2_ROUNDS = 200_000


def _auth_path(cfg) -> Path:
    return Path(cfg.data_dir) / AUTH_FILE


def _load_auth(cfg) -> dict:
    p = _auth_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_auth(cfg, data: dict) -> None:
    p = _auth_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _secret(cfg) -> str:
    data = _load_auth(cfg)
    if not data.get("session_secret"):
        data["session_secret"] = secrets.token_hex(32)
        _save_auth(cfg, data)
    return data["session_secret"]


def _password_set(cfg) -> bool:
    return bool(_load_auth(cfg).get("hash"))


def _password_ok(cfg, given: str) -> bool:
    data = _load_auth(cfg)
    if not data.get("hash") or not given:
        return False
    try:
        salt = bytes.fromhex(str(data.get("salt") or ""))
        rounds = int(data.get("rounds") or _PBKDF2_ROUNDS)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", given.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(calc.hex(), str(data["hash"]))


def _make_session(cfg) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"v1.{exp}"
    sig = hmac.new(_secret(cfg).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _session_ok(cfg, value: str | None) -> bool:
    """cookie 是「版本.过期时间.签名」，签名用落盘的密钥，改密码会换密钥。"""
    if not value or value.count(".") != 2:
        return False
    ver, exp_s, sig = value.split(".")
    payload = f"{ver}.{exp_s}"
    want = hmac.new(_secret(cfg).encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return False
    try:
        return int(exp_s) > time.time()
    except ValueError:
        return False


def _authed(cfg, request: web.Request) -> bool:
    if _token_ok(request, cfg.panel.token):
        return True
    return _session_ok(cfg, request.cookies.get(SESSION_COOKIE))


@web.middleware
async def guard(request: web.Request, handler):
    if not _host_ok(request):
        return web.json_response({"error": "bad host"}, status=403)
    if request.method != "GET" and request.content_type != "application/json":
        return web.json_response({"error": "content-type must be application/json"}, status=403)
    cfg = request.app["cfg"]
    if _authed(cfg, request):
        return await handler(request)
    if not _password_set(cfg):
        return await handler(request)  # 还没设密码，跟以前一样
    if request.path in OPEN_PATHS:
        return await handler(request)
    return web.json_response({"error": "需要登录", "need_login": True}, status=401)


# ---------- 页面 ----------


async def index(_request: web.Request) -> web.Response:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # 面板是活文件，别让浏览器拿旧副本（改完刷新就该看到）
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


# ---------- 状态 ----------


async def api_status(request: web.Request) -> web.Response:
    app = request.app
    state = app["state"]
    onebot = state["onebot"]
    loader = state["loader"]
    store = state["store"]
    since = int(time.time() * 1000) - 24 * 3600 * 1000
    total = store.usage_total(since)
    plugins = loader.list()
    return web.json_response(
        {
            "ok": True,
            "connected": bool(onebot and onebot.connected),
            "self_id": (onebot.self_id if onebot else ""),
            "ws_url": app["cfg"].onebot.ws_url,
            "plugins": len(plugins),
            "plugins_enabled": sum(1 for p in plugins if p["status"] == "enabled"),
            "uptime_s": int(time.time() - state["started_at"]),
            "model": app["cfg"].model.model,
            "merge_window_ms": app["cfg"].merge_window_ms,
            "api_key_set": bool(app["cfg"].model.api_key),
            "today": total,
        }
    )


async def api_plugins(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "plugins": request.app["state"]["loader"].list()})


async def api_toggle(request: web.Request) -> web.Response:
    pid = request.match_info["pid"]
    loader = request.app["state"]["loader"]
    if pid not in loader.metas:
        return web.json_response({"error": "no such plugin"}, status=404)
    body: dict[str, Any] = await request.json()
    try:
        if body.get("enabled", True):
            await loader.enable(pid)
        else:
            await loader.disable(pid)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "status": loader.status.get(pid)})


# ---------- 用量 ----------


async def api_usage(request: web.Request) -> web.Response:
    store = request.app["state"]["store"]
    try:
        days = max(1, min(90, int(request.query.get("days", "7"))))
    except ValueError:
        days = 7
    since = int(time.time() * 1000) - days * 24 * 3600 * 1000
    return web.json_response(
        {
            "ok": True,
            "days": days,
            "total": store.usage_total(since),
            "by_plugin": store.usage_summary(since, group_by=("plugin_id", "feature")),
            "by_conv": store.usage_summary(since, group_by=("conv_id",))[:30],
        }
    )


# ---------- 配置 ----------


def _raw_config(request: web.Request) -> dict[str, Any]:
    path = Path(request.app["cfg_path"])
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


async def api_update_check(request: web.Request) -> web.Response:
    """问 GitHub 有没有新版（只读，不带凭据）。"""
    from .. import updater

    data = await updater.check()
    return web.json_response(data)


async def api_update_apply(request: web.Request) -> web.Response:
    """一键更新：git pull --ff-only。失败原样报错，不 --force。"""
    from .. import updater

    data = await updater.apply_update()
    return web.json_response(data)


async def api_groups(request: web.Request) -> web.Response:
    """她已经能看到的群（遥控发消息时用的就是这个名单）。"""
    store = request.app["state"]["store"]
    groups = {}
    # 插件改过名（qq_bridge → conch），两个键都认
    for ns in ("conch", "qq_bridge"):
        try:
            groups = store.kv_get(ns, "groups", {}) or {}
        except Exception:
            groups = {}
        if groups:
            break
    allow = request.app["cfg"].allow
    items = [
        {
            "group_id": gid,
            "name": name or gid,
            "allowed": (not allow.groups) or gid in allow.groups,
        }
        for gid, name in sorted(groups.items(), key=lambda kv: kv[0])
    ]
    return web.json_response({"ok": True, "groups": items})


async def api_settings_get(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    raw = _raw_config(request)
    strategy = {**DEFAULT_STRATEGY, **(raw.get("strategy") or {})}
    return web.json_response(
        {
            "ok": True,
            "owner_qq": cfg.owner_qq,
            "merge_window_ms": cfg.merge_window_ms,
            "allow": {
                "groups": cfg.allow.groups,
                "private": cfg.allow.private,
                "deny_groups": cfg.allow.deny_groups,
                "deny_private": cfg.allow.deny_private,
                "allow_all_when_empty": cfg.allow.allow_all_when_empty,
            },
            "strategy": strategy,
            "model": {
                "base_url": cfg.model.base_url,
                "model": cfg.model.model,
                "label": cfg.model.label,
                "has_key": bool(cfg.model.api_key),
                "key_hint": ("····" + cfg.model.api_key[-4:]) if cfg.model.api_key else "",
            },
        }
    )


async def api_models_list(request: web.Request) -> web.Response:
    """按当前的地址和 Key 拉一次 /models，给她自己挑模型用。

    中转站不一定实现这个接口，所以拉不到不是错，前端就当没拉到。
    """
    import re as _re

    cfg = request.app["cfg"]
    body: dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    base_url = str(body.get("base_url") or cfg.model.base_url or "").strip().rstrip("/")
    api_key = str(body.get("api_key") or cfg.model.api_key or "").strip()
    if not base_url:
        return web.json_response({"error": "还没填接口地址"}, status=400)
    if not api_key:
        return web.json_response({"error": "还没填 API Key"}, status=400)

    from aiohttp import ClientSession, ClientTimeout

    url = base_url + "/models"
    timeout = ClientTimeout(total=20)
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {api_key}", "User-Agent": "conch-panel"},
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return web.json_response(
                        {"error": f"HTTP {resp.status}：{text[:160]}"}, status=502
                    )
                data = json.loads(text)
    except Exception as exc:
        return web.json_response({"error": f"拉不到：{exc}"}, status=502)

    items = data.get("data") if isinstance(data, dict) else data
    ids = []
    for item in items or []:
        if isinstance(item, dict):
            mid = str(item.get("id") or item.get("model") or "").strip()
        else:
            mid = str(item).strip()
        if mid:
            ids.append(mid)
    ids.sort()
    return web.json_response({"ok": True, "models": ids, "count": len(ids)})


async def api_settings_put(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    body: dict[str, Any] = await request.json()

    raw = _raw_config(request)
    changed: list[str] = []

    if "owner_qq" in body:
        cfg.owner_qq = str(body["owner_qq"]).strip()
        raw["owner_qq"] = cfg.owner_qq
        changed.append("owner_qq")

    if "merge_window_ms" in body:
        value = max(0, int(body["merge_window_ms"]))
        cfg.merge_window_ms = value
        raw["merge_window_ms"] = value
        changed.append("merge_window_ms")

    if "allow" in body and isinstance(body["allow"], dict):
        allow = body["allow"]
        for key in ("groups", "private", "deny_groups", "deny_private"):
            if key in allow and isinstance(allow[key], list):
                values = [str(v).strip() for v in allow[key] if str(v).strip().isdigit()]
                setattr(cfg.allow, key, values)
                raw.setdefault("allow", {})[key] = values
                changed.append(f"allow.{key}")
        if "allow_all_when_empty" in allow:
            cfg.allow.allow_all_when_empty = bool(allow["allow_all_when_empty"])
            raw.setdefault("allow", {})["allow_all_when_empty"] = cfg.allow.allow_all_when_empty
            changed.append("allow.allow_all_when_empty")

    if "strategy" in body and isinstance(body["strategy"], dict):
        merged = {**DEFAULT_STRATEGY, **(raw.get("strategy") or {}), **body["strategy"]}
        merged["cooldown_sec"] = max(0, int(merged.get("cooldown_sec", 20)))
        merged["max_per_hour"] = max(1, int(merged.get("max_per_hour", 30)))
        merged["max_consecutive"] = max(1, int(merged.get("max_consecutive", 3)))
        raw["strategy"] = merged
        changed.append("strategy")

    if isinstance(body.get("model"), dict):
        patch = body["model"]
        raw_model = raw.setdefault("model", {})
        if patch.get("model"):
            cfg.model.model = str(patch["model"]).strip()
            raw_model["model"] = cfg.model.model
            changed.append("model.model")
        if patch.get("base_url"):
            cfg.model.base_url = str(patch["base_url"]).strip()
            raw_model["base_url"] = cfg.model.base_url
            changed.append("model.base_url")
        if patch.get("api_key"):
            cfg.model.api_key = str(patch["api_key"]).strip()
            raw_model["api_key"] = cfg.model.api_key
            changed.append("model.api_key")
        if "label" in patch:
            cfg.model.label = str(patch["label"] or "").strip()
            raw_model["label"] = cfg.model.label
            changed.append("model.label")

    try:
        Path(request.app["cfg_path"]).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        return web.json_response({"error": f"写配置失败：{exc}"}, status=500)

    log_.info("配置已更新：%s", "、".join(changed) or "无变化")
    return web.json_response({"ok": True, "changed": changed})


# ---------- 人设 ----------


def _roles_dir(request: web.Request) -> Path:
    path = Path(request.app["cfg"].roles_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_role(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ")
    return cleaned.strip()


async def api_roles_list(request: web.Request) -> web.Response:
    folder = _roles_dir(request)
    items = []
    for f in sorted(folder.glob("*.md")):
        items.append(
            {
                "name": f.stem,
                "size": f.stat().st_size,
                "mtime": int(f.stat().st_mtime * 1000),
            }
        )
    return web.json_response({"ok": True, "roles": items})


async def api_role_get(request: web.Request) -> web.Response:
    name = _safe_role(request.match_info["name"])
    f = _roles_dir(request) / f"{name}.md"
    if not f.exists():
        return web.json_response({"error": "没有这个人设"}, status=404)
    return web.json_response({"ok": True, "name": name, "content": f.read_text(encoding="utf-8")})


async def api_role_put(request: web.Request) -> web.Response:
    name = _safe_role(request.match_info["name"])
    if not name:
        return web.json_response({"error": "人设名不能为空"}, status=400)
    body: dict[str, Any] = await request.json()
    content = str(body.get("content") or "")
    if len(content) > 20000:
        return web.json_response({"error": "人设超过 20000 字"}, status=400)
    (_roles_dir(request) / f"{name}.md").write_text(content, encoding="utf-8")
    log_.info("人设已保存：%s（%s 字）", name, len(content))
    return web.json_response({"ok": True, "name": name})


# ---------- 日志 ----------


async def api_logs(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "lines": list(log.BUFFER)})


# ---------- 聊天流水 ----------


async def api_message_convs(request: web.Request) -> web.Response:
    store = request.app["state"]["store"]
    return web.json_response({"ok": True, "convs": store.message_convs()})


async def api_messages(request: web.Request) -> web.Response:
    store = request.app["state"]["store"]
    conv = request.query.get("conv") or None
    before = request.query.get("before")
    rows = store.message_list(
        conv_id=conv,
        limit=int(request.query.get("limit", "80") or 80),
        before_id=int(before) if before else None,
    )
    return web.json_response({"ok": True, "messages": rows})


async def api_messages_clear(request: web.Request) -> web.Response:
    store = request.app["state"]["store"]
    body: dict[str, Any] = await request.json()
    conv = str(body.get("conv_id") or "")
    if not conv:
        return web.json_response({"error": "缺少 conv_id"}, status=400)
    removed = store.message_clear(conv)
    log_.info("清空聊天记录 %s（%s 条）", conv, removed)
    return web.json_response({"ok": True, "removed": removed})


# ---------- 词库 ----------


async def api_lexicon(request: web.Request) -> web.Response:
    store = request.app["state"]["store"]
    return web.json_response({"ok": True, "items": store.lexicon_list()})


async def api_lexicon_add(request: web.Request) -> web.Response:
    store = request.app["state"]["store"]
    body: dict[str, Any] = await request.json()
    term = str(body.get("term") or "").strip()
    if not term:
        return web.json_response({"error": "词条不能为空"}, status=400)
    kind = str(body.get("kind") or "slang")
    if kind not in ("slang", "keyword", "alias"):
        kind = "slang"
    store.lexicon_add(term, kind, str(body.get("note") or ""))
    return web.json_response({"ok": True})


async def api_lexicon_remove(request: web.Request) -> web.Response:
    store = request.app["state"]["store"]
    body: dict[str, Any] = await request.json()
    term = str(body.get("term") or "")
    return web.json_response({"ok": True, "removed": store.lexicon_remove(term)})


# ---------- 审批 ----------


async def api_approvals(request: web.Request) -> web.Response:
    store = request.app["state"]["store"]
    return web.json_response({"ok": True, "items": store.approval_list("pending")})


async def api_approval_decide(request: web.Request) -> web.Response:
    app = request.app
    store = app["state"]["store"]
    onebot = app["state"]["onebot"]
    body: dict[str, Any] = await request.json()
    approval_id = int(body.get("id") or 0)
    status = str(body.get("status") or "")
    if not approval_id or status not in ("approved", "rejected"):
        return web.json_response({"error": "参数不对"}, status=400)

    pending = {a["id"]: a for a in store.approval_list("pending")}
    item = pending.get(approval_id)
    if item is None:
        return web.json_response({"error": "这条已经不在了"}, status=404)

    if not store.approval_decide(approval_id, status):
        return web.json_response({"error": "状态已变"}, status=409)

    if status == "approved":
        reply = str((item.get("payload") or {}).get("reply") or "").strip()
        if reply and onebot is not None:
            from ..message import Segment

            try:
                await onebot.send(item["conv_id"], [Segment("text", {"text": reply})])
                store.message_add(item["conv_id"], "out", reply, plugin_id="qq_bridge")
            except Exception as exc:
                log_.error("审批通过但发送失败：%s", exc)
                return web.json_response({"error": f"已通过，发送失败：{exc}"}, status=502)
        log_.info("审批通过 #%s → %s", approval_id, item["conv_id"])
    else:
        log_.info("审批拒绝 #%s", approval_id)
    return web.json_response({"ok": True})


# ---------- 会话操作 ----------


async def api_conversation_action(request: web.Request) -> web.Response:
    app = request.app
    cfg = app["cfg"]
    store = app["state"]["store"]
    body: dict[str, Any] = await request.json()
    conv = str(body.get("conv_id") or "")
    action = str(body.get("action") or "")
    if ":" not in conv:
        return web.json_response({"error": "会话 id 不对"}, status=400)

    kind, target = conv.split(":", 1)
    if action == "deny":
        bucket = cfg.allow.deny_groups if kind == "group" else cfg.allow.deny_private
        if target not in bucket:
            bucket.append(target)
        raw = _raw_config(request)
        key = "deny_groups" if kind == "group" else "deny_private"
        raw.setdefault("allow", {})[key] = bucket
        Path(app["cfg_path"]).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        log_.info("拉黑会话 %s", conv)
        return web.json_response({"ok": True, "action": "denied"})

    if action == "reset":
        store.session_upsert(conv, ai_session_id=None, first_wake=0)
        log_.info("重置会话记忆 %s", conv)
        return web.json_response({"ok": True, "action": "reset"})

    if action == "clear":
        removed = store.message_clear(conv)
        return web.json_response({"ok": True, "action": "cleared", "removed": removed})

    return web.json_response({"error": "未知操作"}, status=400)


# ---------- 测试对话 ----------


async def api_test_chat(request: web.Request) -> web.Response:
    app = request.app
    cfg = app["cfg"]
    store = app["state"]["store"]
    backend = app["state"].get("backend")
    if backend is None:
        return web.json_response({"error": "没有可用的模型后端"}, status=503)

    body: dict[str, Any] = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "说点什么"}, status=400)
    if len(text) > 4000:
        return web.json_response({"error": "太长了"}, status=400)

    role = _safe_role(str(body.get("role") or "default"))
    role_file = Path(cfg.roles_dir) / f"{role}.md"
    if role_file.exists():
        system = role_file.read_text(encoding="utf-8")
    else:
        system = "你是一个在 QQ 上聊天的人。回复要短，像真人打字。"

    extra = str(body.get("extra") or "").strip()
    if extra:
        system = system + "\n\n补充要求：" + extra

    messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
    started = time.time()
    try:
        reply, usage = await backend.chat(messages)
    except Exception as exc:
        log_.error("测试对话失败：%s", exc)
        return web.json_response({"error": str(exc)}, status=502)

    from ..pricing import estimate_cost

    model = cfg.model.model
    cost, peak, known = estimate_cost(
        model,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("cached_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        int(time.time() * 1000),
    )
    store.usage_add(
        {
            "ts": int(time.time() * 1000),
            "plugin_id": "qq_bridge",
            "feature": "test",
            "conv_id": "test:playground",
            "turn_id": str(int(time.time() * 1000)),
            "step": 0,
            "retry": 0,
            "model": model,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cost_yuan": cost,
            "peak": 1 if peak else 0,
        }
    )
    return web.json_response(
        {
            "ok": True,
            "reply": reply,
            "usage": usage,
            "cost_yuan": cost,
            "estimated": not known,
            "elapsed_ms": int((time.time() - started) * 1000),
            "role": role,
        }
    )


async def api_auth(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    return web.json_response(
        {
            "ok": True,
            "password_set": _password_set(cfg),
            "authed": _authed(cfg, request),
            # 浏览器里只认 cookie：URL 里的 token 是给脚本用的，不能等于"人已登录"
            "session": _session_ok(cfg, request.cookies.get(SESSION_COOKIE)),
        }
    )


def _login_response(cfg) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE,
        _make_session(cfg),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


async def api_login(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    if not _password_set(cfg):
        return web.json_response({"ok": True, "password_set": False})
    body: dict[str, Any] = await request.json()
    if not _password_ok(cfg, str(body.get("password") or "")):
        await asyncio.sleep(0.4)  # 慢半秒，挡一下暴力猜
        return web.json_response({"error": "密码不对"}, status=403)
    log_.info("面板登录成功")
    return _login_response(cfg)


async def api_logout(_request: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def api_set_password(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    if _password_set(cfg) and not _authed(cfg, request):
        return web.json_response({"error": "先登录再说"}, status=403)
    body: dict[str, Any] = await request.json()
    pw = str(body.get("password") or "")
    if len(pw) < 6:
        return web.json_response({"error": "太短了，至少 6 位"}, status=400)
    if len(pw) > 200:
        return web.json_response({"error": "太长了"}, status=400)

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ROUNDS).hex()
    data = _load_auth(cfg)
    data.update(
        {
            "salt": salt.hex(),
            "hash": digest,
            "rounds": _PBKDF2_ROUNDS,
            # 换密码就换密钥，别处的旧会话立刻失效
            "session_secret": secrets.token_hex(32),
            "updated_at": int(time.time() * 1000),
        }
    )
    _save_auth(cfg, data)
    log_.info("面板密码已更新")
    return _login_response(cfg)


async def api_verify_model(request: web.Request) -> web.Response:
    """验一次 Key：真的打一发请求，回什么不重要，只要能通。"""
    from dataclasses import replace

    from ..backend import OpenAICompatBackend

    cfg = request.app["cfg"]
    body: dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    model_cfg = replace(
        cfg.model,
        api_key=str(body.get("api_key") or cfg.model.api_key or "").strip(),
        base_url=str(body.get("base_url") or cfg.model.base_url or "").strip(),
        model=str(body.get("model") or cfg.model.model or "").strip(),
    )

    if not model_cfg.api_key:
        return web.json_response({"error": "还没填 API Key"}, status=400)
    if not model_cfg.base_url:
        return web.json_response({"error": "还没填接口地址"}, status=400)

    probe_cfg = replace(cfg, model=model_cfg)
    started = time.time()
    try:
        reply, usage = await OpenAICompatBackend(probe_cfg).chat(
            [{"role": "user", "content": "ping，回一个字就行"}], temperature=0
        )
    except Exception as exc:
        return web.json_response(
            {"error": str(exc), "model": model_cfg.model,
             "elapsed_ms": int((time.time() - started) * 1000)},
            status=502,
        )

    return web.json_response(
        {
            "ok": True,
            "model": model_cfg.model,
            "base_url": model_cfg.base_url,
            "reply": (reply or "").strip()[:60],
            "usage": usage,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    )


def create_app(*, cfg: Any, state: dict[str, Any], cfg_path: str) -> web.Application:
    app = web.Application(middlewares=[guard])
    app["cfg"] = cfg
    app["state"] = state
    app["cfg_path"] = cfg_path
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/plugins", api_plugins)
    app.router.add_post("/api/plugins/{pid}/toggle", api_toggle)
    app.router.add_get("/api/usage/summary", api_usage)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_put("/api/settings", api_settings_put)
    app.router.add_get("/api/roles", api_roles_list)
    app.router.add_get("/api/roles/{name}", api_role_get)
    app.router.add_put("/api/roles/{name}", api_role_put)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/messages", api_messages)
    app.router.add_get("/api/messages/convs", api_message_convs)
    app.router.add_post("/api/messages/clear", api_messages_clear)
    app.router.add_get("/api/lexicon", api_lexicon)
    app.router.add_post("/api/lexicon/add", api_lexicon_add)
    app.router.add_post("/api/lexicon/remove", api_lexicon_remove)
    app.router.add_get("/api/approvals", api_approvals)
    app.router.add_post("/api/approvals/decide", api_approval_decide)
    app.router.add_post("/api/conversation/action", api_conversation_action)
    app.router.add_post("/api/test-chat", api_test_chat)
    app.router.add_post("/api/verify-model", api_verify_model)
    app.router.add_post("/api/models/list", api_models_list)
    app.router.add_get("/api/auth", api_auth)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_post("/api/auth/password", api_set_password)
    app.router.add_get("/api/groups", api_groups)
    app.router.add_get("/api/update", api_update_check)
    app.router.add_post("/api/update/apply", api_update_apply)
    return app