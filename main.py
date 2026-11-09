"""程序入口：把存储、总线、插件、接入、面板装配起来。"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # 支持 `python main.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiohttp import web  # noqa: E402

from core import log
from core.backend import OpenAICompatBackend
from core.bus import Bus
from core.config import Config, ConfigError, load_config
from core.loader import Loader
from core.look import Look
from core.message import Message
from core.onebot import OneBotClient
from core import integrity
from core.panel.server import create_app
from core.plugin_ctx import PluginContext
from core.tools import ToolBox
from core.stickers import Stickers
from core.store import Store


async def run(cfg: Config, cfg_path: str = "config.json") -> None:
    log_ = log.get("main")

    store = Store(cfg.db_path)
    store.ensure_schema()
    bus = Bus()
    backend = OpenAICompatBackend(cfg)

    async def on_message(msg: Message) -> None:
        # 先进库再分发：插件没加载（刚重启、被停用）的时候消息也不会凭空消失，
        # 面板能看见「到底收到过什么」，补回机制也有东西可补。
        try:
            store.message_add(
                msg.conv_id,
                "in",
                msg.text,
                sender_id=msg.sender_id,
                sender_name=msg.sender_name,
                message_id=str(msg.message_id),
                plugin_id="host",
            )
        except Exception as exc:
            log_.warning("消息落库失败：%s", exc)
        await bus.emit("message.received", msg)

    onebot = OneBotClient(cfg, on_message)

    tools = ToolBox()
    services = {"backend": backend, "onebot": onebot, "tools": tools}

    def make_ctx(meta) -> PluginContext:
        return PluginContext(
            meta=meta,
            cfg=cfg,
            bus=bus,
            store=store,
            services=services,
        )

    loader = Loader(Path(cfg.plugins_dir), make_ctx, extra_roots=[Path(x) for x in cfg.plugin_paths])
    loader.scan()

    # 表情包是海螺自带的本事（不是插件）：挂到总线上，面板「表情包」页直接用
    # 留影是外面那套程序，海螺只借它看图 —— 同样不是插件
    look = Look(cfg, store, bus)
    look.attach(bus)
    stickers = Stickers(cfg, store, onebot, bus, tools=tools, look=look, services=services)
    stickers.attach(bus)
    services["stickers"] = stickers

    state = {
        "onebot": onebot,
        "loader": loader,
        "store": store,
        "backend": backend,
        "stickers": stickers,
        "look": look,
        "tools": tools,
        "services": services,   # 插件往这里挂本事：主动开口 = services["proactive"]
        "started_at": time.time(),
    }
    app = create_app(cfg=cfg, state=state, cfg_path=cfg_path)
    runner = web.AppRunner(app, access_log=None)  # 面板 3 秒轮询一次，别刷屏
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cfg.panel.port)
    await site.start()


    # 面板先开门（页面能立刻打能），插件再慢慢加载：
    # 反过来的话用户要干等 4~5 秒才看得到界面。
    await loader.enable_all()
    # 配置里点过名的插件开机就启用（面板上的开关记在这里）
    for pid in (cfg.raw.get("plugins_enabled") or []):
        pid = str(pid)
        meta = loader.metas.get(pid)
        if meta is None or meta.kind == "app":
            # 独立应用（汐、微光域、留影…）不是宿主插件，开关在启动器那边
            continue
        if loader.status.get(pid) != "enabled":
            try:
                await loader.enable(pid)
            except Exception as exc:
                log_.error("插件 %s 启用失败：%s", pid, exc)

    level = integrity.current()
    if level in ("Low", "Untrusted"):
        log_.warning("完整性 %s：%s", level, integrity.warning(level))
    else:
        log_.info("完整性：%s", level)

    await stickers.start()
    await onebot.start()
    log_.info("QQ 接入已启动：%s", cfg.onebot.ws_url)
    log_.info("面板：http://127.0.0.1:%s/?token=%s", cfg.panel.port, cfg.panel.token)

    # 定时把 WAL 合回主库：不然直接打开 海螺.db 看不到最近的记录
    # （WAL 模式下写入先进 -wal 文件，只有 checkpoint 才回主库）
    async def _checkpoint_loop() -> None:
        while True:
            await asyncio.sleep(15)
            await asyncio.to_thread(store.checkpoint)

    checkpoint_task = asyncio.create_task(_checkpoint_loop())

    try:
        await asyncio.Event().wait()
    finally:
        checkpoint_task.cancel()
        await stickers.close()
        await onebot.stop()
        await runner.cleanup()
        store.close()


def _port_in_use(port: int) -> bool:
    """面板端口有没有人在听（= 本体已经在跑）。"""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def _take_host_lock(cfg_path: str) -> bool:
    """单实例的硬闸：原子地占住 data/host.lock。

    端口检查有个时间窗：两个进程几乎同时起来时，谁也还没绑定完成，
    于是两个都通过、两个都连 QQ，互相踢线 —— 表现就是"消息进来了她不回"。
    这里用 O_EXCL 先占位，抢不到的直接退出。
    """
    lock = Path(cfg_path).resolve().parent / "data" / "host.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                pid = int(lock.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _alive(pid):
                return False
            # 上一任已经没了（崩溃/被强杀）：清掉再来一次
            lock.unlink(missing_ok=True)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="海螺", description="海螺插件宿主")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    log.setup(args.log_level)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if not cfg.panel.token:
        cfg.panel.token = secrets.token_urlsafe(24)
    if not cfg.owner_qq:
        log.get("main").warning("没有配置 owner_qq：管理命令和 owner 私聊放行都不可用")

    # 单实例（硬闸 + 端口复核）：两个本体同时连 OneBot 会互相踢线。
    if not _take_host_lock(args.config):
        log.get("main").warning("已经有一个海螺本体在跑，这个进程直接退出（避免抢 QQ 连接）")
        return 0
    if _port_in_use(int(cfg.panel.port)):
        log.get("main").warning(
            "面板端口 %s 已经在用了：本体已经在跑，这个进程直接退出（避免两个实例抢 QQ 连接）",
            cfg.panel.port,
        )
        return 0

    try:
        asyncio.run(run(cfg, args.config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())