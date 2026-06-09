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


# ── resolution feeds the learning loop ───────────────────────────

def test_retest_activation_notifies_and_stays_active(patched):
    # RETEST_WAIT: saat harga menyentuh entry, kirim notif "TRADE AKTIF" dan
    # signal tetap ACTIVE (belum TP/SL) selama masih dalam timeout.
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=1)
    patched["signal"] = _sig(created, entry_mode="RETEST_WAIT")
    # candle menyentuh entry (low 99<=100) tapi belum TP/SL
    patched["candles"] = [_candle(now - timedelta(minutes=30), high=104, low=99)]

    sent = []
    resolved = st.check_pending_signals(send_telegram_fn=lambda m: sent.append(m))
    assert resolved == []                       # belum terminal
    assert any("TRADE AKTIF" in m for m in sent)


def test_retest_never_touched_invalidates(patched):
    # RETEST_WAIT: entry tak pernah tersentuh sampai timeout → INVALIDATED,
    # bukan loss. Notif "SETUP INVALID" dikirim.
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=30)         # lewat timeout 24h
    patched["signal"] = _sig(created, entry_mode="RETEST_WAIT")
    # harga selalu di atas entry (low 101 > entry 100) → tak pernah retest
    patched["candles"] = [_candle(now - timedelta(hours=29), high=106, low=101)]

    # _process_resolved_signals di-mock di fixture → kirim notif manual lewat path terminal
    resolved = st.check_pending_signals()
    assert len(resolved) == 1
    assert resolved[0]["status"] == "INVALIDATED"
    assert resolved[0]["pnl_pct"] == 0.0


def test_tp_ladder_partial_hit_stays_active(patched):
    # Ladder TP1/TP2/TP3: hanya TP1 kena → notif TP1, posisi tetap ACTIVE
    # (runner lanjut), belum terminal.
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=1)
    ladder = st._normalize_ladder(
        [{"level": 1, "price": 110.0}, {"level": 2, "price": 120.0},
         {"level": 3, "price": 130.0}], 110.0, 95.0, 100.0, "LONG")
    patched["signal"] = _sig(created, entry_mode="MOMENTUM_NOW",
                             activated=True, tp_ladder=ladder, tps_hit=[])
    # candle kena TP1 (high 112) saja
    patched["candles"] = [_candle(now - timedelta(minutes=30), high=112, low=100)]

    sent = []
    resolved = st.check_pending_signals(send_telegram_fn=lambda m: sent.append(m))
    assert resolved == []                       # belum semua rung → masih ACTIVE
    assert any("TP1 KENA" in m for m in sent)


def test_resolution_feeds_symbol_memory_and_learning(monkeypatch):
    # Regression: dulu signal_tracker hanya feed learning_engine; symbol_memory
    # write path tidak pernah dipanggil (dead). Sekarang keduanya harus terisi.
    import symbol_memory
    import learning_engine
    sm_calls, le_calls = [], []
    monkeypatch.setattr(symbol_memory, "record_symbol_outcome",
                        lambda **kw: sm_calls.append(kw))
    monkeypatch.setattr(learning_engine, "record_signal_outcome",
                        lambda **kw: le_calls.append(kw))
    monkeypatch.setattr(st, "_check_autobacktest_trigger", lambda *a, **k: None)

    resolved = [{
        "status": "TP_HIT", "symbol": "BTCUSDT", "signal_type": "SCALP",
        "direction": "LONG", "entry_price": 100.0, "exit_price": 110.0,
        "score": 70, "confluence_level": "GOOD", "pnl_pct": 10.0, "hold_hours": 2.0,
    }]
    st._process_resolved_signals(resolved, send_telegram_fn=None)

    assert len(sm_calls) == 1 and sm_calls[0]["symbol"] == "BTCUSDT"
    assert sm_calls[0]["outcome"] == "TP_HIT"
    assert sm_calls[0]["hold_minutes"] == 120
    assert len(le_calls) == 1 and le_calls[0]["symbol"] == "BTCUSDT"
