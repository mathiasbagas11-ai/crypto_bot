#!/usr/bin/env python3
"""
RISK MANAGER MODULE
===================
Kalkulasi position sizing, max loss, risk score per sinyal.
State disimpan di JSON file lokal (risk_state.json).

Fungsi utama:
  set_capital(amount)            → set modal awal
  set_risk_pct(pct)              → set max risk % per trade (default 2%)
  set_daily_loss_limit(pct)      → set max loss harian (default 5%)
  calc_position_size(entry,sl)   → hitung berapa USDT yang aman di-trade
  record_trade_result(pnl_usdt)  → catat hasil trade hari ini
  get_risk_summary()             → dict summary risk harian
  format_risk_block(entry,sl,dir)→ string siap embed di sinyal
  reset_daily()                  → reset counter harian (dipanggil scheduler)
"""

import os, json, logging
from datetime import datetime, timezone, date
from pathlib import Path

log = logging.getLogger("risk_manager")

STATE_FILE = Path("risk_state.json")

# Default config
DEFAULT_STATE = {
    "capital_usdt"    : 1000.0,   # modal total
    "risk_pct"        : 2.0,      # max risk % per trade
    "daily_loss_limit": 5.0,      # max loss % per hari sebelum stop trading
    "daily_pnl_usdt"  : 0.0,      # PnL hari ini
    "daily_trades"    : 0,        # jumlah trade hari ini
    "daily_wins"      : 0,
    "daily_losses"    : 0,
    "trading_halted"  : False,    # True kalau daily loss limit kena
    "last_reset_date" : "",       # tanggal terakhir reset harian
}

# ── State I/O ─────────────────────────────────

def _load() -> dict:
    try:
        from security import secure_load
        s = secure_load(str(STATE_FILE), default=DEFAULT_STATE.copy())
    except ImportError:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    s = json.load(f)
            except Exception as e:
                log.warning(f"risk_state load error: {e}")
                return DEFAULT_STATE.copy()
        else:
            return DEFAULT_STATE.copy()
    # Merge dengan default (handle key baru)
    for k, v in DEFAULT_STATE.items():
        if k not in s:
            s[k] = v
    return s


def _save(state: dict):
    try:
        from security import secure_save
        secure_save(str(STATE_FILE), state)
    except ImportError:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.error(f"risk_state save error: {e}")


def _auto_reset_daily(state: dict) -> dict:
    """Reset counter harian kalau sudah hari baru."""
    today = date.today().isoformat()
    if state.get("last_reset_date") != today:
        state["daily_pnl_usdt"]  = 0.0
        state["daily_trades"]    = 0
        state["daily_wins"]      = 0
        state["daily_losses"]    = 0
        state["trading_halted"]  = False
        state["last_reset_date"] = today
        _save(state)
        log.info("Risk manager: daily reset done")
    return state

# ── Public Setters ────────────────────────────

def set_capital(amount_usdt: float):
    """Set modal total. Contoh: set_capital(500)"""
    s = _load()
    s["capital_usdt"] = float(amount_usdt)
    _save(s)
    log.info(f"Modal diset: ${amount_usdt:.2f} USDT")


def set_risk_pct(pct: float):
    """Set max risk % per trade. Contoh: set_risk_pct(1.5)"""
    s = _load()
    s["risk_pct"] = float(max(0.1, min(pct, 10.0)))  # clamp 0.1–10%
    _save(s)
    log.info(f"Risk/trade diset: {pct:.1f}%")


def set_daily_loss_limit(pct: float):
    """Set max daily loss %. Contoh: set_daily_loss_limit(4)"""
    s = _load()
    s["daily_loss_limit"] = float(max(1.0, min(pct, 20.0)))
    _save(s)
    log.info(f"Daily loss limit diset: {pct:.1f}%")

# ── Core Calculations ─────────────────────────

