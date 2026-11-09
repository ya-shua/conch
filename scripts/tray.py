"""海螺托盘：右下角一个图标，服务全在后台跑。

规矩（2026-11-08 定）
---------------------
1. **桌面只有一个入口**：那个快捷方式只调这里，不再单独起浏览器/控制台。
2. **图标在 = 服务在**：托盘进程负责把全家桶拉起来（雪花、记忆库、情绪、插件…），
   拉起来之后图标一直在右下角，关掉浏览器页面不影响任何服务。
3. **托盘退出 = 全部停掉**：只有从菜单点「退出」，才会执行 scripts/stop_all.py，
   把海螺本体、SnowLuma、记忆库、情绪面板、微光域、声来一起收掉。

菜单：打开面板 / 重启海螺 / 退出（全部停止）
双击图标 = 打开面板。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "data" / "logs" / "tray.log"
LOCK = ROOT / "data" / "tray.lock"
QUIT_FLAG = ROOT / "data" / "tray.quit"
ICON_FILE = ROOT / "data" / "tray.ico"
NO_WINDOW = 0x08000000


# ───────────────────────── 小工具 ─────────────────────────

def log(message: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def panel_url() -> str:
    panel = cfg().get("panel") or {}
    token = str(panel.get("token") or "")
    port = int(panel.get("port") or 7801)
    return f"http://127.0.0.1:{port}/" + (f"?token={token}" if token else "")


def _spawn(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )


def status() -> dict | None:
    """面板活着就返回 /api/status，否则 None。

    先探端口再问 HTTP：面板不在的时候 urlopen 会等满 2 秒（系统代理还会更久），
    而看门狗每 5 秒问一次 —— 那点白等就是「点了图标半天没反应」的一部分。
    """
    port = int((cfg().get("panel") or {}).get("port") or 7801)
    if not port_open(port):
        return None
    url = panel_url().split("?")[0] + "api/status"
    token = str((cfg().get("panel") or {}).get("token") or "")
    try:
        req = urllib.request.Request(url)
        req.add_header("x-console-token", token)
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


# ───────────────────────── 单实例 ─────────────────────────

def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def already_running() -> bool:
    try:
        raw = LOCK.read_text(encoding="utf-8").strip()
        pid = int(raw) if raw.isdigit() else 0
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid() and _alive(pid):
        return True
    return False


def take_lock() -> bool:
    """原子占位：两个托盘几乎同时起来时，只有一个能拿到。

    以前是 read_text → write_text，中间有空窗，双击两下就能起两个托盘，
    每个托盘各自拉一套服务，右下角变成两个图标、面板窗口也跟着弹两次。
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        pass
    try:
        raw = LOCK.read_text(encoding="utf-8").strip()
        pid = int(raw) if raw.isdigit() else 0
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid() and _alive(pid):
        return False  # 真有一个活着的托盘
    try:
        LOCK.unlink(missing_ok=True)
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except (FileExistsError, OSError):
        return False


# ───────────────────────── 图标 ─────────────────────────

def make_icon(path: Path) -> "object":
    """画一只海螺：金色螺旋 + 深色底，托盘里一眼能认出来。"""
    from PIL import Image, ImageDraw

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆底
    d.ellipse((8, 8, size - 8, size - 8), fill=(38, 30, 24, 255),
              outline=(214, 168, 90, 255), width=10)

    # 螺旋（海螺壳）
    import math

    cx = cy = size / 2
    points = []
    for i in range(0, 720):
        t = i / 720.0
        angle = t * math.pi * 4.2
        r = 18 + (size * 0.33) * (1 - t) ** 1.25
        points.append((cx + r * math.cos(angle - 1.2), cy + r * math.sin(angle - 1.2)))
    d.line(points, fill=(246, 208, 132, 255), width=13, joint="curve")

    # 壳口
    d.ellipse((size * 0.46, size * 0.66, size * 0.78, size * 0.92),
              fill=(214, 168, 90, 255))

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return img


# ───────────────────────── 动作 ─────────────────────────

