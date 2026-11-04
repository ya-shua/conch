"""一键启动那些不在本仓库里的程序：QQ 网关、环形的心、刷视频、微光域、汐面板。

它们各有各的启动方式（.bat / .ps1 / node / python），共同点是：
  一键点了就该弹一个新窗口出来，让人能看见日志，而不是闷在后台。

清单在项目根目录的 launchers.json（不进仓库，里面有本机路径和密码）：
    [
      {
        "id": "snowluma",
        "name": "SnowLuma（QQ 网关）",
        "note": "QQ 收发都走它，端口 3000 / 3001",
        "cwd": "D:\\\\AI\\\\QQ阿鱼\\\\SnowLuma",
        "cmd": "launcher.bat",
        "port": 3000,
        "secrets": [{"key": "token", "label": "WS Token", "env": "SNOWLUMA_TOKEN"}]
      }
    ]
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

# 我们自己起的进程，按 id 记住，方便停
_PIDS: dict[str, int] = {}


def _path(cfg_path: str | Path) -> Path:
    return Path(cfg_path).resolve().parent / "launchers.json"


def load(cfg_path: str | Path) -> list[dict[str, Any]]:
    path = _path(cfg_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id")]


def save(cfg_path: str | Path, items: list[dict[str, Any]]) -> None:
    _path(cfg_path).write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def port_open(port: int | None, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).stdout
        return str(pid) in out
    except Exception:
        return False


def status(item: dict[str, Any]) -> dict[str, Any]:
    port = item.get("port")
    pid = _PIDS.get(str(item["id"]), 0)
    by_port = port_open(int(port) if port else None)
    return {
        "id": item["id"],
        "name": item.get("name") or item["id"],
        "note": item.get("note") or "",
        "cwd": item.get("cwd") or "",
        "cmd": item.get("cmd") or "",
        "port": port,
        "secrets": [
            {
                "key": s.get("key"),
                "label": s.get("label") or s.get("key"),
                "filled": bool((item.get("secret_values") or {}).get(s.get("key"))),
            }
            for s in (item.get("secrets") or [])
            if isinstance(s, dict) and s.get("key")
        ],
        "running": bool(by_port or (pid and _pid_alive(pid))),
        "by": "端口" if by_port else ("进程" if pid and _pid_alive(pid) else ""),
        "pid": pid or 0,
    }


def start(item: dict[str, Any], secrets: dict[str, str] | None = None) -> dict[str, Any]:
    """起一个新控制台窗口。有密码的按条目里声明的环境变量传进去。"""
    cwd = str(item.get("cwd") or "").strip()
    cmd = str(item.get("cmd") or "").strip()
    if not cwd or not cmd:
        raise RuntimeError("这条启动项缺 cwd 或 cmd")
    if not Path(cwd).exists():
        raise RuntimeError(f"目录不存在：{cwd}")

    values = dict(item.get("secret_values") or {})
    values.update({str(k): str(v) for k, v in (secrets or {}).items() if v})
    env = os.environ.copy()
    for spec in item.get("secrets") or []:
        key = str(spec.get("key") or "")
        env_name = str(spec.get("env") or "")
        if key and env_name and values.get(key):
            env[env_name] = values[key]

    args = [str(a) for a in (item.get("args") or [])]
    lower = cmd.lower()
    if lower.endswith((".bat", ".cmd")):
        argv = ["cmd", "/c", cmd, *args]
    elif lower.endswith(".ps1"):
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", cmd, *args]
    else:
        argv = [cmd, *args]

    flags = 0x00000010  # CREATE_NEW_CONSOLE：就是要弹个窗口出来
    proc = subprocess.Popen(argv, cwd=cwd, env=env, creationflags=flags, close_fds=True)
    _PIDS[str(item["id"])] = proc.pid
    return {"ok": True, "pid": proc.pid, "argv": argv}


def stop(item: dict[str, Any]) -> dict[str, Any]:
    pid = _PIDS.get(str(item["id"]), 0)
    if not pid:
        return {"ok": False, "error": "不是从这里启动的，关不掉；去那个窗口按 Ctrl+C"}
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=0x08000000,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    _PIDS.pop(str(item["id"]), None)
    return {"ok": True, "pid": pid}