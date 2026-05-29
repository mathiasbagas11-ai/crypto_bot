"""Tier 1 tests — backtest P&L and statistics.

`LocalTrade.close()` produces the per-trade P&L (with fees/slippage) that
every strategy decision is judged on, and `_calc_stats()` rolls trades up into
win rate / profit factor / drawdown / expectancy. A bug here silently corrupts
strategy selection, so these get explicit numeric coverage.
"""
from datetime import datetime, timezone

import pytest

from backtest_engine import LocalTrade, _calc_stats, TAKER_FEE, SLIPPAGE

OPEN = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
COST = TAKER_FEE + SLIPPAGE  # 0.0015 round-trip-side cost applied per leg


# ── LocalTrade.close ─────────────────────────────────────────────

def test_long_winning_pnl_includes_fees():
    t = LocalTrade("BTCUSDT", "LONG", 100.0, tp=110.0, sl=95.0,
                   open_time=OPEN, stake_usdt=100.0)
    # entry filled worse by COST, exit filled worse by COST.
    assert t.actual_entry == pytest.approx(100.0 * (1 + COST))
    close_time = datetime(2024, 1, 1, 5, 0, tzinfo=timezone.utc)
    t.close(110.0, close_time, "TP")

    expected_close = 110.0 * (1 - COST)
    expected_pct = (expected_close - t.actual_entry) / t.actual_entry
    assert t.is_open is False
    assert t.exit_reason == "TP"
    assert t.pnl_pct == pytest.approx(expected_pct)
    assert t.pnl_usdt == pytest.approx(100.0 * expected_pct)
    assert t.pnl_pct > 0
    assert t.hold_hours == pytest.approx(5.0)


def test_short_winning_pnl_includes_fees():
    t = LocalTrade("BTCUSDT", "SHORT", 100.0, tp=90.0, sl=105.0,
                   open_time=OPEN, stake_usdt=100.0)
    assert t.actual_entry == pytest.approx(100.0 * (1 - COST))
    t.close(90.0, datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc), "TP")

    expected_close = 90.0 * (1 + COST)
    expected_pct = (t.actual_entry - expected_close) / t.actual_entry
    assert t.pnl_pct == pytest.approx(expected_pct)
    assert t.pnl_pct > 0


def test_long_losing_trade_is_negative():
    t = LocalTrade("BTCUSDT", "LONG", 100.0, tp=110.0, sl=95.0,
                   open_time=OPEN, stake_usdt=100.0)
    t.close(95.0, datetime(2024, 1, 1, 1, 0, tzinfo=timezone.utc), "SL")
    assert t.pnl_pct < 0
    assert t.pnl_usdt < 0


def test_fees_make_breakeven_close_slightly_negative():
    # Closing exactly at entry should lose the round-trip cost, not break even.
    t = LocalTrade("BTCUSDT", "LONG", 100.0, tp=110.0, sl=95.0,
                   open_time=OPEN, stake_usdt=100.0)
    t.close(100.0, datetime(2024, 1, 1, 1, 0, tzinfo=timezone.utc), "TIMEOUT")
    assert t.pnl_pct < 0


# ── _calc_stats ──────────────────────────────────────────────────

class FakeTrade:
    """Minimal stand-in exposing only the attributes _calc_stats reads."""

    def __init__(self, pnl_pct, pnl_usdt, exit_reason="TP", day=1, hold_hours=2.0):
        self.pnl_pct = pnl_pct
        self.pnl_usdt = pnl_usdt
        self.exit_reason = exit_reason
        self.hold_hours = hold_hours
        self.close_time = datetime(2024, 1, day, tzinfo=timezone.utc)


def test_calc_stats_empty_returns_zeroed():
    r = _calc_stats([], days=30, stake=100.0, symbol="BTC",
                    strategy="scalp", cfg={})
    assert r["total_trades"] == 0
    assert r["win_rate"] == 0
    assert r["profit_factor"] == 0
    assert "note" in r


def test_calc_stats_known_set():
    trades = [
        FakeTrade(0.05, 5.0, exit_reason="TP", day=1),
        FakeTrade(0.03, 3.0, exit_reason="TP", day=2),
        FakeTrade(-0.02, -2.0, exit_reason="SL", day=3),
    ]
    r = _calc_stats(trades, days=30, stake=100.0, symbol="BTC",
                    strategy="scalp", cfg={"description": "x"})

    assert r["total_trades"] == 3
    assert r["win_rate"] == pytest.approx(66.67)
    # gains $8 / losses $2 -> PF 4.0
    assert r["profit_factor"] == pytest.approx(4.0)
    assert r["total_pnl_pct"] == pytest.approx(6.0)
    assert r["total_pnl_usdt"] == pytest.approx(6.0)
    assert r["avg_win_pct"] == pytest.approx(4.0)
    assert r["avg_loss_pct"] == pytest.approx(-2.0)
    assert r["best_trade_pct"] == pytest.approx(5.0)
    assert r["worst_trade_pct"] == pytest.approx(-2.0)
    assert r["tp_count"] == 2
    assert r["sl_count"] == 1
    assert r["timeout_count"] == 0
    # equity 100 -> 105 -> 108 -> 106; peak 108, trough 106 => 1.852% DD.
    assert r["max_drawdown_pct"] == pytest.approx(1.852, abs=1e-3)


def test_calc_stats_profit_factor_capped_when_no_losses():
    trades = [FakeTrade(0.05, 5.0, day=1), FakeTrade(0.02, 2.0, day=2)]
    r = _calc_stats(trades, days=30, stake=100.0, symbol="BTC",
                    strategy="scalp", cfg={})
    assert r["profit_factor"] == pytest.approx(999.0)


def test_calc_stats_trades_per_day():
    trades = [FakeTrade(0.01, 1.0, day=d) for d in range(1, 11)]
    r = _calc_stats(trades, days=10, stake=100.0, symbol="BTC",
                    strategy="scalp", cfg={})
    assert r["trades_per_day"] == pytest.approx(1.0)
