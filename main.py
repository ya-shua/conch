"""程序入口：把存储、总线、插件、接入、面板装配起来。"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # 支持 `python qqbridge/main.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402

from qqbridge.core import log
from qqbridge.core.backend import OpenAICompatBackend
from qqbridge.core.bus import Bus
from qqbridge.core.config import Config, ConfigError, load_config
from qqbridge.core.loader import Loader
from qqbridge.core.message import Message
from qqbridge.core.onebot import OneBotClient
from qqbridge.core.panel.server import create_app
from qqbridge.core.plugin_ctx import PluginContext
from qqbridge.core.store import Store


async def run(cfg: Config, cfg_path: str = "config.json") -> None:
    log_ = log.get("main")

    store = Store(cfg.db_path)
    store.ensure_schema()
    bus = Bus()
    backend = OpenAICompatBackend(cfg)

    async def on_message(msg: Message) -> None:
        await bus.emit("message.received", msg)

    onebot = OneBotClient(cfg, on_message)

    def make_ctx(meta) -> PluginContext:
        return PluginContext(
            meta=meta,
            cfg=cfg,
            bus=bus,
            store=store,
            services={"backend": backend, "onebot": onebot},
        )

    loader = Loader(Path(cfg.plugins_dir), make_ctx)
    loader.scan()
    await loader.enable_all()
    # 配置里点过名的插件开机就启用（面板上的开关记在这里）
    for pid in (cfg.raw.get("plugins_enabled") or []):
        pid = str(pid)
        if pid in loader.metas and loader.status.get(pid) != "enabled":
            try:
                await loader.enable(pid)
            except Exception as exc:
                log_.error("插件 %s 启用失败：%s", pid, exc)

    state = {
        "onebot": onebot,
        "loader": loader,
        "store": store,
        "backend": backend,
        "started_at": time.time(),
    }
    app = create_app(cfg=cfg, state=state, cfg_path=cfg_path)
    runner = web.AppRunner(app, access_log=None)  # 面板 3 秒轮询一次，别刷屏
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cfg.panel.port)
    await site.start()

    await onebot.start()
    log_.info("QQ 接入已启动：%s", cfg.onebot.ws_url)
    log_.info("面板：http://127.0.0.1:%s/?token=%s", cfg.panel.port, cfg.panel.token)

    try:
        await asyncio.Event().wait()
    finally:
        await onebot.stop()
        await runner.cleanup()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qqbridge", description="海螺插件宿主")
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

    try:
        asyncio.run(run(cfg, args.config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())