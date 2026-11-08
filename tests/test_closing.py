"""收尾话判定：主人说"晚安/去忙了"这类，她就不回。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from _paths import conch_main

PLUGIN = conch_main()


class Cfg:
    closing_enabled = True
    closing_words = ["晚安", "我睡了", "睡了", "去忙了", "拜拜", "先这样"]


class Ctx:
    cfg = Cfg()


@pytest.fixture(scope="module")
def plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_closing", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plain_closing(plugin):
    assert plugin.is_closing(Ctx(), "晚安") is True
    assert plugin.is_closing(Ctx(), "去忙了") is True


def test_closing_with_tail_particle(plugin):
    assert plugin.is_closing(Ctx(), "晚安啦") is True
    assert plugin.is_closing(Ctx(), "我睡了哈") is True
    assert plugin.is_closing(Ctx(), "拜拜～") is True


def test_question_is_not_closing(plugin):
    assert plugin.is_closing(Ctx(), "睡了？") is False
    assert plugin.is_closing(Ctx(), "晚安？") is False


def test_long_sentence_with_closing_word_is_not_closing(plugin):
    assert plugin.is_closing(Ctx(), "今晚早点睡啊，晚安") is False
    assert plugin.is_closing(Ctx(), "我睡了明天还要早起上班") is False


def test_ordinary_message_is_not_closing(plugin):
    assert plugin.is_closing(Ctx(), "在吗") is False
    assert plugin.is_closing(Ctx(), "好") is False


def test_can_be_switched_off(plugin):
    class Off(Cfg):
        closing_enabled = False

    class OffCtx:
        cfg = Off()

    assert plugin.is_closing(OffCtx(), "晚安") is False

def _msg(plugin, conv: str, text: str, at_me: bool = False):
    return plugin.Message(
        conv_id=conv, kind=conv.split(":")[0], sender_id="1", sender_name="主人",
        message_id="1", segments=plugin.make_text(text), ts=0, at_me=at_me,
    )


def test_private_closing_stays_quiet(plugin):
    batch = [_msg(plugin, "private:1", "晚安")]
    assert plugin.should_stay_quiet(Ctx(), batch) is True


def test_group_closing_without_at_replies(plugin):
    batch = [_msg(plugin, "group:2", "晚安")]
    assert plugin.should_stay_quiet(Ctx(), batch) is True


def test_group_closing_with_at_still_replies(plugin):
    batch = [_msg(plugin, "group:2", "晚安", at_me=True)]
    assert plugin.should_stay_quiet(Ctx(), batch) is False


def test_ordinary_private_message_replies(plugin):
    batch = [_msg(plugin, "private:1", "在吗")]
    assert plugin.should_stay_quiet(Ctx(), batch) is False
