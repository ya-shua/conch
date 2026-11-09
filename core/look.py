"""看一眼：消息里带图时，借「留影」的看图能力，把看到的东西塞进提示词。

这不是插件 —— 留影是外面那套程序，海螺只是调用方。
`bridge/eye.mjs` 负责跟留影说话（取图 + 描述），这里负责在合适的时机调它。

为什么用子进程而不是 HTTP
------------------------
留影自己有面板（:5188），但那条链路是「监听消息流」的，不是「给张图、要一句话」。
而且面板挂了不该让她连话都不会说 —— 子进程失败就当没看见图。

看图这件事本身仍由留影的代码完成（ring-vision.mjs / describe.mjs），
这里只借它的取图和描述，别把它的提示词抄一份过来。

配置在 `config.json` 的 `look` 块（一般不用动；模型凭据不给就用主模型那份）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from . import log
from .bus import Bus
from .config import Config

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "max_images": 2,
    "max_images_per_day": 200,  # 聊天看图每天最多几眼（0 = 不限）
    "timeout_s": 45,
    # 看图走哪边：auto = 留影装了就走留影；direct = 海螺自己有 key，直连模型
    "engine": "auto",
    "cmd": "",
    "http_url": "",
    "node": "",
    "script": "",
    "eye_root": "",
    "base_url": "",
    "model": "",
    "api_key": "",
}


def _split_cmd(cmd: str) -> list[str]:
    """把面板里填的命令拆成 argv；Windows 下带空格的路径要加引号。"""
    out: list[str] = []
    for tok in shlex.split(cmd, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        if tok:
            out.append(tok)
    return out


class Look:
    def __init__(self, cfg: Config, store: Any = None, bus: Bus | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.log = log.get("look")
        self._seen_day = ""
        self._seen_today = 0

    # ---------- 面板 ----------

    FIELDS: list[dict[str, Any]] = [
        {"key": "max_images", "group": "额度", "label": "一轮最多看几张", "type": "int",
         "min": 0, "max": 9, "unit": "张",
         "hint": "一轮聊天里带的图，只认前几张；0 = 不看聊天里的图"},
        {"key": "max_images_per_day", "group": "额度", "label": "每天最多看几次", "type": "int",
         "min": 0, "max": 5000, "unit": "次",
         "hint": "聊天看图按天计数，和表情包分开；同一张图一天只算一次；用完当天就不再看了（0 = 不限）"},
        {"key": "timeout_s", "group": "额度", "label": "单次最多等多久", "type": "int",
         "min": 5, "max": 300, "unit": "秒",
         "hint": "一次识别最多等这么久，超时算这次失败、不会一直卡着；45 秒一般够"},
        {"key": "engine", "group": "接入", "label": "看图走哪边", "type": "select",
         "options": [
             {"value": "auto", "label": "自动（留影 → 命令 → 接口 → 直连）"},
             {"value": "plugin", "label": "留影"},
             {"value": "direct", "label": "直连（OpenAI 兼容）"},
             {"value": "cmd", "label": "自定义命令"},
             {"value": "http", "label": "自定义接口"},
         ],
         "hint": "换别的视觉插件不用改代码：用「自定义命令」或「自定义接口」，按约定吐 JSON 就行"},
        {"key": "cmd", "group": "接入", "label": "自定义命令", "type": "text",
         "hint": "例：python D:\\vision\\look.py；会把图路径作为最后一个参数，结果从 stdout 最后一行读 JSON"},
        {"key": "http_url", "group": "接入", "label": "自定义接口", "type": "text",
         "hint": "例：http://127.0.0.1:9100/see；POST {image, kind, guidance}，回 JSON"},
        {"key": "base_url", "group": "接入", "label": "模型地址", "type": "text",
         "hint": "直连时用；留空 = 智谱 https://open.bigmodel.cn/api/paas/v4"},
        {"key": "model", "group": "接入", "label": "模型名", "type": "text",
         "hint": "直连时用；留空 = glm-4v-flash"},
        {"key": "api_key", "group": "接入", "label": "Key", "type": "pass",
         "hint": "直连时用；留空 = 用留影那份"},
    ]

    def schema(self) -> list[dict[str, Any]]:
        cur = self.settings()
        return [
            {**f, "value": cur.get(f["key"]), "default": DEFAULTS.get(f["key"])}
            for f in self.FIELDS
        ]

    def usage_today(self) -> int:
        if self.store is None:
            return self._seen_today
        try:
            return int(self.store.counter_get("look_day", time.strftime("%Y-%m-%d")))
        except Exception:
            return self._seen_today

    def _quota_ok(self, cfg: dict) -> bool:
        """聊天里看图也有每日上限，免得被一张张图刷爆。

        计数写在海螺自己的库里（按天），重启不清零；不碰记忆库。
        """
        cap = int(cfg.get("max_images_per_day") or 0)
        if cap <= 0:
            return True
        day = time.strftime("%Y-%m-%d")
        if day != self._seen_day:
            self._seen_day = day
            self._seen_today = 0
        used = self.usage_today()
        if used >= cap:
            self.log.info("今天看图到上限（%d 次），先不看了", cap)
            return False
        try:
            used = self.store.counter_bump("look_day", day) if self.store is not None else (used + 1)
        except Exception:
            used += 1
        self._seen_today = used
        return True

    # ---------- 设置 ----------

    @property
    def raw(self) -> dict[str, Any]:
        got = (self.cfg.raw or {}).get("look")
        if not isinstance(got, dict):
            got = (self.cfg.raw or {}).get("eyes")  # 老写法（插件时代）也认
        return got if isinstance(got, dict) else {}

    def settings(self) -> dict[str, Any]:
        out = dict(DEFAULTS)
        for key, val in self.raw.items():
            if key in DEFAULTS:
                out[key] = val
        return out

    def apply(self, values: dict[str, Any]) -> dict[str, Any]:
        block = self.raw.copy()
        for key in DEFAULTS:
            if key in values:
                block[key] = values[key]
        self.cfg.raw["look"] = block
        return self.settings()

    # ---------- 路径 ----------

    def _node(self, cfg: dict) -> str:
        node = str(cfg.get("node") or "").strip()
        if node and Path(node).exists():
            return node
        return shutil.which("node") or shutil.which("node.exe") or ""

    def _script(self, cfg: dict, name: str = "eye.mjs") -> str:
        if name == "eye.mjs":
            raw = str(cfg.get("script") or "").strip()
            if raw:
                path = Path(raw)
                return str(path) if path.exists() else ""
        path = Path(__file__).resolve().parents[1] / "bridge" / name
        return str(path) if path.exists() else ""

    # ---------- 看一张图 ----------

    def _pick_engine(self) -> str:
        """auto 时按可用性挑一条路：留影 → 自定义命令 → 自定义接口 → 直连。"""
        cfg = self.settings()
        eng = str(cfg.get("engine") or "auto").strip().lower()
        if eng and eng != "auto":
            return eng
        root = str(cfg.get("eye_root") or "").strip()
        if root and Path(root).exists():
            return "plugin"
        if str(cfg.get("cmd") or "").strip():
            return "cmd"
        if str(cfg.get("http_url") or "").strip():
            return "http"
        if str(cfg.get("api_key") or "").strip():
            return "direct"
        return "plugin"  # 都没配：交给留影，报错也说得清

    async def _exec_json(
        self, argv: list[str], env: dict[str, str], timeout: float, what: str
    ) -> dict[str, Any] | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                creationflags=0x08000000,  # CREATE_NO_WINDOW：别弹控制台
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, OSError) as exc:
            self.log.warning("%s失败：%s", what, exc)
            return None

        lines = out.decode("utf-8", "replace").strip().splitlines()
        if not lines:
            return None
        try:
            data = json.loads(lines[-1])
        except json.JSONDecodeError:
            self.log.warning("%s没吐 JSON：%s", what, lines[-1][:120])
            return None
        if not isinstance(data, dict):
            return None
        if data.get("ok") is False:
            self.log.warning("%s没看成：%s", what, str(data.get("error"))[:120])
            return None
        return data

    async def _run_cmd(self, target: str, extra: str, kind: str) -> dict[str, Any] | None:
        """自定义命令：<cmd> <图路径>，结果从 stdout 最后一行读。"""
        cfg = self.settings()
        cmd = str(cfg.get("cmd") or "").strip()
        if not cmd:
            return None
        argv = _split_cmd(cmd)
        if not argv:
            return None
        argv.append(str(target))
        env = dict(os.environ)
        env.update(
            {
                "CONCH_IMAGE": str(target),
                "CONCH_KIND": kind,
                "CONCH_GUIDANCE": extra or "",
                "EYE_TIMEOUT_MS": str(int(float(cfg.get("timeout_s") or 45) * 1000)),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return await self._exec_json(argv, env, float(cfg.get("timeout_s") or 45), "自定义看图命令")

    async def _run_http(self, target: str, extra: str, kind: str) -> dict[str, Any] | None:
        """自定义接口：POST {image, kind, guidance}，回 JSON。"""
        cfg = self.settings()
        url = str(cfg.get("http_url") or "").strip()
        if not url:
            return None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url,
                    json={"image": str(target), "kind": kind, "guidance": extra or ""},
                    timeout=aiohttp.ClientTimeout(total=float(cfg.get("timeout_s") or 45)),
                ) as resp:
                    if resp.status >= 400:
                        self.log.warning("自定义看图接口 %s", resp.status)
                        return None
                    data = await resp.json(content_type=None)
        except Exception as exc:
            self.log.warning("自定义看图接口失败：%s", exc)
            return None
        return data if isinstance(data, dict) else None

    def _norm(self, data: dict[str, Any]) -> dict[str, Any]:
        """把各家插件的结果捏成同一个形状：desc / tags / points。"""
        desc = str(
            data.get("desc") or data.get("text") or data.get("content") or data.get("meaning") or ""
        ).strip()
        tags = data.get("tags") or data.get("labels") or []
        if isinstance(tags, str):
            tags = re.split(r"[，,、;；\s]+", tags)
        tags = [str(t).strip() for t in tags if str(t).strip()][:5]
        points = data.get("points") or []
        points = [str(p).strip() for p in points if str(p).strip()][:4]
        return {"desc": desc[:300], "tags": tags, "points": points,
                "model": str(data.get("model") or "")}

    async def _run(self, script: str, target: str, extra: str = "",
                   engine: str = "") -> dict[str, Any] | None:
        """跑一次桥脚本，拿它吐的 JSON。"""
        cfg = self.settings()
        node = self._node(cfg)
        if not node or not script:
            return None

        # 凭据只认 look 块里写死的：没写就完全交给留影自己的配置。
        # （早先这里会回落到主模型的 key，结果拿 DeepSeek 的 key 去请求留影配的智谱地址，
        #   回来一串 403「Access to model denied」，看着像留影坏了。）
        env = dict(os.environ)
        base = str(cfg.get("base_url") or "")
        model = str(cfg.get("model") or "")
        key = str(cfg.get("api_key") or "")
        if base:
            env["EYE_BASE_URL"] = base
        if model:
            env["EYE_MODEL"] = model
        if key:
            env["EYE_API_KEY"] = key
        if cfg.get("eye_root"):
            env["EYE_ROOT"] = str(cfg["eye_root"])
        env["EYE_ENGINE"] = engine or str(cfg.get("engine") or "auto")
        env["EYE_TIMEOUT_MS"] = str(int(float(cfg.get("timeout_s") or 45) * 1000))
        env["PYTHONIOENCODING"] = "utf-8"

        timeout = float(cfg.get("timeout_s") or 45)
        try:
            proc = await asyncio.create_subprocess_exec(
                node,
                str(script),
                target,
                *([str(extra)] if extra else []),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                creationflags=0x08000000,  # CREATE_NO_WINDOW：别弹控制台
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, OSError) as exc:
            self.log.warning("看图失败（%s）：%s", str(target)[:60], exc)
            return None

        lines = out.decode("utf-8", "replace").strip().splitlines()
        if not lines:
            return None
        try:
            data = json.loads(lines[-1])
        except json.JSONDecodeError:
            return None
        if not data.get("ok"):
            self.log.warning("看图没看成：%s", str(data.get("error"))[:120])
            return None
        return data

    async def see(self, target: str) -> dict[str, Any] | None:
        """看图，拿原样的结果回来：{desc, points, model}。

        `target` 可以是在线图链，也可以是本地文件路径（表情包就是这么用的）。
        """
        eng = self._pick_engine()
        if eng == "cmd":
            data = await self._run_cmd(target, "", "chat")
        elif eng == "http":
            data = await self._run_http(target, "", "chat")
        else:
            data = await self._run(self._script(self.settings()), target, engine=eng)
        if not data:
            return None
        got = self._norm(data)
        if not got["desc"]:
            return None
        return {"desc": got["desc"], "points": got["points"], "model": got["model"]}

    async def see_sticker(self, target: str, guidance: str = "") -> dict[str, Any] | None:
        """看懂一张表情：意思 + 情绪/场合标签（给她挑表情用）。

        带了 `guidance`（主人亲手写的描述）就按他的说法来，模型只补标签 ——
        人写的描述比模型瞎猜准，标签则让模型按这个意思给。
        """
        eng = self._pick_engine()
        if eng == "cmd":
            data = await self._run_cmd(target, guidance, "sticker")
        elif eng == "http":
            data = await self._run_http(target, guidance, "sticker")
        else:
            data = await self._run(
                self._script(self.settings(), "sticker-eye.mjs"), target, guidance, engine=eng
            )
        if not data:
            return None
        got = self._norm(data)
        if not got["desc"] and not got["tags"]:
            return None
        return {"desc": got["desc"], "tags": got["tags"], "model": got["model"]}

    # ---------- 看一张图 ----------

    async def look(self, url: str) -> str:
        """看一张图，返回一句能塞进提示词的话；任何失败都返回空字符串。

        同一张图当天认过就直接复用那句描述：不重新调模型，也不占额度。
        """
        day = time.strftime("%Y-%m-%d")
        key = self._cache_key(url)
        if self.store is not None and key:
            hit = str(self.store.look_cache_get(key, day) or "")
            if hit:
                return hit
        if not self._quota_ok(self.settings()):
            return ""
        seen = await self.see(url)
        if not seen:
            return ""
        desc = seen["desc"]
        points = seen["points"]
        line = f"（有人发了张图，你看到的是：{desc}"
        if points:
            line += "；要点：" + "、".join(points[:4])
        line += "）"
        if self.store is not None and key:
            try:
                self.store.look_cache_put(key, day, line)
                self.store.look_cache_cleanup(1)
            except Exception:  # 缓存写不进去不影响这次看图
                pass
        return line

    def _cache_key(self, url: str) -> str:
        """认图不认链：QQ 的图链每次都不一样（rkey 会换），fileid 才是那张图本身。"""
        raw = str(url or "").strip()
        if not raw:
            return ""
        try:
            got = parse_qs(urlparse(raw).query).get("fileid") or []
            if got and got[0]:
                return "f:" + str(got[0])[:200]
        except Exception:
            pass
        return "u:" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    # ---------- 总线 ----------

    async def on_prompt(self, payload: dict) -> None:
        cfg = self.settings()
        if not cfg.get("enabled", True):
            return
        images = payload.get("images") or []
        if not images:
            return
        limit = int(cfg.get("max_images") or 2)
        seen: list[str] = []
        for url in images[:limit]:
            text = await self.look(url)
            if text:
                seen.append(text)
        if seen:
            payload["extra"] = str(payload.get("extra") or "") + "\n\n" + "\n".join(seen)

    def attach(self, bus: Bus) -> None:
        self.bus = bus
        bus.on("speech.prompt", self.on_prompt, priority=20, owner="look")
        cfg = self.settings()
        where = "有图就看" if cfg.get("enabled", True) else "关着，不看图"
        self.log.info("留影接好了（%s；%s）", where, ("模型 " + str(cfg["model"])) if cfg.get("model") else "用留影面板里配的视觉模型")