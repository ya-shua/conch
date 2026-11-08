"""配置加载。

规矩：配置坏了就退出，绝不用默认值继续跑。
少了白名单、少了 owner、少了令牌却"看起来在正常运行"，是本项目最危险的失败方式。
"""

from __future__ import annotations

import json
import os
import re
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


# 默认的"收尾话"：主人说这些就是想结束，不用再追一句。
# 只在句尾短句（≤10 字）上判定，避免"晚安"出现在长句里也闭嘴。
DEFAULT_CLOSING_WORDS: tuple[str, ...] = (
    "晚安", "我睡了", "睡了", "睡觉了", "去睡了", "先睡了", "睡觉去", "洗洗睡",
    "拜拜", "再见", "我走了", "走了", "先走了", "下了", "我下了", "下线了",
    "去忙了", "先忙了", "我忙了", "不聊了", "不说了", "先这样", "就这样", "回头聊",
    "改天聊", "下次聊", "改天再说", "吃饭去了", "去洗澡了", "洗澡去了",
    "上课去了", "上班去了", "干活去了", "溜了", "我溜了", "撤了", "先撤了",
)

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
    # 管理员可以有多个：面板里用逗号隔开填。owner_qq 永远是第一个（老字段照旧能用）
    owner_ids: list[str] = field(default_factory=list)
    # 称呼卡：一卡一人（可挂多个号），带各场景想让她怎么叫
    admins: list[Admin] = field(default_factory=list)
    merge_window_ms: int = 4000
    merge_max_wait_ms: int = 0
    # 一句一条时的停顿：base + 字数*20ms，再拿 max 封顶（面板可调）
    reply_gap_base_ms: int = 700
    reply_gap_max_ms: int = 2200
    reply_gap_char_ms: int = 20
    # 一轮最多发几条（0 = 不限）；超出的并到最后一条
    reply_max_lines: int = 4
    # 重启/断线后，把没回成的私聊补一次
    catch_up_enabled: bool = True
    catch_up_minutes: int = 30      # 只回看这么久，更早的不吵她
    # 主人用一句"收尾话"结束聊天时，她就不回了
    closing_enabled: bool = True
    closing_words: list[str] = field(default_factory=lambda: list(DEFAULT_CLOSING_WORDS))
    # 听到的话顺手写进记忆库（环形的心），跟她想不想得起来无关
    memory_ingest_enabled: bool = True
    data_dir: str = "data"
    roles_dir: str = "roles"
    plugins_dir: str = "plugins"
    plugin_paths: list[str] = field(default_factory=list)
    strategy: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "海螺.db"


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
    single = str(model_raw.get("api_key") or os.environ.get("CONCH_API_KEY") or os.environ.get("QQBRIDGE_API_KEY", "") or "").strip()
    if not out and single:
        out.append({"key": single, "label": "默认"})
    return out


def owner_ids_of(value: Any) -> list[str]:
    """把「管理员 QQ」拆成多个：逗号、中文逗号、分号、空格都认，去重保序。"""
    out: list[str] = []
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = [p for p in re.split(r"[,，;；\s]+", str(value or ""))]
    for part in parts:
        uid = norm_id(part)
        if uid and uid not in out:
            out.append(uid)
    return out


@dataclass
class Admin:
    """称呼卡：一个自己人。

    - 可以挂多个 QQ 号（单人多号），这些号在她眼里是同一个人
    - 称呼只是「这个场景希望你怎么叫」，真正的称呼词在人设卡里
    """

    id: str
    name: str
    ids: list[str] = field(default_factory=list)
    private_call: str = ""
    group_call: str = ""
    by_group: dict[str, str] = field(default_factory=dict)

    def call_for(self, kind: str, gid: str = "") -> str:
        """这个场景该叫他什么：本群单独设的 > 群里默认 > 私聊。"""
        if kind == "group":
            return (self.by_group.get(norm_id(gid)) or self.group_call).strip()
        return self.private_call.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ids": list(self.ids),
            "call": {
                "private": self.private_call,
                "group": self.group_call,
                "by_group": dict(self.by_group),
            },
        }


