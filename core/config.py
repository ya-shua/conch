"""配置加载。

规矩：配置坏了就退出，绝不用默认值继续跑。
少了白名单、少了 owner、少了令牌却"看起来在正常运行"，是本项目最危险的失败方式。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """配置不可用。调用方应当直接退出，不要兜底。"""


def norm_id(value: Any) -> str:
    """把 QQ 号 / 群号统一成字符串。

    OneBot 的不同实现在 int64 上会给出数字或字符串两种形态，
    直接 `==` 会让白名单静默失效，所以所有 id 比较前都要过这里。
    """
    if value is None:
        return ""
    return str(value).strip()


def _as_id_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} 必须是数组")
    return [norm_id(v) for v in value if norm_id(v)]


@dataclass
class OneBotConfig:
    ws_url: str = "ws://127.0.0.1:3001"
    http_url: str = "http://127.0.0.1:3000"
    access_token: str = ""
    ws_access_token: str = ""


@dataclass
class AllowConfig:
    groups: list[str] = field(default_factory=list)
    private: list[str] = field(default_factory=list)
    deny_groups: list[str] = field(default_factory=list)
    deny_private: list[str] = field(default_factory=list)
    allow_all_when_empty: bool = False


@dataclass
class ModelConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = "deepseek-chat"
    timeout_s: int = 60
    # 自定义 / 中转时自己起的名字，只用来显示
    label: str = ""
    # 多把 Key：按顺序用，前面那把欠费/失效就换下一把（见 backend.py）
    keys: list[dict] = field(default_factory=list)


@dataclass
class PanelConfig:
    port: int = 7801
    token: str = ""


DEFAULT_STRATEGY: dict[str, Any] = {
    "trigger": "at_only",  # at_only / at_or_name / any
    "participate": "must_reply",  # must_reply / may_chime / listen_only
    "cooldown_sec": 20,
    "max_per_hour": 30,
    "max_consecutive": 3,
    "confirm_uncertain": False,  # 命中关键词时先挂起等主人点头
    "wake_gap_sec": 0,      # 她主动开口的最短间隔，0 = 不限
    "sleep_after_sec": 0,   # 群里安静多久算睡着，0 = 不睡
    "quiet_start": "",      # 免打扰开始，HH:MM；空 = 不设
    "quiet_end": "",        # 免打扰结束，HH:MM
    "min_chars": 0,         # 群里短于这个字数的消息不接（@ 除外）
}

# 自由发言：没人喊她，她自己决定要不要开口。
# tick 只是"给她一次机会"，不是"派任务"——每次都会先问模型想不想说。

@dataclass
class Config:
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    allow: AllowConfig = field(default_factory=AllowConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    panel: PanelConfig = field(default_factory=PanelConfig)
    owner_qq: str = ""
    merge_window_ms: int = 1500
    data_dir: str = "data"
    roles_dir: str = "roles"
    plugins_dir: str = "plugins"
    plugin_paths: list[str] = field(default_factory=list)
    strategy: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "qqbridge.db"


def _loads(text: str, path: Path) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 不是合法 JSON：第 {exc.lineno} 行 {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层必须是对象")
    return data


def _parse_keys(model_raw: dict) -> list[dict]:
    """多把 Key：`[{"key": "...", "label": "主号"}]`，保留摆放顺序。

    老配置只有 `api_key` 一把，也当成「只有一条」的列表，
    这样别处的代码不用分两种情况。
    """
    out: list[dict] = []
    raw = model_raw.get("keys")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                if item.strip():
                    out.append({"key": item.strip(), "label": ""})
            elif isinstance(item, dict):
                key = str(item.get("key") or "").strip()
                if key:
                    out.append({"key": key, "label": str(item.get("label") or "").strip()[:40]})
    single = str(model_raw.get("api_key") or os.environ.get("QQBRIDGE_API_KEY", "") or "").strip()
    if not out and single:
        out.append({"key": single, "label": "默认"})
    return out


def load_config(path: str | Path) -> Config:
    """读取并校验配置。任何一步失败都抛 ConfigError。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读不了配置文件：{path}：{exc}") from exc

    raw = _loads(text, path)

    onebot_raw = raw.get("onebot") or {}
    allow_raw = raw.get("allow") or {}
    model_raw = raw.get("model") or {}
    panel_raw = raw.get("panel") or {}

    if not isinstance(onebot_raw, dict) or not isinstance(allow_raw, dict):
        raise ConfigError("onebot / allow 必须是对象")

    cfg = Config(raw=raw)

    cfg.onebot = OneBotConfig(
        ws_url=str(onebot_raw.get("ws_url") or "ws://127.0.0.1:3001"),
        http_url=str(onebot_raw.get("http_url") or "http://127.0.0.1:3000"),
        access_token=str(onebot_raw.get("access_token") or ""),
        # SnowLuma 的 HTTP 与 WS 是两个不同 token，不能混用
        ws_access_token=str(onebot_raw.get("ws_access_token") or onebot_raw.get("access_token") or ""),
    )

    cfg.allow = AllowConfig(
        groups=_as_id_list(allow_raw.get("groups"), "allow.groups"),
        private=_as_id_list(allow_raw.get("private"), "allow.private"),
        deny_groups=_as_id_list(allow_raw.get("deny_groups"), "allow.deny_groups"),
        deny_private=_as_id_list(allow_raw.get("deny_private"), "allow.deny_private"),
        allow_all_when_empty=bool(allow_raw.get("allow_all_when_empty", False)),
    )

    cfg.model = ModelConfig(
        base_url=str(model_raw.get("base_url") or ""),
        api_key=str(model_raw.get("api_key") or os.environ.get("QQBRIDGE_API_KEY", "")),
        model=str(model_raw.get("model") or "deepseek-chat"),
        timeout_s=int(model_raw.get("timeout_s") or 60),
        keys=_parse_keys(model_raw),
    )

    cfg.panel = PanelConfig(
        port=int(panel_raw.get("port") or 7801),
        token=str(panel_raw.get("token") or ""),
    )

    cfg.owner_qq = norm_id(raw.get("owner_qq"))
    cfg.merge_window_ms = int(raw.get("merge_window_ms") or 1500)
    cfg.strategy = {**DEFAULT_STRATEGY, **(raw.get("strategy") or {})}
    cfg.data_dir = str(raw.get("data_dir") or "data")
    cfg.roles_dir = str(raw.get("roles_dir") or "roles")
    cfg.plugins_dir = str(raw.get("plugins_dir") or "plugins")
    cfg.plugin_paths = [str(x) for x in (raw.get("plugin_paths") or []) if str(x).strip()]

    if cfg.merge_window_ms < 0:
        raise ConfigError("merge_window_ms 不能为负")

    # 相对路径一律相对于配置文件所在目录，避免从别处启动时找不到
    base = path.parent
    cfg.plugin_paths = [
        str(p if p.is_absolute() else (base / p).resolve()) for p in map(Path, cfg.plugin_paths)
    ]

    for attr in ("data_dir", "roles_dir", "plugins_dir"):
        value = Path(getattr(cfg, attr))
        if not value.is_absolute():
            setattr(cfg, attr, str((base / value).resolve()))

    return cfg