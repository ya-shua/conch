"""一键起停那些不在本仓库里的程序：QQ 网关、环形的心、刷视频、微光域、汐面板。

它们各有各的启动方式（.bat / .ps1 / node / python），共同点是：
  一键点了就在后台静默跑（CREATE_NO_WINDOW，日志落 data/logs/<id>.log），不弹窗口。

PID 记两处：本进程内存一份（_PIDS），data/launcher-pids.json 一份——
桌面启动器（scripts/start_all.py）和控制台面板是两个进程，靠这份档案互相接得上，
「一键关闭所有服务」才能把两个入口起的全关掉。

清单在项目根目录的 launchers.json（不进仓库，里面有本机路径和密码）：
    [
      {
        "id": "snowluma",
        "name": "SnowLuma（QQ 网关）",
        "note": "QQ 收发都走它，端口 3000 / 3001",
        "cwd": "D:\\\\AI\\\\SnowLuma",
        "cmd": "launcher.bat",
        "port": 3000,
        "secrets": [{"key": "token", "label": "WS Token", "env": "SNOWLUMA_TOKEN"}]
      }
    ]
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 我们自己起的进程，按 id 记住，方便停
_PIDS: dict[str, int] = {}

# 跨进程的 PID 档案：桌面启动器（start_all.py）和控制台面板各是一个进程，
# 各自记一份没用，所以都写进 data/launcher-pids.json，谁都能关谁起的。
_CFG_PATH: str = ""

# 杀进程前先认一下脸：PID 可能被系统复用给别的程序，名字不对就不动手
_KILLABLE_IMAGES = {
    "node.exe",
    "python.exe",
    "python3.exe",
    "pythonw.exe",
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
}


def _path(cfg_path: str | Path) -> Path:
    return Path(cfg_path).resolve().parent / "launchers.json"


def _pid_path(cfg_path: str | Path | None = None) -> Path:
    base = Path(cfg_path or _CFG_PATH or ".").resolve().parent
    return base / "data" / "launcher-pids.json"


def _read_pids(cfg_path: str | Path | None = None) -> dict[str, int]:
    try:
        data = json.loads(_pid_path(cfg_path).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for key, val in data.items():
        try:
            out[str(key)] = int(val)
        except (TypeError, ValueError):
            continue
    return out


def _write_pids(cfg_path: str | Path | None, pids: dict[str, int]) -> None:
    path = _pid_path(cfg_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pids, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def record_pid(item_id: str, pid: int) -> None:
    """记住一条服务的 PID：内存一份、档案一份。桌面启动器也走这个。"""
    _PIDS[str(item_id)] = int(pid)
    pids = _read_pids()
    pids[str(item_id)] = int(pid)
    _write_pids(None, pids)


def _forget_pid(item_id: str) -> None:
    _PIDS.pop(str(item_id), None)
    pids = _read_pids()
    if str(item_id) in pids:
        pids.pop(str(item_id), None)
        _write_pids(None, pids)


_LOG_DIR: Path | None = None


def log_dir() -> Path:
    """服务日志统一落在 data/logs，不去动别人的目录。"""
    if _LOG_DIR is not None:
        return _LOG_DIR
    return Path(__file__).resolve().parent.parent / "data" / "logs"


TOKEN_FILES = (
    "state/console-token",
    "state/token",
    "data/token",
    "data/token.txt",
    "data/console-token",
    "console-token",
    ".token",
    "token.txt",
    "token",
)


def _plugin_launch_items(roots: list[str]) -> list[dict[str, Any]]:
    """扫插件目录，把 metadata.json 里的 launch 段变成启动项。"""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for meta_path in sorted(base.glob("*/metadata.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            launch = meta.get("launch")
            if not isinstance(launch, dict):
                continue
            pid = str(meta.get("id") or meta_path.parent.name)
            if pid in seen:
                continue
            seen.add(pid)
            plugin_dir = meta_path.parent
            cwd = str(launch.get("cwd") or plugin_dir)
            item = {
                "id": pid,
                "name": launch.get("name") or meta.get("name") or pid,
                "note": launch.get("note") or meta.get("description") or "",
                "cwd": cwd,
                "cmd": launch.get("cmd") or "",
                "args": list(launch.get("args") or []),
                "port": launch.get("port"),
                "from_plugin": str(plugin_dir),
            }
            for key in ("url", "url_token_file", "env", "log_dir", "secrets", "kind"):
                if launch.get(key) is not None:
                    item[key] = launch[key]
            if not item["cmd"]:
                continue
            items.append(item)
    return items


def _secret_value(item: dict[str, Any]) -> str:
    """条目里存着的密码值（有就优先用它，比猜文件靠谱）。"""
    values = item.get("secret_values") or {}
    if not isinstance(values, dict):
        return ""
    for spec in item.get("secrets") or []:
        key = str((spec or {}).get("key") or "") if isinstance(spec, dict) else ""
        if key and values.get(key):
            return str(values[key])
    for val in values.values():
        if val:
            return str(val)
    return ""


_PWD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


def gen_password(length: int = 16) -> str:
    """随机密码：去掉了 0/o/1/l 这些看着像的字符。"""
    import secrets as _secrets

    return "".join(_secrets.choice(_PWD_ALPHABET) for _ in range(length))


def _owned_item(items: list[dict[str, Any]], path_hint: str | Path) -> dict[str, Any] | None:
    """哪个启动项管着这个目录：只认「正好是这个插件」或「cwd 就是它」。"""
    target = Path(str(path_hint)).resolve()

    def same(a: object) -> bool:
        try:
            return Path(str(a)).resolve() == target
        except (OSError, TypeError, ValueError):
            return False

    for item in items:
        if same(item.get("from_plugin")):
            return item

    for item in items:
        if same(item.get("cwd")):
            return item

    # 不再按「上位目录」猜：项目根目录是很多服务的上位目录，会认错人
    return None


def ensure_password(
    cfg_path: str | Path,
    path_hint: str | Path,
    plugin_roots: list[str] | None = None,
) -> dict[str, Any]:
    """刚落地的插件：把它那条启动项里空着的密码补一个随机值。

    只补空的，不覆盖已有的；插件自己带 launch 声明的那种，会顺手写进 launchers.json，
    这样密码才有地方存、下次启动才注得进去。
    """
    path = _path(cfg_path)
    raw: list[dict[str, Any]] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            return {"ok": False, "reason": f"launchers.json 读不动：{exc}"}
        if not isinstance(loaded, list):
            return {"ok": False, "reason": "launchers.json 不是列表格式"}
        raw = [i for i in loaded if isinstance(i, dict) and i.get("id")]
    elif not plugin_roots:
        return {"ok": False, "reason": "没有 launchers.json，先建一条启动项再装"}

    # 手写的优先，但插件自己声明的 launch 也要算进来
    merged = load(cfg_path, plugin_roots) if plugin_roots else list(raw)
    item = _owned_item(merged, path_hint)
    if item is None:
        return {"ok": False, "reason": "这个插件没有独立服务，不需要面板密码"}

    known = {str(i.get("id")) for i in raw}
    need_save = str(item["id"]) not in known
    if need_save:
        raw.append(item)

    specs = [s for s in (item.get("secrets") or []) if isinstance(s, dict) and s.get("key")]
    if not specs:
        specs = [
            {
                "key": "password",
                "label": f"{item.get('name') or item['id']} 面板密码",
                "env": f"CONCH_{str(item['id']).upper()}_PASSWORD",
            }
        ]
        item["secrets"] = specs

    values = dict(item.get("secret_values") or {})
    created: dict[str, str] = {}
    for spec in specs:
        key = str(spec["key"])
        if not values.get(key):
            values[key] = gen_password()
            created[key] = values[key]
    if not created:
        if need_save:
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "ok": True,
            "created": {},
            "id": item["id"],
            "name": item.get("name") or item["id"],
            "note": "这条启动项本来就有密码，没动",
        }

    item["secret_values"] = values
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "created": created,
        "id": item["id"],
        "name": item.get("name") or item["id"],
    }


def _location_tokens(cwd: Path) -> list[str]:
    """有的插件把数据目录搬走了（data-location.json），令牌跟着搬走。

    环形的心就是这样：插件目录里 data/token.txt 是搬家前的旧令牌，
    真正在用的是搬家后的 dataDir/token.txt —— 不跟着走就会显示错密码。
    """
    found: list[str] = []
    for name in ("data-location.json", "location.json"):
        fp = cwd / name
        if not fp.is_file():
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        data_dir = str(data.get("dataDir") or "").strip()
        if not data_dir:
            continue
        base = Path(data_dir)
        if not base.is_absolute():
            base = cwd / data_dir
        found.append(str(base / "token.txt"))
        found.append(str(base / "token"))
    return found


def _discover_token(item: dict[str, Any]) -> str:
    """令牌：先按声明的文件找，再看有没有搬过家，最后按常见位置碰运气。"""
    cwd = Path(str(item.get("cwd") or "."))
    declared = item.get("url_token_file")
    candidates = [str(declared)] if declared else []
    candidates += _location_tokens(cwd)
    candidates += list(TOKEN_FILES)
    for rel in candidates:
        try:
            fp = Path(rel)
            if not fp.is_absolute():
                fp = cwd / rel
            if fp.is_file():
                tok = fp.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                if tok and tok[0].strip():
                    return tok[0].strip()
        except OSError:
            continue
    return ""


def load(
    cfg_path: str | Path,
    plugin_roots: list[str] | None = None,
) -> list[dict[str, Any]]:
    global _CFG_PATH
    _CFG_PATH = str(Path(cfg_path).resolve())
    path = _path(cfg_path)
    items: list[dict[str, Any]] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            items = [item for item in data if isinstance(item, dict) and item.get("id")]
        except Exception:
            items = []
    if plugin_roots:
        known = {str(i["id"]) for i in items}
        for extra in _plugin_launch_items(plugin_roots):
            if str(extra["id"]) not in known:  # 手写的 launchers.json 优先
                items.append(extra)
                known.add(str(extra["id"]))
    return items


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
    """进程还活着吗？ctypes 直接问系统，不靠 tasklist（它在这台机器的
    子进程里时灵时不灵，OpenProcess 又快又稳）。"""
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle, err = _open_query(pid)
    if handle:
        _close(handle)
        return True
    return err == 5  # ERROR_ACCESS_DENIED：进程在，只是摸不动（比如管理员起的）


def _open_query(pid: int) -> tuple[int, int]:
    """拿 PROCESS_QUERY_LIMITED_INFORMATION 句柄，失败回 (0, GetLastError)。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    return (handle or 0), (ctypes.get_last_error() if not handle else 0)


