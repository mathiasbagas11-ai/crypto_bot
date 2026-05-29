"""Tier 1 tests — risk_manager position sizing & daily risk controls.

These are the highest-stakes calculations in the bot: they decide how much
real money goes into a trade and when to halt trading. The state I/O
(`_load`/`_save`) is monkeypatched to an in-memory dict so the pure math is
exercised in isolation, offline and fast.
"""
from datetime import date

import pytest

import risk_manager as rm


@pytest.fixture
def state(monkeypatch):
    """In-memory risk state: $1000 capital, 2% risk, 5% daily loss limit.

    `_load` returns the same mutable dict every call so mutations from
    `record_trade_result` accumulate across calls, mirroring the file-backed
    behaviour. `last_reset_date` is set to today so `_auto_reset_daily` does
    not wipe the state mid-test.
    """
    st = dict(rm.DEFAULT_STATE)
    st.update({
        "capital_usdt": 1000.0,
        "balance_set": True,
        "risk_pct": 2.0,
        "daily_loss_limit": 5.0,
        "last_reset_date": date.today().isoformat(),
    })
    monkeypatch.setattr(rm, "_load", lambda: st)
    monkeypatch.setattr(rm, "_save", lambda s: None)
    return st


# ── calc_position_size ────────────────────────────────────────────

def test_position_size_basic_formula(state):
    # entry 100, SL 90 -> 10% SL distance; risk = 1000*2% = $20
    # position = 20 / 0.10 = $200; qty = 200 / 100 = 2.0; under the 30% cap.
    r = rm.calc_position_size(100.0, 90.0)
    assert r["sl_distance_pct"] == pytest.approx(10.0)
    assert r["risk_amount_usdt"] == pytest.approx(20.0)
    assert r["position_size_usdt"] == pytest.approx(200.0)
    assert r["qty"] == pytest.approx(2.0)
    assert r["capped"] is False


def test_position_size_capped_at_30pct(state):
    # entry 100, SL 98 -> 2% distance; raw position = 20 / 0.02 = $1000,
    # but capped at 30% of capital = $300.
    r = rm.calc_position_size(100.0, 98.0)
    assert r["capped"] is True
    assert r["position_size_usdt"] == pytest.approx(300.0)
    assert r["qty"] == pytest.approx(3.0)


def test_position_size_leverage_scales_position(state):
    # entry 100, SL 90, 3x leverage: raw = (20/0.10)*3 = $600,
    # cap = 1000*0.30*3 = $900, so not capped; qty = 600/100 = 6.0.
    r = rm.calc_position_size(100.0, 90.0, leverage=3)
    assert r["leverage"] == 3
    assert r["position_size_usdt"] == pytest.approx(600.0)
    assert r["qty"] == pytest.approx(6.0)
    assert r["capped"] is False


@pytest.mark.parametrize("entry,sl", [(0, 90), (100, 0), (-5, 90)])
def test_position_size_invalid_prices(state, entry, sl):
    assert "error" in rm.calc_position_size(entry, sl)


def test_position_size_sl_too_close(state):
    # 0.001% distance is below the 0.01% guard.
    r = rm.calc_position_size(100.0, 99.999)
    assert "error" in r


def test_position_size_rr_profit_options(state):
    r = rm.calc_position_size(100.0, 90.0)
    # risk amount is $20 -> R:R options are simple multiples.
    assert r["tp_profit_options"]["1.5:1"] == pytest.approx(30.0)
    assert r["tp_profit_options"]["2:1"] == pytest.approx(40.0)
    assert r["tp_profit_options"]["3:1"] == pytest.approx(60.0)


# ── calc_personal_trade_plan ─────────────────────────────────────

