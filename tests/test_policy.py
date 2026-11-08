from core.config import Config, AllowConfig
from core.policy import allowed


def make(allow: AllowConfig, owner: str = "") -> Config:
    cfg = Config()
    cfg.allow = allow
    cfg.owner_qq = owner
    return cfg


def test_deny_beats_allow():
    cfg = make(AllowConfig(groups=["111"], deny_groups=["111"]))
    assert allowed(cfg, "group:111", "999") is False


def test_private_allow_by_id():
    cfg = make(AllowConfig(private=["222"]))
    assert allowed(cfg, "private:222", "222") is True
    assert allowed(cfg, "private:333", "333") is False


def test_numeric_ids_still_match():
    cfg = make(AllowConfig(groups=["111"]))
    assert allowed(cfg, "group:111", 999) is True


def test_empty_allowlist_denies_by_default():
    cfg = make(AllowConfig())
    assert allowed(cfg, "group:111", "999") is False


def test_allow_all_when_empty():
    cfg = make(AllowConfig(allow_all_when_empty=True))
    assert allowed(cfg, "group:111", "999") is True


def test_owner_private_always_allowed():
    cfg = make(AllowConfig(private=["222"]), owner="777")
    assert allowed(cfg, "private:777", "777") is True


def test_deny_private_beats_owner():
    cfg = make(AllowConfig(deny_private=["777"]), owner="777")
    assert allowed(cfg, "private:777", "777") is False