def _close(handle: int) -> None:
    ctypes.WinDLL("kernel32").CloseHandle(handle)


def _image_of(pid: int) -> str:
    """这个 PID 现在跑的是什么程序（如 node.exe），问不出来就回空串。"""
    if sys.platform != "win32" or not pid:
        return ""
    handle, _ = _open_query(pid)
    if not handle:
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name
    finally:
        _close(handle)


def _run_text(argv: list[str], timeout: int = 10) -> str:
    """跑一条 Windows 命令拿输出。netstat/tasklist 在中文系统上吐 GBK，
    让 subprocess 按默认编码解码会直接炸，所以拿字节自己容错解码。"""
    try:
        out = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).stdout
    except Exception:
        return ""
    return out.decode("utf-8", errors="replace")


def _pid_on_port(port: int) -> int:
    """netstat 找出正听着这个端口的 PID，找不到回 0。"""
    for line in _run_text(["netstat", "-ano", "-p", "tcp"]).splitlines():
        parts = line.split()
        # TCP    127.0.0.1:5173    0.0.0.0:0    LISTENING    4321
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return 0


def _tracked_pid(item_id: str) -> int:
    """内存里的 PID 优先（肯定是这一轮起的），档案里的兜底（桌面启动器起的）。"""
    for source in (_PIDS, _read_pids()):
        pid = int(source.get(str(item_id)) or 0)
        if pid and _pid_alive(pid):
            return pid
    return 0


