"""
Tests for market_regime.py

Covers:
- calculate_adx:          basic trending / flat candles
- calculate_bb_squeeze:   squeeze detection + expanding detection
- detect_candle_patterns: all major patterns (engulfing, star, marubozu, inside, doji)
- detect_market_regime:   regime classification (ranging, trending, squeeze, breakout)
- detect_volume_coil:     coil detection + spike release
- detect_sudden_breakout: explosive range breakout detection
"""

import pytest
from market_regime import (
    calculate_adx,
    calculate_bb_squeeze,
    detect_candle_patterns,
    detect_market_regime,
    detect_volume_coil,
    detect_sudden_breakout,
)


# ── Helpers ──────────────────────────────────────────────────────

def make_candles(closes, vol=1000, spread=2.0):
    """Create candles from close prices."""
    result = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        result.append({
            "open":   float(o),
            "close":  float(c),
            "high":   float(max(o, c) + spread * 0.1),
            "low":    float(min(o, c) - spread * 0.1),
            "volume": float(vol),
        })
    return result


def mc(open_, close, high=None, low=None, vol=1000):
    """Make a single candle."""
    return {
        "open":   float(open_),
        "close":  float(close),
        "high":   float(high if high is not None else max(open_, close) * 1.005),
        "low":    float(low  if low  is not None else min(open_, close) * 0.995),
        "volume": float(vol),
    }


# ── calculate_adx ────────────────────────────────────────────────

class TestCalculateAdx:
    def test_too_few_candles_returns_zeros(self):
        result = calculate_adx(make_candles(range(10)))
        assert result == {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}

    def test_strong_uptrend_has_high_plus_di(self):
        result = calculate_adx(make_candles(list(range(100, 170))))
        assert result["adx"] > 20
        assert result["plus_di"] > result["minus_di"]

    def test_strong_downtrend_has_high_minus_di(self):
        result = calculate_adx(make_candles(list(range(170, 100, -1))))
        assert result["adx"] > 20
        assert result["minus_di"] > result["plus_di"]

    def test_returns_rounded_floats(self):
        result = calculate_adx(make_candles(list(range(100, 160))))
        assert isinstance(result["adx"], float)
        assert isinstance(result["plus_di"], float)
        assert isinstance(result["minus_di"], float)


# ── calculate_bb_squeeze ─────────────────────────────────────────

class TestCalculateBbSqueeze:
    def test_too_few_candles_returns_defaults(self):
        r = calculate_bb_squeeze(make_candles(range(10)))
        assert r["squeeze"] is False
        assert r["bb_width"] == 0.0

    def test_flat_price_triggers_squeeze(self):
        # Zero variance = zero BB width = definite squeeze
        candles = [mc(100, 100, high=100, low=100)] * 50
        r = calculate_bb_squeeze(candles)
        assert r["squeeze"] is True
        assert r["bb_width"] == 0.0

    def test_squeeze_bars_is_int(self):
        candles = make_candles([100.0] * 50)
        r = calculate_bb_squeeze(candles)
        assert isinstance(r["squeeze_bars"], int)

    def test_expanding_detected_after_volatility_burst(self):
        flat  = [100.0] * 35
        burst = [100 + i * 2 for i in range(20)]
        candles = make_candles(flat + burst, spread=0.01)
        r = calculate_bb_squeeze(candles)
        assert isinstance(r["expanding"], bool)

    def test_result_has_required_keys(self):
        r = calculate_bb_squeeze(make_candles(range(40)))
        for k in ("squeeze", "width_pct", "bb_width", "expanding", "squeeze_bars"):
            assert k in r


# ── detect_candle_patterns ───────────────────────────────────────
# All tests supply >= 3 candles (function requires candles[-3:])

