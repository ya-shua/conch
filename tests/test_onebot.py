from core.onebot import escape_cq, parse_event


def private_event():
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 10000,
        "user_id": 123,
        "message_id": 55,
        "time": 1700000000,
        "sender": {"nickname": "张三"},
        "message": [{"type": "text", "data": {"text": "你好"}}],
    }


def test_parse_private():
    msg = parse_event(private_event(), self_id="10000")
    assert msg is not None
    assert msg.conv_id == "private:123"
    assert msg.kind == "private"
    assert msg.sender_id == "123"
    assert msg.text == "你好"
    assert msg.at_me is False


def test_parse_group_and_at_me():
    raw = {
        "post_type": "message",
        "message_type": "group",
        "self_id": "10000",
        "user_id": "456",
        "group_id": "789",
        "message_id": "66",
        "time": 1700000000,
        "sender": {"card": "群名片"},
        "message": [
            {"type": "at", "data": {"qq": "10000"}},
            {"type": "text", "data": {"text": " 在吗"}},
        ],
    }
    msg = parse_event(raw, self_id="10000")
    assert msg.conv_id == "group:789"
    assert msg.at_me is True
    assert msg.sender_name == "群名片"


def test_non_message_event_ignored():
    assert parse_event({"post_type": "notice"}, self_id="1") is None


def test_escape_cq():
    assert escape_cq("[CQ:at,qq=all]") == "[CQ：at,qq=all]"