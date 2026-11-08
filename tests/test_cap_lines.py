"""一轮最多几条：超出的并进最后一条，表情包行不吞。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from _paths import conch_main

PLUGIN = conch_main()


@pytest.fixture(scope="module")
def plugin():
    spec = importlib.util.spec_from_file_location("conch_plugin_main_cap", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_under_limit_is_untouched(plugin):
    lines = ["一", "二", "三"]
    assert plugin.cap_lines(lines, 4) == lines


def test_limit_zero_means_no_limit(plugin):
    lines = ["一", "二", "三", "四", "五"]
    assert plugin.cap_lines(lines, 0) == lines


def test_extra_lines_merge_into_last(plugin):
    lines = ["一", "二", "三", "四", "五", "六"]
    got = plugin.cap_lines(lines, 3)
    assert got == ["一", "二", "三 四 五 六"]


def test_sticker_line_is_not_swallowed(plugin):
    lines = ["一", "二", "三", "【表情包：猫猫】", "四"]
    got = plugin.cap_lines(lines, 2)
    assert "【表情包：猫猫】" in got
    assert len([x for x in got if not x.startswith("【")]) == 2