def calc_position_size(entry_price: float, sl_price: float,
                       leverage: int = 1) -> dict:
    """
    Hitung position size yang aman berdasarkan risk % dan SL jarak.

    Formula:
      risk_amount = capital * risk_pct / 100
      sl_distance_pct = |entry - sl| / entry * 100
      position_size_usdt = risk_amount / (sl_distance_pct/100) / leverage
      qty = position_size_usdt / entry_price

    Returns dict dengan semua kalkulasi.
    """
    s       = _load()
    s       = _auto_reset_daily(s)
    capital = s["capital_usdt"]
    rsk_pct = s["risk_pct"]

    if entry_price <= 0 or sl_price <= 0:
        return {"error": "Harga tidak valid"}

    sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
    if sl_dist_pct < 0.01:
        return {"error": "SL terlalu dekat entry"}

    risk_amount_usdt     = capital * rsk_pct / 100
    position_size_usdt   = (risk_amount_usdt / (sl_dist_pct / 100)) * leverage
    qty                  = position_size_usdt / entry_price

    # Safety: position size max 30% dari modal (hindari over-leverage)
    max_position = capital * 0.30 * leverage
    if position_size_usdt > max_position:
        position_size_usdt = max_position
        qty                = position_size_usdt / entry_price
        capped             = True
    else:
        capped = False

    # Estimasi TP berdasarkan R:R
    rr_options = {
        "1.5:1": round(risk_amount_usdt * 1.5, 2),
        "2:1"  : round(risk_amount_usdt * 2.0, 2),
        "3:1"  : round(risk_amount_usdt * 3.0, 2),
    }

    return {
        "capital"            : round(capital, 2),
        "risk_pct"           : rsk_pct,
        "risk_amount_usdt"   : round(risk_amount_usdt, 2),
        "sl_distance_pct"    : round(sl_dist_pct, 3),
        "position_size_usdt" : round(position_size_usdt, 2),
        "qty"                : round(qty, 6),
        "leverage"           : leverage,
        "capped"             : capped,
        "tp_profit_options"  : rr_options,
        "daily_pnl_usdt"     : round(s["daily_pnl_usdt"], 2),
        "daily_trades"       : s["daily_trades"],
        "trading_halted"     : s["trading_halted"],
    }


def record_trade_result(pnl_usdt: float):
    """
    Catat hasil trade (positif = profit, negatif = loss).
    Otomatis halt trading kalau daily loss limit terlewat.
    """
    s = _load()
    s = _auto_reset_daily(s)

    s["daily_pnl_usdt"] += pnl_usdt
    s["daily_trades"]   += 1
    if pnl_usdt >= 0:
        s["daily_wins"]   += 1
    else:
        s["daily_losses"] += 1

    # Cek daily loss limit
    daily_loss_pct = abs(min(0, s["daily_pnl_usdt"])) / s["capital_usdt"] * 100
    if daily_loss_pct >= s["daily_loss_limit"] and s["daily_pnl_usdt"] < 0:
        s["trading_halted"] = True
        log.warning(f"⛔ TRADING HALTED — daily loss {daily_loss_pct:.1f}% >= limit {s['daily_loss_limit']:.1f}%")

    _save(s)
    log.info(f"Trade recorded: {pnl_usdt:+.2f} USDT | Daily PnL: {s['daily_pnl_usdt']:+.2f}")


def get_risk_summary() -> dict:
    """Ambil summary risk hari ini."""
    s = _load()
    s = _auto_reset_daily(s)
    capital = s["capital_usdt"]
    daily_pnl = s["daily_pnl_usdt"]
    return {
        "capital"          : capital,
        "risk_pct"         : s["risk_pct"],
        "daily_loss_limit" : s["daily_loss_limit"],
        "daily_pnl_usdt"   : round(daily_pnl, 2),
        "daily_pnl_pct"    : round(daily_pnl / capital * 100, 2) if capital else 0,
        "daily_trades"     : s["daily_trades"],
        "daily_wins"       : s["daily_wins"],
        "daily_losses"     : s["daily_losses"],
        "win_rate"         : (
            round(s["daily_wins"] / s["daily_trades"] * 100, 1)
            if s["daily_trades"] > 0 else 0
        ),
        "trading_halted"   : s["trading_halted"],
        "max_risk_per_trade": round(capital * s["risk_pct"] / 100, 2),
        "max_daily_loss"   : round(capital * s["daily_loss_limit"] / 100, 2),
    }


