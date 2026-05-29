"""Tier 2 tests — technical indicator primitives in auto_validator.

EMA / RSI / trend / structure-break are the foundation every higher-level
gate is built on. They are pure functions over close/high/low lists, so they
get exact numeric coverage against hand-computed and reference values.
"""
import pytest

import auto_validator as av


def closes(values):
    return [{"close": float(v), "high": float(v), "low": float(v)} for v in values]


# ── _ema ─────────────────────────────────────────────────────────

def test_ema_too_few_values_all_none():
    assert av._ema([1, 2], period=5) == [None, None]


def test_ema_known_values():
    # seed = mean(first 3) = 2.0; k = 2/4 = 0.5.
    out = av._ema([1, 2, 3, 4, 5], period=3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5
    assert out[4] == pytest.approx(4.0)  # 5*0.5 + 3*0.5


def test_ema_constant_series_is_flat():
    out = av._ema([5, 5, 5, 5], period=2)
    assert out[1:] == [pytest.approx(5.0)] * 3


# ── _rsi ─────────────────────────────────────────────────────────

def test_rsi_insufficient_data_defaults_to_50():
    assert av._rsi(closes(range(10)), period=14) == 50.0


def test_rsi_all_gains_is_100():
    assert av._rsi(closes(range(1, 17)), period=14) == 100.0


def test_rsi_all_losses_is_0():
    assert av._rsi(closes(range(20, 4, -1)), period=14) == 0.0


# ── _trend_from_candles ──────────────────────────────────────────

def test_trend_unknown_when_short():
    assert av._trend_from_candles(closes(range(10))) == "UNKNOWN"


def test_trend_bullish_on_uptrend():
    assert av._trend_from_candles(closes(range(100, 160))) == "BULLISH"


def test_trend_bearish_on_downtrend():
    assert av._trend_from_candles(closes(range(160, 100, -1))) == "BEARISH"


# ── _choch_bos ───────────────────────────────────────────────────

def test_choch_bos_short_returns_none():
    r = av._choch_bos(closes(range(5)))
    assert r == {"choch": False, "bos": False, "direction": "NONE"}


def test_bos_up_detected():
    candles = ([{"high": 10, "low": 5, "close": 8}] * 15
               + [{"high": 12, "low": 9, "close": 11}] * 5)
    r = av._choch_bos(candles)
    assert r["bos"] is True
    assert r["direction"] == "BULLISH"
    assert r["choch"] is False
