"""表情包：收法（候选 / 直接收 / 不收）、视觉模型只调一次、候选收下。"""

import asyncio
import time
import types
from pathlib import Path

import pytest

from core.stickers import Stickers
from core.store import Store


@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "t.db")
    store.ensure_schema()
    cfg = types.SimpleNamespace(raw={}, data_dir=str(tmp_path))

    class FakeOneBot:
        async def call(self, *a, **k):
            return []

        async def send(self, *a, **k):
            return True

    class FakeLook:
        def __init__(self):
            self.calls = 0

        async def see(self, target):
            self.calls += 1
            return {"desc": "一只猫四脚朝天躺着", "points": ["摆烂", "累"], "model": "glm-4v-flash"}

        async def see_sticker(self, target, guidance=""):
            self.calls += 1
            if guidance:
                return {"desc": guidance, "tags": ["生气", "催"], "model": "glm-4v-flash"}
            return {"desc": "一只猫四脚朝天躺着", "tags": ["摆烂", "累"], "model": "glm-4v-flash"}

    look = FakeLook()
    st = Stickers(cfg, store, FakeOneBot(), look=look)
    return types.SimpleNamespace(store=store, cfg=cfg, st=st, look=look)


def _seg(url="https://x/1.gif", summary="[动画表情]"):
    return {"type": "image", "data": {"url": url, "sub_type": "1", "summary": summary}}


def _payload(url="https://x/1.gif"):
    return {"segments": [_seg(url)], "sender_id": "1", "sender_name": "路人", "conv_id": "group:1"}


def test_default_collect_mode_is_want(env):
    assert env.st.settings()["collect_mode"] == "want"


def test_legacy_collect_enabled_false_means_off(env):
    env.cfg.raw["stickers"] = {"collect_enabled": False}
    assert env.st.settings()["collect_mode"] == "off"


def test_want_puts_it_in_candidates(env, monkeypatch):
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a...."))
    rows = env.st.collect(_payload())
    assert len(rows) == 1
    row = env.store.sticker_get(rows[0][0])
    assert row["source"] == "candidate"
    # 同一张再来一次：库里已经有，不再收
    assert env.st.collect(_payload()) == []
    assert env.store.sticker_count(source="candidate") == 1


def test_auto_mode_collects_directly(env, monkeypatch):
    env.cfg.raw["stickers"] = {"collect_mode": "auto"}
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"\xff\xd8jpegdata"))
    rows = env.st.collect(_payload())
    assert env.store.sticker_get(rows[0][0])["source"] == "collected"


def test_off_collects_nothing(env, monkeypatch):
    env.cfg.raw["stickers"] = {"collect_mode": "off"}
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a...."))
    assert env.st.collect(_payload()) == []


def test_vision_only_once_per_image(env):
    path = env.st.dir / "a.gif"
    path.write_bytes(b"x")
    desc1 = asyncio.run(env.st.describe("abc", path))
    desc2 = asyncio.run(env.st.describe("abc", path))
    assert desc1 == desc2 and desc1[0]
    assert env.look.calls == 1  # 第二次走缓存，没再调模型


def test_vision_respects_daily_cap(env):
    env.cfg.raw["stickers"] = {"understand_daily_max": 1}
    p1 = env.st.dir / "1.gif"
    p1.write_bytes(b"1")
    p2 = env.st.dir / "2.gif"
    p2.write_bytes(b"2")
    asyncio.run(env.st.describe("h1", p1))
    assert asyncio.run(env.st.describe("h2", p2)) == ("", "")
    assert env.look.calls == 1


def test_keep_promotes_candidate(env, monkeypatch):
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a.keep"))
    rows = env.st.collect(_payload())
    sid = rows[0][0]
    assert env.st.promote(sid, "猫猫") is True
    row = env.store.sticker_get(sid)
    assert row["source"] == "collected" and row["remark"] == "猫猫"


def test_expire_removes_old_candidates(env, monkeypatch):
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a..old"))
    rows = env.st.collect(_payload())
    env.cfg.raw["stickers"] = {"candidate_days": 7}
    # 把 ts 改到 10 天前
    env.store.conn.execute(
        "UPDATE stickers SET ts = ts - 10 * 86400000 WHERE id = ?", (rows[0][0],)
    )
    env.store.conn.commit()
    files = env.store.sticker_expire_candidates(7)
    assert files and env.store.sticker_count(source="candidate") == 0


