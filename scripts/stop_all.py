"""一键关掉全家桶：海螺 + 所有外部服务。

顺序：先让海螺自己优雅退出 → 再按端口/命令行把外面那些收掉 → 最后汇报。
窗口是隐藏的，所以结果用一个小弹窗告诉你关了哪些。
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # qqbridge/
CONFIG = ROOT / "config.json"
NO_WINDOW = 0x08000000
MARKERS = ("qqbridge.main", "tide.panel", "window_guard")


def panel_url() -> str:
    raw = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    panel = raw.get("panel") or {}
    token = str(panel.get("token") or "")
    port = int(panel.get("port") or 7801)
    return f"http://127.0.0.1:{port}/" + (f"?token={token}" if token else "")


def log(message: str) -> None:
    try:
        path = ROOT / "data" / "logs" / "start.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def say(title: str, text: str) -> None:
    # 0x40 = 信息图标；弹窗在最前面
    ctypes.windll.user32.MessageBoxTimeoutW(None, text, title, 0x40 | 0x40000, 0, 9000)


def pids_listening(port: int) -> list[int]:
    out = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        creationflags=NO_WINDOW,
    ).stdout
    found: set[int] = set()
    for line in out.splitlines():
        if f":{port} " not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            found.add(int(parts[-1]))
    return sorted(found)


def pids_by_marker() -> dict[int, str]:
    """按命令行找海螺/心情，兜底那些没端口的。"""
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
        ).stdout.strip()
        rows = json.loads(raw) if raw else []
    except Exception:
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    hits: dict[int, str] = {}
    for row in rows:
        line = str(row.get("CommandLine") or "")
        pid = int(row.get("ProcessId") or 0)
        for marker in MARKERS:
            if marker in line:
                hits[pid] = marker
    return hits


def kill_and_wait(pid: int, port: int | None = None) -> bool:
    """杀掉并把端口等它真的释放，别急着汇报。"""
    kill(pid)
    time.sleep(0.8)
    if port is None:
        return True
    for _ in range(3):
        if not port_open(port):
            return True
        again = [p for p in pids_listening(port) if p != pid]
        if not again:
            return not port_open(port)
        for pid2 in again:
            kill(pid2)
        time.sleep(0.8)
    return not port_open(port)


def kill(pid: int) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=NO_WINDOW,
        )
        return True
    except Exception:
        return False


def graceful_shutdown() -> bool:
    url = panel_url()
    try:
        req = urllib.request.Request(
            url.split("?")[0] + "api/shutdown",
            data=b"{}",
            headers={"x-console-token": "", "content-type": "application/json"},
            method="POST",
        )
        # 带上 token，免得 401
        raw = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        token = str((raw.get("panel") or {}).get("token") or "")
        req.add_header("x-console-token", token)
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception:
        return False


def port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def main() -> int:
    done: list[str] = []
    missed: list[str] = []

    if graceful_shutdown():
        done.append("海螺（面板通知退出）")
    time.sleep(1.2)

    # 外部服务：先按端口，再按命令行
    items = []
    lp = ROOT / "launchers.json"
    if lp.exists():
        items = json.loads(lp.read_text(encoding="utf-8-sig"))

    killed: set[int] = set()
    for item in items:
        port = item.get("port")
        hit = False
        if port:
            for pid in pids_listening(int(port)):
                if pid in killed:
                    hit = True
                    continue
                if kill_and_wait(pid, int(port)):
                    killed.add(pid)
                    hit = True
            if hit:
                done.append(f"{item.get('name') or item.get('id')}（端口 {port}）")
        else:
            hit = False
        if not hit and not port:
            missed.append(f"{item.get('name') or item.get('id')}（没端口，没找到进程）")

    for pid, marker in pids_by_marker().items():
        if pid in killed:
            continue
        if kill(pid):
            killed.add(pid)
            done.append(f"{marker}（pid {pid}）")

    # 复查：端口还开着的，如实说
    left = [str(i.get("port")) for i in items if i.get("port") and port_open(int(i["port"]))]
    if 7801 not in [int(p) for p in left if str(p).isdigit()] and port_open(7801):
        left.append("7801")

    lines = ["已经关掉："] if done else ["没有正在跑的东西。"]
    lines += [f"  · {x}" for x in done]
    if left:
        lines.append("")
        lines.append("还占着端口的（可能不是从海螺起的）：" + "、".join(left))
    for line in lines:
        log("关 " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())