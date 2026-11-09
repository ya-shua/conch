"""主动开口：别人说「她想找他说句话」，海螺替她用自己的嘴说。

要点（这三点写错就会出怪事）：
1. 没开 proactive.enabled → 端点直接拒绝，不能偷偷发
2. 说的那句走她平时的链路（人设 + 心情 + 闸），不是模板句
3. **失败要如实说失败**：模型没说话 / 被闸拦 / 发送报错，都要 sent=False，
   不然 Tide 会以为她说过这句话、在等一个根本没发出去的回音
"""
from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace

import pytest

from _paths import conch_main

PLUGIN = conch_main()


@pytest.fixture(scope="module")
def plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_proactive", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Cfg:
    allow = SimpleNamespace(
        groups=[], private=[], deny_groups=[], deny_private=[],
        allow_all_when_empty=True,
    )
    owner_ids = ["10000"]
    strategy = {}
    model = SimpleNamespace(model="test-model")
    admins = []


class Store:
    def __init__(self):
        self.out = []

    def message_add(self, *a, **kw):
        self.out.append((a, kw))

    def message_list(self, *a, **kw):
        return []


class Log:
    def __getattr__(self, _name):
        return lambda *a, **kw: None


class Ctx:
    def __init__(self):
        self.cfg = Cfg()
        self.store = Store()
        self.log = Log()
        self.services = {}
        self.id = "conch"
        self.ledger = SimpleNamespace(record=lambda **kw: kw)


@pytest.fixture()
def wired(plugin, monkeypatch):
    """把链路里跟网络/模型有关的部分换成假的，只看编排对不对。"""
    ctx = Ctx()
    state = {"reply": "在吗", "talk_exc": None, "gate": (True, ""), "send_exc": None}

    monkeypatch.setattr(plugin, "load_history", lambda *a, **kw: [])
    monkeypatch.setattr(plugin, "system_prompt", lambda c: "你是她")
    monkeypatch.setattr(plugin, "lexicon_block", lambda c: "")
    monkeypatch.setattr(plugin, "voice_hint", lambda c: "")
    monkeypatch.setattr(plugin, "call_hint", lambda c, m: "")

    async def fake_extra(ctx_, conv, text, **kw):
        return "\n\n心情：想你"

    async def fake_talk(ctx_, messages, scope, *a, **kw):
        if state["talk_exc"]:
            raise state["talk_exc"]
        return state["reply"], {"prompt_tokens": 10, "completion_tokens": 5}

    async def fake_gate(ctx_, conv, text, **kw):
        return state["gate"]

    async def fake_send(ctx_, conv, text):
        if state["send_exc"]:
            raise state["send_exc"]

    async def fake_remember(*a, **kw):
        return None

    monkeypatch.setattr(plugin, "ask_prompt_extra", fake_extra)
    monkeypatch.setattr(plugin, "talk", fake_talk)
    monkeypatch.setattr(plugin, "ask_gate", fake_gate)
    monkeypatch.setattr(plugin, "send_lines", fake_send)
    monkeypatch.setattr(plugin, "remember_own", fake_remember)
    return ctx, state


def run(plugin, coro):
    return asyncio.run(coro)


def test_says_something(plugin, wired):
    ctx, state = wired
    out = run(plugin, plugin.speak_proactive(ctx, {"conv_id": "private:10000", "why": "想你 91"}))
    assert out["ok"] is True and out["sent"] is True
    assert out["text"] == "在吗"
    assert ctx.store.out, "发出去的话要写进聊天记录"


def test_no_conv_id(plugin, wired):
    ctx, _ = wired
    out = run(plugin, plugin.speak_proactive(ctx, {}))
    assert out == {"ok": False, "sent": False, "error": "没给 conv_id"}


def test_model_silent_is_not_sent(plugin, wired):
    ctx, state = wired
    state["reply"] = ""
    out = run(plugin, plugin.speak_proactive(ctx, {"conv_id": "private:10000"}))
    assert out["sent"] is False
    assert "没说话" in out["error"]


def test_model_error_is_not_sent(plugin, wired):
    ctx, state = wired
    state["talk_exc"] = RuntimeError("上游 500")
    out = run(plugin, plugin.speak_proactive(ctx, {"conv_id": "private:10000"}))
    assert out["sent"] is False
    assert "500" in out["error"]


def test_gate_denied_is_not_sent(plugin, wired):
    ctx, state = wired
    state["gate"] = (False, "别的插件拦了")
    out = run(plugin, plugin.speak_proactive(ctx, {"conv_id": "private:10000"}))
    assert out["sent"] is False
    assert "拦" in out["error"]


def test_send_error_is_not_sent(plugin, wired):
    ctx, state = wired
    state["send_exc"] = RuntimeError("OneBot 断线")
    out = run(plugin, plugin.speak_proactive(ctx, {"conv_id": "private:10000"}))
    assert out["sent"] is False
    assert "发送失败" in out["error"]
    assert not ctx.store.out


def test_config_defaults_off(tmp_path):
    """没写 proactive 就是关着 —— 她主动找人这件事不能靠默认值自己生效。"""
    import json

    from core.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"onebot": {"ws_url": "ws://x"}, "allow": {}}),
                        encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.proactive["enabled"] is False
    assert cfg.proactive["conv_id"] == ""


def test_config_reads_block(tmp_path):
    import json

    from core.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "onebot": {"ws_url": "ws://x"},
        "allow": {},
        "proactive": {"enabled": True, "conv_id": "private:1",
                      "report_url": "http://127.0.0.1:7880/api/proactive-report"},
    }), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.proactive["enabled"] is True
    assert cfg.proactive["conv_id"] == "private:1"
    assert cfg.proactive["report_url"].endswith("/api/proactive-report")