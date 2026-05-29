"""Tier 2 tests — SMC detectors and quality scoring in the main bot module.

These pure functions read a list of OHLC candle dicts and emit structure /
fair-value-gap / quality signals that feed the confluence score. Expected
values for the structure detector were captured from the implementation on a
deliberately clear zig-zag, then locked in as a regression guard.
"""
import crypto_screening_bot_v13 as bot


def mk(high, low, close=None):
    return {"high": high, "low": low,
            "close": close if close is not None else (high + low) / 2,
            "open": (high + low) / 2, "volume": 1.0, "time": 0}


# ── detect_market_structure ──────────────────────────────────────

def test_market_structure_guard_short_input():
    r = bot.detect_market_structure([mk(1, 1)] * 5)
    assert r == {"trend": "UNKNOWN", "choch": False, "bos": False}


def test_market_structure_bullish_with_bos():
    seq = [
        (100, 95), (102, 96), (104, 97), (106, 98), (108, 99),
        (112, 100), (110, 99), (108, 98), (106, 97), (104, 96),
        (101, 94), (100, 93), (99, 92), (100, 93), (101, 94),
        (104, 97), (108, 99), (112, 101), (116, 103), (120, 105),
        (124, 108), (122, 107), (120, 106), (118, 105), (116, 104),
        (113, 101), (112, 100), (111, 99), (112, 100), (113, 101),
        (118, 104), (124, 108), (130, 112),
    ]
    candles = [mk(h, l) for h, l in seq]
    candles[-1]["close"] = 130
    r = bot.detect_market_structure(candles)
    assert r["trend"] == "BULLISH"
    assert r["bos"] is True
    assert r["choch"] is False


# ── detect_fvg ───────────────────────────────────────────────────

def test_fvg_bullish_gap():
    candles = [mk(100, 99, 99), mk(101, 100, 100), mk(112, 105, 108)]
    r = bot.detect_fvg(candles)
    assert r["fvg_type"] == "BULLISH"
    assert r["bearish_fvg"] is None
    assert r["bullish_fvg"]["gap_pct"] == 5.0
    assert r["bullish_fvg"]["top"] == 105
    assert r["bullish_fvg"]["bottom"] == 100


def test_fvg_bearish_gap():
    candles = [mk(110, 108, 109), mk(105, 103, 104), mk(100, 98, 99)]
    r = bot.detect_fvg(candles)
    assert r["fvg_type"] == "BEARISH"
    assert r["bullish_fvg"] is None
    assert r["bearish_fvg"]["gap_pct"] == 8.0


def test_fvg_none_when_overlapping():
    candles = [mk(100, 98, 99), mk(101, 99, 100), mk(102, 100, 101)]
    assert bot.detect_fvg(candles)["fvg_type"] == "NONE"


# ── calculate_quality_score ──────────────────────────────────────

def test_quality_score_high_quality_coin():
    coin = {"price_change_percentage_24h": 5,
            "total_volume": 120_000_000, "market_cap": 3_000_000_000}
    assert bot.calculate_quality_score(coin, 80) == 8.13


def test_quality_score_capped_at_10():
    coin = {"price_change_percentage_24h": 5,
            "total_volume": 500_000_000, "market_cap": 10_000_000_000}
    assert bot.calculate_quality_score(coin, 150) <= 10.0


def test_quality_score_minimal_coin():
    # pc=0 -> +0.5, tiny volume -> +0.3, no volume increase -> nothing.
    assert bot.calculate_quality_score({}, 0) == 0.8