def open_panel() -> None:
    """打开（或提到最前）控制台页面。

    Win32 调用都在本进程里做（scripts\\panel_window.py），不再拉 powershell —— 
    以前一次点击要等 1~2 秒，人以为没反应就再点，于是满屏窗口。
    """
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import panel_window
    except Exception as exc:
        log(f"面板窗口模块没加载上（{exc}），退回 shell 打开")
        try:
            os.startfile(panel_url())  # noqa: S606
        except Exception as exc2:
            log(f"打开面板失败：{exc2}")
        return
    try:
        panel_window.open_panel(panel_url(), log=log)
    except Exception as exc:
        log(f"打开面板失败：{exc}")


def start_services(wait: float = 90.0) -> bool:
    """把全家桶拉起来，等面板能应答。

    返回 True = 面板可用（不管是刚起的还是本来就在跑）。
    想知道"这次是不是真起了东西"，用 was_down()。
    """
    if status() is not None:
        return True
    log("启动全家桶")
    try:
        _spawn([str(PYTHONW), str(ROOT / "scripts" / "start_all.py")])
    except Exception as exc:
        log(f"起不来：{exc}")
        return False
    end = time.time() + wait
    while time.time() < end:
        time.sleep(1.0)
        if status() is not None:
            log("服务就绪")
            return True
    log("等超时了，面板还没应答")
    return False


