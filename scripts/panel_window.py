"""找 / 唤起 / 新开海螺控制台的那个浏览器窗口。

纯 ctypes 直接调 Win32，**不启 powershell**（只有兜底那一条路才查命令行）。

为什么换掉 open_panel.ps1：
  以前每点一次图标都要 `powershell -File` 拉起一个新进程、再现场 `Add-Type`
  编译一段 C#，光这两步就 1~2 秒，人感觉是「点了没反应」，于是连点几下 ——
  每一下都开一个新窗口。现在放在托盘进程里直接 EnumWindows，已有窗口时基本是 0ms。

五条规矩（前三条和 open_panel.ps1 一致，别退回去）：

1. 记着窗口句柄（data\\panel.hwnd）：Chromium 的标题跟着「当前标签」变，
   只看标题会在切走标签后认错，于是越点越多窗口。
2. 只有在最小化时才 SW_RESTORE，否则最大化窗口会被打回小窗。
3. 抢不到前台不算失败：窗口可见就够了，否则会被误判成「没开着」再开一个。
4. **认进程要连启动时间一起认**（data\\panel.proc = pid + 启动时间）。
   只认 pid 会认错：Windows 会回收 pid，微光域那套 Playwright 的 msedge 随时可能
   拿到同一个号 —— 于是「点海螺弹出微光域的浏览器」。
5. **进程活着但窗口没了，就再开一个**，不要干等。
   以前这里返回 "waiting"，玩家看到的是「点了没反应」，再点几下还是没反应，
   因为那条死路永远是死路。窗口关掉是正常操作（点 X 不等于退出程序）。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import winreg
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "panel.hwnd"          # {"hwnd":..., "pid":..., "start":...}
PIDFILE = ROOT / "data" / "panel.proc"         # {"pid":..., "start":...}
OPENING = ROOT / "data" / "panel.opening"
# 面板用**自己的**浏览器配置目录：不和你平时开的浏览器抢同一个实例，
# 也不会因为「已有浏览器任务」被合并进去（合并后就看不见新窗口了）。
# 也因为目录是独一份的 —— 微光域的 data/profile 跟这里不是一回事。
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
_kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_EPOCH_DIFF = 11644473600.0        # 1601-01-01 → 1970-01-01 的秒数（privacy-ok）


# ---------------------------------------------------------------- 进程身份

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


def _proc_start(pid: int) -> float:
    """进程启动时间（epoch 秒）。0.0 = 拿不到（进程不在 / 权限不够）。

    这是认出「同一个进程」的关键：pid 会被 Windows 回收，启动时间不会。
    """
    if not pid:
        return 0.0
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0.0
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = _kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return 0.0
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 1e7 - _EPOCH_DIFF
    finally:
        _kernel32.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    if not pid:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return True


def _is_browser_process(pid: int) -> bool:
    name = _proc_name(pid)
    return bool(name) and any(b in name for b in BROWSERS)


def _same_proc(pid: int, start: float) -> bool:
    """还是当初那个进程吗？

    启动时间对得上 → 就是它（pid 复用会换启动时间，这是主要的判据）。
    启动时间拿不到（老文件 / 权限不够）→ 退一步查它的命令行里有没有
    `--user-data-dir=…\\panel-profile`，确认不是微光域那套浏览器。
    """
    if not pid or not _process_alive(pid) or not _is_browser_process(pid):
        return False
    now = _proc_start(pid)
    if start and now:
        return abs(now - float(start)) < 2.0
    return _uses_panel_profile(pid)


def _uses_panel_profile(pid: int) -> bool:
    """兜底判据：这个浏览器进程用的是不是面板专用的配置目录。

    只在这条少见的路上才查（一次 ~0.4s 的 powershell），平时一句都不问。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=6, creationflags=0x08000000,
        ).stdout or ""
    except Exception:
        return False
    return "panel-profile" in out.lower()


# ---------------------------------------------------------------- 窗口

def _title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(400)
    _user32.GetWindowTextW(hwnd, buf, 400)
    return buf.value


def _owner_pid(hwnd: int) -> int:
    holder = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(holder))
    return int(holder.value)


def alive(hwnd: int) -> bool:
    return bool(hwnd) and bool(_user32.IsWindow(hwnd)) and bool(_user32.IsWindowVisible(hwnd))


