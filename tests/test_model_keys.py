"""面板的 Key 管理：加几把都在、删到最后一把 api_key 要跟着清掉。

起因（2026-11-08 反馈）：
- 「添加」按钮压根没接事件，点下去没反应；
- 删到一把不剩时 config 里的 `model.api_key` 还留着旧值，界面显示没有 Key、
  实际调模型还在用那把删掉的 —— 看着就是「删不掉」。
"""
import json
from types import SimpleNamespace

from core.config import ModelConfig
from core.panel.server import _save_model_keys


def _req(tmp_path, keys):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"model": {"base_url": "http://x/v1", "model": "m", "api_key": keys[0] if keys else ""}}),
        encoding="utf-8",
    )
    cfg = SimpleNamespace(model=ModelConfig(base_url="http://x/v1", model="m", api_key=keys[0] if keys else "", keys=[]))
    app = {"cfg_path": str(cfg_path), "cfg": cfg}
    return SimpleNamespace(app=app), cfg_path, cfg


def test_add_three_keys_all_persist(tmp_path):
    req, path, cfg = _req(tmp_path, [])
    keys = [{"key": f"sk-{i}", "label": f"第{i}把"} for i in range(1, 4)]
    _save_model_keys(req, keys)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert [k["key"] for k in raw["model"]["keys"]] == ["sk-1", "sk-2", "sk-3"]
    assert raw["model"]["api_key"] == "sk-1"
    assert [k["key"] for k in cfg.model.keys] == ["sk-1", "sk-2", "sk-3"]


def test_remove_middle_key(tmp_path):
    req, path, _cfg = _req(tmp_path, [])
    _save_model_keys(req, [{"key": "sk-1", "label": ""}, {"key": "sk-2", "label": ""}, {"key": "sk-3", "label": ""}])
    _save_model_keys(req, [{"key": "sk-1", "label": ""}, {"key": "sk-3", "label": ""}])

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert [k["key"] for k in raw["model"]["keys"]] == ["sk-1", "sk-3"]


def test_remove_all_keys_clears_api_key(tmp_path):
    req, path, cfg = _req(tmp_path, ["sk-old"])
    _save_model_keys(req, [{"key": "sk-old", "label": "默认"}])
    _save_model_keys(req, [])

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["model"]["keys"] == []
    assert "api_key" not in raw["model"], "删光了还把 api_key 留着，会继续用那把删掉的"
    assert cfg.model.api_key == ""
    assert cfg.model.keys == []


def test_blank_keys_are_dropped(tmp_path):
    req, path, _cfg = _req(tmp_path, [])
    _save_model_keys(req, [{"key": "sk-1", "label": ""}, {"key": "  ", "label": "空"}])

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert [k["key"] for k in raw["model"]["keys"]] == ["sk-1"]