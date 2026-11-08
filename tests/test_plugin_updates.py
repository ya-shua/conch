"""插件版本检查：只读、带缓存、不碰任何插件文件。"""

from __future__ import annotations

import asyncio
import json

import pytest

from core import updater


class Meta:
    def __init__(self, version="1.0.0", source=None):
        self.version = version
        self.source = source


def test_source_parsing():
    assert updater._plugin_source("ya-shua/tide") == ("ya-shua/tide", "release")
    assert updater._plugin_source({"repo": "ya-shua/tide"}) == ("ya-shua/tide", "release")
    assert updater._plugin_source({"repo": "ya-shua/tide", "check": "tag"}) == ("ya-shua/tide", "tag")
    assert updater._plugin_source("") == ("", "release")
    assert updater._plugin_source(None) == ("", "release")


def test_compare_tags():
    assert updater.compare("1.0.0", "1.2.0")
    assert not updater.compare("1.2.0", "1.2.0")
    assert not updater.compare("1.10.0", "1.9.0")


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "project_root", lambda: tmp_path)
    updater._write_plugin_updates({"fetched_at": 123, "items": {"tide": {"latest": "v1.2.0"}}})
    got = updater.read_plugin_updates()
    assert got["items"]["tide"]["latest"] == "v1.2.0"
    assert json.loads((tmp_path / "data" / "plugin_updates.json").read_text(encoding="utf-8"))["fetched_at"] == 123


def test_cache_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "project_root", lambda: tmp_path)
    assert updater.read_plugin_updates() == {}


def test_check_plugins_flags_update_by_version(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "project_root", lambda: tmp_path)

    async def fake_latest(session, repo, check):
        return {"ya-shua/tide": "v1.2.0", "ya-shua/meme-library": "v1.0.0"}[repo], ""

    monkeypatch.setattr(updater, "_latest_of", fake_latest)
    metas = {
        "tide": Meta("0.1.0", {"repo": "ya-shua/tide"}),
        "meme-library": Meta("1.1.0", {"repo": "ya-shua/meme-library"}),
        "local-only": Meta("3.0.0", None),
    }
    items = asyncio.run(updater.check_plugins(metas, force=True))
    assert items["tide"]["has_update"] is True
    assert items["meme-library"]["has_update"] is False
    assert "local-only" not in items  # 没声明 source 的插件不打扰


def test_check_plugins_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "project_root", lambda: tmp_path)
    calls = []

    async def fake_latest(session, repo, check):
        calls.append(repo)
        return "v9.9.9", ""

    monkeypatch.setattr(updater, "_latest_of", fake_latest)
    metas = {"tide": Meta("0.1.0", "ya-shua/tide")}
    asyncio.run(updater.check_plugins(metas, force=True))
    first = len(calls)
    asyncio.run(updater.check_plugins(metas))  # 缓存里还新鲜
    assert len(calls) == first