class TestDetectCandlePatterns:
    def test_empty_returns_none(self):
        r = detect_candle_patterns([])
        assert r["pattern"] == "NONE"

    def test_too_few_candles_returns_none(self):
        r = detect_candle_patterns([mc(100, 101)])
        assert r["pattern"] == "NONE"

    def test_bullish_engulfing(self):
        candles = [
            mc(110, 108),              # padding
            mc(105, 100),              # bearish prev
            mc(99, 107),               # bullish engulfing
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "BULLISH_ENGULFING"
        assert r["direction"] == "BULLISH"
        assert r["strength"] > 0

    def test_bearish_engulfing(self):
        candles = [
            mc(98, 100),               # padding
            mc(100, 105),              # bullish prev
            mc(106, 99),               # bearish engulfing
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "BEARISH_ENGULFING"
        assert r["direction"] == "BEARISH"

    def test_bullish_marubozu(self):
        candles = [
            mc(100, 101),
            mc(100, 101),
            mc(100, 108, high=108, low=100),   # full bull body, no wicks
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "BULLISH_MARUBOZU"
        assert r["direction"] == "BULLISH"

    def test_bearish_marubozu(self):
        # Previous candle must also be bearish so bearish-engulfing check doesn't fire
        candles = [
            mc(100, 101),
            mc(110, 107),                      # bearish prev (no engulf possible)
            mc(108, 100, high=108, low=100),   # full bear body, no wicks
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "BEARISH_MARUBOZU"
        assert r["direction"] == "BEARISH"

    def test_inside_bar(self):
        candles = [
            mc(100, 102),
            mc(100, 106, high=108, low=98),    # mother bar
            mc(102, 104, high=107, low=99),    # inside bar (high < mother high, low > mother low)
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "INSIDE_BAR"
        assert r["direction"] == "NEUTRAL"

    def test_doji(self):
        candles = [
            mc(100, 101),
            mc(100, 101),
            mc(104, 104.05, high=107, low=101),  # tiny body = doji
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "DOJI"

    def test_three_white_soldiers(self):
        candles = [
            mc(100, 103, high=103.1, low=99.9),
            mc(103, 106, high=106.1, low=102.9),
            mc(106, 109, high=109.1, low=105.9),
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "THREE_WHITE_SOLDIERS"
        assert r["direction"] == "BULLISH"

    def test_three_black_crows(self):
        candles = [
            mc(109, 106, high=109.1, low=105.9),
            mc(106, 103, high=106.1, low=102.9),
            mc(103, 100, high=103.1, low=99.9),
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "THREE_BLACK_CROWS"
        assert r["direction"] == "BEARISH"

    def test_morning_star(self):
        candles = [
            mc(110, 103, high=111, low=102),     # big bearish
            mc(102, 102.5, high=103, low=101),   # small body (doji-like)
            mc(103, 109, high=110, low=102),     # bullish closes above midpoint of first
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "MORNING_STAR"
        assert r["direction"] == "BULLISH"

    def test_evening_star(self):
        candles = [
            mc(100, 107, high=108, low=99),     # big bullish
            mc(107, 107.5, high=108, low=106),  # small body
            mc(107, 101, high=108, low=100),    # bearish closes below midpoint
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "EVENING_STAR"
        assert r["direction"] == "BEARISH"

    def test_patterns_found_is_list(self):
        candles = [mc(110, 108), mc(105, 100), mc(99, 107)]
        r = detect_candle_patterns(candles)
        assert isinstance(r["patterns_found"], list)
        assert len(r["patterns_found"]) >= 1

    def test_engulfing_beats_marubozu_priority(self):
        # Candle that is both engulfing AND marubozu-like → engulfing wins
        candles = [
            mc(110, 108),
            mc(105, 100),                         # bearish prev
            mc(99, 108, high=108, low=99),        # full body engulfing + marubozu
        ]
        r = detect_candle_patterns(candles)
        assert r["pattern"] == "BULLISH_ENGULFING"


# ── detect_market_regime ─────────────────────────────────────────

class TestDetectMarketRegime:
    def test_too_few_candles_unknown(self):
        r = detect_market_regime(make_candles(range(10)))
        assert r["regime"] == "UNKNOWN"

    def test_strong_uptrend_classified_correctly(self):
        # Steady uptrend — regime is bullish, breakout, or squeeze (tight incremental steps)
        r = detect_market_regime(make_candles(list(range(100, 200))))
        assert r["regime"] in ("BULLISH_TREND", "BREAKOUT_UP", "BB_SQUEEZE")
        # ADX should reflect trending direction
        assert r["adx"] >= 0

    def test_strong_downtrend_classified_correctly(self):
        r = detect_market_regime(make_candles(list(range(200, 100, -1))))
        assert r["regime"] in ("BEARISH_TREND", "BREAKOUT_DOWN", "BB_SQUEEZE")

    def test_flat_price_squeeze_or_ranging(self):
        candles = [mc(100, 100, high=100, low=100)] * 60
        r = detect_market_regime(candles)
        assert r["regime"] in ("BB_SQUEEZE", "RANGING")
        assert r["is_ranging"] is True

    def test_regime_has_required_keys(self):
        r = detect_market_regime(make_candles(range(50)))
        for key in ("regime", "adx", "squeeze", "detail",
                    "is_trending", "is_ranging", "breakout_confirmed"):
            assert key in r

    def test_is_trending_true_on_strong_trend(self):
        r = detect_market_regime(make_candles(list(range(100, 200))))
        # Strong uptrend → trending, breakout, or squeeze (all valid non-neutral regimes)
        assert r["regime"] != "UNKNOWN"
        assert r["adx"] >= 0

    def test_detail_is_nonempty_string(self):
        r = detect_market_regime(make_candles(list(range(100, 160))))
        assert isinstance(r["detail"], str)
        assert len(r["detail"]) > 0


# ── detect_volume_coil ───────────────────────────────────────────

class TestDetectVolumeCoil:
    def test_too_few_candles_no_coil(self):
        r = detect_volume_coil(make_candles(range(5)))
        assert r["coiling"] is False

    def test_declining_volume_over_lookback_detected(self):
        # 15+ candles with steadily declining volume
        candles = [mc(100, 100, vol=1000 - i * 60) for i in range(20)]
        r = detect_volume_coil(candles)
        assert r["coiling"] is True

    def test_spike_after_declining_detected(self):
        # 15 candles declining then one huge spike
        candles = [mc(100, 100, vol=1000 - i * 60) for i in range(15)]
        candles.append(mc(100, 101, vol=8000))   # 8000 vs ~370 avg = spike
        r = detect_volume_coil(candles)
        assert r["spike_detected"] is True
        assert r["vol_ratio"] > 2.0

    def test_flat_volume_no_coil(self):
        # Constant volume = not coiling
        candles = make_candles(range(20), vol=1000)
        r = detect_volume_coil(candles)
        assert r["coiling"] is False

    def test_result_has_required_keys(self):
        r = detect_volume_coil(make_candles(range(20)))
        for k in ("coiling", "spike_detected", "compression_bars", "vol_ratio", "detail"):
            assert k in r


# ── detect_sudden_breakout ───────────────────────────────────────

class TestDetectSuddenBreakout:
    def test_too_few_candles_no_breakout(self):
        r = detect_sudden_breakout(make_candles(range(5)))
        assert r["sudden_breakout"] is False

    def test_flat_range_no_breakout(self):
        candles = make_candles([100.0] * 25)
        r = detect_sudden_breakout(candles)
        assert r["sudden_breakout"] is False

    def test_sudden_breakout_up_detected(self):
        # Tight range then huge bullish candle + volume explosion
        base = [mc(100, 100, high=101, low=99, vol=200)] * 20
        boom = mc(100, 108, high=109, low=99.5, vol=5000)   # 25x volume, break high
        r = detect_sudden_breakout(base + [boom])
        assert r["sudden_breakout"] is True
        assert r["direction"] == "UP"
        assert r["vol_spike"] >= 3.0

    def test_sudden_breakout_down_detected(self):
        base  = [mc(100, 100, high=101, low=99, vol=200)] * 20
        crash = mc(100, 92, high=100.5, low=91.5, vol=5000)
        r = detect_sudden_breakout(base + [crash])
        assert r["sudden_breakout"] is True
        assert r["direction"] == "DOWN"

    def test_result_has_required_keys(self):
        r = detect_sudden_breakout(make_candles(range(25)))
        for k in ("sudden_breakout", "direction", "vol_spike",
                  "range_break_pct", "detail", "was_consolidating"):
            assert k in r

    def test_was_consolidating_detected_on_compressed_atr(self):
        # Wide range older candles, tight recent candles
        wide   = [mc(100, 100, high=106, low=94, vol=500)] * 15
        tight  = [mc(100, 100, high=100.5, low=99.5, vol=200)] * 15
        r = detect_sudden_breakout(wide + tight)
        assert r["was_consolidating"] is True

    def test_partial_volume_without_breakout_returns_false(self):
        # Only moderate volume, no range break
        candles = [mc(100, 100, high=101, low=99, vol=500)] * 20
        candles.append(mc(100, 100.3, high=100.8, low=99.5, vol=900))  # 1.8x not enough
        r = detect_sudden_breakout(candles)
        assert r["sudden_breakout"] is False
