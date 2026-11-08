"""程序入口：把存储、总线、插件、接入、面板装配起来。"""

from __future__ import annotations

import argparse
import asyncio
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
    stickers = Stickers(cfg, store, onebot, bus)
    stickers.attach(bus)
    services["stickers"] = stickers
    # 留影是外面那套程序，海螺只借它看图 —— 同样不是插件
    look = Look(cfg, bus)
    look.attach(bus)

    state = {
        "onebot": onebot,
        "loader": loader,
        "store": store,
        "backend": backend,
        "stickers": stickers,
        "look": look,
        "tools": tools,
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
        if pid in loader.metas and loader.status.get(pid) != "enabled":
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

    # 单实例：面板端口已经有人听了，说明本体已经在跑。
    # 两个本体同时连 OneBot 会互相踢线，表现就是"消息进来了她不回"。
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