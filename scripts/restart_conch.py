#!/usr/bin/env python3
"""把自己换掉：等一会儿、杀掉正在跑的海螺、再用 start_conch.vbs 拉起来。

单独成文件是因为在 Popen 参数里塞一长串 PowerShell 引号太容易翻车。
由 core/updater.restart_self() 用 venv 的 pythonw 以 DETACHED 方式唤起。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KILL_PS = (
    "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
    "Where-Object { $_.CommandLine -match 'qqbridge.main' } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
    "Where-Object { $_.CommandLine -match 'qqbridge.main' } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
)


def main() -> int:
    time.sleep(1.6)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", KILL_PS],
            timeout=30,
            capture_output=True,
        )
    except Exception as exc:  # noqa: BLE001
        print("kill failed:", exc)
    time.sleep(1.0)

    vbs = ROOT / "start_conch.vbs"
    if vbs.exists():
        try:
            os.startfile(str(vbs))  # noqa: S606 走 shell，等价于双击
        except Exception as exc:  # noqa: BLE001
            print("launch failed:", exc)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())