def log_file(item_id: str) -> Path:
    return log_dir() / f"{item_id}.log"


def _url_with_token(item: dict[str, Any], port: Any) -> str:
    """页面地址；工作台那种要令牌的，把 state 里的 token 拼上去，点开就登录。"""
    url = str(item.get("url") or "").strip()
    if not url and port:
        url = f"http://127.0.0.1:{port}/"
    if not url:
        return ""
    if "token=" not in url:
        tok = _discover_token(item)
        if tok:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={tok}"
    return url


_ALIVE_CACHE: dict[str, tuple[float, list[int]]] = {}


def pids_under(cwd: str) -> list[int]:
    """这个目录下起来的进程（按可执行文件路径认）。

    有的服务（微光域）没有固定端口、也不是从海螺面板启的，PID 档案里没有它，
    光看端口就会把「明明在跑」算成「已关闭」。用 exe 路径兜一下。
    """
    root = str(cwd or "").strip().rstrip("\\/").lower()
    if not root:
        return []
    now = time.time()
    hit = _ALIVE_CACHE.get(root)
    if hit and now - hit[0] < 3.0:          # 面板一次加载会问好几遍，别重复查
        return list(hit[1])
    out: list[int] = []
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
             "Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath "
             "| ConvertTo-Json -Compress"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=0x08000000,
        ).stdout.strip()
        rows = json.loads(raw) if raw else []
    except Exception:
        rows = []
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        exe = str(row.get("ExecutablePath") or "")
        pid = int(row.get("ProcessId") or 0)
        if not exe or not pid:
            continue
        path = exe.lower().replace("/", "\\")
        if path == root or path.startswith(root + "\\"):
            out.append(pid)
    _ALIVE_CACHE[root] = (now, out)
    return out