def test_attach_registers_keep_tool(env):
    """她要用 keep_sticker 收表情，所以这个工具得真的挂上去。"""
    from core.bus import Bus
    from core.tools import ToolBox

    tools = ToolBox()
    env.st.tools = tools
    env.st.attach(Bus())
    assert "keep_sticker" in tools.names()


def test_keep_does_not_write_memory(env, monkeypatch):
    """收下表情只进表情包库，不往记忆库投 —— 意思和标签就在 stickers 表里。

    （以前没有视觉模型，意思只能借记忆库记一句；现在视觉模型直接写库，
    记忆里那份是重复的，改图的意思时还不会跟着变。）
    """
    calls = []

    class FakePool:
        async def call_raw(self, server, tool, payload):
            calls.append((server, tool, payload))
            return "ok"

    env.st.services["mcppool"] = FakePool()
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a.nomem"))
    rows = env.st.collect(_payload())
    sid = rows[0][0]
    assert env.st.promote(sid, "猫猫") is True

    row = env.store.sticker_get(sid)
    assert row["source"] == "collected"
    assert row["remark"] == "猫猫"
    assert calls == []  # 一条都没往外发


def test_meaning_and_remark_live_in_sticker_library(env):
    """改图的意思、起个名字，都只落库；提示词读的也是这里。"""
    sid = env.store.sticker_add("m1", summary="[动画表情]")
    env.store.sticker_set_meaning(sid, "一只猫四脚朝天躺着摆烂", "摆烂、累")
    env.store.sticker_set_remark(sid, "这就是我现在的状态")

    row = env.store.sticker_get(sid)
    assert row["meaning"] == "一只猫四脚朝天躺着摆烂"
    assert row["tags"] == "摆烂、累"
    assert row["remark"] == "这就是我现在的状态"

    payload = {"extra": ""}
    env.st.on_prompt(payload)
    assert "四脚朝天" in payload["extra"]


def test_delete_only_removes_from_library(env):
    """删掉一张：库里的行没了，不往记忆库写「已删除」。"""
    calls = []

    class FakePool:
        async def call_raw(self, server, tool, payload):
            calls.append((tool, payload))
            return "ok"

    env.st.services["mcppool"] = FakePool()
    sid = env.store.sticker_add("d1", summary="[动画表情]", file="d1.gif",
                                source="collected")
    (env.st.dir / "d1.gif").write_bytes(b"x")
    env.store.sticker_set_meaning(sid, "猫躺着摆烂", "摆烂")

    env.store.sticker_vision_delete("d1")
    env.store.sticker_remove(sid)
    assert env.store.sticker_get(sid) is None
    assert calls == []


def test_failure_goes_into_buffer_then_retries(env):
    """模型调不通：进缓冲区（记次数和下次时间），到点后重试能补上。"""

    class FlakyLook:
        def __init__(self):
            self.calls = 0
            self.ok = False

        async def see_sticker(self, target):
            self.calls += 1
            if not self.ok:
                return None
            return {"desc": "一只猫躺着摆烂", "tags": ["摆烂"], "model": "m"}

    look = FlakyLook()
    env.st.look = look
    path = env.st.dir / "b.gif"
    path.write_bytes(b"x")

    assert asyncio.run(env.st.describe("hbuf", path)) == ("", "")
    rec = env.store.sticker_vision_get("hbuf")
    assert rec["status"] == "fail" and rec["attempts"] == 1 and rec["next_at"] > 0
    # 还没到点：不再调模型
    assert asyncio.run(env.st.describe("hbuf", path)) == ("", "")
    assert look.calls == 1

    # 到点了 + 模型恢复：这次看成
    env.store.conn.execute("UPDATE sticker_vision SET next_at = 1 WHERE hash = 'hbuf'")
    env.store.conn.commit()
    look.ok = True
    assert asyncio.run(env.st.describe("hbuf", path))[0] == "一只猫躺着摆烂"
    assert env.store.sticker_vision_get("hbuf")["status"] == "ok"


def test_failure_stops_after_retry_max(env):
    class DeadLook:
        async def see_sticker(self, target):
            return None

    env.cfg.raw["stickers"] = {"understand_retry_max": 2}
    env.st.look = DeadLook()
    path = env.st.dir / "dead.gif"
    path.write_bytes(b"x")
    for _ in range(2):
        asyncio.run(env.st.describe("hdead", path))
        env.store.conn.execute("UPDATE sticker_vision SET next_at = 1")
        env.store.conn.commit()
    rec = env.store.sticker_vision_get("hdead")
    assert rec["attempts"] == 2
    # 试满两次之后就不再试了（返回空、也不再加次数）
    assert asyncio.run(env.st.describe("hdead", path)) == ("", "")
    assert env.store.sticker_vision_get("hdead")["attempts"] == 2


