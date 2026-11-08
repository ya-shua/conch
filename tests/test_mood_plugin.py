"""情绪插件：只当电话线。Tide 说什么它就转什么，Tide 不在就静默。

验证四件事：
    speech.prompt 会把"你现在的心情"拼进去（陈述句，不是命令）
    睡觉时段也只是告诉她"你被吵醒了"，不替她拒绝
    speech.gate 永远不 veto——Tide 没有否决权，只回记一句"她说了"
    面板连不上时什么都不加，聊天照常
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging

import pytest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

def _find_mood() -> Path:
    """情绪插件是外部程序：先看旁边的 tide，再看本机 config 里 plugin_paths 指到哪。"""
    root = Path(__file__).resolve().parents[1]
    cands = [root / "tide" / "plugins" / "mood" / "main.py"]
    cfg = root / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
            for raw in data.get("plugin_paths") or []:
                cands.append(Path(raw) / "mood" / "main.py")
        except Exception:
            pass
    return next((c for c in cands if c.exists()), cands[0])


PLUGIN = _find_mood()

pytestmark = pytest.mark.skipif(not PLUGIN.exists(), reason="情绪插件不在本机")


class StubPanel:
    def __init__(self, state: dict):
        self.state = state
        self.observed: list[dict] = []
        self.woke = 0

    def serve(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj):
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.startswith("/api/state"):
                    self._json(outer.state)
                elif self.path == "/api/health":
                    self._json({"ok": True})
                else:
                    self._json({})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/observe":
                    outer.observed.append(body)
                elif self.path == "/api/woke":
                    outer.woke += 1
                self._json({"ok": True})

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd


class FakeCtx:
    def __init__(self, panel_url: str, cfg: dict | None = None):
        self.config = {"enabled": True, "panel_url": panel_url}
        self.cfg = type("Cfg", (), {"owner_qq": "10000"})()
        self.services: dict = {}
        self.handlers: dict = {}
        self.log = logging.getLogger("test.mood")

    def on(self, event, handler, priority=100):
        self.handlers[event] = handler


def load_plugin():
    spec = importlib.util.spec_from_file_location("mood_plugin_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


STATE = {
    "snapshot": {
        "feelings": "心里有点闷，想他。",
        "quiet": False,
        "wake": {"wake": False, "why": "还不到想找你的程度"},
        "emotion": {"accounts": {"calm": 100}},
    },
    "plan": {"prompt": "你现在的心情：心里有点闷，想他。\n需求：想你 40"},
}


def run(coro):
    return asyncio.run(coro)


def test_prompt_carries_feelings():
    panel = StubPanel(STATE)
    httpd = panel.serve()
    try:
        ctx = FakeCtx(f"http://127.0.0.1:{httpd.server_address[1]}")
        mod = load_plugin()
        run(mod.setup(ctx))
        payload = {"conv_id": "private:10000", "sender_id": "10000", "extra": "", "text": "在吗"}
        run(mod.prompt_hook(ctx, payload))
        assert "心里有点闷" in payload["extra"]
        assert payload["tide"]["feelings"].startswith("心里")
    finally:
        httpd.shutdown()


def test_gate_never_blocks_even_when_she_does_not_want_to_speak():
    """Tide 是身体不是老板：想不想说话由她/海螺定，插件不许拦。"""
    panel = StubPanel(STATE)
    httpd = panel.serve()
    try:
        ctx = FakeCtx(f"http://127.0.0.1:{httpd.server_address[1]}")
        mod = load_plugin()
        run(mod.setup(ctx))
        payload = {"conv_id": "private:10000", "feature": "proactive",
                   "text": "在吗", "allow": True}
        run(mod.gate_hook(ctx, payload))
        assert payload["allow"] is True
        assert panel.observed and panel.observed[0]["speaker"] == "self"
    finally:
        httpd.shutdown()


def test_gate_allows_and_reports_her_own_words():
    state = json.loads(json.dumps(STATE))
    state["snapshot"]["wake"] = {"wake": True, "why": "想你 88"}
    panel = StubPanel(state)
    httpd = panel.serve()
    try:
        ctx = FakeCtx(f"http://127.0.0.1:{httpd.server_address[1]}")
        mod = load_plugin()
        run(mod.setup(ctx))
        payload = {"conv_id": "private:10000", "feature": "proactive", "text": "在吗", "allow": True}
        run(mod.gate_hook(ctx, payload))
        assert payload["allow"] is True
        assert panel.observed and panel.observed[0]["speaker"] == "self"
        assert panel.observed[0]["initiated"] is True
        assert panel.woke == 1
    finally:
        httpd.shutdown()


def test_quiet_is_told_as_a_fact_not_a_block():
    state = json.loads(json.dumps(STATE))
    state["snapshot"]["quiet"] = True
    panel = StubPanel(state)
    httpd = panel.serve()
    try:
        ctx = FakeCtx(f"http://127.0.0.1:{httpd.server_address[1]}")
        mod = load_plugin()
        run(mod.setup(ctx))
        payload = {"conv_id": "private:10000", "extra": "", "text": "睡了吗"}
        run(mod.prompt_hook(ctx, payload))
        assert "睡了" in payload["extra"]
        gate = {"conv_id": "private:10000", "feature": "chat", "text": "没睡", "allow": True}
        run(mod.gate_hook(ctx, gate))
        assert gate["allow"] is True
    finally:
        httpd.shutdown()


def test_dead_panel_stays_silent():
    ctx = FakeCtx("http://127.0.0.1:9")   # 没人听
    mod = load_plugin()
    run(mod.setup(ctx))
    payload = {"conv_id": "private:10000", "extra": "", "text": "在吗"}
    run(mod.prompt_hook(ctx, payload))
    assert payload["extra"] == ""
    gate = {"conv_id": "private:10000", "feature": "chat", "text": "在吗", "allow": True}
    run(mod.gate_hook(ctx, gate))
    assert gate["allow"] is True
