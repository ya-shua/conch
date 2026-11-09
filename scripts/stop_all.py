"""一键关掉全家桶：海螺 + 所有外部服务。

顺序：先让海螺自己优雅退出 → 再按端口/命令行把外面那些收掉 → 最后汇报。
窗口是隐藏的，所以结果用一个小弹窗告诉你关了哪些。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 海螺/
CONFIG = ROOT / "config.json"
TRAY_LOCK = ROOT / "data" / "tray.lock"
TRAY_QUIT = ROOT / "data" / "tray.quit"
NO_WINDOW = 0x08000000
MARKERS = ("main.py --config", "tide.panel", "window_guard")


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


def _items() -> list[dict]:
    """启动清单跟 start_all 用同一份。

    launchers.json 里 ring / video / eyes 三个没写 port（端口在各自插件的 metadata 里），
    以前关的时候只读 launchers.json —— 于是这三个永远关不掉，端口一直占着。
    """
    try:
        import sys

        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        from start_all import _launch_items

        items = _launch_items()
        if items:
            return items
    except Exception as exc:  # noqa: BLE001 - 读不到就退回只读清单
        log(f"读合并清单失败（{exc}），只用 launchers.json")
    lp = ROOT / "launchers.json"
    if not lp.exists():
        return []
    return json.loads(lp.read_text(encoding="utf-8-sig"))


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def kill(pid: int) -> bool:
    """真的杀掉了吗 —— 以「进程还在不在」为准，不看 taskkill 有没有报错。"""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=NO_WINDOW,
            timeout=10,
        )
    except Exception:
        pass
    if not _alive(pid):
        return True
    # taskkill 被权限挡住时再来一次硬终止
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
    time.sleep(0.1)
    return not _alive(pid)


def graceful_shutdown() -> bool:
    """先请它自己退（它会关库、断连、写存档）。失败也不是事，后面还有 taskkill。"""
    raw = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    token = str((raw.get("panel") or {}).get("token") or "")
    url = panel_url()
    try:
        req = urllib.request.Request(
            url.split("?")[0] + "api/shutdown",
            data=b"{}",
            headers={"x-console-token": token, "content-type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3).read()
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


def tray_pid() -> int:
    """托盘在不在。它在的话，光停服务是没用的 —— 它的看门狗 10 秒后全拉回来。"""
    try:
        pid = int(TRAY_LOCK.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0
    return pid if _alive(pid) else 0


def quit_tray(wait: float = 20.0) -> bool:
    """请托盘自己收尾（跟菜单「退出」同一条路）：它停服务，然后把自己收掉。

    以前一键关闭只调 stop_all，没先请托盘退 —— 关掉的那一秒看着是关了，
    10 秒后看门狗把全家桶又拉起来，玩家看到的就是「关不干净、还要关两次」。

    注意别互等：托盘那边拉起来的 stop_all 会带 CONCH_TRAY_QUITTING=1，
    走到这里就直接跳过（那边正在干活）。
    """
    pid = tray_pid()
    if not pid:
        return False
    log(f"请托盘退出（pid {pid}）")
    try:
        TRAY_QUIT.parent.mkdir(parents=True, exist_ok=True)
        TRAY_QUIT.write_text("quit", encoding="ascii")
    except OSError as exc:
        log(f"  写退出请求失败：{exc}")
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.15)
        if not _alive(pid):
            log("  托盘已退出")
            _drop_stale_tray_lock()
            return True
    log("  托盘没在等的时间内退出，直接收掉它")
    kill(pid)
    try:
        TRAY_QUIT.unlink(missing_ok=True)
    except OSError:
        pass
    _drop_stale_tray_lock()
    return True


def _drop_stale_tray_lock() -> None:
    """托盘是被硬收掉的时候，锁文件会留下 —— 清掉，免得下次看起来「托盘还在」。"""
    try:
        pid = int(TRAY_LOCK.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return
    if pid and not _alive(pid):
        try:
            TRAY_LOCK.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    """一口气收掉：先请托盘/海螺自己退，其余按端口/命令行**并行**找、并行杀。

    以前是一个端口一个端口地找、杀、再等它释放（每个 0.8 秒起），
    七个服务串下来要十几秒；现在等端口释放是并行的，总耗时 ≈ 最慢的那一个。
    """
    done: list[str] = []

    tray_gone = False
    if not os.environ.get("CONCH_TRAY_QUITTING"):
        tray_gone = quit_tray()
        if tray_gone:
            done.append("托盘（它会停掉全部服务）")

    items = _items()
    names = {int(i["port"]): (i.get("name") or i.get("id")) for i in items if i.get("port")}
    ports = list(names) + ([] if 7801 in names else [7801])

    # 托盘已经停完、端口也空了：收工。别再跑一遍 PowerShell 找进程（白等 5 秒）
    if tray_gone and not any(port_open(p) for p in ports):
        _report(done + ["全部服务（托盘退出时停的）"], [])
        return 0

    if graceful_shutdown():
        done.append("海螺（面板通知退出）")

    # 找：端口扫描 + 命令行扫描，同时进行
    targets: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(ports)) + 1)) as pool:
        by_port = {pool.submit(pids_listening, p): p for p in ports}
        marker_job = pool.submit(pids_by_marker)
        for job, port in by_port.items():
            for pid in job.result() or []:
                targets.setdefault(pid, f"{names.get(port, port)}（端口 {port}）")
        for pid, marker in (marker_job.result() or {}).items():
            targets.setdefault(pid, f"{marker}（pid {pid}）")

    # 杀：并行（taskkill /T 会连子进程一起收）
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as pool:
        jobs = {pool.submit(kill, pid): (pid, label) for pid, label in targets.items()}
        for job, (pid, label) in jobs.items():
            if job.result():
                done.append(label)

    # 等端口真的释放：并行等，总预算 8 秒
    left = [p for p in ports if port_open(p)]
    deadline = time.time() + 8
    while left and time.time() < deadline:
        time.sleep(0.2)
        left = [p for p in left if port_open(p)]

    _report(done, left)
    return 0


def _report(done: list[str], left: list[int]) -> None:
    lines = ["已经关掉："] if done else ["没有正在跑的东西。"]
    lines += [f"  · {x}" for x in done]
    lines.append("")
    if left:
        lines.append("还占着端口的（可能不是从海螺起的）：" + "、".join(str(x) for x in left))
    else:
        lines.append("端口都放干净了。")
    for line in lines:
        log("关 " + line)


if __name__ == "__main__":
    raise SystemExit(main())