def status(item: dict[str, Any]) -> dict[str, Any]:
    port = item.get("port")
    pid = _tracked_pid(str(item["id"]))
    by_port = port_open(int(port) if port else None)
    found = pids_under(str(item.get("cwd") or "")) if not (pid or by_port) else []
    if not pid and found:
        pid = found[0]
    by_process = bool(pid)
    return {
        "id": item["id"],
        "name": item.get("name") or item["id"],
        "en": item.get("en") or "",
        "note": item.get("note") or "",
        "cwd": item.get("cwd") or "",
        "cmd": item.get("cmd") or "",
        "port": port,
        "url": _url_with_token(item, port),
        "log": str(log_file(str(item["id"]))),
        "secrets": [
            {
                "key": s.get("key"),
                "label": s.get("label") or s.get("key"),
                "filled": bool((item.get("secret_values") or {}).get(s.get("key"))),
            }
            for s in (item.get("secrets") or [])
            if isinstance(s, dict) and s.get("key")
        ],
        "secret": _secret_value(item) or "",
        "token": _discover_token(item) or "",
        "running": bool(by_port or by_process),
        "by": "端口" if by_port else ("进程" if by_process else ""),
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
    for key, value in (item.get("env") or {}).items():
        env[str(key)] = str(value)
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

    # bat 交给 ShellExecute（跟双击等价），否则 pause/title 会失效
    if str(argv[0]).lower().endswith((".bat", ".cmd")):
        bat = str(Path(cwd) / argv[0]) if not Path(argv[0]).is_absolute() else argv[0]
        ps = f"Start-Process -FilePath '{bat}' -WorkingDirectory '{cwd}' -WindowStyle Hidden"
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            cwd=cwd,
            capture_output=True,
            creationflags=0x08000000,
        )
        _PIDS[str(item["id"])] = 0
        return {"ok": True, "pid": 0, "argv": argv, "log": "", "via": "shellexec"}

    # 隐藏窗口：日志落到 data/logs/<id>.log，出问题看那里。
    # 关键：不能写进服务自己的目录（有些目录当前进程没权限，一 open 就 500）。
    logs = Path(str(item.get("log_dir") or "")).expanduser() if item.get("log_dir") else log_dir()
    log_path = logs / f"{item['id']}.log"
    handle = None
    try:
        logs.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab")
    except OSError:
        log_path = Path("")
        handle = None
    flags = 0x08000000  # CREATE_NO_WINDOW
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        creationflags=flags,
        startupinfo=startup,
        stdout=handle if handle else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if handle else subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    record_pid(str(item["id"]), proc.pid)
    return {"ok": True, "pid": proc.pid, "argv": argv, "log": str(log_path)}


def stop_all(items: list[dict[str, Any]]) -> dict[str, Any]:
    """关掉所有服务：不管是控制台起的还是桌面启动器起的，PID 档案里都有账。"""
    stopped, skipped = [], []
    for item in items:
        result = stop(item)
        (stopped if result.get("ok") else skipped).append(item.get("id"))
    return {"ok": True, "stopped": stopped, "skipped": skipped}


def stop(item: dict[str, Any]) -> dict[str, Any]:
    """按 内存 PID → 档案 PID → 端口 的顺序找到进程，连树杀掉。"""
    item_id = str(item["id"])
    port = item.get("port")
    pid = _tracked_pid(item_id)
    if not pid and port:
        pid = _pid_on_port(int(port))
    if not pid:
        return {"ok": False, "error": "没在跑，或者不是从海螺起的，关不掉"}

    image = _image_of(pid)
    if image and image.lower() not in _KILLABLE_IMAGES:
        return {"ok": False, "error": f"PID {pid} 现在是 {image}，不像海螺起的服务，没动它"}
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=0x08000000,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    _forget_pid(item_id)
    if _pid_alive(pid) or (port and port_open(int(port))):
        # taskkill 偶发没杀干净（比如子进程还攥着端口），再按端口补一刀
        if port:
            straggler = _pid_on_port(int(port))
            if straggler and straggler != pid:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(straggler), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        creationflags=0x08000000,
                    )
                except Exception:
                    pass
        if _pid_alive(pid) or (port and port_open(int(port))):
            return {"ok": False, "error": "杀了但端口还占着，可能要再按一次"}
    return {"ok": True, "pid": pid}