def reset_daily():
    """Manual reset harian. Dipanggil oleh scheduler jam 00:00 UTC."""
    s = _load()
    s["daily_pnl_usdt"]  = 0.0
    s["daily_trades"]    = 0
    s["daily_wins"]      = 0
    s["daily_losses"]    = 0
    s["trading_halted"]  = False
    s["last_reset_date"] = date.today().isoformat()
    _save(s)
    log.info("Risk manager: manual daily reset")

# ── Telegram Format ───────────────────────────

def format_risk_block(entry: float, sl: float, direction: str,
                      leverage: int = 1) -> str:
    """
    Format kalkulasi risk sebagai string Telegram-ready.
    Dipanggil dari build_coin_analysis_block.
    """
    calc = calc_position_size(entry, sl, leverage)
    if "error" in calc:
        return f"⚠️ Risk calc error: {calc['error']}"

    halted = calc["trading_halted"]
    lines  = []

    if halted:
        lines.append("⛔ *TRADING HALTED — Daily loss limit tercapai!*")
        lines.append("_Tunggu reset besok atau adjust limit via /setrisk_\n")

    dir_emoji = "🟢" if direction == "LONG" else "🔴"
    lines.append(f"💰 *Risk Management ({dir_emoji} {direction}):*")
    lines.append(f"  Modal          : ${calc['capital']:,.2f} USDT")
    lines.append(f"  Risk/trade     : {calc['risk_pct']}% = ${calc['risk_amount_usdt']:.2f} USDT")
    lines.append(f"  SL jarak       : {calc['sl_distance_pct']:.2f}%")
    lines.append(f"  Position size  : ${calc['position_size_usdt']:.2f} USDT"
                 + (" _(capped 30% modal)_" if calc['capped'] else ""))
    lines.append(f"  Qty            : {calc['qty']:.6f}")
    if leverage > 1:
        lines.append(f"  Leverage       : {leverage}x")

    # TP estimasi
    lines.append("  Estimasi profit jika hit TP:")
    for rr, profit in calc["tp_profit_options"].items():
        lines.append(f"    R:R {rr} → +${profit:.2f}")

    # Daily summary
    dpnl = calc["daily_pnl_usdt"]
    pnl_emoji = "🟢" if dpnl >= 0 else "🔴"
    lines.append(f"\n  {pnl_emoji} Daily PnL : {dpnl:+.2f} USDT | "
                 f"Trades: {calc['daily_trades']}")

    return "\n".join(lines)


def format_risk_status() -> str:
    """Format /risk command — summary harian lengkap."""
    s   = get_risk_summary()
    ts  = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    pnl_emoji    = "🟢" if s["daily_pnl_usdt"] >= 0 else "🔴"
    halted_line  = "\n⛔ *TRADING HALTED!* Daily loss limit tercapai.\n" if s["trading_halted"] else ""

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "💰 *RISK MANAGEMENT STATUS*",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        halted_line,
        f"💵 Modal Total    : ${s['capital']:,.2f} USDT",
        f"⚡ Risk/Trade     : {s['risk_pct']}% = ${s['max_risk_per_trade']:.2f} USDT max",
        f"🛑 Daily Loss Limit: {s['daily_loss_limit']}% = ${s['max_daily_loss']:.2f} USDT",
        "",
        f"📊 *Hari Ini:*",
        f"  {pnl_emoji} PnL      : {s['daily_pnl_usdt']:+.2f} USDT ({s['daily_pnl_pct']:+.2f}%)",
        f"  📈 Trades  : {s['daily_trades']} "
        f"(W:{s['daily_wins']} L:{s['daily_losses']} WR:{s['win_rate']:.0f}%)",
        "",
        "⚙️ *Ubah settings:*",
        "  `/setmodal 1000`   → set modal USDT",
        "  `/setrisk 2`       → set risk % per trade",
        "  `/setdailyloss 5`  → set max loss harian %",
        "  `/logpnl +50`      → catat profit",
        "  `/logpnl -30`      → catat loss",
    ]
    return "\n".join(l for l in lines if l is not None)