def parse_admins(value: Any) -> list[Admin]:
    """读「称呼卡」数组。老配置没有这个字段就返回空，由 owner_qq 兜底。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("admins 必须是数组")
    out: list[Admin] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError("admins 里每一项都必须是对象")
        ids = owner_ids_of(item.get("ids"))
        if not ids:
            raise ConfigError(f"admins 第 {i + 1} 项没有填 QQ 号")
        for uid in ids:
            if uid in seen:
                raise ConfigError(f"QQ {uid} 同时出现在两张称呼卡里")
            seen.add(uid)
        call = item.get("call") or {}
        if not isinstance(call, dict):
            raise ConfigError("admins[].call 必须是对象")
        by_group = call.get("by_group") or {}
        if not isinstance(by_group, dict):
            raise ConfigError("admins[].call.by_group 必须是对象")
        out.append(
            Admin(
                id=str(item.get("id") or f"a{i + 1}").strip() or f"a{i + 1}",
                name=str(item.get("name") or "管理员").strip() or "管理员",
                ids=ids,
                private_call=str(call.get("private") or "").strip(),
                group_call=str(call.get("group") or "").strip(),
                by_group={
                    norm_id(k): str(v).strip()
                    for k, v in by_group.items()
                    if norm_id(k) and str(v).strip()
                },
            )
        )
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
        api_key=str(model_raw.get("api_key") or os.environ.get("CONCH_API_KEY") or os.environ.get("QQBRIDGE_API_KEY", "")),
        model=str(model_raw.get("model") or "deepseek-chat"),
        timeout_s=int(model_raw.get("timeout_s") or 60),
        keys=_parse_keys(model_raw),
    )

    cfg.panel = PanelConfig(
        port=int(panel_raw.get("port") or 7801),
        token=str(panel_raw.get("token") or ""),
    )

    legacy_ids = owner_ids_of(raw.get("owner_qq"))
    cfg.owner_qq = legacy_ids[0] if legacy_ids else ""
    cfg.admins = parse_admins(raw.get("admins"))
    if cfg.admins:
        ids: list[str] = []
        for card in cfg.admins:
            for uid in card.ids:
                if uid not in ids:
                    ids.append(uid)
        # 老的 owner_qq 不在任何卡里、又没被删掉：单独补一张卡，别让它凭空失去管理员身份
        extra = [uid for uid in legacy_ids if uid not in ids]
        if extra:
            cfg.admins.insert(0, Admin(id="a0", name="管理员", ids=extra))
            ids = extra + ids
        cfg.owner_ids = ids
        cfg.owner_qq = ids[0]
    else:
        cfg.owner_ids = legacy_ids
        cfg.owner_qq = legacy_ids[0] if legacy_ids else ""
        cfg.admins = [Admin(id="a1", name="管理员", ids=list(legacy_ids))] if legacy_ids else []
    cfg.merge_window_ms = int(raw.get("merge_window_ms") or 4000)
    cfg.merge_max_wait_ms = int(raw.get("merge_max_wait_ms") or 0)
    cfg.reply_gap_base_ms = max(0, int(raw.get("reply_gap_base_ms") if raw.get("reply_gap_base_ms") is not None else 700))
    cfg.reply_gap_max_ms = max(0, int(raw.get("reply_gap_max_ms") if raw.get("reply_gap_max_ms") is not None else 2200))
    cfg.reply_gap_char_ms = max(0, int(raw.get("reply_gap_char_ms") if raw.get("reply_gap_char_ms") is not None else 20))
    cfg.reply_max_lines = max(0, int(raw.get("reply_max_lines") if raw.get("reply_max_lines") is not None else 4))
    cfg.catch_up_enabled = bool(raw.get("catch_up_enabled", True))
    cfg.catch_up_minutes = max(1, int(raw.get("catch_up_minutes") if raw.get("catch_up_minutes") is not None else 30))
    cfg.closing_enabled = bool(raw.get("closing_enabled", True))
    cfg.memory_ingest_enabled = bool(raw.get("memory_ingest_enabled", True))
    words = raw.get("closing_words")
    if isinstance(words, list):
        cfg.closing_words = [str(w).strip() for w in words if str(w).strip()]
    elif isinstance(words, str):
        cfg.closing_words = [w.strip() for w in re.split(r"[,，;；\s]+", words) if w.strip()]
    cfg.strategy = {**DEFAULT_STRATEGY, **(raw.get("strategy") or {})}
    cfg.data_dir = str(raw.get("data_dir") or "data")
    cfg.roles_dir = str(raw.get("roles_dir") or "roles")
    cfg.plugins_dir = str(raw.get("plugins_dir") or "plugins")
    cfg.plugin_paths = [str(x) for x in (raw.get("plugin_paths") or []) if str(x).strip()]

    if cfg.merge_window_ms < 0:
        raise ConfigError("merge_window_ms 不能为负")
    if cfg.merge_max_wait_ms and cfg.merge_max_wait_ms < cfg.merge_window_ms:
        cfg.merge_max_wait_ms = cfg.merge_window_ms
    if cfg.reply_gap_max_ms and cfg.reply_gap_max_ms < cfg.reply_gap_base_ms:
        cfg.reply_gap_max_ms = cfg.reply_gap_base_ms

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