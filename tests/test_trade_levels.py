"""Tier 2 tests — trade-level sanitizer.

`_sanitize_trade_levels` adalah guard terakhir sebelum sinyal dikirim: ia
menjamin TP/SL ada di sisi yang benar relatif entry (LONG: sl<entry<tp1<tp2,
SHORT: tp2<tp1<entry<sl). Tanpa ini, override AI / level struktural salah-sisi
bisa menghasilkan sinyal "TP di bawah entry untuk LONG" yang langsung kena TP
tapi rugi (ini yang mencemari ~52% outcome historis).
"""
import pytest

import crypto_screening_bot_v13 as bot

san = bot._sanitize_trade_levels


def test_long_wrong_side_tp_is_repaired():
    # Kasus nyata dari signal_outcomes.json: BNB LONG, TP di bawah entry.
    t = {"entry": 671.87, "tp1": 663.42, "tp2": 663.42, "sl": 647.81, "tp": 663.42}
    r = san(t, "LONG")
    assert r["tp1"] > r["entry"], "TP1 harus di atas entry untuk LONG"
    assert r["tp2"] > r["tp1"], "TP2 harus lebih jauh dari TP1"
    assert r["sl"] < r["entry"]
    assert r["tp"] == r["tp1"]            # alias ikut terupdate
    # risk = 24.06 → tp1 = entry + 2R
    assert r["tp1"] == pytest.approx(671.87 + 2 * (671.87 - 647.81), abs=1e-2)


def test_short_wrong_side_tp_is_repaired():
    t = {"entry": 100.0, "tp1": 105.0, "tp2": 108.0, "sl": 103.0, "tp": 105.0}
    r = san(t, "SHORT")
    assert r["tp1"] < r["entry"]
    assert r["tp2"] < r["tp1"]
    assert r["sl"] > r["entry"]


def test_sane_long_unchanged():
    t = {"entry": 100.0, "tp1": 104.0, "tp2": 107.0, "sl": 98.0, "tp": 104.0}
    r = san(dict(t), "LONG")
    assert r["tp1"] == 104.0 and r["tp2"] == 107.0 and r["sl"] == 98.0


def test_sl_wrong_side_long_repaired_and_tp_recomputed():
    # SL di atas entry untuk LONG → SL diperbaiki, TP ikut dihitung ulang dari R:R.
    t = {"entry": 100.0, "tp1": 90.0, "tp2": 80.0, "sl": 105.0, "tp": 90.0}
    r = san(t, "LONG")
    assert r["sl"] < r["entry"]
    assert r["tp1"] > r["entry"] < r["tp2"]
    assert r["tp2"] > r["tp1"]


def test_invalid_entry_left_untouched():
    t = {"entry": 0, "tp1": 5, "sl": 1}
    r = san(dict(t), "LONG")
    assert r["tp1"] == 5            # tidak diutak-atik kalau entry tak valid


def test_tp_r_multiples_recomputed():
    t = {"entry": 100.0, "tp1": 50.0, "tp2": 40.0, "sl": 95.0, "tp": 50.0}
    r = san(t, "LONG")
    # risk = 5 → tp1 = 110 (2R), tp2 = 117.5 (3.5R)
    assert r["tp1_r"] == pytest.approx(2.0)
    assert r["tp2_r"] == pytest.approx(3.5)


# ── _compute_tp_profile (skema TP adaptif kondisi market) ─────────

def _reg(regime, adx):
    return {"market_regime": {"regime": regime, "adx": adx}}


def test_tp_profile_aggressive_on_strong_aligned_trend():
    p = bot._compute_tp_profile(_reg("BULLISH_TREND", 32), _reg("BULLISH_TREND", 30), "LONG")
    assert p["label"] == "AGGRESSIVE"
    assert (p["tp1_mult"], p["tp2_mult"]) == (2.0, 3.5)


def test_tp_profile_conservative_on_ranging():
    p = bot._compute_tp_profile(_reg("RANGING", 12), _reg("RANGING", 10), "LONG")
    assert p["label"] == "CONSERVATIVE"
    assert p["tp1_mult"] < 2.0 and p["tp2_mult"] < 3.5


def test_tp_profile_balanced_default():
    p = bot._compute_tp_profile(_reg("WEAK_TREND", 22), _reg("WEAK_TREND", 21), "LONG")
    assert p["label"] == "BALANCED"


def test_tp_profile_trend_against_direction_not_aggressive():
    # Trend bullish kuat TAPI kita SHORT → tidak boleh agresif.
    p = bot._compute_tp_profile(_reg("BULLISH_TREND", 35), _reg("BULLISH_TREND", 33), "SHORT")
    assert p["label"] != "AGGRESSIVE"


def test_conservative_tp_is_closer_than_aggressive():
    cons = bot.calculate_tp1_tp2(100.0, 95.0, "LONG", tp1_mult=1.2, tp2_mult=2.0)
    aggr = bot.calculate_tp1_tp2(100.0, 95.0, "LONG", tp1_mult=2.0, tp2_mult=3.5)
    assert cons["tp1"] < aggr["tp1"]
    assert cons["tp2"] < aggr["tp2"]
    assert cons["tp1"] == pytest.approx(106.0)   # 100 + 5*1.2


# ── _entry_action_reco (NOW vs WAIT) ─────────────────────────────

def test_action_reco_momentum_now():
    assert "NOW" in bot._entry_action_reco("LONG", entry_mode="MOMENTUM_NOW")


def test_action_reco_retest_wait():
    assert "WAIT" in bot._entry_action_reco("SHORT", entry_mode="RETEST_WAIT")


def test_action_reco_extended_waits():
    r = bot._entry_action_reco("LONG", {"level": "GOOD", "entry_extended": True},
                               {"momentum_label": "BULL_MOMENTUM"})
    assert "WAIT" in r


def test_action_reco_heuristic_now_on_aligned_momentum():
    r = bot._entry_action_reco("LONG", {"level": "GOOD"}, {"momentum_label": "STRONG_BULL_MOMENTUM"})
    assert "NOW" in r
