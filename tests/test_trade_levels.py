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