def test_personal_trade_plan_full(state):
    # entry 100, SL 95 (5% dist), TP1 110 (+10%), TP2 120 (+20%).
    # risk = $20, notional = 20/0.05 = $400, margin = min(400, 20% of 1000=200) = 200,
    # leverage = round(400/200) = 2, actual notional = 400.
    r = rm.calc_personal_trade_plan(100.0, 95.0, tp1=110.0, tp2=120.0)
    assert r["risk_amount"] == pytest.approx(20.0)
    assert r["notional"] == pytest.approx(400.0)
    assert r["margin_rec"] == pytest.approx(200.0)
    assert r["leverage_rec"] == 2
    assert r["actual_notional"] == pytest.approx(400.0)
    assert r["tp1_profit"] == pytest.approx(40.0)
    assert r["tp1_profit_pct"] == pytest.approx(4.0)
    assert r["tp2_profit"] == pytest.approx(80.0)
    assert r["tp2_profit_pct"] == pytest.approx(8.0)
    # SL loss capped at risk_amount * 1.05.
    assert r["sl_loss"] == pytest.approx(20.0)
    assert r["sl_loss_pct"] == pytest.approx(2.0)


def test_personal_trade_plan_leverage_capped_at_20(state):
    # Tiny SL distance pushes leverage above 20 -> clamped to 20.
    r = rm.calc_personal_trade_plan(100.0, 99.6)  # 0.4% distance
    assert r["leverage_rec"] == 20


def test_personal_trade_plan_invalid(state):
    assert "error" in rm.calc_personal_trade_plan(0, 95.0)
    # SL too close (< 0.1%).
    assert "error" in rm.calc_personal_trade_plan(100.0, 99.95)


def test_personal_trade_plan_no_balance(state):
    state["capital_usdt"] = 0.0
    assert "error" in rm.calc_personal_trade_plan(100.0, 95.0)


# ── record_trade_result / daily loss limit ───────────────────────

def test_record_trade_win(state):
    rm.record_trade_result(50.0)
    assert state["daily_pnl_usdt"] == pytest.approx(50.0)
    assert state["daily_trades"] == 1
    assert state["daily_wins"] == 1
    assert state["daily_losses"] == 0
    assert state["trading_halted"] is False


def test_record_trade_loss_below_limit_no_halt(state):
    rm.record_trade_result(-30.0)  # 3% of 1000, under 5% limit
    assert state["daily_losses"] == 1
    assert state["trading_halted"] is False


def test_record_trade_loss_triggers_halt(state):
    rm.record_trade_result(-60.0)  # 6% of 1000 -> over 5% limit
    assert state["trading_halted"] is True


def test_record_trade_accumulates_then_halts(state):
    rm.record_trade_result(-30.0)
    assert state["trading_halted"] is False
    rm.record_trade_result(-40.0)  # cumulative -70 = 7% -> halt
    assert state["daily_pnl_usdt"] == pytest.approx(-70.0)
    assert state["daily_trades"] == 2
    assert state["trading_halted"] is True


def test_profit_does_not_halt_even_if_large(state):
    rm.record_trade_result(500.0)  # 50% gain, positive -> never halts
    assert state["trading_halted"] is False


# ── get_risk_summary ─────────────────────────────────────────────

def test_risk_summary_after_win(state):
    rm.record_trade_result(50.0)
    s = rm.get_risk_summary()
    assert s["capital"] == pytest.approx(1000.0)
    assert s["daily_pnl_usdt"] == pytest.approx(50.0)
    assert s["daily_pnl_pct"] == pytest.approx(5.0)
    assert s["win_rate"] == pytest.approx(100.0)
    assert s["max_risk_per_trade"] == pytest.approx(20.0)
    assert s["max_daily_loss"] == pytest.approx(50.0)


def test_risk_summary_win_rate_mixed(state):
    rm.record_trade_result(50.0)
    rm.record_trade_result(-10.0)
    s = rm.get_risk_summary()
    assert s["daily_trades"] == 2
    assert s["win_rate"] == pytest.approx(50.0)


def test_risk_summary_no_trades_no_divzero(state):
    s = rm.get_risk_summary()
    assert s["daily_trades"] == 0
    assert s["win_rate"] == 0
