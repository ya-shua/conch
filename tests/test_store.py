from qqbridge.core.store import Store


def make_store(tmp_path) -> Store:
    s = Store(tmp_path / "t.db")
    s.ensure_schema()
    return s


def row(**kw):
    base = {
        "ts": 1_000,
        "plugin_id": "qq_bridge",
        "feature": "chat",
        "conv_id": "private:1",
        "turn_id": "t1",
        "step": 0,
        "retry": 0,
        "model": "deepseek-chat",
        "prompt_tokens": 100,
        "cached_tokens": 20,
        "completion_tokens": 30,
        "cost_yuan": 0.001,
        "peak": 0,
    }
    base.update(kw)
    return base


def test_usage_add_and_summary(tmp_path):
    s = make_store(tmp_path)
    s.usage_add(row())
    s.usage_add(row(turn_id="t2", plugin_id="memory", feature="memory", cost_yuan=0.002))
    rows = s.usage_summary(0, group_by=("plugin_id",))
    by = {r["plugin_id"]: r for r in rows}
    assert by["qq_bridge"]["calls"] == 1
    assert by["memory"]["cost_yuan"] == 0.002
    assert by["qq_bridge"]["prompt_tokens"] == 100


def test_usage_idempotent_on_replay(tmp_path):
    s = make_store(tmp_path)
    s.usage_add(row())
    s.usage_add(row())  # 重放同一笔
    rows = s.usage_summary(0, group_by=("plugin_id",))
    assert rows[0]["calls"] == 1
    assert rows[0]["prompt_tokens"] == 100


def test_kv_isolated_by_plugin(tmp_path):
    s = make_store(tmp_path)
    s.kv_put("a", "k", {"v": 1})
    s.kv_put("b", "k", {"v": 2})
    assert s.kv_get("a", "k") == {"v": 1}
    assert s.kv_get("b", "k") == {"v": 2}
    assert s.kv_get("c", "k", "none") == "none"


def test_usage_total(tmp_path):
    s = make_store(tmp_path)
    s.usage_add(row())
    s.usage_add(row(turn_id="t2"))
    total = s.usage_total(0)
    assert total["calls"] == 2
    assert round(total["cost_yuan"], 6) == 0.002


def test_messages_roundtrip(tmp_path):
    s = make_store(tmp_path)
    s.message_add("private:1", "in", "在吗", sender_id="1", sender_name="小明")
    s.message_add("private:1", "out", "在")
    s.message_add("group:2", "in", "谁在")
    convs = s.message_convs()
    by = {c["conv_id"]: c for c in convs}
    assert by["private:1"]["total"] == 2
    assert by["group:2"]["total"] == 1
    rows = s.message_list("private:1")
    assert [r["direction"] for r in rows] == ["out", "in"]  # 倒序
    assert s.message_clear("private:1") == 2
    assert s.message_list("private:1") == []


def test_lexicon_upsert_and_remove(tmp_path):
    s = make_store(tmp_path)
    s.lexicon_add("典", "slang", "很典型")
    s.lexicon_add("典", "slang", "改过的说明")
    items = s.lexicon_list()
    assert len(items) == 1
    assert items[0]["note"] == "改过的说明"
    assert s.lexicon_remove("典") == 1
    assert s.lexicon_list() == []


def test_approval_flow(tmp_path):
    s = make_store(tmp_path)
    aid = s.approval_add("private:1", "转账给我", {"reply": "不行"})
    pending = s.approval_list("pending")
    assert len(pending) == 1 and pending[0]["id"] == aid
    assert pending[0]["payload"]["reply"] == "不行"
    assert s.approval_decide(aid, "approved") is True
    assert s.approval_list("pending") == []
    assert s.approval_decide(aid, "approved") is False  # 已经决定了