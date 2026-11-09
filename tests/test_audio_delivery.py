"""语音产出：认 AUDIO 行 → 海螺自己发 → 把真话还给她。

这条链以前是断的：声来返回路径 + 一句「把路径发给用户即可播放」，模型读到就以为
给过了；就算她老实抄路径，抄错一个字符 send_voice 就静默跳过，而她的文字已经说出去。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging

import pytest

from _paths import conch_main

PLUGIN = conch_main()

# 声来真实产出的文件名：带空格、带 ·、带中文 —— 旧的"正则猜路径"就是死在这上面
REAL = r"D:\AI\声音工坊\outputs\龙婉·小米 MiMo TTS_20261110_004015.wav"
REAL_RESULT = (
    "✅ 说话完成（音色「龙婉」）\n"
    f"文件: {REAL}\n"
    f"AUDIO: {REAL}\n"
    "大小 225 KB · 格式 wav"
)


def load_plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_main", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self) -> None:
        self.log = logging.getLogger("test.audio")
        self.services: dict = {}


@pytest.fixture()
def plugin(monkeypatch):
    mod = load_plugin()
    state = {"ok": True, "sent": []}

    async def fake_send_voice(ctx, conv_id, path):
        state["sent"].append((conv_id, path))
        return state["ok"]

    monkeypatch.setattr(mod, "send_voice", fake_send_voice)
    mod._state = state
    return mod


# ── 取路径 ──────────────────────────────────────────────


def test_audio_line_wins(plugin):
    assert plugin.audio_path_of(REAL_RESULT) == REAL


def test_file_line_is_the_fallback(plugin):
    only_file = "✅ 完成\n文件: " + REAL + "\n大小 225 KB"
    assert plugin.audio_path_of(only_file) == REAL


def test_fullwidth_colon_and_chinese_label(plugin):
    assert plugin.audio_path_of("文件：" + REAL) == REAL


def test_no_audio_returns_empty(plugin):
    assert plugin.audio_path_of("✅ 完成，但只有文字") == ""
    assert plugin.audio_path_of("") == ""


def test_non_audio_extension_rejected(plugin):
    assert plugin.audio_path_of("AUDIO: D:\\a\\b.txt") == ""


def test_quoted_path_is_unquoted(plugin):
    assert plugin.audio_path_of(f'AUDIO: "{REAL}"') == REAL


# ── 心情转参数 ──────────────────────────────────────────


def test_mood_becomes_rate_pitch_style(plugin):
    token = plugin._CURRENT_VOICE.set({"speed": 1.06, "pitch": 2, "instruct": "语速偏快，句尾上扬"})
    try:
        got = plugin.mood_voice_args({"text": "想你了"})
    finally:
        plugin._CURRENT_VOICE.reset(token)
    assert got["rate"] == 1.06
    assert got["pitch"] == 1.122  # 半音 +2 → 2^(2/12)，留三位
    assert got["style"] == "语速偏快，句尾上扬"
    assert got["text"] == "想你了"


def test_self_given_values_are_not_overwritten(plugin):
    token = plugin._CURRENT_VOICE.set({"speed": 1.06, "pitch": 2, "instruct": "x"})
    try:
        got = plugin.mood_voice_args({"text": "t", "rate": 0.9, "pitch": 1.0, "style": "我自己来"})
    finally:
        plugin._CURRENT_VOICE.reset(token)
    assert got == {"text": "t", "rate": 0.9, "pitch": 1.0, "style": "我自己来"}


def test_emotion_beats_style(plugin):
    """她自己给了 emotion 就别再塞 style：声来里 emotion 会盖掉 style，白传。"""
    token = plugin._CURRENT_VOICE.set({"speed": 1.0, "pitch": 0, "instruct": "语气描述"})
    try:
        got = plugin.mood_voice_args({"text": "t", "emotion": "撒娇"})
    finally:
        plugin._CURRENT_VOICE.reset(token)
    assert "style" not in got and got["emotion"] == "撒娇"


def test_without_mood_nothing_added(plugin):
    token = plugin._CURRENT_VOICE.set({})
    try:
        assert plugin.mood_voice_args({"text": "t"}) == {"text": "t"}
    finally:
        plugin._CURRENT_VOICE.reset(token)


# ── 发送与回报 ──────────────────────────────────────────


def run_deliver(plugin, result: str, conv: str = "private:1"):
    ctx = FakeCtx()
    token = plugin._CURRENT_CONV.set(conv)
    try:
        out = asyncio.run(plugin.deliver_audio(ctx, result))
    finally:
        plugin._CURRENT_CONV.reset(token)
    return out


def test_voice_is_sent_and_she_is_told_it_worked(plugin):
    out = run_deliver(plugin, REAL_RESULT)
    assert plugin._state["sent"] == [("private:1", REAL)]
    assert "已经把它发成" in out
    assert REAL not in out  # 路径不回到她嘴里，免得她再抄一遍


def test_send_failure_is_told_honestly(plugin):
    plugin._state["ok"] = False
    out = run_deliver(plugin, REAL_RESULT)
    assert "失败" in out and "别跟人说你发了" in out


def test_no_audio_keeps_result_as_is(plugin):
    text = "✅ 完成，但只有文字"
    assert run_deliver(plugin, text) == text
    assert plugin._state["sent"] == []


def test_unknown_conv_refuses_to_guess(plugin):
    out = run_deliver(plugin, REAL_RESULT, conv="")
    assert "不知道该把这条语音发给谁" in out
    assert plugin._state["sent"] == []