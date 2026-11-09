"""聊天看图：同一张图当天只认一次（复用描述、不再占额度）。"""

import asyncio
import types

import pytest

from core.look import Look
from core.store import Store


@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "t.db")
    store.ensure_schema()
    cfg = types.SimpleNamespace(raw={"look": {"enabled": True, "max_images": 2, "max_images_per_day": 10}})

    class FakeLook(Look):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.calls = 0

        async def see(self, target):
            self.calls += 1
            return {"desc": "一只猫躺着", "points": ["摆烂"], "model": "glm-4v-flash"}

    lk = FakeLook(cfg, store)
    return types.SimpleNamespace(store=store, cfg=cfg, look=lk)


def _url(fileid="ABC123"):
    return f"https://multimedia.nt.qq.com.cn/download?appid=1407&fileid={fileid}&rkey=xyz"


def test_same_image_only_looked_once_per_day(env):
    first = asyncio.run(env.look.look(_url()))
    second = asyncio.run(env.look.look(_url()))  # rkey 变了也无所谓，fileid 一样
    assert "一只猫躺着" in first
    assert second == first
    assert env.look.calls == 1


def test_different_images_both_looked(env):
    asyncio.run(env.look.look(_url("AAA")))
    asyncio.run(env.look.look(_url("BBB")))
    assert env.look.calls == 2


def test_cached_look_does_not_burn_quota(env):
    day = __import__("time").strftime("%Y-%m-%d")
    asyncio.run(env.look.look(_url()))
    assert env.store.counter_get("look_day", day) == 1
    asyncio.run(env.look.look(_url()))
    assert env.store.counter_get("look_day", day) == 1


def test_no_fileid_falls_back_to_url_hash(env):
    a = "https://example.com/x.png"
    asyncio.run(env.look.look(a))
    asyncio.run(env.look.look(a + "#again"))
    assert env.look.calls == 2
    asyncio.run(env.look.look(a))
    assert env.look.calls == 2


def test_old_cache_rows_cleaned(env):
    import time

    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    env.store.look_cache_put("f:OLD", yesterday, "旧的一句")
    asyncio.run(env.look.look(_url("NEW1")))
    assert env.store.look_cache_get("f:OLD", yesterday) == ""


def test_custom_command_plugin_works(tmp_path):
    """别人写的视觉插件：只要吐 JSON，海螺就能用。"""
    import json
    import sys

    script = tmp_path / "my_vision.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'desc': '自定义插件看到的猫', 'tags': '猫、可爱'}))\n",
        encoding="utf-8",
    )
    store = Store(tmp_path / "t.db")
    store.ensure_schema()
    cfg = types.SimpleNamespace(
        raw={"look": {"engine": "cmd", "cmd": f'"{sys.executable}" "{script}"'}}
    )
    lk = Look(cfg, store)
    got = asyncio.run(lk.see_sticker("any.png"))
    assert got["desc"] == "自定义插件看到的猫"
    assert got["tags"] == ["猫", "可爱"]


def test_auto_prefers_custom_command_when_no_liuying(tmp_path):
    import sys

    script = tmp_path / "v.py"
    script.write_text("print('{\"desc\":\"auto 选到我\"}')", encoding="utf-8")
    store = Store(tmp_path / "t.db")
    store.ensure_schema()
    cfg = types.SimpleNamespace(
        raw={"look": {"engine": "auto", "cmd": f'"{sys.executable}" "{script}"', "eye_root": ""}}
    )
    lk = Look(cfg, store)
    assert asyncio.run(lk.see("x"))["desc"] == "auto 选到我"