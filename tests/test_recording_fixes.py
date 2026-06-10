"""Tier 3 tests — perbaikan recording integrity.

Dua bug yang diperbaiki:
  1. record_pending_signal() harus menolak TP/SL di sisi yang salah relatif
     entry (mencegah outcome "TP_HIT" palsu).
  2. _save_confirmed_signal() harus pulih dari history JSON yang corrupt
     (anti death-spiral: corrupt sekali → recording mati selamanya).
"""
import glob
import json
import os

import confirmed_signal as cs
import signal_tracker as st


# ── Bug #2: TP/SL side guard di record_pending_signal ────────────
def _captured(monkeypatch):
    saved = {"rows": None}
    monkeypatch.setattr(st, "_load_pending", lambda: [])
    monkeypatch.setattr(st, "_save_pending", lambda data: saved.__setitem__("rows", data))
    return saved


def test_record_rejects_long_tp_below_entry(monkeypatch):
    saved = _captured(monkeypatch)
    st.record_pending_signal("BNBUSDT", "CONFIRMED", "LONG",
                             entry_price=671.87, tp=663.42, sl=650.0, score=99)
    assert saved["rows"] is None   # ditolak → tidak pernah save


def test_record_rejects_short_tp_above_entry(monkeypatch):
    saved = _captured(monkeypatch)
    st.record_pending_signal("ETHUSDT", "CONFIRMED", "SHORT",
                             entry_price=2252.69, tp=2260.78, sl=2280.0, score=100)
    assert saved["rows"] is None


def test_record_rejects_sl_wrong_side(monkeypatch):
    saved = _captured(monkeypatch)
    # LONG dengan SL di atas entry → invalid
    st.record_pending_signal("BTCUSDT", "CONFIRMED", "LONG",
                             entry_price=100.0, tp=110.0, sl=105.0, score=80)
    assert saved["rows"] is None


def test_record_rejects_nonpositive(monkeypatch):
    saved = _captured(monkeypatch)
    st.record_pending_signal("BTCUSDT", "CONFIRMED", "LONG",
                             entry_price=0, tp=110.0, sl=95.0, score=80)
    assert saved["rows"] is None


def test_record_accepts_valid_long(monkeypatch):
    saved = _captured(monkeypatch)
    st.record_pending_signal("BTCUSDT", "CONFIRMED", "LONG",
                             entry_price=100.0, tp=110.0, sl=95.0, score=80)
    assert saved["rows"] is not None and len(saved["rows"]) == 1
    row = saved["rows"][0]
    assert row["direction"] == "LONG" and row["tp"] == 110.0


def test_record_accepts_valid_short(monkeypatch):
    saved = _captured(monkeypatch)
    st.record_pending_signal("BTCUSDT", "CONFIRMED", "SHORT",
                             entry_price=100.0, tp=90.0, sl=105.0, score=80)
    assert saved["rows"] is not None and len(saved["rows"]) == 1


# ── Bug #1: anti death-spiral di _save_confirmed_signal ──────────
def test_confirmed_save_recovers_from_corrupt(tmp_path, monkeypatch):
    f = tmp_path / "confirmed_signals_history.json"
    # tulis JSON corrupt (truncated mid-write, persis kasus production)
    f.write_text('[\n  {\n    "symbol": "BTCUSDT",\n    "atr_based": ')
    monkeypatch.setattr(cs, "CONFIRMED_SIGNAL_FILE", str(f))

    cs._save_confirmed_signal({"symbol": "SOLUSDT", "direction": "LONG", "master_score": 80})

    # File sekarang harus valid JSON dgn record baru — recording TIDAK mati
    data = json.loads(f.read_text())
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["symbol"] == "SOLUSDT"
    # file corrupt di-backup
    assert glob.glob(str(f) + ".corrupt-*")


def test_confirmed_save_appends_when_valid(tmp_path, monkeypatch):
    f = tmp_path / "confirmed_signals_history.json"
    f.write_text(json.dumps([{"symbol": "BTCUSDT", "master_score": 70}]))
    monkeypatch.setattr(cs, "CONFIRMED_SIGNAL_FILE", str(f))

    cs._save_confirmed_signal({"symbol": "ETHUSDT", "master_score": 85})

    data = json.loads(f.read_text())
    assert len(data) == 2 and data[-1]["symbol"] == "ETHUSDT"
