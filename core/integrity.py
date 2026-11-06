"""完整性自检。

Windows 会做一件很容易踩的事：进程的完整性级别取「父进程」与「可执行文件所在目录的标签」
里**更低**的那个。如果 .venv 所在的目录被打了 Low 标签，海螺一启动就是 Low，
它拉起来的每个服务（SnowLuma、情绪、留影…）也全是 Low——
表现就是 SnowLuma 报 `Node integrity: process=Low`、并且写不了自己的日志（EPERM）。

这里只做两件事：读自己的级别、给一句人话。
"""

from __future__ import annotations

import sys

RID_NAMES = {
    0x0000: "Untrusted",
    0x1000: "Low",
    0x2000: "Medium",
    0x3000: "High",
    0x4000: "System",
}


def current() -> str:
    """返回 'Low' / 'Medium' / 'High' / 'System'，读不到就回 '未知'。"""
    if sys.platform != "win32":
        return "非 Windows"
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        # 64 位下句柄是 8 字节，不声明原型会被截成 int，后面必然失败
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            return "未知"

        buf = ctypes.create_string_buffer(64)
        need = wintypes.DWORD()
        ok = advapi32.GetTokenInformation(
            token, 25, buf, ctypes.sizeof(buf), ctypes.byref(need)  # TokenIntegrityLevel
        )
        kernel32.CloseHandle(token)
        if not ok:
            return "未知"

        # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes } }
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        sub_authority_count = ctypes.cast(sid_ptr, ctypes.POINTER(ctypes.c_ubyte))[1]
        offset = 8 + (sub_authority_count - 1) * 4
        rid = ctypes.cast(sid_ptr + offset, ctypes.POINTER(wintypes.DWORD))[0]
        return RID_NAMES.get(rid, f"RID {rid}")
    except Exception:
        return "未知"


def warning(level: str) -> str:
    """级别不对劲时给一句能照着办的提示，正常返回空串。"""
    if level not in ("Low", "Untrusted"):
        return ""
    return (
        f"海螺自己在「{level}」完整性下运行，它拉起的服务也都会是 {level}："
        "这些服务会写不进自己的目录（SnowLuma 甚至写不了日志，并报运行环境告警）。"
        "修法见 docs/windows-low-integrity.md，或跑 scripts/fix_integrity.ps1 后重启海螺。"
    )