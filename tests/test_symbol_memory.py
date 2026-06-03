"""Tier 3 tests — per-symbol stats and auto-blacklist.

`_compute_stats` aggregates a symbol's trade history (win/SL rate, per-type and
per-entry-mode breakdowns) and `_check_blacklist` decides when a symbol is too
toxic to signal. Both are pure given their inputs; file I/O is monkeypatched.
"""
import pytest

import symbol_memory as sm


def trade(pnl, outcome, signal_type="SCALP", entry_mode="MOMENTUM_NOW",
          hold=30, score=70):
    return {"pnl_pct": pnl, "outcome": outcome, "signal_type": signal_type,
            "entry_mode": entry_mode, "hold_minutes": hold, "score": score}


# ── _compute_stats ───────────────────────────────────────────────

def test_compute_stats_empty():
    assert sm._compute_stats([]) == {}


def test_compute_stats_mixed_history():
    trades = [
        trade(5, "TP1_HIT", "SCALP", "MOMENTUM_NOW", 30, 70),
        trade(-3, "SL_HIT", "SCALP", "RETEST_WAIT", 60, 60),
        trade(2, "TP1_HIT", "SWING", "MOMENTUM_NOW", 120, 80),
    ]
    s = sm._compute_stats(trades)
    assert s["total_trades"] == 3
    assert s["win_rate_pct"] == 66.7
    assert s["sl_rate_pct"] == 33.3
    assert s["avg_pnl_pct"] == 1.33
    assert s["avg_hold_min"] == 70
    assert s["avg_score"] == 70.0
    assert s["tp_hits"] == 2
    assert s["sl_hits"] == 1
    assert s["by_type"]["SCALP"] == {"total": 2, "wins": 1, "sl_hits": 1}
    assert s["by_type"]["SWING"] == {"total": 1, "wins": 1, "sl_hits": 0}
    assert s["momentum_wr"] == 100.0
    assert s["retest_wr"] == 0.0


def test_compute_stats_entry_mode_none_when_absent():
    s = sm._compute_stats([trade(5, "TP1_HIT", entry_mode="")])
    assert s["momentum_wr"] is None
    assert s["retest_wr"] is None


# ── _check_blacklist ─────────────────────────────────────────────

def _sym(trades):
    return {"SOL": {"symbol": "SOL", "trades": trades,
                    "stats": sm._compute_stats(trades),
                    "blacklisted": False, "blacklist_reason": ""}}


def test_blacklist_requires_minimum_trades():
    data = _sym([trade(-1, "SL_HIT")] * 3)  # below BLACKLIST_MIN_TRADES (5)
    sm._check_blacklist(data, "SOL")
    assert data["SOL"]["blacklisted"] is False


def test_blacklist_triggers_on_high_sl_rate():
    data = _sym([trade(-1, "SL_HIT")] * 6)  # 100% SL over recent trades
    sm._check_blacklist(data, "SOL")
    assert data["SOL"]["blacklisted"] is True
    assert "SL rate" in data["SOL"]["blacklist_reason"]


def test_blacklist_not_triggered_on_healthy_symbol():
    trades = [trade(5, "TP1_HIT")] * 4 + [trade(-2, "SL_HIT")]  # 20% SL
    data = _sym(trades)
    sm._check_blacklist(data, "SOL")
    assert data["SOL"]["blacklisted"] is False


# ── is_blacklisted ───────────────────────────────────────────────

def test_is_blacklisted_strips_usdt(monkeypatch):
    monkeypatch.setattr(sm, "_load", lambda: {
        "SOL": {"blacklisted": True, "blacklist_reason": "toxic"}})
    flagged, reason = sm.is_blacklisted("SOLUSDT")
    assert flagged is True
    assert reason == "toxic"


def test_is_blacklisted_unknown_symbol(monkeypatch):
    monkeypatch.setattr(sm, "_load", lambda: {})
    assert sm.is_blacklisted("DOGEUSDT") == (False, "")


# ── get_symbol_memory (AI-prompt shape) ──────────────────────────

def test_get_symbol_memory_empty_for_unknown(monkeypatch):
    monkeypatch.setattr(sm, "_load", lambda: {})
    assert sm.get_symbol_memory("BTCUSDT") == {}


def test_get_symbol_memory_roundtrip(monkeypatch):
    # record_symbol_outcome → get_symbol_memory harus kasih shape yang dipakai
    # deepseek_analyze_coin (win_rate, total_trades, best_signal_type, lessons).
    store = {}
    monkeypatch.setattr(sm, "_load", lambda: store)
    monkeypatch.setattr(sm, "_save", lambda d: None)   # mutasi sudah in-place
    sm.record_symbol_outcome("SOLUSDT", "SCALP", "LONG", "TP_HIT", 3.0, 70, hold_minutes=30)
    sm.record_symbol_outcome("SOLUSDT", "SCALP", "LONG", "SL_HIT", -2.0, 60, hold_minutes=40)
    m = sm.get_symbol_memory("SOLUSDT")
    assert m["total_trades"] == 2
    assert m["win_rate"] == 50.0
    assert m["best_signal_type"] == "SCALP"
    assert isinstance(m["lessons"], list)