def _windows_of_pid(pid: int) -> list[int]:
    """这个进程的所有可见顶层窗口。"""
    out: list[int] = []

    def cb(hwnd, _lp):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _owner_pid(hwnd) == pid and _title(hwnd):
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


def _newest_browser_window(since: float) -> int:
    """兜底：刚起的那一秒里冒出来的浏览器窗口，哪个算我们的。

    只用在「我们 spawn 的进程没窗口」这条少见的路上（浏览器把窗口交给了别的实例），
    而且要求进程启动时间也在 since 之后 —— 不会把微光域早就开着的窗口认进来。
    """
    found: list[int] = []

    def cb(hwnd, _lp):
        if not _user32.IsWindowVisible(hwnd) or not _title(hwnd):
            return True
        pid = _owner_pid(hwnd)
        if not _is_browser_process(pid):
            return True
        started = _proc_start(pid)
        if started and started >= float(since) - 1.5:
            found.append(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found[0] if found else 0


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


# ---------------------------------------------------------------- 记在磁盘上的身份

def _read_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        # 老格式：只有一个数字（pid / hwnd），没有启动时间
        return {"legacy": raw}
    if isinstance(data, dict):
        return data
    # 也是老格式：裸的 1234
    return {"legacy": str(data)}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _remember(hwnd: int, pid: int = 0) -> None:
    hwnd = int(hwnd or 0)
    pid = int(pid or 0) or _owner_pid(hwnd)
    _write_json(STATE, {"hwnd": hwnd, "pid": pid, "start": round(_proc_start(pid), 3)})


def _remembered_window() -> dict:
    data = _read_json(STATE)
    if data.get("legacy"):
        return {}
    try:
        return {
            "hwnd": int(data.get("hwnd") or 0),
            "pid": int(data.get("pid") or 0),
            "start": float(data.get("start") or 0.0),
        }
    except (TypeError, ValueError):
        return {}


def _window_matches(win: dict) -> bool:
    """记着的那个窗口，还是当初那个进程的窗口吗。"""
    hwnd = int(win.get("hwnd") or 0)
    if not hwnd or not alive(hwnd):
        return False
    pid = int(win.get("pid") or 0) or _owner_pid(hwnd)
    if pid and _owner_pid(hwnd) != pid:
        return False
    if pid and not _same_proc(pid, float(win.get("start") or 0.0)):
        return False
    return True


def _remember_pid(pid: int) -> None:
    pid = int(pid or 0)
    _write_json(PIDFILE, {"pid": pid, "start": round(_proc_start(pid), 3)})


def _remembered_proc() -> dict:
    data = _read_json(PIDFILE)
    if data.get("legacy"):
        try:
            pid = int(str(data["legacy"]).strip() or 0)
        except ValueError:
            return {}
        # 老文件没有启动时间：只敢当「可能」，由 _same_proc 再核一遍
        return {"pid": pid, "start": 0.0} if pid else {}
    try:
        pid = int(data.get("pid") or 0)
        return {"pid": pid, "start": float(data.get("start") or 0.0)} if pid else {}
    except (TypeError, ValueError):
        return {}


def _remembered_pid() -> int:
    return int(_remembered_proc().get("pid") or 0)


# ---------------------------------------------------------------- 启动

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


def _panel_profile_pids() -> list[int]:
    """哪些浏览器进程用的是面板专用配置目录。

    只在「spawn 出去的进程没有窗口」这条兜底路上查：Chromium 会把新窗口交给
    同配置的已有实例，于是我们 spawn 的那个进程自己是个空壳、窗口在别人身上。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
             "Where-Object { $_.CommandLine -like '*panel-profile*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, creationflags=0x08000000,
        ).stdout or ""
    except Exception:
        return []
    pids: list[int] = []
    for token in out.split():
        if token.strip().isdigit():
            pids.append(int(token))
    return pids


def _adopt_profile_window() -> int:
    """面板专用配置的那个浏览器已经有窗口了 → 认它。

    这就是「点了没反应」的另一半：spawn 出去的进程没窗口，但窗口其实开着，
    只是落在同配置的旧实例上。认出来就把它提上来，别再新开。
    """
    for pid in _panel_profile_pids():
        wins = _windows_of_pid(pid)
        if wins:
            _remember_pid(pid)
            return wins[0]
    return 0


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


def _wait_reuse(url: str, log, end: float, start: float) -> str:
    """进程还在、窗口没了：在同一个配置里再开一个窗口。

    这条是「点了没反应」的病根所在 —— 以前这里直接返回 waiting，谁点都没用。
    """
    exe = _browser_exe()
    if not exe:
        os.startfile(url)  # noqa: S606
        log("没有指定浏览器，交给系统默认浏览器")
        return "opened"
    try:
        PROFILE.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # 同一个 user-data-dir 的旧实例会自己把窗口开出来（不会重复起浏览器）
    subprocess.Popen(  # noqa: S603
        [exe, f"--user-data-dir={PROFILE}", "--no-first-run",
         "--no-default-browser-check", "--new-window", "--start-maximized", url],
        close_fds=True,
    )
    while time.time() < end:
        time.sleep(0.12)
        pid = _remembered_pid()
        hwnd = find_for_pid(pid)
        if not hwnd:
            hwnd = _newest_browser_window(start)
        if not hwnd and time.time() - start > 1.2:
            hwnd = _adopt_profile_window()
        if hwnd:
            _remember(hwnd, pid or _owner_pid(hwnd))
            raise_window(hwnd)
            log(f"窗口关了又打开（{time.time() - start:.1f}s）")
            return "opened"
    log("再开一次窗口还是没出来")
    return "failed"


def open_panel(url: str, wait: float = 12.0, log=print) -> str:
    """打开（或提到最前）控制台页面。返回 raised / opened / waiting / failed。"""
    # 1) 记着的窗口还在，而且确实是当初那个进程的窗口
    win = _remembered_window()
    if win and _window_matches(win) and raise_window(win["hwnd"]):
        log("面板窗口提到最前")
        return "raised"

    start = time.time()
    end = start + wait

    # 2) 记着的那个浏览器进程还在吗（连启动时间一起对）
    proc = _remembered_proc()
    if proc and _same_proc(int(proc["pid"]), float(proc.get("start") or 0.0)):
        hwnd = find_for_pid(int(proc["pid"]))
        if hwnd:
            _remember(hwnd, int(proc["pid"]))
            raise_window(hwnd)
            log("按进程找到面板窗口，提到最前")
            return "raised"
        # 进程活着但窗口被关了（点 X ≠ 退出）：在同一个配置里再开一个
        log("面板进程还在，窗口被关了 —— 给它再开一个窗口")
        if not _claim_launch():
            log("别的入口正在打开，等它")
        else:
            try:
                return _wait_reuse(url, log, end, start)
            finally:
                _release_launch()
    elif proc:
        log("上次那个浏览器已经不是同一个进程了（pid 被系统回收），重新开")
        # 记录过期了，但面板专用配置的那个浏览器可能还开着（只是 pid 记的不是它）
        hwnd = _adopt_profile_window()
        if hwnd:
            _remember(hwnd)
            raise_window(hwnd)
            log("面板窗口本来就开着（按配置目录认出来的），提到最前")
            return "raised"

    if not _claim_launch():
        # 别的进程正在开：等它开出来，别自己也开一个
        log("别的入口正在打开，等它")
        while time.time() < end:
            time.sleep(0.12)
            other = _remembered_window()
            if other and _window_matches(other) and raise_window(other["hwnd"]):
                return "raised"
            pid = _remembered_pid()
            hwnd = find_for_pid(pid) if pid else 0
            if hwnd:
                _remember(hwnd, pid)
                raise_window(hwnd)
                return "raised"
        log("等不到那个窗口")
        return "waiting"

    spawned_at = time.time()
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
            target = proc.pid
        else:
            os.startfile(url)  # noqa: S606
            _remember_pid(0)
            target = 0

        while time.time() < end:
            time.sleep(0.12)
            hwnd = find_for_pid(target) if target else 0
            if not hwnd:
                # 浏览器把窗口交给已有实例时，pid 对不上，按「刚冒出来的窗口」兜一次
                hwnd = _newest_browser_window(spawned_at)
            if not hwnd and time.time() - spawned_at > 1.2:
                # 再兜一次：同配置的旧实例接了窗口（它比我们 spawn 的进程老）
                hwnd = _adopt_profile_window()
            if hwnd:
                _remember(hwnd, target or _owner_pid(hwnd))
                _user32.ShowWindow(hwnd, 3)
                raise_window(hwnd)
                log(f"新开了一个窗口（{time.time() - start:.1f}s）")
                return "opened"
        log("窗口没等到（浏览器起得慢？）")
        return "failed"
    finally:
        _release_launch()