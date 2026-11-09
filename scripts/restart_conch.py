#!/usr/bin/env python3
"""把自己换掉：等一会儿、杀掉正在跑的海螺、再把服务拉起来。

为什么不再走 start_conch.vbs
--------------------------
vbs 会拉起托盘，托盘的规矩是「服务不在就顺手把面板打开」。
于是每次自动重启，浏览器里那张面板就自己弹一下 —— 明明没人点它。
重启只要把服务换新，面板窗口留给用户自己点。

由 core/updater.restart_self() 用 venv 的 pythonw 以 DETACHED 方式唤起。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
NO_WINDOW = 0x08000000

KILL_PS = (
    "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
    "Where-Object { $_.CommandLine -match 'main\\.py --config' } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
    "Where-Object { $_.CommandLine -match 'main\\.py --config' } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
)


def main() -> int:
    time.sleep(1.6)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", KILL_PS],
            timeout=30,
            capture_output=True,
            creationflags=NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        print("kill failed:", exc)
    time.sleep(1.0)

    # 占位锁属于上一任，先清掉；不清的话新本体会以为自己重复启动直接退出
    try:
        (ROOT / "data" / "host.lock").unlink(missing_ok=True)
    except OSError:
        pass

    # 只拉服务，不开面板：重启是后台动作，弹窗留给用户自己点
    start_all = ROOT / "scripts" / "start_all.py"
    if not start_all.exists():
        print("start_all.py 不见了")
        return 1
    try:
        subprocess.Popen(
            [str(PYTHONW), str(start_all)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW | 0x00000008,  # DETACHED_PROCESS：别跟着父进程一起死
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        print("launch failed:", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())