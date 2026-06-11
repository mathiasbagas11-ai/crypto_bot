"""Tests untuk DAILY LEARNING SUMMARY (analyze_signal_outcomes_daily).

Dua hal yang dijaga di sini:
1. Rolling window: bagian "X JAM TERAKHIR" hanya menghitung outcome yang
   resolved dalam window (default 24h) → summary berubah tiap hari, bukan
   total all-time yang statis.
2. EXPIRED_WIN dihitung sebagai menang (dulu cuma TP_HIT → win rate understated).
"""
import json
import os
from datetime import datetime, timezone, timedelta

import learning_engine as le


def _write_outcomes(tmp_path, monkeypatch, entries):
    p = tmp_path / "signal_outcomes.json"
    p.write_text(json.dumps(entries))
    monkeypatch.chdir(tmp_path)
    # stub AI biar nggak panggil network
    monkeypatch.setattr(le, "_call_deepseek_analysis", lambda a, s: "(stub)")


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_recent_window_only_counts_in_window(tmp_path, monkeypatch):
    entries = [
        {"signal_type": "CONFIRMED", "status": "TP_HIT", "pnl_pct": 2.0,
         "resolved_at": _iso(2)},    # dalam 24h
        {"signal_type": "CONFIRMED", "status": "SL_HIT", "pnl_pct": -1.0,
         "resolved_at": _iso(100)},  # di luar 24h
    ]
    _write_outcomes(tmp_path, monkeypatch, entries)
    monkeypatch.setenv("LEARNING_SUMMARY_WINDOW_H", "24")

    msg = le.analyze_signal_outcomes_daily()
    assert "24 JAM TERAKHIR" in msg
    # window cuma 1 trade (yang 2 jam lalu), all-time 2 trade
    assert "1 trade" in msg
    assert "ALL-TIME" in msg
    assert "2 trade" in msg


def test_empty_window_shows_placeholder_and_alltime(tmp_path, monkeypatch):
    entries = [
        {"signal_type": "GATED_SIGNAL", "status": "TP_HIT", "pnl_pct": 1.0,
         "resolved_at": _iso(200)},
    ]
    _write_outcomes(tmp_path, monkeypatch, entries)
    monkeypatch.setenv("LEARNING_SUMMARY_WINDOW_H", "24")

    msg = le.analyze_signal_outcomes_daily()
    assert "Belum ada sinyal yang resolve" in msg
    # all-time tetap kebawa
    assert "GATED_SIGNAL" in msg


def test_expired_win_counts_as_win(tmp_path, monkeypatch):
    entries = [
        {"signal_type": "GATED_SIGNAL", "status": "EXPIRED_WIN", "pnl_pct": 1.8,
         "resolved_at": _iso(1)},
        {"signal_type": "GATED_SIGNAL", "status": "EXPIRED_WIN", "pnl_pct": 2.2,
         "resolved_at": _iso(1)},
    ]
    _write_outcomes(tmp_path, monkeypatch, entries)
    monkeypatch.setenv("LEARNING_SUMMARY_WINDOW_H", "24")

    msg = le.analyze_signal_outcomes_daily()
    # 2 EXPIRED_WIN → WR 100%
    assert "WR 100%" in msg


def test_expired_loss_counts_as_loss(tmp_path, monkeypatch):
    entries = [
        {"signal_type": "CONFIRMED", "status": "EXPIRED_WIN",  "pnl_pct": 1.0,
         "resolved_at": _iso(1)},
        {"signal_type": "CONFIRMED", "status": "EXPIRED_LOSS", "pnl_pct": -1.0,
         "resolved_at": _iso(1)},
    ]
    _write_outcomes(tmp_path, monkeypatch, entries)
    monkeypatch.setenv("LEARNING_SUMMARY_WINDOW_H", "24")

    msg = le.analyze_signal_outcomes_daily()
    # 1 win / 2 trade → WR 50%
    assert "WR 50%" in msg
