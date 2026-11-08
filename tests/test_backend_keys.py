"""用一个假的 OpenAI 端点证明轮换逻辑：第 1 把 401、第 2 把 200。"""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from aiohttp import web
from types import SimpleNamespace

hits = []

async def handle(request):
    auth = request.headers.get("Authorization", "")
    key = auth.replace("Bearer ", "")
    hits.append(key)
    if key.startswith("bad"):
        return web.json_response({"error": {"message": "invalid api key"}}, status=401)
    if key.startswith("poor"):
        return web.json_response({"error": {"message": "insufficient balance"}}, status=429)
    return web.json_response({
        "choices": [{"message": {"content": "在的"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    })

async def main():
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8899).start()

    from core.backend import OpenAICompatBackend, BackendError
    from core.config import ModelConfig

    def mk(keys):
        cfg = SimpleNamespace(model=ModelConfig(
            base_url="http://127.0.0.1:8899/v1", model="x", api_key="", keys=keys))
        return OpenAICompatBackend(cfg)

    print("=== 场景 1：第 1 把 401，第 2 把正常 ===")
    b = mk([{"key": "bad-key", "label": "坏"}, {"key": "good-key", "label": "好"}])
    text, usage = await b.chat([{"role": "user", "content": "在吗"}])
    print("  回复:", text, "| usage:", usage, "| 打过的 Key:", hits[-2:], "| 现在用第", b.current_index(), "把")

    print("=== 场景 2：第 1 把欠费（429 insufficient），第 2 把正常 ===")
    hits.clear()
    b2 = mk([{"key": "poor-key", "label": "穷"}, {"key": "good-key", "label": "好"}])
    text, _ = await b2.chat([{"role": "user", "content": "在吗"}])
    print("  回复:", text, "| 打过的 Key:", hits, "| 现在用第", b2.current_index(), "把")

    print("=== 场景 3：两把都坏 → 报错，不装死 ===")
    b3 = mk([{"key": "bad-key", "label": "坏"}, {"key": "bad-key2", "label": "也坏"}])
    try:
        await b3.chat([{"role": "user", "content": "在吗"}])
        print("  ✗ 居然成功了（不对）")
    except BackendError as exc:
        print("  报错（符合预期）:", str(exc)[:80])

    print("=== 场景 4：只配了一把（老配置） ===")
    b4 = mk([])
    b4.cfg.model.api_key = "good-key"  # privacy-ok 测试用假 key
    text, _ = await b4.chat([{"role": "user", "content": "在吗"}])
    print("  回复:", text)

    await runner.cleanup()

asyncio.run(main())