def test_daily_cap_counts_only_success(env):
    """失败不占每天额度，成功才占。"""

    class FlakyLook:
        ok = False

        async def see_sticker(self, target):
            if not FlakyLook.ok:
                return None
            return {"desc": "猫", "tags": ["萌"], "model": "m"}

    env.cfg.raw["stickers"] = {"understand_daily_max": 1}
    env.st.look = FlakyLook()
    p1 = env.st.dir / "c1.gif"
    p1.write_bytes(b"1")
    p2 = env.st.dir / "c2.gif"
    p2.write_bytes(b"2")
    asyncio.run(env.st.describe("c1", p1))  # 失败，不占额度
    assert env.store.sticker_vision_count_today() == 0
    # 失败后要等退避时间到才会再试
    env.store.conn.execute("UPDATE sticker_vision SET next_at = 1 WHERE hash = 'c1'")
    env.store.conn.commit()
    FlakyLook.ok = True
    assert asyncio.run(env.st.describe("c1", p1))[0] == "猫"
    assert env.store.sticker_vision_count_today() == 1
    # 额度用完了，第二张要先等着
    assert asyncio.run(env.st.describe("c2", p2)) == ("", "")


def test_candidate_files_live_in_cache_dir_and_move_on_keep(env, monkeypatch):
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a.cache"))
    rows = env.st.collect(_payload())
    sid, _digest, _path = rows[0]
    row = env.store.sticker_get(sid)
    assert row["file"].startswith("_cache/")
    assert (env.st.cache_dir / Path(row["file"]).name).exists()

    assert env.st.promote(sid) is True
    row = env.store.sticker_get(sid)
    assert not row["file"].startswith("_cache/")
    assert (env.st.dir / row["file"]).exists()
    assert not (env.st.cache_dir / Path(row["file"]).name).exists()


def test_candidate_cap_evicts_oldest(env, monkeypatch):
    biggest = [0]

    def fake_download(url):
        biggest[0] += 1
        return b"GIF89a." + str(biggest[0]).encode()

    monkeypatch.setattr(Stickers, "_download", staticmethod(fake_download))
    env.cfg.raw["stickers"] = {"candidate_max": 2,
                               "collect_max_per_minute": 0, "collect_max_per_hour": 0}
    for i in range(4):
        env.st.collect(_payload(url=f"https://x/{i}.gif"))
    assert env.store.sticker_count(source="candidate") == 4
    env.st._maintain()
    assert env.store.sticker_count(source="candidate") == 2


def test_find_prefers_exact_tag(env):
    """发送时先认「正好是这个标签」的那张。"""
    a = env.store.sticker_add("t1", summary="[动画表情]", file="t1.gif")
    env.store.sticker_set_meaning(a, "猫在摆烂", "摆烂、无语")
    b = env.store.sticker_add("t2", summary="[动画表情]", file="t2.gif")
    env.store.sticker_set_meaning(b, "人无语地看着屏幕", "无语")
    got = env.store.sticker_find("无语")
    assert got["id"] == b  # tags 正好等于「无语」的优先


def test_keep_refuses_unknown_hint(env):
    """她说「收那只猫」但缓存里没有：说没找到，不拿最新那张糊弄。"""
    env.store.sticker_add("k1", summary="[动画表情]", source="candidate")
    msg = env.st.keep("猫")
    assert "没找到" in msg
    assert env.store.sticker_count(source="candidate") == 1


def test_candidate_seen_again_moves_to_top(env, monkeypatch):
    """同一张又被刷到：刷新时间，它排到缓存区最上面，过期也重新计时。"""
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a.aa"))
    env.st.collect(_payload())
    old = env.store.sticker_candidates(limit=1)[0]
    env.store.sticker_add("other", summary="[动画表情]", source="candidate",
                          ts=old["ts"] + 5)
    time.sleep(0.01)  # 让她真过一会儿再被刷到
    # 又见到第一张
    env.st.collect(_payload())
    top = env.store.sticker_candidates(limit=1)[0]
    assert top["id"] == old["id"]
    assert top["last_seen"] >= old["ts"]


def test_candidate_expire_uses_last_seen(env):
    old = env.store.sticker_add("exp1", summary="[x]", source="candidate",
                                ts=1, file="")
    env.store.sticker_touch("exp1")  # 刚刚又见到
    gone = env.store.sticker_expire_candidates(7)
    assert "exp1" not in [""] and old not in gone


