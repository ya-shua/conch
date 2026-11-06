"""MCP 客户端：把桌面上的独立插件接成「她能调用的工具」。

为什么要有这层
--------------
环形的心（记忆）、刷视频工具包、微光域（浏览器）各自都是 **stdio MCP server**：
一行一条 JSON-RPC，没有端口、没有鉴权、被谁 spawn 就归谁管。
以前那个「手」是 DSH，现在这条链路上的宿主是海螺，所以手要长在这里。

设计要求
--------
- 懒启动：第一次要用才 spawn，闲太久就收掉，别让一堆 node 常驻。
- 不拖累主流程：任何一个 server 起不来，其他照用；工具出错把错误当文本回给模型。
- 超时可配：刷视频那种要几十秒的，按 server 单独给。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

# 0x08000000：不给子进程分配控制台窗口（Windows）
CREATE_NO_WINDOW = 0x08000000

from . import log

log_ = log.get("mcp")

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "conch", "version": "0.1.0"}


class McpError(Exception):
    pass


class McpServer:
    """一个 stdio MCP server 的连接。"""

    def __init__(self, spec: dict[str, Any]):
        self.name = str(spec.get("name") or "mcp")
        self.command = str(spec.get("command") or "")
        self.args = [str(a) for a in (spec.get("args") or [])]
        self.cwd = str(spec.get("cwd") or "") or None
        self.env_extra = dict(spec.get("env") or {})
        self.timeout_s = float(spec.get("timeout_s") or 60)
        self.scope = str(spec.get("scope") or "owner")  # owner = 只给主人私聊
        self.enabled = bool(spec.get("enabled", True))

        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._tools: list[dict[str, Any]] = []
        self._stderr_task: asyncio.Task | None = None

    # ── 生命周期 ──────────────────────────────────────────

    async def ensure(self) -> None:
        """保证子进程活着，并拿到工具清单。"""
        if self._proc is not None and self._proc.returncode is None:
            return
        if not self.command or not Path(self.command).exists():
            raise McpError(f"{self.name}: 找不到可执行文件 {self.command}")

        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.env_extra.items()})
        env.setdefault("PYTHONIOENCODING", "utf-8")

        # Windows：不传 CREATE_NO_WINDOW 的话，控制台子程序会各自弹一个黑窗口
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise McpError(f"{self.name}: 起不来（{exc}）") from exc

        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._handshake()
        log_.info("%s 就绪，%s 个工具", self.name, len(self._tools))

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        self._tools = []
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text:
                    log_.debug("%s stderr: %s", self.name, text[:300])
        except (asyncio.CancelledError, Exception):
            pass

    # ── 协议 ──────────────────────────────────────────────

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError(f"{self.name}: 进程不在")
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _recv(self, want_id: int) -> dict[str, Any]:
        """读到对应 id 的回包；server 发来的通知跳过。"""
        if self._proc is None or self._proc.stdout is None:
            raise McpError(f"{self.name}: 进程不在")
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout_s)
            if not line:
                raise McpError(f"{self.name}: 连接断了")
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            await self._send(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            )
            msg = await self._recv(rid)
        if "error" in msg:
            raise McpError(str((msg["error"] or {}).get("message") or msg["error"])[:300])
        return msg.get("result")

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _handshake(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        if not isinstance(result, dict):
            raise McpError(f"{self.name}: initialize 回包不认识")
        await self._notify("notifications/initialized")
        listed = await self._request("tools/list")
        tools = (listed or {}).get("tools") or []
        # 只留她用得上的：清单里每项都转成 OpenAI function 形状
        self._tools = [t for t in tools if isinstance(t, dict) and t.get("name")]

    # ── 对外 ──────────────────────────────────────────────

    def tool_schemas(self) -> list[dict[str, Any]]:
        out = []
        for tool in self._tools:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": str(tool.get("description") or "")[:600],
                        "parameters": tool.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        return out

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        result = await self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        return _flatten(result)


def _flatten(result: Any) -> str:
    """MCP 的回包可能是 content 数组，抽成纯文本给模型看。"""
    if not isinstance(result, dict):
        return str(result)[:4000]
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif item.get("type") == "image":
            parts.append("[图片]")
        else:
            parts.append(f"[{item.get('type')}]")
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        text = json.dumps(result, ensure_ascii=False)[:2000]
    return text[:6000]


class McpPool:
    """一组 server，按工具名路由。"""

    def __init__(self, specs: list[dict[str, Any]] | None = None):
        self.servers: dict[str, McpServer] = {}
        for spec in specs or []:
            srv = McpServer(spec)
            if srv.enabled:
                self.servers[srv.name] = srv
        self._owner: dict[str, McpServer] = {}

    async def start(self) -> None:
        for srv in self.servers.values():
            try:
                await srv.ensure()
            except Exception as exc:
                log_.warning("%s 没起来：%s", srv.name, exc)

    async def close(self) -> None:
        for srv in self.servers.values():
            await srv.close()

    def tools_for(self, scope: str) -> list[dict[str, Any]]:
        """scope=owner 给全部；否则只给 scope=all 的那些。"""
        out: list[dict[str, Any]] = []
        self._owner.clear()
        for srv in self.servers.values():
            if scope != "owner" and srv.scope != "all":
                continue
            for schema in srv.tool_schemas():
                name = schema["function"]["name"]
                if name in self._owner:
                    continue
                self._owner[name] = srv
                out.append(schema)
        return out

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        srv = self._owner.get(tool)
        if srv is None:
            # 没走过 tools_for（比如模型凭空编了个名字），按名字找回
            for cand in self.servers.values():
                if any(t["name"] == tool for t in cand._tools):
                    srv = cand
                    break
        if srv is None:
            return f"没有这个工具：{tool}"
        try:
            await srv.ensure()
            return await srv.call(tool, arguments)
        except Exception as exc:
            return f"工具 {tool} 出错：{exc}"