"""计价：把 token 折成人民币。

价目表按 DeepSeek 官方口径（元 / 百万 token），高峰是空闲的 2 倍。
读的时候算，不写死在记录里——改价目表不用重算历史。
"""

from __future__ import annotations

import datetime as dt

# 元 / 百万 token：缓存命中 / 未命中 / 输出
PRICES: dict[str, dict[str, float]] = {
    "deepseek-chat": {"cache_hit": 0.02, "cache_miss": 1.0, "output": 4.0},
    "deepseek-v4-flash": {"cache_hit": 0.02, "cache_miss": 1.0, "output": 4.0},
    "deepseek-v4-pro": {"cache_hit": 0.15, "cache_miss": 4.5, "output": 13.5},
    # 阿里云百炼（qwen）：没有 DeepSeek 那种错峰减半，no_peak 关掉倍数
    "qwen-plus": {"cache_hit": 0.8, "cache_miss": 0.8, "output": 2.0, "no_peak": True},
    "qwen-turbo": {"cache_hit": 0.3, "cache_miss": 0.3, "output": 0.6, "no_peak": True},
    "qwen-max": {"cache_hit": 2.4, "cache_miss": 2.4, "output": 9.6, "no_peak": True},
    "qwen-long": {"cache_hit": 0.5, "cache_miss": 0.5, "output": 2.0, "no_peak": True},
}

FALLBACK_MODEL = "deepseek-chat"
PEAK_MULTIPLIER = 2.0


def is_peak(ts_ms: int) -> bool:
    """工作日 9:00–12:00、14:00–18:00（北京时间）算高峰。节假日表 M1 先不维护。"""
    tz = dt.timezone(dt.timedelta(hours=8))
    local = dt.datetime.fromtimestamp(ts_ms / 1000, tz)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return (9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60)


def price_of(model: str) -> tuple[dict[str, float], bool]:
    key = (model or "").strip().lower()
    if key in PRICES:
        return PRICES[key], True
    return PRICES[FALLBACK_MODEL], False


def estimate_cost(
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    ts_ms: int,
) -> tuple[float, bool, bool]:
    """返回 (金额, 是否高峰, 模型是否在价目表里)。"""
    table, known = price_of(model)
    peak = is_peak(ts_ms) and not table.get("no_peak")
    mult = PEAK_MULTIPLIER if peak else 1.0

    miss = max(0, prompt_tokens - cached_tokens)
    cost = (
        miss / 1_000_000 * table["cache_miss"]
        + cached_tokens / 1_000_000 * table["cache_hit"]
        + completion_tokens / 1_000_000 * table["output"]
    ) * mult
    return round(cost, 6), peak, known