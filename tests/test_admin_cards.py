"""称呼卡：配置解析 + 权限判定。"""
from __future__ import annotations

import pytest

from core.config import Admin, ConfigError, load_config, owner_ids_of, parse_admins
from core.policy import allowed


def test_parse_two_cards():
    cards = parse_admins([
        {"name": "管理员A", "ids": ["1234567890", "9876543210"],
         "call": {"private": "老爹", "group": "老大", "by_group": {"123456": "老翰"}}},
        {"name": "管理员B", "ids": ["12345678901"]},
    ])
    assert [c.name for c in cards] == ["管理员A", "管理员B"]
    assert cards[0].ids == ["1234567890", "9876543210"]
    assert cards[0].call_for("private") == "老爹"
    assert cards[0].call_for("group", "123456") == "老翰"
    assert cards[0].call_for("group", "100000") == "老大"
    assert cards[1].call_for("private") == ""


def test_duplicate_qq_rejected():
    with pytest.raises(ConfigError):
        parse_admins([
            {"name": "A", "ids": ["123"]},
            {"name": "B", "ids": ["123"]},
        ])


def test_card_without_ids_rejected():
    with pytest.raises(ConfigError):
        parse_admins([{"name": "A", "ids": []}])


def test_owner_ids_split_chinese_comma():
    assert owner_ids_of("1234567890，9876543210") == ["1234567890", "9876543210"]


def test_legacy_owner_qq_becomes_one_card(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"owner_qq": "1234567890", "onebot": {}, "allow": {}}', encoding="utf-8")
    cfg = load_config(p)
    assert len(cfg.admins) == 1
    assert cfg.admins[0].name == "管理员"
    assert cfg.owner_ids == ["1234567890"]


def test_admin_cards_drive_permissions(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(
        '{"owner_qq": "1234567890", "onebot": {}, "allow": {"private": ["1234567890"]},'
        ' "admins": [{"name": "A", "ids": ["1234567890"]}, {"name": "B", "ids": ["300000"]}]}',
        encoding="utf-8",
    )
    cfg = load_config(p)
    # 卡里的号都算管理员：B 没在私聊白名单里也能进
    assert allowed(cfg, "private:300000", "300000") is True
    # 不在任何卡上、也不在白名单里的人：不理
    assert allowed(cfg, "private:100000", "100000") is False