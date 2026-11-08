"""白名单判定。

顺序：黑名单 > owner > 白名单 > 空白名单策略。
所有比较都用 norm_id 归一化过的字符串。
"""

from __future__ import annotations

from .config import Config, norm_id


def split_conv(conv_id: str) -> tuple[str, str]:
    if ":" not in conv_id:
        raise ValueError(f"非法会话 id：{conv_id}")
    kind, target = conv_id.split(":", 1)
    if kind not in ("private", "group"):
        raise ValueError(f"非法会话类型：{conv_id}")
    return kind, norm_id(target)


def owner_set(cfg: Config) -> set[str]:
    """所有管理员 QQ（可能有多个号、多张称呼卡）。老配置只有 owner_qq 也照样算。"""
    ids: set[str] = set()
    for card in getattr(cfg, "admins", None) or []:
        ids.update(getattr(card, "ids", []) or [])
    ids.update(getattr(cfg, "owner_ids", None) or [])
    single = norm_id(getattr(cfg, "owner_qq", "") or "")
    if single:
        ids.add(single)
    return ids


def allowed(cfg: Config, conv_id: str, sender_id: str) -> bool:
    kind, target = split_conv(conv_id)
    sender = norm_id(sender_id)
    allow = cfg.allow

    if kind == "group":
        if target in allow.deny_groups:
            return False
    else:
        if target in allow.deny_private or sender in allow.deny_private:
            return False

    if kind == "private" and sender in owner_set(cfg):
        return True

    listed = allow.groups if kind == "group" else allow.private
    if not listed:
        return allow.allow_all_when_empty
    return target in listed