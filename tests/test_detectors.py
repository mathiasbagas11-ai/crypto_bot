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


# ── v16: reversal patterns (V-Shape + Quasimodo) ──────────────────
import reversal_patterns as rp


def _c(o, h, l, c, v=1.0):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "time": 0}


def _seg(a, b, n):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def test_vshape_bullish_confirm():
    candles = [_c(100, 101, 99, 100, 1.0) for _ in range(8)]
    for px in [98, 96, 94, 92, 90]:
        candles.append(_c(px + 1, px + 1.5, px - 1, px, 1.5))
    candles.append(_c(90, 91, 88, 91, 3.0))          # pivot + rejection wick
    for px in [93, 94, 96, 97]:
        candles.append(_c(px - 1, px + 0.5, px - 1.2, px, 2.0))
    r = rp.detect_v_shape(candles)
    assert r["type"] == "V_SHAPE_BULLISH"
    assert r["direction"] == "LONG"
    assert r["stage"] in ("EARLY", "CONFIRM")
    assert r["score"] > 50


def test_vshape_bearish():
    candles = [_c(100, 101, 99, 100, 1.0) for _ in range(8)]
    for px in [102, 104, 106, 108, 110]:
        candles.append(_c(px - 1, px + 1, px - 1.5, px, 1.5))
    candles.append(_c(110, 112, 109, 109, 3.0))      # pivot high + upper wick
    for px in [107, 106, 104, 103]:
        candles.append(_c(px + 1, px + 1.2, px - 0.5, px, 2.0))
    r = rp.detect_v_shape(candles)
    assert r["type"] == "V_SHAPE_BEARISH"
    assert r["direction"] == "SHORT"


def test_vshape_none_on_uptrend():
    up = [_c(100 + i, 101 + i, 99 + i, 100 + i, 1.0) for i in range(30)]
    assert rp.detect_v_shape(up)["type"] == "NONE"


def test_vshape_guard_short_input():
    assert rp.detect_v_shape([])["type"] == "NONE"
    assert rp.detect_v_shape([_c(1, 1, 1, 1)] * 5)["type"] == "NONE"


def test_qm_bullish_confirm_in_rs_zone():
    # LS low ~95 → LS-high ~105 → head sweep ~92 → break >105 (CHoCH) → retrace into RS
    path = _seg(100, 95, 6) + _seg(95, 105, 7) + _seg(105, 92, 7) + _seg(92, 107, 8) + _seg(107, 94.4, 6)
    qc = [_c(p, p + 0.6, p - 0.6, p, 1.0) for p in path]
    r = rp.detect_qm_pattern(qc)
    assert r["type"] == "QM_BULLISH"
    assert r["direction"] == "LONG"
    assert r["meta"]["choch"] is True
    assert r["stage"] == "CONFIRM"
    assert r["zone"]["bottom"] < r["zone"]["top"]


def test_qm_bearish():
    path = _seg(100, 105, 6) + _seg(105, 95, 7) + _seg(95, 108, 7) + _seg(108, 93, 8) + _seg(93, 104.7, 6)
    qc = [_c(p, p + 0.6, p - 0.6, p, 1.0) for p in path]
    r = rp.detect_qm_pattern(qc)
    assert r["type"] == "QM_BEARISH"
    assert r["direction"] == "SHORT"
    assert r["meta"]["choch"] is True


def test_qm_none_on_uptrend():
    up = [_c(100 + i, 101 + i, 99 + i, 100 + i, 1.0) for i in range(40)]
    assert rp.detect_qm_pattern(up)["type"] == "NONE"


def test_qm_guard_short_input():
    assert rp.detect_qm_pattern([_c(1, 1, 1, 1)] * 5)["type"] == "NONE"
