import datetime as dt

from qqbridge.core.pricing import estimate_cost, is_peak

TZ = dt.timezone(dt.timedelta(hours=8))


def ts_of(y, m, d, hh, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp() * 1000)


def test_peak_weekday_morning():
    # 2026-11-04 是周三
    assert is_peak(ts_of(2026, 11, 4, 10, 0)) is True
    assert is_peak(ts_of(2026, 11, 4, 13, 0)) is False
    assert is_peak(ts_of(2026, 11, 4, 15, 0)) is True


def test_weekend_off_peak():
    assert is_peak(ts_of(2026, 11, 7, 10, 0)) is False


def test_cost_uses_peak_multiplier():
    t = ts_of(2026, 11, 4, 10, 0)  # 高峰
    cost, peak, known = estimate_cost("deepseek-chat", 1_000_000, 0, 0, t)
    assert peak is True and known is True
    assert cost == 2.0  # 1 元 * 2


def test_cost_off_peak():
    t = ts_of(2026, 11, 4, 23, 0)
    cost, peak, _ = estimate_cost("deepseek-chat", 1_000_000, 0, 0, t)
    assert peak is False
    assert cost == 1.0


def test_cached_tokens_are_cheaper():
    t = ts_of(2026, 11, 4, 23, 0)
    miss, _, _ = estimate_cost("deepseek-chat", 1_000_000, 0, 0, t)
    hit, _, _ = estimate_cost("deepseek-chat", 1_000_000, 1_000_000, 0, t)
    assert hit < miss


def test_unknown_model_flagged_estimated():
    t = ts_of(2026, 11, 4, 23, 0)
    _, _, known = estimate_cost("some-unknown-model", 100, 0, 100, t)
    assert known is False