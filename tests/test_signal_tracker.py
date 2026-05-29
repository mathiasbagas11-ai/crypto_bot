"""Tier 3 tests — pending-signal resolution.

`check_pending_signals` walks the candles after a signal was issued and decides
TP / SL / timeout outcomes. The key correctness property is *conservative*
resolution: when a single candle straddles both TP and SL, SL must win. Network
and persistence are mocked so only the resolution logic runs.
"""
from datetime import datetime, timedelta, timezone

import pytest

import signal_tracker as st


@pytest.fixture
def patched(monkeypatch):
    """Isolate check_pending_signals from I/O. Returns a config dict the test
    fills in: candles, current price, and the single pending signal."""
    cfg = {"candles": [], "current_price": None, "signal": None}
    monkeypatch.setattr(st, "_load_pending", lambda: [cfg["signal"]])
    monkeypatch.setattr(st, "_load_outcomes", lambda: [])
    monkeypatch.setattr(st, "_save_pending", lambda x: None)
    monkeypatch.setattr(st, "_save_outcomes", lambda x: None)
    monkeypatch.setattr(st, "_get_price_history", lambda *a, **k: cfg["candles"])
    monkeypatch.setattr(st, "_get_current_price", lambda s: cfg["current_price"])
    monkeypatch.setattr(st, "_process_resolved_signals", lambda *a, **k: None)
    return cfg


def _sig(created, **over):
    base = {"status": "PENDING", "created_at": created.isoformat(),
            "symbol": "BTCUSDT", "direction": "LONG", "entry_price": 100.0,
            "tp": 110.0, "sl": 95.0, "timeout_hours": 24}
    base.update(over)
    return base


def _candle(when, high, low):
    return {"time": int(when.timestamp() * 1000), "high": high, "low": low,
            "close": (high + low) / 2, "open": (high + low) / 2, "volume": 1.0}


def test_long_take_profit(patched):
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=2)
    patched["signal"] = _sig(created)
    patched["candles"] = [_candle(now - timedelta(hours=1), high=111, low=99)]

    resolved = st.check_pending_signals()
    assert len(resolved) == 1
    assert resolved[0]["status"] == "TP_HIT"
    assert resolved[0]["pnl_pct"] == 10.0


def test_conservative_sl_wins_when_candle_straddles(patched):
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=2)
    patched["signal"] = _sig(created)
    # One candle touches BOTH sl (94<=95) and tp (111>=110): SL must win.
    patched["candles"] = [_candle(now - timedelta(hours=1), high=111, low=94)]

    resolved = st.check_pending_signals()
    assert resolved[0]["status"] == "SL_HIT"
    assert resolved[0]["pnl_pct"] == -5.0


def test_timeout_expired_win_uses_current_price(patched):
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=30)  # past the 24h timeout
    patched["signal"] = _sig(created)
    patched["candles"] = [_candle(now - timedelta(hours=29), high=105, low=99)]  # no hit
    patched["current_price"] = 105.0

    resolved = st.check_pending_signals()
    assert resolved[0]["status"] == "EXPIRED_WIN"
    assert resolved[0]["pnl_pct"] == 5.0


def test_still_pending_within_timeout(patched):
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=1)
    patched["signal"] = _sig(created)
    patched["candles"] = [_candle(now - timedelta(minutes=30), high=105, low=99)]  # no hit

    resolved = st.check_pending_signals()
    assert resolved == []


def test_short_take_profit(patched):
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=2)
    patched["signal"] = _sig(created, direction="SHORT", tp=90.0, sl=105.0)
    patched["candles"] = [_candle(now - timedelta(hours=1), high=101, low=89)]

    resolved = st.check_pending_signals()
    assert resolved[0]["status"] == "TP_HIT"
    assert resolved[0]["pnl_pct"] == 10.0  # (100-90)/100*100
