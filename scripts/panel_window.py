"""找 / 唤起 / 新开海螺控制台的那个浏览器窗口。

纯 ctypes 直接调 Win32，**不启 powershell**。

为什么换掉 open_panel.ps1：
  以前每点一次图标都要 `powershell -File` 拉起一个新进程、再现场 `Add-Type`
  编译一段 C#，光这两步就 1~2 秒，人感觉是「点了没反应」，于是连点几下 ——
  每一下都开一个新窗口。现在放在托盘进程里直接 EnumWindows，已有窗口时基本是 0ms。

三条规矩（和 open_panel.ps1 一致，别退回去）：
1. 记着窗口句柄（data\\panel.hwnd）：Chromium 的标题跟着「当前标签」变，
   只看标题会在切走标签后认错，于是越点越多窗口。
2. 只有在最小化时才 SW_RESTORE，否则最大化窗口会被打回小窗。
3. 抢不到前台不算失败：窗口可见就够了，否则会被误判成「没开着」再开一个。
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
import winreg
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "panel.hwnd"
PIDFILE = ROOT / "data" / "panel.pid"
OPENING = ROOT / "data" / "panel.opening"
# 面板用**自己的**浏览器配置目录：不和你平时开的浏览器抢同一个实例，
# 也不会因为「已有浏览器任务」被合并进去（合并后就看不见新窗口了）
PROFILE = ROOT / "data" / "panel-profile"
BROWSERS = (
    "msedge", "chrome", "brave", "firefox", "vivaldi", "opera",
    "360se", "360chrome", "qqbrowser", "sogouexplorer", "arc",
)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsZoomed.argtypes = [wintypes.HWND]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _proc_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        _kernel32.CloseHandle(handle)


def _title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(400)
    _user32.GetWindowTextW(hwnd, buf, 400)
    return buf.value


def alive(hwnd: int) -> bool:
    return bool(hwnd) and bool(_user32.IsWindow(hwnd)) and bool(_user32.IsWindowVisible(hwnd))


def _windows_of_pid(pid: int) -> list[int]:
    """这个进程的所有可见顶层窗口。"""
    out: list[int] = []

    def cb(hwnd, _lp):
        if not _user32.IsWindowVisible(hwnd):
            return True
        holder = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(holder))
        if holder.value == pid and _title(hwnd):
            out.append(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return out


def find_for_pid(pid: int) -> int:
    """认出「我们自己起的那个浏览器窗口」—— 只认进程，不看标题。

    标题认窗口会认错：GitHub 上的仓库页标题里就有「海螺」，
    以前会把那个标签当面板提上来，于是「面板打不开、打开的是 GitHub」。
    """
    if not pid or not _is_browser_process(pid):
        return 0
    wins = _windows_of_pid(pid)
    return wins[0] if wins else 0


def _is_browser_process(pid: int) -> bool:
    name = _proc_name(pid)
    return bool(name) and any(b in name for b in BROWSERS)


def _remember(hwnd: int) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(str(int(hwnd)), encoding="ascii")
    except OSError:
        pass


def _remember_pid(pid: int) -> None:
    try:
        PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(str(int(pid)), encoding="ascii")
    except OSError:
        pass


def _remembered_pid() -> int:
    try:
        return int(PIDFILE.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        return 0


def _process_alive(pid: int) -> bool:
    if not pid:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return True


def _remembered() -> int:
    try:
        return int(STATE.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        return 0


def raise_window(hwnd: int) -> bool:
    if not alive(hwnd):
        return False
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, 9)      # SW_RESTORE：只有最小化时才还原
    else:
        _user32.ShowWindow(hwnd, 5)      # SW_SHOW：不动最大化状态
    if not _user32.IsZoomed(hwnd):
        _user32.ShowWindow(hwnd, 3)      # SW_MAXIMIZE：打开就该是铺满
    _user32.BringWindowToTop(hwnd)
    _user32.SetForegroundWindow(hwnd)
    if _user32.GetForegroundWindow() != hwnd:
        # 前台抢不到很常见（别的程序刚拿过焦点）：把当前前台的线程挂过来再试一次
        fg_pid = wintypes.DWORD()
        thread = _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(fg_pid))
        me = _kernel32.GetCurrentThreadId()
        if thread and thread != me and _user32.AttachThreadInput(me, thread, True):
            _user32.SetForegroundWindow(hwnd)
            _user32.AttachThreadInput(me, thread, False)
    return alive(hwnd)


def _browser_exe() -> str:
    prog = ""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        )
        prog = str(winreg.QueryValueEx(key, "ProgId")[0])
    except OSError:
        pass
    cands: list[str] = []
    if "Edge" in prog:
        cands = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ]
    elif "Chrome" in prog:
        cands = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        ]
    for path in cands:
        if path and Path(path).exists():
            return path
    return ""


def _claim_launch() -> bool:
    """跨进程的「我正在开」标记。

    托盘里的连点靠线程锁挡住了，但双击桌面快捷方式 + 点托盘可能同时进来两个进程，
    两个都看不到窗口就一起开浏览器 —— 又是一堆窗口。这个标记文件让后来的那个
    只等着，不自己开。
    """
    try:
        if OPENING.exists():
            age = time.time() - OPENING.stat().st_mtime
            if age < 15:
                return False
            OPENING.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        OPENING.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT|O_EXCL 是原子的：两个进程同时抢，只有一个能拿到
        fd = os.open(str(OPENING), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def _release_launch() -> None:
    try:
        OPENING.unlink(missing_ok=True)
    except OSError:
        pass


def open_panel(url: str, wait: float = 12.0, log=print) -> str:
    """打开（或提到最前）控制台页面。返回 raised / opened / waiting / failed。"""
    hwnd = _remembered()
    if alive(hwnd) and raise_window(hwnd):
        log("面板窗口提到最前")
        return "raised"

    # 上次起的那个浏览器还在跑？按 pid 认它（标题不可靠）
    pid = _remembered_pid()
    if pid and _process_alive(pid):
        hwnd = find_for_pid(pid)
        if hwnd:
            _remember(hwnd)
            raise_window(hwnd)
            log("按进程找到面板窗口，提到最前")
            return "raised"
        log("面板进程还活着，但窗口还没出来（可能正在关）")
        return "waiting"

    start = time.time()
    end = start + wait

    if not _claim_launch():
        # 别的进程正在开：等它开出来，别自己也开一个
        log("别的入口正在打开，等它")
        while time.time() < end:
            time.sleep(0.15)
            other = _remembered_pid()
            if other:
                hwnd = find_for_pid(other)
                if hwnd:
                    _remember(hwnd)
                    raise_window(hwnd)
                    return "raised"
            time.sleep(0)
        log("等不到那个窗口")
        return "waiting"

    try:
        exe = _browser_exe()
        if exe:
            # 独立 profile：不跟已有的浏览器任务合并，保证窗口真的新开一个
            try:
                PROFILE.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            proc = subprocess.Popen(  # noqa: S603
                [exe, f"--user-data-dir={PROFILE}", "--no-first-run",
                 "--no-default-browser-check", "--new-window", "--start-maximized", url],
                close_fds=True,
            )
            _remember_pid(proc.pid)
        else:
            os.startfile(url)  # noqa: S606
            _remember_pid(0)

        while time.time() < end:
            time.sleep(0.15)
            target = _remembered_pid()
            hwnd = find_for_pid(target) if target else 0
            if hwnd:
                _remember(hwnd)
                _user32.ShowWindow(hwnd, 3)
                raise_window(hwnd)
                log(f"新开了一个窗口（{time.time() - start:.1f}s）")
                return "opened"
        log("窗口没等到（浏览器起得慢？）")
        return "failed"
    finally:
        _release_launch()