def stop_services() -> None:
    """全停：海螺 + 所有外部服务。托盘退出时才会走这里。"""
    log("停全家桶")
    try:
        env = dict(os.environ)
        # 告诉 stop_all：托盘已经在退了，别再反过来等我 —— 否则两边互等
        env["CONCH_TRAY_QUITTING"] = "1"
        subprocess.run(
            [str(PYTHON), str(ROOT / "scripts" / "stop_all.py")],
            cwd=str(ROOT), timeout=90, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except Exception as exc:
        log(f"停的时候出错：{exc}")


def ensure_and_open() -> None:
    """打开面板；要是服务已经没了（本体崩了/被关了），先把它们拉回来。"""
    if status() is None:
        log("服务不在，先拉起来")
        if not start_services():
            return
    open_panel()


_OPEN_LOCK = threading.Lock()


def open_once() -> None:
    """连点保护：同一时间只跑一轮「找窗口 / 开窗口」。

    以前每点一下都新起一个线程去开窗口，鼠标连点就是一堆窗口。
    现在第二下开始直接丢掉（日志里留一行，看得到是点了没生效还是没点到）。
    """
    if not _OPEN_LOCK.acquire(blocking=False):
        log("上一轮还在开，这次点击忽略了")
        return
    try:
        ensure_and_open()
    finally:
        _OPEN_LOCK.release()





# ───────────────────────── 主流程 ─────────────────────────

def main() -> int:
    if sys.platform != "win32":
        print("托盘只支持 Windows")
        return 1

    if not take_lock():
        # 抢不到锁 = 已经有一个托盘在跑：只把面板打开就退出，别再起一套服务。
        # 这里必须**同步**做：开窗口可能要等浏览器起来，放后台线程的话
        # 主线程 return 了，守护线程会被直接掐掉 —— 窗口开了一半没人管。
        log("托盘已经在跑，只打开面板")
        open_once()
        return 0

    try:
        import pystray
        from pystray import Menu, MenuItem

        image = make_icon(ICON_FILE)
        state = {"busy": False, "title": "海螺 · 启动中…"}

        def refresh() -> dict | None:
            st = status()
            if st is None:
                title = "海螺 · 未运行（双击重开）"
            elif st.get("connected"):
                title = "海螺 · 运行中（QQ 已连）"
            else:
                title = "海螺 · 运行中（QQ 未连）"
            # 只在真变了的时候刷菜单：每 5 秒重设一遍没必要
            if title != state["title"]:
                state["title"] = title
                try:
                    icon.update_menu()
                except Exception:
                    pass
            return st

        def on_open(_icon=None, _item=None) -> None:
            threading.Thread(target=open_once, daemon=True).start()

        def on_restart(_icon=None, _item=None) -> None:
            if state["busy"]:
                return
            state["busy"] = True

            def work() -> None:
                try:
                    stop_services()
                    time.sleep(1.0)
                    start_services()
                    # 重启就是重启：不替用户开面板（以前这里顺手 open_panel，
                    # 结果每次点「重启海螺」都弹一张面板窗口出来）
                finally:
                    state["busy"] = False
                    refresh()

            threading.Thread(target=work, daemon=True).start()

        def on_quit(_icon=None, _item=None) -> None:
            if state["busy"]:
                return
            state["busy"] = True
            state["title"] = "海螺 · 正在退出…"
            try:
                icon.update_menu()
            except Exception:
                pass

            def work() -> None:
                stop_services()
                # 连托盘自己也收掉：这才叫"完全退出"
                try:
                    LOCK.unlink(missing_ok=True)
                    QUIT_FLAG.unlink(missing_ok=True)
                except OSError:
                    pass
                icon.stop()

            threading.Thread(target=work, daemon=True).start()

        menu = Menu(
            # 状态放在菜单第一行（不可点）：托盘提示文字保持固定的「海螺」，
            # 不然每次改提示，Windows 那边会把旧字符串也留在提示里（会看到两段拼一起）
            MenuItem(lambda _item=None: state["title"], None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("打开面板", on_open, default=True),
            MenuItem("重启海螺", on_restart),
            Menu.SEPARATOR,
            MenuItem("扫码登录 QQ（雪花控制台）", lambda *_: os.startfile("http://127.0.0.1:5099")),  # noqa: S606
            Menu.SEPARATOR,
            MenuItem("退出（全部停止）", on_quit),
        )

        icon = pystray.Icon("conch", image, "海螺", menu)

        def _open_when_ready(limit: float = 75.0) -> None:
            """面板一能应答就把页面打开。

            以前是「等服务全齐（7~10 秒）再开页面」，这几秒里点是没反应的；
            现在页面先弹出来，面板自己会显示加载中。
            """
            t0 = time.time()
            while time.time() - t0 < limit:
                if status() is not None:
                    open_once()
                    return
                time.sleep(0.2)
            log("等面板超时，还是试着打开")

        def boot() -> None:
            # ⚠️ 自定义 setup 必须自己把 visible 打开，否则 pystray 注册完就是「看不见的图标」：
            # 窗口建出来了、Shell_NotifyIcon 也登记了，但通知区域里什么都不显示。
            icon.visible = True
            # 本来就是自己起的（不是别人已经在跑），才顺手开面板
            was_down = status() is None
            if was_down:
                # 边起服务边等面板：一可用就开，不等全部服务
                threading.Thread(target=_open_when_ready, daemon=True).start()
            start_services()
            refresh()
            # 守着：本体要是崩了/被关了，图标还在这儿，就把它拉回来。
            # 这样"图标在 = 服务在"才立得住；只有菜单「退出」才会真的全停。
            misses = 0
            ticks = 0
            while True:
                time.sleep(1.0)
                # 外部喊退出（tray.py --quit）：必须在这里收，不能只停服务 ——
                # 只停服务的话下面这个看门狗 10 秒后又把全家桶拉回来了
                if QUIT_FLAG.exists():
                    log("收到退出请求，全停")
                    try:
                        QUIT_FLAG.unlink(missing_ok=True)
                    except OSError:
                        pass
                    on_quit()
                    return
                ticks += 1
                if ticks % 5:              # 服务每 5 秒探一次就够
                    continue
                st = refresh()
                if st is None:
                    misses += 1
                    if misses >= 2 and not state["busy"]:
                        log("发现服务没了，自动拉起")
                        misses = 0
                        start_services()
                else:
                    misses = 0

        icon.run(setup=lambda _icon: threading.Thread(target=boot, daemon=True).start())
        return 0
    finally:
        try:
            LOCK.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    # `tray.py --quit` 走和菜单「退出」同一条路。
    # 托盘在跑的时候不能自己调 stop_services：那样只停了服务，托盘还在，
    # 它的看门狗过几秒又把全家桶拉起来（以前就是这个毛病）。改成留一张字条，
    # 让托盘自己去收；托盘没在跑才直接停服务。
    if "--quit" in sys.argv:
        if already_running():
            try:
                QUIT_FLAG.parent.mkdir(parents=True, exist_ok=True)
                QUIT_FLAG.write_text("quit", encoding="ascii")
            except OSError as exc:
                log(f"写退出请求失败：{exc}")
            for _ in range(60):
                time.sleep(0.5)
                if not already_running():
                    log("托盘已退出")
                    break
            else:
                log("等不到托盘退出，直接停服务兜底")
                stop_services()
                try:
                    LOCK.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            stop_services()
            try:
                (ROOT / "data" / "tray.lock").unlink(missing_ok=True)
            except OSError:
                pass
        raise SystemExit(0)
    raise SystemExit(main())