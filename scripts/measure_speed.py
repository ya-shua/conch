"""量一次「一键启动 / 一键关闭」的真实耗时（秒表 + 快速端口轮询）。

    .venv\\Scripts\\python.exe scripts\\measure_speed.py start
    .venv\\Scripts\\python.exe scripts\\measure_speed.py stop

启动量三件事：面板端口可用、其余服务端口齐、面板窗口出现。
关闭量两件：所有端口释放、托盘退出。数字很小的话，改完之后别再来回怀疑。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 海螺/
PORTS = [7801, 7880, 8765, 5099, 3001, 5188, 8412, 5173, 17878]  # privacy-ok: 都是端口


def open_(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return True
    except OSError:
        return False


def has_window() -> bool:
    sys.path.insert(0, str(ROOT / "scripts"))
    import panel_window as pw

    w = pw._remembered_window()
    return bool(w) and pw._window_matches(w)


def _fmt(x: float | None) -> str:
    return "没出现" if x is None else f"{x:.1f}s"


def main() -> int:
    what = (sys.argv[1] if len(sys.argv) > 1 else "start").lower()

    if what == "start":
        t0 = time.time()
        subprocess.run(["wscript.exe", str(ROOT / "start_conch.vbs")])
        panel = rest = win = None
        while time.time() - t0 < 120:
            time.sleep(0.1)
            if panel is None and open_(7801):
                panel = time.time() - t0
            if rest is None and all(open_(p) for p in PORTS[1:]):
                rest = time.time() - t0
            if win is None and has_window():
                win = time.time() - t0
            if panel and rest and win:
                break
        print(f"起：面板可用 {_fmt(panel)}｜其余端口齐 {_fmt(rest)}｜面板窗口 {_fmt(win)}")
        print(f"（脚本总耗时 {time.time() - t0:.1f}s）")
        return 0

    t0 = time.time()
    subprocess.run(["wscript.exe", str(ROOT / "stop_conch.vbs")])
    closed = gone = None
    tray = ROOT / "data" / "tray.lock"
    while time.time() - t0 < 90:
        time.sleep(0.1)
        if closed is None and not any(open_(p) for p in PORTS):
            closed = time.time() - t0
        if gone is None and not tray.exists():
            gone = time.time() - t0
        if closed and gone:
            break
    print(f"关：所有端口释放 {_fmt(closed)}｜托盘退出 {_fmt(gone)}")
    print(f"（脚本总耗时 {time.time() - t0:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
