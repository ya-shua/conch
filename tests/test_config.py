import json

import pytest

from core.config import ConfigError, load_config, norm_id


def _write(tmp_path, data):
    p = tmp_path / "config.json"
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")


def test_bad_json_raises(tmp_path):
    p = _write(tmp_path, "{bad")
    with pytest.raises(ConfigError):
        load_config(p)


def test_norm_id():
    assert norm_id(123456) == "123456"
    assert norm_id(" 123456 ") == "123456"
    assert norm_id(None) == ""


def test_relative_dirs_resolve_to_config_folder(tmp_path):
    p = _write(tmp_path, {"plugins_dir": "plugins", "data_dir": "data"})
    cfg = load_config(p)
    assert cfg.plugins_dir == str((tmp_path / "plugins").resolve())
    assert cfg.db_path == tmp_path / "data" / "海螺.db"


def test_allow_ids_are_normalized(tmp_path):
    p = _write(tmp_path, {"allow": {"groups": [123456], "private": [" 789 "]}})
    cfg = load_config(p)
    assert cfg.allow.groups == ["123456"]
    assert cfg.allow.private == ["789"]


def test_allow_list_must_be_array(tmp_path):
    p = _write(tmp_path, {"allow": {"groups": "123456"}})
    with pytest.raises(ConfigError):
        load_config(p)