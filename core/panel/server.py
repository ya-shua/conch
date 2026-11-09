"""面板：网页面 + 只读接口 + 少量写接口（配置、人设）。

安全按文档来：只监听回环、校验 Host、非 GET 要 JSON、令牌定长比较。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from core.version import __version__ as CONCH_VERSION
from .. import log
from ..config import (
    Admin,
    ConfigError,
    DEFAULT_STRATEGY,
    owner_ids_of,
    parse_admins,
)
from .. import websearch

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
OPEN_PATHS = (
    "/",
    "/api/login",
    "/api/auth",
    # 只读、无隐私的两个契约端点：探活和「我是谁」。别人接的时候不该先要令牌。
    "/api/health",
    "/api/manifest",
)
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


def _integrity_level() -> str:
    try:
        from core import integrity as _ig

        return _ig.current()
    except Exception:
        return "未知"


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
            "version": CONCH_VERSION,
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
            "integrity": _integrity_level(),
        }
    )


async def api_health(request: web.Request) -> web.Response:
    """契约端点（docs/插件对接契约.md）：只报「进程活着」。"""
    state = request.app["state"]
    return web.json_response(
        {
            "ok": True,
            "id": "conch",
            "version": CONCH_VERSION,
            "uptime_s": round(time.time() - state["started_at"], 1),
        }
    )


async def api_manifest(request: web.Request) -> web.Response:
    """我是谁、有哪些能力、怎么调 —— 让别的宿主/工具不读代码也能接上。"""
    app = request.app
    meta = {}
    try:
        meta_path = _meta_path()
        if meta_path and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except Exception:
        meta = {}
    return web.json_response(
        {
            "ok": True,
            "metadata": meta,
            "runtime": {
                "port": int(getattr(app["cfg"].panel, "port", 7801) or 7801),
                "pid": os.getpid(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "uptime_s": round(time.time() - app["state"]["started_at"], 1),
            },
            "capabilities": [
                {"name": "说话（人格 + 称呼 + 表情包）", "kind": "event", "how": "接 OneBot 消息"},
                {"name": "插件加载与一键启停", "kind": "http", "how": "POST /api/plugins/{pid}/toggle"},
                {"name": "主动开口（替插件说话）", "kind": "http", "how": "POST /api/proactive/say"},
                {"name": "记忆与用量", "kind": "http", "how": "GET /api/messages、/api/usage/summary"},
                {"name": "面板与设置", "kind": "http", "how": "GET /api/settings、/api/plugins"},
            ],
        }
    )


def _meta_path():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent.parent / "plugins" / "conch" / "metadata.json"


def _display_names(app) -> dict[str, str]:
    """用户给插件/服务起的名字：{id: 名字}。没起过的就不在表里。"""
    cfg = app["cfg"]
    raw = getattr(cfg, "raw", None) or {}
    got = raw.get("display_names")
    if not isinstance(got, dict):
        return {}
    return {str(k): str(v) for k, v in got.items() if str(v).strip()}


def _port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
            return True
    except OSError:
        return False


def _meta_port(meta: dict[str, Any], page: str = "") -> int:
    """插件自己的端口：先看 metadata.port，再看 launch.port，最后从 page 地址里抠。"""
    raw = meta.get("port")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    launch = meta.get("launch") or {}
    if isinstance(launch, dict) and launch.get("port"):
        try:
            return int(launch["port"])
        except (TypeError, ValueError):
            pass
    if page:
        try:
            from urllib.parse import urlparse

            return int(urlparse(page).port or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _plugin_meta(path: str) -> dict[str, Any]:
    """读插件目录里 metadata.json 的可选字段（页面地址、端口、是否有设置）。"""
    try:
        meta = json.loads((Path(path) / "metadata.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    extra: dict[str, Any] = {}
    if meta.get("name_en"):
        extra["name_en"] = str(meta["name_en"])
    page = str(meta.get("page") or ((meta.get("launch") or {}).get("url") if isinstance(meta.get("launch"), dict) else "") or "")
    if page:
        extra["page"] = page
    port = _meta_port(meta, page)
    if port:
        extra["port"] = port
    schema = meta.get("settings")
    if isinstance(schema, list) and schema:
        extra["settings"] = True
        extra["schema"] = [f for f in schema if isinstance(f, dict) and f.get("key")]
    elif (Path(path) / "config.json").exists():
        extra["settings"] = True
        extra["schema"] = []
    return extra


async def api_plugins(request: web.Request) -> web.Response:
    items = request.app["state"]["loader"].list()
    names = _display_names(request.app)
    for item in items:
        item.update(_plugin_meta(str(item.get("path") or "")))
        item["alias"] = names.get(str(item.get("id")), "")
        # 统一口径：启用 + 声明的端口真的在听 = 运行中，其余一律已关闭
        port = int(item.get("port") or 0)
        up = _port_open(port) if port else True
        item["service_up"] = up
        item["running"] = item.get("status") == "enabled" and up
    # 插件有没有新版：只读缓存（不联网），点「检查更新」才真正去查
    try:
        from core import updater as _upd
        _updates = _upd.read_plugin_updates().get("items") or {}
    except Exception:
        _updates = {}
    for item in items:
        info = _updates.get(item.get("id"))
        if info:
            item["update"] = info

    return web.json_response({"ok": True, "plugins": items})


def _plugin_default(meta, key: str, default=None):
    schema = getattr(meta, "extra", None) or {}
    return default


def _schema_of(meta) -> list[dict]:
    raw = getattr(meta, "raw", None) or {}
    schema = raw.get("settings")
    if isinstance(schema, list):
        return [f for f in schema if isinstance(f, dict) and f.get("key")]
    return []


def _coerce(field: dict, value):
    """按 schema 把表单值变成该有的类型，越界就夹回范围里。"""
    typ = str(field.get("type") or "str")
    if typ == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)
    if typ in ("int", "number"):
        try:
            num = int(float(value))
        except (TypeError, ValueError):
            num = int(field.get("default") or 0)
        lo = field.get("min")
        hi = field.get("max")
        if isinstance(lo, (int, float)):
            num = max(int(lo), num)
        if isinstance(hi, (int, float)):
            num = min(int(hi), num)
        return num
    if typ == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(field.get("default") or 0)
    text = str(value if value is not None else "")
    limit = field.get("maxLength")
    if isinstance(limit, int) and limit > 0:
        text = text[:limit]
    return text


async def api_plugins_check_updates(request: web.Request) -> web.Response:
    """真正去 GitHub 查一遍（插件自己的页面负责更新，这里只回报有没有新版）。"""
    from core import updater

    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool((body or {}).get("force"))
    loader = request.app["state"]["loader"]
    metas = getattr(loader, "metas", None) or {}
    items = await updater.check_plugins(metas, force=force)
    return web.json_response({"ok": True, "items": items, "checked": len(items)})


# 谁能"一键更新"：目前只有 Tide —— 它自己带 POST /api/update，下载完会退出重启
UPDATE_CAPABLE = {"tide"}


async def api_plugin_apply_update(request: web.Request) -> web.Response:
    """替插件调它自己的更新接口。只转发，不碰插件文件。

    插件自己下载、自己重启；连接在半路断掉是正常的（它会把进程退掉）。
    """
    pid = str(request.match_info["pid"])
    if pid not in UPDATE_CAPABLE:
        return web.json_response(
            {"error": "这个插件没有一键更新，去它自己的页面更新"}, status=400
        )
    loader = request.app["state"]["loader"]
    meta = (getattr(loader, "metas", None) or {}).get(pid)
    page = ""
    if meta is not None:
        page = str(_plugin_meta(str(getattr(meta, "path", ""))).get("page") or "")
    if not page:
        return web.json_response({"error": "它没报页面地址，先把它启动起来"}, status=400)
    url = page.rstrip("/") + "/api/update"
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={}) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {}
                return web.json_response({"ok": True, "result": data})
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response(
            {"ok": True, "result": {"applying": True}, "note": "它已经开始更新并重启"}
        )


async def api_plugin_config(request: web.Request) -> web.Response:
    """插件自己的 config.json：支持表单（schema）和原样 JSON 两种改法。"""
    pid = request.match_info["pid"]
    loader = request.app["state"]["loader"]
    meta = loader.metas.get(pid)
    if meta is None:
        return web.json_response({"error": "no such plugin"}, status=404)
    path = Path(str(getattr(meta, "path", ""))) / "config.json"
    schema = _schema_of(meta)

    def _read() -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except Exception:
            return {}

    if request.method == "GET":
        current = _read()
        values = {}
        for field in schema:
            key = str(field["key"])
            values[key] = current.get(key, field.get("default"))
        return web.json_response(
            {
                "ok": True,
                "path": str(path),
                "schema": schema,
                "values": values,
                "raw": current,
                "text": json.dumps(current, ensure_ascii=False, indent=2),
            }
        )

    body: dict[str, Any] = await request.json()

    # 表单提交：{"values": {...}} 只改传上来的字段
    if isinstance(body.get("values"), dict):
        current = _read()
        updated = body["values"]
        by_key = {str(f["key"]): f for f in schema}
        for key, value in updated.items():
            field = by_key.get(key)
            if field is None:
                continue
            current[key] = _coerce(field, value)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            return web.json_response({"error": f"写不进去：{exc}"}, status=500)
        note = "改好了，重载这个插件就生效"
        if body.get("reload"):
            try:
                if loader.status.get(pid) == "enabled":
                    await loader.disable(pid)
                await loader.enable(pid)
                note = "改好了，插件已重载"
            except Exception as exc:
                note = f"配置写了，但重载失败：{exc}"
        return web.json_response({"ok": True, "path": str(path), "note": note, "values": current})

    # 原样 JSON 文本（老入口，留着）
    text = str(body.get("text") or "").strip()
    try:
        parsed = json.loads(text or "{}")
    except Exception as exc:
        return web.json_response({"error": f"不是合法 JSON：{exc}"}, status=400)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return web.json_response({"error": f"写不进去：{exc}"}, status=500)
    return web.json_response({"ok": True, "path": str(path), "note": "重启该插件或海螺后生效"})


# ---------- 插件管理 ----------


def _plugin_dirs(cfg) -> list[Path]:
    return [Path(cfg.plugins_dir), *[Path(p) for p in cfg.plugin_paths]]


def _find_git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for cand in (r"D:\Git\cmd\git.exe", r"C:\Program Files\Git\cmd\git.exe"):
        if Path(cand).exists():
            return cand
    return None


def _rmtree_force(path: Path) -> bool:
    """Windows 上插件目录里常有只读文件，先改权限再删。"""

    def _on_error(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except Exception:
            pass

    try:
        shutil.rmtree(path, onerror=_on_error)
        return not path.exists()
    except Exception:
        return False


async def api_plugin_remove(request: web.Request) -> web.Response:
    """删掉一个插件目录。只允许删 plugins/ 里自己装进来的。"""
    app = request.app
    pid = request.match_info["pid"]
    loader = app["state"]["loader"]
    cfg = app["cfg"]
    meta = loader.metas.get(pid)
    if meta is None:
        return web.json_response({"error": "没有这个插件"}, status=404)
    if getattr(meta, "builtin", False):
        return web.json_response({"error": "海螺自带的插件删不了，只能关掉"}, status=400)
    target = Path(str(getattr(meta, "path", ""))).resolve()
    plugins_dir = Path(cfg.plugins_dir).resolve()
    if plugins_dir not in target.parents:
        return web.json_response({"error": "这个插件不在 plugins/ 目录里，得自己去删"}, status=400)
    if loader.status.get(pid) == "enabled":
        try:
            await loader.disable(pid)
        except Exception:
            pass
    ok = _rmtree_force(target)
    loader.scan()
    if not ok:
        return web.json_response({"error": "删不干净，可能有文件被占用"}, status=500)
    return web.json_response({"ok": True, "removed": pid})


async def api_reload(request: web.Request) -> web.Response:
    pid = request.match_info["pid"]
    loader = request.app["state"]["loader"]
    if pid not in loader.metas:
        return web.json_response({"error": "没有这个插件"}, status=404)
    try:
        await loader.reload(pid)
    except Exception as exc:
        return web.json_response({"error": str(exc)[:200]}, status=500)
    return web.json_response({"ok": True, "status": loader.status.get(pid)})


async def api_reload_all(request: web.Request) -> web.Response:
    loader = request.app["state"]["loader"]
    result = await loader.reload_all()
    return web.json_response({"ok": True, **result})


def _save_enabled(app, pid: str, enabled: bool) -> None:
    """把启用状态记进 config.json 的 plugins_enabled。"""
    cfg = app["cfg"]
    raw = getattr(cfg, "raw", None)
    if not isinstance(raw, dict):
        return
    ids = [str(x) for x in (raw.get("plugins_enabled") or [])]
    if enabled and pid not in ids:
        ids.append(pid)
    if not enabled:
        ids = [x for x in ids if x != pid]
    raw["plugins_enabled"] = ids
    try:
        Path(app["cfg_path"]).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


async def api_toggle(request: web.Request) -> web.Response:
    pid = request.match_info["pid"]
    app = request.app
    loader = app["state"]["loader"]
    if pid not in loader.metas:
        return web.json_response({"error": "没有这个插件"}, status=404)
    body: dict[str, Any] = await request.json()
    want = bool(body.get("enabled"))
    try:
        if want:
            if loader.status.get(pid) != "enabled":
                await loader.enable(pid)
        else:
            if loader.status.get(pid) == "enabled":
                await loader.disable(pid)
    except Exception as exc:
        return web.json_response({"error": str(exc)[:200]}, status=500)
    _save_enabled(app, pid, want)
    return web.json_response({"ok": True, "enabled": want, "status": loader.status.get(pid)})


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


async def api_update_restart(request: web.Request) -> web.Response:
    """更新完重启自己：先回 ok，再换进程。"""
    from .. import updater

    data = await updater.restart_self()
    return web.json_response(data)


def _store_kv(request: web.Request, key: str, default):
    """插件改过名（qq_bridge → conch），两个命名空间都认。"""
    store = request.app["state"]["store"]
    for ns in ("conch", "qq_bridge"):
        try:
            got = store.kv_get(ns, key, None)
        except Exception:
            got = None
        if got:
            return got
    return default


async def api_groups(request: web.Request) -> web.Response:
    """她能看见的聊天：群 + 私聊。遥控发消息、白名单都对着这份名单。"""
    allow = request.app["cfg"].allow
    groups = _store_kv(request, "groups", {}) or {}

    # 好友名单：优先现拉，拉不到就用上次存的
    friends = _store_kv(request, "friends", {}) or {}
    client = request.app["state"].get("onebot")
    if client is not None:
        try:
            data = await client.call("get_friend_list")
            got = {}
            for it in data or []:
                uid = str(it.get("user_id") or "").strip()
                if not uid:
                    continue
                got[uid] = str(it.get("remark") or it.get("nickname") or uid).strip() or uid
            if got:
                friends = got
                try:
                    store = request.app["state"]["store"]
                    store.kv_put("conch", "friends", dict(got))
                except Exception:
                    pass
        except Exception:
            pass

    chats = [
        {
            "type": "group",
            "id": gid,
            "name": name or gid,
            "allowed": (not allow.groups) or gid in allow.groups,
            "denied": gid in allow.deny_groups,
        }
        for gid, name in sorted(groups.items(), key=lambda kv: kv[0])
    ] + [
        {
            "type": "private",
            "id": uid,
            "name": name or uid,
            "allowed": (not allow.private) or uid in allow.private,
            "denied": uid in allow.deny_private,
        }
        for uid, name in sorted(friends.items(), key=lambda kv: kv[0])
    ]

    groups_only = [c for c in chats if c["type"] == "group"]
    return web.json_response(
        {"ok": True, "chats": chats, "groups": groups_only, "total": len(chats)}
    )


async def api_settings_get(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    raw = _raw_config(request)
    strategy = {**DEFAULT_STRATEGY, **(raw.get("strategy") or {})}
    return web.json_response(
        {
            "ok": True,
            "owner_qq": "，".join(getattr(cfg, "owner_ids", None) or ([cfg.owner_qq] if cfg.owner_qq else [])),
            "admins": [card.to_dict() for card in (getattr(cfg, "admins", None) or [])],
            "merge_window_ms": cfg.merge_window_ms,
            "reply_gap_base_ms": cfg.reply_gap_base_ms,
            "reply_gap_max_ms": cfg.reply_gap_max_ms,
            "reply_max_lines": cfg.reply_max_lines,
            "catch_up_enabled": cfg.catch_up_enabled,
            "catch_up_minutes": cfg.catch_up_minutes,
            "closing_enabled": cfg.closing_enabled,
            "closing_words": cfg.closing_words,
            "allow": {
                "groups": cfg.allow.groups,
                "private": cfg.allow.private,
                "deny_groups": cfg.allow.deny_groups,
                "deny_private": cfg.allow.deny_private,
                "allow_all_when_empty": cfg.allow.allow_all_when_empty,
            },
            "strategy": strategy,
            "persona": str(raw.get("persona") or "default"),
            "model": {
                "base_url": cfg.model.base_url,
                "model": cfg.model.model,
                "label": cfg.model.label,
                "has_key": bool(cfg.model.api_key),
                "key_hint": ("····" + cfg.model.api_key[-4:]) if cfg.model.api_key else "",
            },
        }
    )


# ---------- 一键启动 ----------


def _launch_items(request: web.Request) -> list[dict[str, Any]]:
    from .. import launchers as _launchers

    roots: list[str] = []
    cfg = request.app.get("cfg")
    for d in (getattr(cfg, "plugins_dir", "") or "",):
        if d:
            roots.append(str(d))
    for d in (getattr(cfg, "plugin_paths", None) or []):
        if d:
            roots.append(str(d))
    try:
        return _launchers.load(request.app["cfg_path"], plugin_roots=roots)
    except TypeError:  # 老签名兜底
        return _launchers.load(request.app["cfg_path"])


async def api_launchers(request: web.Request) -> web.Response:
    from .. import launchers as _launchers

    items = _launch_items(request)
    names = _display_names(request.app)
    out = []
    for i in items:
        row = _launchers.status(i)
        row["alias"] = names.get(str(row.get("id")), "")
        out.append(row)
    return web.json_response({"items": out})


async def api_name_set(request: web.Request) -> web.Response:
    """给插件/服务起个名字（空字符串 = 恢复原名）。"""
    app = request.app
    body: dict[str, Any] = await request.json()
    pid = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()[:24]
    if not pid:
        return web.json_response({"error": "要指定是哪个"}, status=400)

    cfg = app["cfg"]
    raw = getattr(cfg, "raw", None)
    if not isinstance(raw, dict):
        return web.json_response({"error": "配置是只读的"}, status=500)
    names = raw.get("display_names")
    if not isinstance(names, dict):
        names = {}
    if name:
        names[pid] = name
    else:
        names.pop(pid, None)
    raw["display_names"] = names
    try:
        Path(app["cfg_path"]).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        return web.json_response({"error": f"没写进 config.json：{exc}"}, status=500)
    return web.json_response({"ok": True, "id": pid, "alias": name})


async def api_launcher_start(request: web.Request) -> web.Response:
    from .. import launchers as _launchers

    lid = request.match_info["id"]
    body: dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    items = _launch_items(request)
    item = next((i for i in items if str(i.get("id")) == lid), None)
    if item is None:
        return web.json_response({"error": "没有这一条"}, status=404)

    secrets = body.get("secrets") if isinstance(body.get("secrets"), dict) else {}
    if secrets:
        stored = dict(item.get("secret_values") or {})
        stored.update({str(k): str(v) for k, v in secrets.items() if v})
        item["secret_values"] = stored
        _launchers.save(request.app["cfg_path"], items)

    try:
        return web.json_response(_launchers.start(item, secrets))
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def api_launcher_stop_all(request: web.Request) -> web.Response:
    """一键关闭：把所有从控制台起的服务全停掉。"""
    from .. import launchers as _launchers

    items = _launch_items(request)
    return web.json_response(_launchers.stop_all(items))


async def api_shutdown(request: web.Request) -> web.Response:
    """关掉海螺自己。先回话，再退出，免得前端看到连接被重置。"""
    import asyncio as _asyncio
    import os as _os

    log_.info("收到一键关闭，海螺退出")
    _asyncio.get_running_loop().call_later(0.4, lambda: _os._exit(0))
    return web.json_response({"ok": True, "message": "海螺正在退出"})


async def api_launcher_restart(request: web.Request) -> web.Response:
    """服务重启 = 停 + 起（插件那边叫重载，这边叫重启，一个意思）。"""
    from .. import launchers as _launchers

    lid = request.match_info["id"]
    body: dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    items = _launch_items(request)
    item = next((i for i in items if str(i.get("id")) == lid), None)
    if item is None:
        return web.json_response({"error": "no such launcher"}, status=404)
    _launchers.stop(item)
    try:
        result = _launchers.start(item, body.get("secrets") or None)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, **result})


async def api_launcher_log(request: web.Request) -> web.Response:
    """看这个服务最近的日志（就在 data/logs/<id>.log）。"""
    from .. import launchers as _launchers

    lid = request.match_info["id"]
    try:
        lines = max(20, min(800, int(request.query.get("lines", "120"))))
    except ValueError:
        lines = 120
    path = _launchers.log_file(lid)
    if not path.exists():
        return web.json_response({"ok": True, "path": str(path), "text": "（还没有日志）"})
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return web.json_response({"error": f"读不到：{exc}"}, status=500)
    tail = "\n".join(text.splitlines()[-lines:])
    return web.json_response({"ok": True, "path": str(path), "text": tail})


async def api_launcher_stop(request: web.Request) -> web.Response:
    from .. import launchers as _launchers

    lid = request.match_info["id"]
    items = _launch_items(request)
    item = next((i for i in items if str(i.get("id")) == lid), None)
    if item is None:
        return web.json_response({"error": "没有这一条"}, status=404)
    return web.json_response(_launchers.stop(item))


def _mask_key(key: str) -> str:
    key = str(key or "")
    if len(key) <= 10:
        return ("*" * max(0, len(key) - 4)) + key[-4:] if key else ""
    return f"{key[:6]}…{key[-4:]}"


def _save_model_keys(request: web.Request, keys: list[dict]) -> None:
    """写回 config.json（model.keys），顺手让 model.api_key 跟第一把保持一致。

    一把都不剩的时候，`api_key` 也得清掉 —— 以前留着旧的，界面上显示「没有 Key」，
    实际调模型还在用那把删掉的（看着就是「删不掉」）。
    """
    cfg = request.app["cfg"]
    raw = _raw_config(request)
    model_raw = raw.setdefault("model", {})
    clean = [
        {"key": str(k.get("key") or "").strip(), "label": str(k.get("label") or "").strip()[:40]}
        for k in keys
        if str(k.get("key") or "").strip()
    ]
    model_raw["keys"] = clean
    if clean:
        model_raw["api_key"] = clean[0]["key"]
    else:
        model_raw.pop("api_key", None)
    cfg.model.keys = clean
    cfg.model.api_key = clean[0]["key"] if clean else ""
    Path(request.app["cfg_path"]).write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def api_model_keys(request: web.Request) -> web.Response:
    """看现在有几把 Key、按什么顺序、用着哪把。"""
    backend = request.app["state"].get("backend")
    keys = list(request.app["cfg"].model.keys or [])
    if not keys:
        single = str(request.app["cfg"].model.api_key or "").strip()
        if single:
            keys = [{"key": single, "label": "默认"}]
    out = [
        {"index": i, "label": k.get("label") or "", "tail": k["key"][-4:], "masked": _mask_key(k["key"])}
        for i, k in enumerate(keys)
    ]
    current = backend.current_index() if backend is not None else 0
    return web.json_response(
        {"ok": True, "keys": out, "current": current, "label": backend.current_label() if backend else ""}
    )


async def api_model_keys_put(request: web.Request) -> web.Response:
    """加 / 删 / 上移下移 / 改备注。"""
    cfg = request.app["cfg"]
    body: dict[str, Any] = await request.json()
    action = str(body.get("action") or "").strip()
    keys = [
        {"key": str(k.get("key") or "").strip(), "label": str(k.get("label") or "").strip()[:40]}
        for k in (cfg.model.keys or [])
        if str(k.get("key") or "").strip()
    ]
    if not keys and cfg.model.api_key:
        keys = [{"key": cfg.model.api_key, "label": "默认"}]

    try:
        index = int(body.get("index", -1))
    except (TypeError, ValueError):
        index = -1

    if action == "add":
        key = str(body.get("key") or "").strip()
        if not key:
            return web.json_response({"error": "Key 不能空"}, status=400)
        if any(k["key"] == key for k in keys):
            return web.json_response({"error": "这把 Key 已经在列表里了"}, status=400)
        pos = index if 0 <= index <= len(keys) else len(keys)
        keys.insert(pos, {"key": key, "label": str(body.get("label") or "").strip()[:40]})
    elif action == "remove":
        if not (0 <= index < len(keys)):
            return web.json_response({"error": "没有这一把"}, status=404)
        keys.pop(index)
    elif action == "move":
        delta = -1 if str(body.get("dir") or "") == "up" else 1
        if not (0 <= index < len(keys)):
            return web.json_response({"error": "没有这一把"}, status=404)
        target = index + delta
        if 0 <= target < len(keys):
            keys[index], keys[target] = keys[target], keys[index]
    elif action == "label":
        if not (0 <= index < len(keys)):
            return web.json_response({"error": "没有这一把"}, status=404)
        keys[index]["label"] = str(body.get("label") or "").strip()[:40]
    elif action == "replace":
        if not (0 <= index < len(keys)):
            return web.json_response({"error": "没有这一把"}, status=404)
        key = str(body.get("key") or "").strip()
        if not key:
            return web.json_response({"error": "Key 不能空"}, status=400)
        keys[index]["key"] = key
        if body.get("label") is not None:
            keys[index]["label"] = str(body.get("label") or "").strip()[:40]
    else:
        return web.json_response({"error": "不认识的操作"}, status=400)

    try:
        _save_model_keys(request, keys)
    except OSError as exc:
        return web.json_response({"error": f"写不进去：{exc}"}, status=500)

    backend = request.app["state"].get("backend")
    if backend is not None:
        backend._i = 0  # 结构变了，从第一把重新算
    return web.json_response({"ok": True, "count": len(keys)})


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
    api_key = str(body.get("api_key") or "").strip()
    used_label = "手填的 Key"
    used_index = -1
    if not api_key:
        # 没手填就用列表里的：`index` 指定第几把，不给就用当前在用的那把
        keys = list(cfg.model.keys or [])
        try:
            want = int(body.get("index", -1))
        except (TypeError, ValueError):
            want = -1
        if keys and 0 <= want < len(keys):
            api_key = str(keys[want].get("key") or "").strip()
            used_label = str(keys[want].get("label") or f"第 {want + 1} 把")
            used_index = want
        elif keys:
            api_key = str(cfg.model.api_key or keys[0].get("key") or "").strip()
            used_label = str(keys[0].get("label") or "第 1 把")
            used_index = 0
    if not base_url:
        return web.json_response({"error": "还没填接口地址"}, status=400)
    if not api_key:
        return web.json_response({"error": "还没有可用的 Key"}, status=400)

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
    return web.json_response(
        {"ok": True, "models": ids, "count": len(ids), "key_index": used_index, "key_label": used_label}
    )


async def api_settings_put(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    body: dict[str, Any] = await request.json()

    raw = _raw_config(request)
    changed: list[str] = []

    if "admins" in body:
        # 称呼卡：校验不过就整批拒绝（别存一半）
        try:
            cards = parse_admins(body["admins"])
        except ConfigError as exc:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}, ensure_ascii=False))
        raw["admins"] = [card.to_dict() for card in cards]
        cfg.admins = cards
        cfg.admins_explicit = True
        # 卡和「管理员 QQ」是两件事：这里只动卡，不碰 owner_qq（删光卡也得保留管理员身份）
        changed.append("admins")

    if "owner_qq" in body and "admins" not in body:
        # 管理员可以填多个（逗号隔开）：owner_qq 存第一个，owner_ids 存全部
        ids = owner_ids_of(body["owner_qq"])
        cfg.owner_ids = ids
        cfg.owner_qq = ids[0] if ids else ""
        raw["owner_qq"] = ids[0] if ids else ""
        # 只改管理员身份，不碰称呼卡：卡归「称呼卡」页管，别在这儿凭空造一张
        changed.append("owner_qq")

    if "merge_window_ms" in body:
        value = max(0, int(body["merge_window_ms"]))
        cfg.merge_window_ms = value
        raw["merge_window_ms"] = value
        changed.append("merge_window_ms")

    # 先校验再落值：直接改完再抛异常，内存里的值会被改脏（面板一刷新就看到没保存的数）
    want_base = max(0, int(body["reply_gap_base_ms"])) if "reply_gap_base_ms" in body else None
    want_max = max(0, int(body["reply_gap_max_ms"])) if "reply_gap_max_ms" in body else None
    if want_base is not None or want_max is not None:
        base = cfg.reply_gap_base_ms if want_base is None else want_base
        cap = cfg.reply_gap_max_ms if want_max is None else want_max
        if cap and cap < base:
            raise web.HTTPBadRequest(text='{"error": "句间停顿的上限不能比基础值小"}')
        if want_base is not None:
            cfg.reply_gap_base_ms = want_base
            raw["reply_gap_base_ms"] = want_base
            changed.append("reply_gap_base_ms")
        if want_max is not None:
            cfg.reply_gap_max_ms = want_max
            raw["reply_gap_max_ms"] = want_max
            changed.append("reply_gap_max_ms")

    if "reply_max_lines" in body:
        value = max(0, int(body["reply_max_lines"]))
        cfg.reply_max_lines = value
        raw["reply_max_lines"] = value
        changed.append("reply_max_lines")

    if "catch_up_enabled" in body:
        cfg.catch_up_enabled = bool(body["catch_up_enabled"])
        raw["catch_up_enabled"] = cfg.catch_up_enabled
        changed.append("catch_up_enabled")

    if "catch_up_minutes" in body:
        value = max(1, int(body["catch_up_minutes"]))
        cfg.catch_up_minutes = value
        raw["catch_up_minutes"] = value
        changed.append("catch_up_minutes")

    if "closing_enabled" in body:
        cfg.closing_enabled = bool(body["closing_enabled"])
        raw["closing_enabled"] = cfg.closing_enabled
        changed.append("closing_enabled")

    if "closing_words" in body:
        words = body["closing_words"]
        if isinstance(words, str):
            words = re.split(r"[,，;；\s]+", words)
        values = [str(w).strip() for w in (words or []) if str(w).strip()]
        cfg.closing_words = values
        raw["closing_words"] = values
        changed.append("closing_words")

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
        merged["wake_gap_sec"] = max(0, int(merged.get("wake_gap_sec", 0)))
        merged["sleep_after_sec"] = max(0, int(merged.get("sleep_after_sec", 0)))
        merged["min_chars"] = max(0, int(merged.get("min_chars", 0) or 0))
        for key in ("quiet_start", "quiet_end"):
            value = str(merged.get(key) or "").strip()
            if value and not (len(value) == 5 and value[2] == ":"):
                value = ""
            merged[key] = value
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


async def api_persona_set(request: web.Request) -> web.Response:
    """换当前人格（config.json 的 persona）。"""
    body: dict[str, Any] = await request.json()
    name = _safe_role(str(body.get("name") or "").strip())
    if not name:
        return web.json_response({"error": "没说要哪个"}, status=400)
    folder = _roles_dir(request)
    if not (folder / f"{name}.md").exists():
        return web.json_response({"error": f"没有「{name}」这个人格"}, status=404)
    raw = _raw_config(request)
    raw["persona"] = name
    try:
        Path(request.app["cfg_path"]).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        return web.json_response({"error": f"写不进去：{exc}"}, status=500)
    request.app["cfg"].raw["persona"] = name
    return web.json_response({"ok": True, "persona": name})


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


async def api_sender_names(request: web.Request) -> web.Response:
    """按 QQ 号从聊天记录里认最近用过的名字：?ids=1,2,3 → {id: 名字}。

    只读 messages 表；没聊过、没名字的号不会出现在结果里。
    """
    raw = request.query.get("ids") or ""
    ids = [x.strip() for x in raw.replace("，", ",").replace(" ", ",").split(",") if x.strip()]
    ids = [x for x in dict.fromkeys(ids) if x.isdigit()][:20]
    out: dict[str, str] = {}
    if not ids:
        return web.json_response({"ok": True, "names": {}})
    store = request.app["state"]["store"]
    marks = ",".join("?" for _ in ids)
    sql = (
        "SELECT sender_id, sender_name FROM messages "
        f"WHERE sender_id IN ({marks}) AND sender_name <> '' "
        "ORDER BY id DESC LIMIT 400"
    )
    try:
        with store._lock:
            rows = store.conn.execute(sql, ids).fetchall()
    except Exception as exc:  # 库还没建好/表还没迁移
        return web.json_response({"ok": False, "error": str(exc), "names": {}})
    for row in rows:
        sid = str(row["sender_id"])
        if sid not in out:
            out[sid] = str(row["sender_name"])
    return web.json_response({"ok": True, "names": out})

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


async def api_stickers(request: web.Request) -> web.Response:
    """收藏到的表情包列表，可按来源过滤（candidate = 她还没收的缓存区）。"""
    store = request.app["state"]["store"]
    try:
        limit = max(1, min(500, int(request.query.get("limit", "200"))))
    except ValueError:
        limit = 200
    source = str(request.query.get("source") or "")
    unread = str(request.query.get("unread") or "") in ("1", "true", "yes")
    fav = store.sticker_count(source="qq_favorite")
    total = store.sticker_count()
    pending = store.sticker_count(source="candidate")
    items = store.sticker_list(limit=limit, source=source)
    failed = store.sticker_vision_failed_hashes()
    if unread:
        items = [it for it in items if str(it.get("hash")) in failed and not it.get("meaning")]
    for it in items:
        # 面板要显示「待补」：收下了但还没看懂
        it["needs_meaning"] = bool(it.get("source") != "candidate" and not it.get("meaning"))
        it["buffered"] = str(it.get("hash")) in failed
    stickers = request.app["state"].get("stickers")
    vision = stickers.vision_status() if stickers is not None else {}
    return web.json_response(
        {
            "ok": True,
            "count": total,
            "fav_count": fav,
            "candidate_count": pending,
            "collected_count": total - fav - pending,
            "buffer_count": len(failed),
            "vision": vision,
            "items": items,
        }
    )


async def api_stickers_sync(request: web.Request) -> web.Response:
    """立刻同步一次 QQ「我的表情」。"""
    state = request.app["state"]
    stickers = state.get("stickers")
    if stickers is None:
        return web.json_response({"error": "表情包没就绪"}, status=500)
    try:
        added = await stickers.sync(force=True)
    except Exception as exc:
        return web.json_response({"error": f"同步失败：{exc}"}, status=500)
    store = state["store"]
    return web.json_response(
        {"ok": True, "added": added, "count": store.sticker_count(),
         "fav_count": store.sticker_count(source="qq_favorite")}
    )


async def api_stickers_config(request: web.Request) -> web.Response:
    """表情包的设置：GET 拿字段和当前值，PUT 存回去（写 config.json）。"""
    app = request.app
    stickers = app["state"].get("stickers")
    if stickers is None:
        return web.json_response({"error": "表情包没就绪"}, status=500)
    if request.method == "GET":
        return web.json_response({"ok": True, "fields": stickers.schema(), "values": stickers.settings()})

    body: dict[str, Any] = await request.json()
    values = body.get("values") if isinstance(body.get("values"), dict) else body
    if not isinstance(values, dict):
        return web.json_response({"error": "要 {values: {...}}"}, status=400)
    saved = stickers.apply(values)

    # 落到 config.json：只动 stickers 这一块
    cfg = app["cfg"]
    raw = getattr(cfg, "raw", None)
    if isinstance(raw, dict):
        raw["stickers"] = saved
        try:
            Path(app["cfg_path"]).write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            return web.json_response({"ok": True, "values": saved, "warning": f"没写进 config.json：{exc}"})
    return web.json_response({"ok": True, "values": saved})


async def api_look_config(request: web.Request) -> web.Response:
    """聊天看图的设置（也放在表情包页，跟视觉模型是同一路）：GET / PUT。"""
    app = request.app
    look = app["state"].get("look")
    if look is None:
        return web.json_response({"error": "看图没就绪"}, status=500)
    if request.method == "GET":
        return web.json_response({"ok": True, "fields": look.schema(),
                                  "values": look.settings(), "used_today": look.usage_today()})

    body: dict[str, Any] = await request.json()
    values = body.get("values") if isinstance(body.get("values"), dict) else body
    if not isinstance(values, dict):
        return web.json_response({"error": "要 {values: {...}}"}, status=400)
    saved = look.apply(values)
    cfg = app["cfg"]
    raw = getattr(cfg, "raw", None)
    if isinstance(raw, dict):
        raw["look"] = saved
        try:
            Path(app["cfg_path"]).write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            return web.json_response({"ok": True, "values": saved,
                                      "warning": f"没写进 config.json：{exc}"})
    return web.json_response({"ok": True, "values": saved})


async def api_sticker_remark(request: web.Request) -> web.Response:
    """给一张表情起个名（备注），发的时候可以按名字找。"""
    store = request.app["state"]["store"]
    sid = request.match_info["sid"]
    body: dict[str, Any] = await request.json()
    remark = str(body.get("remark") or "").strip()[:40]
    if not store.sticker_set_remark(int(sid), remark):
        return web.json_response({"error": "没这张"}, status=404)
    # 表情只归表情包库：改名就是改 remark（提示词里会带），不再往记忆库写 ring_note
    return web.json_response({"ok": True, "remark": remark})


async def api_sticker_meaning(request: web.Request) -> web.Response:
    """主人亲手写/改描述；写了就按他的说法重新算标签（他不用懂标签怎么打）。"""
    app = request.app
    store = app["state"]["store"]
    sid = request.match_info["sid"]
    body: dict[str, Any] = await request.json()
    meaning = str(body.get("meaning") or "").strip()[:200]
    retag = body.get("retag", True) is not False
    row = store.sticker_get(int(sid))
    if row is None:
        return web.json_response({"error": "没这张"}, status=404)
    store.sticker_set_meaning(int(sid), meaning, str(row.get("tags") or ""))
    tags = ""
    stickers = app["state"].get("stickers")
    if stickers is not None and meaning and retag:
        try:
            tags = await stickers.retag(int(sid))
        except Exception as exc:
            return web.json_response({"ok": True, "meaning": meaning, "warning": f"标签没算成：{exc}"})
    return web.json_response({"ok": True, "meaning": meaning, "tags": tags})


async def api_sticker_tags(request: web.Request) -> web.Response:
    """手填/改标签（没看懂的那张、或者她觉得标签不对）。"""
    store = request.app["state"]["store"]
    sid = request.match_info["sid"]
    body: dict[str, Any] = await request.json()
    tags = "、".join(
        t.strip() for t in str(body.get("tags") or "").replace("，", "、").split("、") if t.strip()
    )[:200]
    row = store.sticker_get(int(sid))
    if row is None:
        return web.json_response({"error": "没这张"}, status=404)
    store.sticker_set_meaning(int(sid), str(row.get("meaning") or ""), tags)
    return web.json_response({"ok": True, "tags": tags})


async def api_sticker_keep(request: web.Request) -> web.Response:
    """候选 → 收下（面板上点「收下」）。"""
    state = request.app["state"]
    stickers = state.get("stickers")
    if stickers is None:
        return web.json_response({"error": "表情包没就绪"}, status=500)
    sid = request.match_info["sid"]
    body: dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    remark = str(body.get("remark") or "").strip()[:40]
    if not stickers.promote(int(sid), remark):
        return web.json_response({"error": "没这张"}, status=404)
    return web.json_response({"ok": True})


async def api_sticker_file(request: web.Request) -> web.Response:
    """把收藏的图发给面板看。"""
    from pathlib import Path as _P

    state = request.app["state"]
    store = state["store"]
    sid = request.match_info["sid"]
    row = next((r for r in store.sticker_list(limit=500) if str(r["id"]) == str(sid)), None)
    if not row or not row.get("file"):
        raise web.HTTPNotFound()
    stickers = state.get("stickers")
    path = stickers.resolve(row["file"]) if stickers is not None else _P(str(row["file"]))
    if path is None or not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


async def api_sticker_delete(request: web.Request) -> web.Response:
    from pathlib import Path as _P

    state = request.app["state"]
    store = state["store"]
    sid = request.match_info["sid"]
    row = next((r for r in store.sticker_list(limit=500) if str(r["id"]) == str(sid)), None)
    if not row:
        raise web.HTTPNotFound()
    stickers = state.get("stickers")
    # 表情只归表情包库：删了就删了，不再往记忆库补一条「已删除」
    store.sticker_vision_delete(str(row.get("hash") or ""))  # 图没了，账也没用了
    store.sticker_remove(int(sid))
    file = str(row.get("file") or "")
    if file:
        path = stickers.resolve(file) if stickers is not None else _P(file)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return web.json_response({"ok": True})


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


LEX_SYS = (
    "你在帮一个 QQ 机器人理解群里的黑话。只输出一句中文解释，"
    "不要引号、不要客套、不要重复词条本身，不要写来源。60 字以内。"
)


def _first_sentence(items: list[dict[str, Any]]) -> str:
    """从搜索结果里挑一句能看的：第一条摘要的第一句。"""
    import re as _re

    for it in items:
        raw = str(it.get("snippet") or "").strip()
        if len(raw) < 12:
            continue
        raw = _re.sub(r"^\d+[\.、]\s*", "", raw)
        raw = raw.split("。")[0].strip(" .，,")
        if len(raw) >= 10:
            return raw[:100]
    for it in items:
        title = str(it.get("title") or "").strip()
        if title:
            return title[:60]
    return ""


async def api_lexicon_explain(request: web.Request) -> web.Response:
    """联网解释一个词：搜一遍，让模型压成一句。解释完留一条记录。"""
    state = request.app["state"]
    store = state["store"]
    backend = state.get("backend")
    body: dict[str, Any] = await request.json()
    term = str(body.get("term") or "").strip()[:60]
    if not term:
        return web.json_response({"error": "词条不能为空"}, status=400)

    evidence: list[dict[str, str]] = []
    material = ""
    warn = ""
    items = await websearch.search(f"{term} 是什么意思 梗", limit=6)
    if not items:
        items = await websearch.search(f"{term} 网络用语", limit=6)
    evidence = [{"title": i["title"], "url": i["url"]} for i in items if i.get("title")][:6]
    material = websearch.as_material(items)
    if not material:
        warn = "联网没搜到东西（可能断网）"

    prompt = f"群友说的词：「{term}」。"
    if material:
        prompt += f"\n\n网上的说法：\n{material}"
    prompt += "\n\n请写出这个词在群聊语境里的含义。"

    source = "联网"
    meaning = ""
    try:
        if backend is None:
            raise RuntimeError("模型还没接上")
        meaning, _usage = await backend.chat(
            [{"role": "system", "content": LEX_SYS},
             {"role": "user", "content": prompt}],
            temperature=0.3,
        )
    except Exception as exc:
        text = str(exc)[:160]
        if material:  # 模型不通：先给搜到的原文，别让功能整个废掉
            meaning = _first_sentence(items)
            source = "联网·摘录"
            warn = f"模型这会儿不通（{text}），先给你搜到的原文"
        else:
            return web.json_response({"error": f"模型不通：{text}"}, status=502)

    meaning = meaning.strip().strip("「」“”\"'").strip().split("\n")[0][:300]
    if not meaning:
        return web.json_response({"error": "模型没给出内容"}, status=502)

    save = str(body.get("save") or "1") != "0"
    if save:
        store.lexicon_set_note(term, meaning, source)
    hid = store.lex_history_add(term, meaning, source, material or "")
    return web.json_response(
        {"ok": True, "term": term, "meaning": meaning, "source": source,
         "evidence": evidence, "warn": warn, "history_id": hid, "saved": save}
    )


async def api_lexicon_note(request: web.Request) -> web.Response:
    """人工改含义。"""
    store = request.app["state"]["store"]
    body: dict[str, Any] = await request.json()
    term = str(body.get("term") or "").strip()[:60]
    note = str(body.get("note") or "").strip()[:300]
    if not term:
        return web.json_response({"error": "词条不能为空"}, status=400)
    store.lexicon_set_note(term, note, "人工")
    return web.json_response({"ok": True, "term": term, "note": note, "source": "人工"})


async def api_lex_history(request: web.Request) -> web.Response:
    """解释记录：搜得到的都在这儿。"""
    store = request.app["state"]["store"]
    q = str(request.query.get("q") or "").strip()[:60]
    return web.json_response({"ok": True, "items": store.lex_history_list(q)})


async def api_lex_history_update(request: web.Request) -> web.Response:
    """改一条记录的解释。"""
    store = request.app["state"]["store"]
    body: dict[str, Any] = await request.json()
    hid = int(body.get("id") or 0)
    note = str(body.get("note") or "").strip()[:300]
    if not hid:
        return web.json_response({"error": "缺记录 id"}, status=400)
    n = store.lex_history_update(hid, note)
    return web.json_response({"ok": True, "updated": n, "id": hid, "note": note})


async def api_lex_history_default(request: web.Request) -> web.Response:
    """把这条认定为这个词的默认解释：词库跟着改，以后她就按这条理解。"""
    store = request.app["state"]["store"]
    body: dict[str, Any] = await request.json()
    hid = int(body.get("id") or 0)
    if not hid:
        return web.json_response({"error": "缺记录 id"}, status=400)
    row = store.lex_history_pick(hid)
    if not row:
        return web.json_response({"error": "没有这条记录"}, status=404)
    return web.json_response({"ok": True, "term": row["term"], "note": row["note"]})


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
                import asyncio as _asyncio

                parts = [ln.strip() for ln in re.split(r"\n+", reply) if ln.strip()] or [reply]
                for _i, _part in enumerate(parts):
                    if _i:
                        await _asyncio.sleep(min(0.30 + len(_part) * 0.012, 1.10))
                    await onebot.send(item["conv_id"], [Segment("text", {"text": _part})])
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


_BAD_MODEL_WORDS = (
    "audio", "realtime", "tts", "asr", "embedding", "embed", "rerank",
    "image", "video", "voice", "mt-", "-mt", "livetranslate", "omni", "ocr",
)


def _chat_model_rank(mid: str) -> tuple[int, int]:
    """挑候选时的偏好：flash / chat 优先，花哨的多模态后放。"""
    low = mid.lower()
    score = 0
    if "flash" in low:
        score -= 4
    if "chat" in low:
        score -= 2
    if "deepseek" in low:
        score -= 3
    if "qwen" in low:
        score -= 1
    if any(w in low for w in _BAD_MODEL_WORDS):
        score += 10
    return (score, len(mid))


async def api_model_find(request: web.Request) -> web.Response:
    """当前模型用不了时：从 /models 里挑几个能对话的，挨个真打一次，找能用的那个。"""
    from dataclasses import replace

    from ..backend import OpenAICompatBackend

    cfg = request.app["cfg"]
    body: dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    base_url = str(body.get("base_url") or cfg.model.base_url or "").strip().rstrip("/")
    api_key = str(body.get("api_key") or cfg.model.api_key or "").strip()
    wanted = str(body.get("model") or cfg.model.model or "").strip()
    if not api_key or not base_url:
        return web.json_response({"error": "Key 或接口地址还空着"}, status=400)

    ids: list[str] = []
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                base_url + "/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    raw = data.get("data") or data.get("models") or []
                    ids = [str(m.get("id")) for m in raw if isinstance(m, dict) and m.get("id")]
    except Exception:
        ids = []

    cands: list[str] = []
    if wanted:
        cands.append(wanted)
    for mid in sorted(ids, key=_chat_model_rank):
        if mid not in cands and not any(w in mid.lower() for w in _BAD_MODEL_WORDS):
            cands.append(mid)
    cands = cands[:8]

    results: list[dict[str, Any]] = []
    for mid in cands:
        probe = replace(cfg, model=replace(cfg.model, api_key=api_key, base_url=base_url, model=mid))
        try:
            reply, _ = await OpenAICompatBackend(probe).chat(
                [{"role": "user", "content": "ping"}], temperature=0
            )
            results.append({"model": mid, "ok": True, "reply": (reply or "").strip()[:30]})
            return web.json_response(
                {"ok": True, "model": mid, "results": results, "listed": len(ids)}
            )
        except Exception as exc:
            results.append({"model": mid, "ok": False, "error": str(exc)[:180]})

    return web.json_response(
        {
            "ok": False,
            "model": "",
            "results": results,
            "listed": len(ids),
            "hint": "这个 Key 在所有候选上都过不去（多半是账号没开通 / 余额到期），换 Key 或换接口地址吧。"
            if ids
            else "连 /models 都拿不到，先确认接口地址对不对。",
        }
    )


async def ping_js(request: web.Request) -> web.Response:
    """给启动时的转场页用：脚本能加载 = 面板已经活了。"""
    return web.Response(
        text="window.__conch_ready=1;",
        content_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


async def _proactive_report(state: dict[str, Any], url: str, body: dict[str, Any],
                            timeout: float) -> None:
    """把「说成了没有」回执给请求方。失败就记日志，不抛。"""
    if not url:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                await resp.read()
                if resp.status >= 400:
                    log_.warning("主动开口回执被拒：HTTP %s", resp.status)
    except Exception as exc:  # noqa: BLE001 - 回执不到不该影响她已经说出口的话
        log_.warning("主动开口回执失败：%s", exc)


async def _run_proactive(state: dict[str, Any], speaker, payload: dict[str, Any],
                         report_url: str, timeout: float) -> None:
    event_id = str(payload.get("event_id") or "")
    try:
        result = await speaker(payload)
        result = result if isinstance(result, dict) else {"ok": False, "error": "说话那边没回结果"}
    except Exception as exc:  # noqa: BLE001
        log_.warning("主动开口出错：%s", exc)
        result = {"ok": False, "error": str(exc)[:160]}
    body = {
        "event_id": event_id,
        "sent": bool(result.get("sent")),
        "text": str(result.get("text") or ""),
        "error": str(result.get("error") or ""),
    }
    log_.info("主动开口 %s：%s", "成了" if body["sent"] else "没成",
              (body["text"] or body["error"])[:60])
    await _proactive_report(state, report_url, body, timeout)


async def api_proactive_say(request: web.Request) -> web.Response:
    """宿主端点：插件说「我想找他说句话」，这里替她用自己的嘴说。

    契约（跟 Tide 无关，任何插件都能接）：
        POST /api/proactive/say
        {"event_id","why","urgency","needs","person","conv_id"?,"report_url"}
        → {"ok": true, "accepted": true}    接了，后台说，说完往 report_url 回执
        → {"ok": false, "error": "..."}      不接

    这里只负责「接住」并立刻返回：真说话要跑一次模型，几秒钟，
    不能让上游的 5 秒超时把它掐掉。
    """
    state = request.app["state"]
    cfg = request.app["cfg"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "要 JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"ok": False, "error": "要 JSON 对象"}, status=400)

    block = dict(getattr(cfg, "proactive", None) or {})
    if not block.get("enabled", False):
        return web.json_response(
            {"ok": False, "error": "海螺没开主动开口（config.proactive.enabled）"}
        )
    services = state.get("services") or {}
    speaker = services.get("proactive")
    if speaker is None:
        return web.json_response(
            {"ok": False, "error": "没有插件提供主动开口（说话插件没加载？）"}
        )

    conv_id = str(data.get("conv_id") or block.get("conv_id") or "").strip()
    if not conv_id:
        # 没指定就发给第一个称呼卡的私聊 —— 她主动说话本来就是冲他一个人
        owner = (cfg.owner_ids or [""])[0]
        if owner:
            conv_id = f"private:{owner}"
    if not conv_id:
        return web.json_response({"ok": False, "error": "没给发给谁（config.proactive.conv_id）"})

    payload = dict(data)
    payload["conv_id"] = conv_id
    payload.setdefault("why", "")
    report_url = str(data.get("report_url") or block.get("report_url") or "")
    timeout = float(block.get("report_timeout_sec") or 8)
    asyncio.create_task(_run_proactive(state, speaker, payload, report_url, timeout))
    return web.json_response({"ok": True, "accepted": True, "conv_id": conv_id})


def create_app(*, cfg: Any, state: dict[str, Any], cfg_path: str) -> web.Application:
    app = web.Application(middlewares=[guard])
    app["cfg"] = cfg
    app["state"] = state
    app["cfg_path"] = cfg_path
    app.router.add_get("/", index)
    app.router.add_get("/ping.js", ping_js)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/manifest", api_manifest)
    app.router.add_get("/api/plugins", api_plugins)
    app.router.add_post("/api/plugins/check-updates", api_plugins_check_updates)
    app.router.add_post("/api/plugins/{pid}/update", api_plugin_apply_update)
    app.router.add_get("/api/plugins/{pid}/config", api_plugin_config)
    app.router.add_put("/api/plugins/{pid}/config", api_plugin_config)
    app.router.add_post("/api/plugins/{pid}/toggle", api_toggle)
    app.router.add_post("/api/plugins/{pid}/reload", api_reload)
    app.router.add_post("/api/plugins/reload-all", api_reload_all)
    app.router.add_get("/api/usage/summary", api_usage)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_put("/api/settings", api_settings_put)
    app.router.add_get("/api/roles", api_roles_list)
    app.router.add_post("/api/persona", api_persona_set)
    app.router.add_get("/api/roles/{name}", api_role_get)
    app.router.add_put("/api/roles/{name}", api_role_put)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/messages", api_messages)
    app.router.add_get("/api/sender-names", api_sender_names)
    app.router.add_get("/api/messages/convs", api_message_convs)
    app.router.add_post("/api/messages/clear", api_messages_clear)
    app.router.add_post("/api/plugins/{pid}/remove", api_plugin_remove)
    app.router.add_get("/api/stickers", api_stickers)
    app.router.add_get("/api/stickers/{sid}/file", api_sticker_file)
    app.router.add_delete("/api/stickers/{sid}", api_sticker_delete)
    app.router.add_post("/api/stickers/sync", api_stickers_sync)
    app.router.add_get("/api/stickers/config", api_stickers_config)
    app.router.add_put("/api/stickers/config", api_stickers_config)
    app.router.add_get("/api/look/config", api_look_config)
    app.router.add_put("/api/look/config", api_look_config)
    app.router.add_post("/api/stickers/{sid}/remark", api_sticker_remark)
    app.router.add_post("/api/stickers/{sid}/keep", api_sticker_keep)
    app.router.add_post("/api/stickers/{sid}/tags", api_sticker_tags)
    app.router.add_post("/api/stickers/{sid}/meaning", api_sticker_meaning)
    app.router.add_get("/api/lexicon", api_lexicon)
    app.router.add_post("/api/lexicon/add", api_lexicon_add)
    app.router.add_post("/api/lexicon/remove", api_lexicon_remove)
    app.router.add_post("/api/lexicon/explain", api_lexicon_explain)
    app.router.add_post("/api/lexicon/note", api_lexicon_note)
    app.router.add_get("/api/lexicon/history", api_lex_history)
    app.router.add_post("/api/lexicon/history/update", api_lex_history_update)
    app.router.add_post("/api/lexicon/history/default", api_lex_history_default)
    app.router.add_get("/api/approvals", api_approvals)
    app.router.add_post("/api/approvals/decide", api_approval_decide)
    app.router.add_post("/api/conversation/action", api_conversation_action)
    app.router.add_post("/api/test-chat", api_test_chat)
    app.router.add_post("/api/verify-model", api_verify_model)
    app.router.add_post("/api/model/find", api_model_find)
    app.router.add_get("/api/model/keys", api_model_keys)
    app.router.add_post("/api/model/keys", api_model_keys_put)
    app.router.add_post("/api/models/list", api_models_list)
    app.router.add_get("/api/launchers", api_launchers)
    app.router.add_post("/api/name", api_name_set)
    app.router.add_post("/api/launchers/{id}/start", api_launcher_start)
    app.router.add_post("/api/launchers/{id}/stop", api_launcher_stop)
    app.router.add_post("/api/launchers/{id}/restart", api_launcher_restart)
    app.router.add_get("/api/launchers/{id}/log", api_launcher_log)
    app.router.add_post("/api/launchers/stop-all", api_launcher_stop_all)
    app.router.add_post("/api/proactive/say", api_proactive_say)
    app.router.add_post("/api/shutdown", api_shutdown)
    app.router.add_get("/api/auth", api_auth)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_post("/api/auth/password", api_set_password)
    app.router.add_get("/api/groups", api_groups)
    app.router.add_get("/api/update", api_update_check)
    app.router.add_post("/api/update/apply", api_update_apply)
    app.router.add_post("/api/update/restart", api_update_restart)
    return app