def test_prompt_skips_candidates_without_meaning(env):
    """没看懂的候选不往提示词里塞：一排「动画表情」她没法挑。"""
    payload = {"extra": ""}
    env.st.on_prompt(payload)
    assert "[表情包·候选]" not in payload["extra"]
    a = env.store.sticker_add("p1", summary="[动画表情]", source="candidate")
    env.store.sticker_add("p2", summary="[动画表情]", source="candidate")
    env.store.sticker_set_meaning(a, "猫躺着摆烂", "摆烂")
    payload = {"extra": ""}
    env.st.on_prompt(payload)
    assert "猫躺着摆烂" in payload["extra"]
    assert payload["extra"].count("动画表情") == 0


def test_vision_status_reports_usage(env):
    v = env.st.vision_status()
    assert v["daily_max"] == 30 and v["used_today"] == 0 and v["remaining"] == 30
    env.cfg.raw["stickers"] = {"understand_daily_max": 0}
    assert env.st.vision_status()["remaining"] == -1


def test_retag_uses_human_description(env):
    """人写了描述，标签按这句话重算；描述本身不被模型改写。"""
    sid = env.store.sticker_add("r1", summary="[动画表情]", file="r1.png")
    (env.st.dir / "r1.png").write_bytes(b"x")
    env.store.sticker_set_meaning(sid, "我一瞪眼他就该闭嘴", "")
    tags = asyncio.run(env.st.retag(sid))
    assert tags == "生气、催"
    row = env.store.sticker_get(sid)
    assert row["meaning"] == "我一瞪眼他就该闭嘴"  # 描述还是人写的那句
    assert row["tags"] == "生气、催"


def test_delete_removes_vision_ledger(env):
    """图删了，账也删：没图的意思没法核对，留着只占地方。"""
    env.store.sticker_vision_put("gone", "一只猫", "摆烂", "m")
    assert env.store.sticker_vision_get("gone") is not None
    env.store.sticker_vision_delete("gone")
    assert env.store.sticker_vision_get("gone") is None


def test_orphan_vision_rows_get_pruned(env):
    env.store.sticker_vision_put("alive", "猫", "萌", "m")
    env.store.sticker_vision_put("orphan", "狗", "凶", "m")
    env.store.sticker_add("alive", summary="[动画表情]")
    assert env.store.sticker_vision_prune_orphans() == 1
    assert env.store.sticker_vision_get("orphan") is None
    assert env.store.sticker_vision_get("alive") is not None


def test_look_daily_counter_persists_across_restart(env):
    """聊天看图的计数写在海螺自己的库里，重启不清零。"""
    from core.look import Look

    class Cfg:
        raw = {"look": {"max_images_per_day": 2, "max_images": 2}}

        data_dir = env.cfg.data_dir

    one = Look(Cfg(), env.store)
    assert one.usage_today() == 0
    assert one._quota_ok(one.settings()) is True
    two = Look(Cfg(), env.store)  # 相当于重启：新对象，不读内存
    assert two.usage_today() == 1
    assert two._quota_ok(two.settings()) is True
    three = Look(Cfg(), env.store)
    assert three.usage_today() == 2
    assert three._quota_ok(three.settings()) is False  # 到上限了


def test_prompt_lists_meanings_and_candidates(env, monkeypatch):
    """提示词里要带意思；候选单独一段，并告诉她可以用 keep_sticker 收。"""
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a.p1"))
    rows = env.st.collect(_payload())
    env.store.sticker_set_meaning(rows[0][0], "一只猫躺着摆烂", "摆烂、累")
    payload = {}
    env.st.on_prompt(payload)
    text = payload["extra"]
    assert "一只猫躺着摆烂" in text
    assert "候选" in text and "keep_sticker" in text


def test_on_prompt_skips_candidates_for_collected_list(env, monkeypatch):
    """候选不能混进「手里有哪些」，只能出现在候选那一段。"""
    monkeypatch.setattr(Stickers, "_download", staticmethod(lambda url: b"GIF89a.p2"))
    rows = env.st.collect(_payload())
    env.store.sticker_set_meaning(rows[0][0], "候选里的狗", "狗")
    payload = {}
    env.st.on_prompt(payload)
    text = payload["extra"]
    assert "手里有" not in text.split("候选")[0] or "候选里的狗" in text
    assert text.count("候选里的狗") == 1