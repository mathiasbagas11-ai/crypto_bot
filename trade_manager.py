#!/usr/bin/env python3
"""
MANUAL TRADE MANAGER
====================
User kasih tau bot posisi yang dibuka secara manual:
  /trade BTC LONG 95000 60   → beli BTC di $95k, modal $60

Bot otomatis hitung:
  - SL (ATR-based 1.5x), Breakeven trigger, TP1 (2x ATR), TP2 (3.5x ATR)
  - Trailing stop aktif setelah TP1 tercapai (trail 1x ATR di bawah high)

Per-scan monitoring (NOTIFY-ONLY, bot tidak auto-close):
  - Price ≥ breakeven trigger → geser SL ke entry + kirim alert
  - Price ≥ TP1              → SL → entry, aktifkan trailing + kirim alert
  - Trailing stop tersentuh  → kirim alert + saran /close
  - Price ≥ TP2              → kirim alert + saran /close
  - Price ≤ SL / BE-stop     → kirim alert + saran /close

User yang konfirmasi tutup lewat /close → baru hasil masuk compound + journal/Excel.
Sizing pakai equity (balance + unrealized PnL posisi aktif) × stake_pct, jadi
bisa jalan banyak posisi sekaligus.

/close BTC [exit_price]     → user konfirmasi full close manual
/trades                     → lihat semua posisi aktif + P&L realtime
"""

import os
import json
import time
import logging
import threading
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("trade_manager")

TRADES_FILE    = "active_trades.json"
COMPOUND_FILE  = "compound_config.json"
BINANCE_BASE   = "https://api.binance.com/api/v3"
BINANCE_FUT    = "https://fapi.binance.com"

_lock = threading.Lock()

# Default % fallbacks kalau ATR tidak bisa di-fetch
DEFAULT_SL_PCT      = 0.020   # 2.0%
DEFAULT_BE_PCT      = 0.010   # 1.0%  (breakeven trigger)
DEFAULT_TP1_PCT     = 0.040   # 4.0%
DEFAULT_TP2_PCT     = 0.070   # 7.0%
DEFAULT_TRAIL_PCT   = 0.020   # 2.0%  (trailing stop distance)

DEFAULT_STAKE_PCT   = 0.10    # 10% of balance per trade

# ─────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────

def _load() -> list:
    with _lock:
        try:
            if os.path.exists(TRADES_FILE):
                with open(TRADES_FILE) as f:
                    return json.load(f)
        except Exception as e:
            log.warning(f"Load trades error: {e}")
        return []


def _save(trades: list) -> bool:
    """Tulis atomik (tmp + os.replace) supaya crash di tengah tulis tidak
    meninggalkan file korup. Return True kalau sukses."""
    with _lock:
        try:
            tmp = TRADES_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(trades, f, indent=2)
            os.replace(tmp, TRADES_FILE)
            return True
        except Exception as e:
            log.error(f"Save trades error: {e}")
            return False


# ─────────────────────────────────────────────
# COMPOUND STAKE
# ─────────────────────────────────────────────

def _load_compound() -> dict:
    try:
        if os.path.exists(COMPOUND_FILE):
            with open(COMPOUND_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"balance": None, "stake_pct": DEFAULT_STAKE_PCT, "total_pnl": 0.0, "trades_closed": 0}


def _save_compound(cfg: dict):
    try:
        cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(COMPOUND_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.error(f"Save compound error: {e}")


def set_balance(amount: float) -> dict:
    """Set modal awal / update balance manual."""
    cfg = _load_compound()
    cfg["balance"] = round(amount, 2)
    _save_compound(cfg)
    return cfg


def set_stake_pct(pct: float) -> dict:
    """Set persentase stake per trade (1–50%). Input dalam persen, e.g. 10 = 10%."""
    pct = max(1.0, min(50.0, pct))
    cfg = _load_compound()
    cfg["stake_pct"] = round(pct / 100, 4)
    _save_compound(cfg)
    return cfg


def _current_equity() -> Optional[float]:
    """Equity = balance + unrealized PnL semua posisi aktif (buat sizing)."""
    cfg = _load_compound()
    if cfg.get("balance") is None:
        return None
    equity = cfg["balance"]
    for t in _load():
        if t.get("status") != "ACTIVE":
            continue
        price = _fetch_price(t["symbol"])
        if price is None:
            continue
        entry = t["entry_price"]
        size  = t["size_usdt"]
        if not entry or entry <= 0:
            continue
        if t["direction"] == "LONG":
            equity += size * (price - entry) / entry
        else:
            equity += size * (entry - price) / entry
    return equity


def get_auto_stake() -> Optional[float]:
    """Stake otomatis = equity (balance + unrealized PnL posisi aktif) x stake_pct.
    None kalau balance belum diset."""
    cfg = _load_compound()
    if cfg.get("balance") is None:
        return None
    equity = _current_equity()
    if equity is None:
        return None
    stake = equity * cfg["stake_pct"]
    return round(max(stake, 1.0), 2)


def _update_compound_balance(pnl_usdt: float):
    """Tambah/kurang PnL ke balance setelah trade close."""
    cfg = _load_compound()
    if cfg.get("balance") is None:
        return
    cfg["balance"]       = round(cfg["balance"] + pnl_usdt, 2)
    cfg["total_pnl"]     = round(cfg.get("total_pnl", 0.0) + pnl_usdt, 4)
    cfg["trades_closed"] = cfg.get("trades_closed", 0) + 1
    _save_compound(cfg)
    log.info(f"Compound balance updated: +{pnl_usdt:.2f} → ${cfg['balance']:.2f}")


def format_compound_status() -> str:
    cfg  = _load_compound()
    bal  = cfg.get("balance")
    pct  = cfg.get("stake_pct", DEFAULT_STAKE_PCT)
    tpnl = cfg.get("total_pnl", 0.0)
    cnt  = cfg.get("trades_closed", 0)

    if bal is None:
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📈 <b>COMPOUND STAKE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ Balance belum diset.\n\n"
            "Set dengan: <code>/balance 500</code>\n"
            "Set stake %: <code>/setstake 10</code> (default 10%)"
        )

    next_stake = round(bal * pct, 2)
    pnl_emoji  = "🟢" if tpnl >= 0 else "🔴"

    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 <b>COMPOUND STAKE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Balance saat ini: <b>${bal:,.2f}</b>\n"
        f"📊 Stake per trade: <b>{pct*100:.1f}%</b> = <b>${next_stake:,.2f}</b>\n"
        f"{pnl_emoji} Total PnL: <b>${tpnl:+.2f}</b> ({cnt} trade)\n\n"
        "💡 <code>/balance &lt;jumlah&gt;</code> — update balance manual\n"
        "💡 <code>/setstake &lt;%&gt;</code> — ubah persentase stake\n"
        "💡 <code>/trade BTC LONG 95000</code> — tanpa size → auto dari balance"
    )


# ─────────────────────────────────────────────
# LEVEL CALCULATOR (ATR-based)
# ─────────────────────────────────────────────

def _fetch_atr(symbol: str, period: int = 14) -> Optional[float]:
    """Ambil ATR 1H dari Binance untuk kalkulasi level."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym = sym + "USDT"
    try:
        # Coba Futures dulu, fallback ke Spot
        for base in [BINANCE_FUT + "/fapi/v1", BINANCE_BASE]:
            try:
                r = requests.get(
                    f"{base}/klines",
                    params={"symbol": sym, "interval": "1h", "limit": period + 5},
                    timeout=8
                )
                if r.status_code == 200:
                    candles = r.json()
                    if len(candles) >= period + 1:
                        trs = []
                        for i in range(1, len(candles)):
                            h  = float(candles[i][2])
                            l  = float(candles[i][3])
                            pc = float(candles[i-1][4])
                            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                        return float(np.mean(trs[-period:]))
            except Exception:
                continue
    except Exception as e:
        log.debug(f"ATR fetch error {symbol}: {e}")
    return None


def _fetch_price(symbol: str) -> Optional[float]:
    """Fetch harga terakhir dari Binance."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym = sym + "USDT"
    try:
        for base, path in [
            (BINANCE_FUT, "/fapi/v1/ticker/price"),
            (BINANCE_BASE, "/ticker/price"),
        ]:
            try:
                r = requests.get(f"{base}{path}", params={"symbol": sym}, timeout=5)
                if r.status_code == 200:
                    return float(r.json()["price"])
            except Exception:
                continue
    except Exception as e:
        log.debug(f"Price fetch error {symbol}: {e}")
    return None


def calculate_levels(symbol: str, direction: str, entry: float, size_usdt: float) -> dict:
    """
    Hitung SL, TP1, TP2, breakeven trigger, dan trailing ATR.
    ATR-based kalau bisa fetch, fallback ke persentase tetap.
    """
    atr = _fetch_atr(symbol)

    if atr and atr > 0:
        atr_pct = atr / entry
        sl_dist      = max(atr * 1.5, entry * DEFAULT_SL_PCT)
        be_dist      = atr * 0.5
        tp1_dist     = atr * 2.0
        tp2_dist     = atr * 3.5
        trail_dist   = atr * 1.0
        method       = f"ATR-based (1H ATR={atr:.4f}, {atr_pct*100:.2f}%)"
    else:
        sl_dist      = entry * DEFAULT_SL_PCT
        be_dist      = entry * DEFAULT_BE_PCT
        tp1_dist     = entry * DEFAULT_TP1_PCT
        tp2_dist     = entry * DEFAULT_TP2_PCT
        trail_dist   = entry * DEFAULT_TRAIL_PCT
        method       = "Percentage-based (ATR unavailable)"

    if direction == "LONG":
        sl              = round(entry - sl_dist, 8)
        be_trigger      = round(entry + be_dist, 8)
        tp1             = round(entry + tp1_dist, 8)
        tp2             = round(entry + tp2_dist, 8)
    else:  # SHORT
        sl              = round(entry + sl_dist, 8)
        be_trigger      = round(entry - be_dist, 8)
        tp1             = round(entry - tp1_dist, 8)
        tp2             = round(entry - tp2_dist, 8)

    qty = size_usdt / entry

    sl_pct   = abs(entry - sl) / entry * 100
    tp1_pct  = abs(tp1 - entry) / entry * 100
    tp2_pct  = abs(tp2 - entry) / entry * 100
    rr1      = tp1_pct / sl_pct if sl_pct > 0 else 0
    rr2      = tp2_pct / sl_pct if sl_pct > 0 else 0

    return {
        "sl":           sl,
        "sl_initial":   sl,
        "be_trigger":   be_trigger,
        "tp1":          tp1,
        "tp2":          tp2,
        "trail_dist":   round(trail_dist, 8),
        "qty":          round(qty, 8),
        "sl_pct":       round(sl_pct, 2),
        "tp1_pct":      round(tp1_pct, 2),
        "tp2_pct":      round(tp2_pct, 2),
        "rr1":          round(rr1, 2),
        "rr2":          round(rr2, 2),
        "method":       method,
    }


# ─────────────────────────────────────────────
# TRADE LIFECYCLE
# ─────────────────────────────────────────────

def record_trade(symbol: str, direction: str, entry_price: float,
                 size_usdt: Optional[float] = None) -> dict:
    """
    Catat posisi manual yang dibuka user.
    Return trade dict (juga disimpan ke active_trades.json).
    """
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym = sym + "USDT"
    direction = direction.upper()
    if direction not in ("LONG", "SHORT"):
        return {"error": f"Direction harus LONG atau SHORT, bukan '{direction}'"}

    # Auto-stake dari compound balance kalau size tidak diberikan
    if size_usdt is None:
        size_usdt = get_auto_stake()
        if size_usdt is None:
            return {"error": "Size trade tidak diberikan dan compound balance belum diset.\nGunakan /balance <jumlah> atau tulis size: /trade BTC LONG 95000 60"}

    levels = calculate_levels(sym, direction, entry_price, size_usdt)

    trade = {
        "id":              f"{sym}_{int(time.time() * 1000)}",
        "symbol":          sym,
        "direction":       direction,
        "entry_price":     entry_price,
        "size_usdt":       size_usdt,
        "qty":             levels["qty"],

        # Levels
        "sl":              levels["sl"],
        "sl_initial":      levels["sl_initial"],
        "be_trigger":      levels["be_trigger"],
        "tp1":             levels["tp1"],
        "tp2":             levels["tp2"],
        "trail_dist":      levels["trail_dist"],

        # Stats info
        "sl_pct":          levels["sl_pct"],
        "tp1_pct":         levels["tp1_pct"],
        "tp2_pct":         levels["tp2_pct"],
        "rr1":             levels["rr1"],
        "rr2":             levels["rr2"],
        "level_method":    levels["method"],

        # State
        "status":          "ACTIVE",
        "tp1_hit":         False,
        "tp1_hit_price":   None,
        "tp1_hit_time":    None,
        "sl_at_be":        False,
        "trailing_high":   None,
        "trailing_low":    None,
        "trailing_stop":   None,
        "partial_size":    size_usdt * 0.5,   # 50% ambil di TP1
        "partial_done":    False,
        "partial_price":   None,

        # Alerts sent (hindari spam)
        "alert_be":        False,
        "alert_tp1":       False,
        "alert_tp2":       False,
        "alert_trail":     False,

        # Close info
        "opened_at":       datetime.now(timezone.utc).isoformat(),
        "closed_at":       None,
        "exit_price":      None,
        "exit_reason":     None,
        "pnl_usdt":        None,
        "pnl_pct":         None,
    }

    trades = _load()
    # Jangan duplicate symbol aktif
    existing = [t for t in trades if t["symbol"] == sym and t["status"] == "ACTIVE"]
    if existing:
        return {"error": f"{sym} sudah ada posisi aktif. Kirim /close {sym.replace('USDT','')} dulu."}

    trades.append(trade)
    _save(trades)
    log.info(f"Trade recorded: {sym} {direction} @ {entry_price} size=${size_usdt}")
    return trade


def close_trade(symbol: str, exit_price: Optional[float] = None,
                reason: str = "MANUAL") -> Optional[dict]:
    """
    Tutup posisi aktif. Kalau exit_price None → fetch current price.
    Return trade dict yang sudah di-close.
    """
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym = sym + "USDT"

    trades = _load()
    trade  = next((t for t in trades if t["symbol"] == sym and t["status"] == "ACTIVE"), None)
    if not trade:
        return None

    if exit_price is None:
        exit_price = _fetch_price(sym) or trade["entry_price"]

    _do_close(trade, exit_price, reason)
    if _save(trades):
        # Hanya kredit compound balance setelah status CLOSED ter-persist.
        _update_compound_balance(trade["pnl_usdt"])
        _log_to_journal(trade)
    else:
        log.error(f"close_trade: gagal simpan state untuk {sym} — balance TIDAK dikredit")
    return trade


def _do_close(trade: dict, exit_price: float, reason: str):
    """Mutate trade dict: isi close fields, hitung PnL total."""
    trade["status"]     = "CLOSED"
    trade["exit_price"] = exit_price
    trade["exit_reason"]= reason
    trade["closed_at"]  = datetime.now(timezone.utc).isoformat()

    entry  = trade["entry_price"]
    size   = trade["size_usdt"]
    direct = trade["direction"]

    # PnL full position
    if direct == "LONG":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100

    # Kalau partial TP1 sudah done, hitung blended PnL
    if trade.get("partial_done") and trade.get("partial_price"):
        half = size * 0.5
        if direct == "LONG":
            pnl_p1 = (trade["partial_price"] - entry) / entry * half
            pnl_p2 = (exit_price - entry) / entry * half
        else:
            pnl_p1 = (entry - trade["partial_price"]) / entry * half
            pnl_p2 = (entry - exit_price) / entry * half
        pnl_usdt = pnl_p1 + pnl_p2
    else:
        pnl_usdt = size * (pnl_pct / 100)

    trade["pnl_usdt"] = round(pnl_usdt, 4)
    trade["pnl_pct"]  = round(pnl_pct, 2)
    # NOTE: compound balance di-kredit di caller SETELAH _save sukses, supaya
    # tidak double-credit kalau save gagal dan trade masih ACTIVE saat retry.


def _log_to_journal(trade: dict):
    """Log ke trade_journal.py (Google Sheets) kalau tersedia."""
    try:
        import importlib
        tj = importlib.import_module("trade_journal")
        log_trade = getattr(tj, "log_trade")
        sym   = trade["symbol"].replace("USDT", "")
        note  = (
            f"Auto-managed | Entry:{trade['entry_price']} "
            f"Exit:{trade['exit_price']} Reason:{trade['exit_reason']}"
        )
        log_trade(
            coin       = sym,
            direction  = trade["direction"],
            entry_price= trade["entry_price"],
            margin_usdt= trade["size_usdt"],
            leverage   = 1,
            pnl_usdt   = trade["pnl_usdt"] or 0,
            note       = note,
        )
        log.info(f"Trade logged to journal: {sym} PnL={trade['pnl_usdt']}")
    except BaseException as e:
        log.warning(f"Journal log skipped: {e}")


# ─────────────────────────────────────────────
# PER-SCAN MONITORING
# ─────────────────────────────────────────────

def check_active_trades(send_telegram_fn=None) -> list:
    """
    Dipanggil tiap scan. NOTIFY-ONLY: saat level kena (SL/BE/TP1/TP2/trailing)
    bot kirim alert + saran /close, TAPI tidak auto-close. User konfirmasi tutup
    lewat /close SYMBOL -> baru hasil masuk compound + journal/Excel.
    SL->BE shift & trailing stop tetap di-update di tracking. Alert di-dedup dan
    di-reset saat harga balik aman. Return list trade yang memicu alert scan ini.
    """
    trades = _load()
    active = [t for t in trades if t["status"] == "ACTIVE"]
    if not active:
        return []

    alerted_now = []

    def _send(msg):
        if send_telegram_fn:
            try:
                send_telegram_fn(msg)
            except Exception as e:
                log.warning(f"trade alert send failed: {e}")

    for trade in active:
        sym   = trade["symbol"]
        price = _fetch_price(sym)
        if price is None:
            log.warning(f"check_active_trades: cannot fetch price for {sym}")
            continue

        direc  = trade["direction"]
        entry  = trade["entry_price"]
        sl     = trade["sl"]
        be     = trade["be_trigger"]
        tp1    = trade["tp1"]
        tp2    = trade["tp2"]
        trail  = trade["trail_dist"]
        tp1hit = trade["tp1_hit"]
        coin   = sym.replace("USDT", "")

        pnl_now = (price - entry) / entry * 100 if direc == "LONG" else (entry - price) / entry * 100

        # 1. SL / breakeven-stop kena -> notify (TIDAK close)
        sl_triggered = (direc == "LONG" and price <= sl) or (direc == "SHORT" and price >= sl)
        if sl_triggered:
            if not trade.get("alert_sl"):
                trade["alert_sl"] = True
                alerted_now.append(trade)
                tag = "BREAKEVEN STOP" if trade.get("sl_at_be") else "STOP LOSS"
                _send(
                    f"\U0001F6D1 <b>{coin} - {tag} KENA</b>\n"
                    f"{direc} | entry ${entry:,.4f} -> now ${price:,.4f}\n"
                    f"PnL: {pnl_now:+.2f}%\n"
                    f"Konfirmasi tutup posisi: <code>/close {coin}</code>"
                )
            continue
        else:
            trade["alert_sl"] = False  # reset kalau harga balik aman

        # 2. Setelah TP1: trailing stop + TP2
        if tp1hit:
            if direc == "LONG":
                trade["trailing_high"] = max(trade.get("trailing_high") or price, price)
                trade["trailing_stop"] = trade["trailing_high"] - trail
                trail_hit = price <= trade["trailing_stop"]
            else:
                trade["trailing_low"]  = min(trade.get("trailing_low") or price, price)
                trade["trailing_stop"] = trade["trailing_low"] + trail
                trail_hit = price >= trade["trailing_stop"]

            tp2_hit = (direc == "LONG" and price >= tp2) or (direc == "SHORT" and price <= tp2)
            if tp2_hit:
                if not trade.get("alert_tp2"):
                    trade["alert_tp2"] = True
                    alerted_now.append(trade)
                    _send(
                        f"\U0001F3AF <b>{coin} - TP2 KENA</b>\n"
                        f"{direc} | now ${price:,.4f} | PnL {pnl_now:+.2f}%\n"
                        f"Amankan profit: <code>/close {coin}</code>"
                    )
                continue

            if trail_hit:
                if not trade.get("alert_trail"):
                    trade["alert_trail"] = True
                    alerted_now.append(trade)
                    _send(
                        f"\U0001F4C9 <b>{coin} - TRAILING STOP KENA</b>\n"
                        f"{direc} | now ${price:,.4f} | PnL {pnl_now:+.2f}%\n"
                        f"trailing stop ${trade['trailing_stop']:,.4f}\n"
                        f"Konfirmasi tutup: <code>/close {coin}</code>"
                    )
            else:
                trade["alert_trail"] = False  # reset saat harga menjauh dari trailing
            continue

        # 3. Sebelum TP1: cek TP1
        tp1_hit_now = (direc == "LONG" and price >= tp1) or (direc == "SHORT" and price <= tp1)
        if tp1_hit_now:
            if not trade.get("alert_tp1"):
                trade["tp1_hit"]       = True
                trade["tp1_hit_price"] = price
                trade["tp1_hit_time"]  = datetime.now(timezone.utc).isoformat()
                # Tandai partial 50% sudah diambil di harga TP1 supaya _do_close
                # memakai blended PnL (separuh di TP1, separuh di exit akhir).
                trade["partial_done"]  = True
                trade["partial_price"] = price
                trade["sl"]            = entry      # geser SL ke breakeven
                trade["sl_at_be"]      = True
                trade["alert_tp1"]     = True
                trade["alert_sl"]      = False
                if direc == "LONG":
                    trade["trailing_high"] = price
                    trade["trailing_stop"] = price - trail
                else:
                    trade["trailing_low"]  = price
                    trade["trailing_stop"] = price + trail
                alerted_now.append(trade)
                _send(
                    f"\u2705 <b>{coin} - TP1 KENA</b>\n"
                    f"{direc} | now ${price:,.4f} | PnL {pnl_now:+.2f}%\n"
                    f"\U0001F6E1 SL digeser ke breakeven (${entry:,.4f}), trailing aktif.\n"
                    f"Ambil partial / tutup penuh: <code>/close {coin}</code>"
                )
            continue

        # 4. Breakeven trigger (sebelum TP1) -> geser SL ke BE + notify
        be_hit = (direc == "LONG" and price >= be) or (direc == "SHORT" and price <= be)
        if be_hit and not trade.get("sl_at_be"):
            trade["sl"]       = entry
            trade["sl_at_be"] = True
            if not trade.get("alert_be"):
                trade["alert_be"] = True
                alerted_now.append(trade)
                _send(
                    f"\U0001F6E1 <b>{coin} - BREAKEVEN</b>\n"
                    f"{direc} | now ${price:,.4f} | PnL {pnl_now:+.2f}%\n"
                    f"SL diamankan ke entry (${entry:,.4f}). Posisi tetap jalan."
                )

    _save(trades)
    return alerted_now


def get_active_trades() -> list:
    return [t for t in _load() if t["status"] == "ACTIVE"]


def get_all_trades(limit: int = 20) -> list:
    return _load()[-limit:]


# ─────────────────────────────────────────────
# TELEGRAM FORMATTERS
# ─────────────────────────────────────────────

def format_trade_opened(trade: dict) -> str:
    sym   = trade["symbol"].replace("USDT", "")
    d     = trade["direction"]
    entry = trade["entry_price"]
    size  = trade["size_usdt"]
    qty   = trade["qty"]
    sl    = trade["sl"]
    be    = trade["be_trigger"]
    tp1   = trade["tp1"]
    tp2   = trade["tp2"]
    s_pct = trade["sl_pct"]
    t1pct = trade["tp1_pct"]
    t2pct = trade["tp2_pct"]
    rr1   = trade["rr1"]
    rr2   = trade["rr2"]
    meth  = trade["level_method"]

    d_emoji = "🟢" if d == "LONG" else "🔴"
    arr     = "↗" if d == "LONG" else "↘"

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{d_emoji} <b>TRADE RECORDED — {sym} {d} {arr}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Entry : <b>${entry:,.4f}</b>\n"
        f"📦 Size  : <b>${size:.2f}</b> ({qty:.6f} {sym})\n\n"
        f"🎯 <b>TARGETS:</b>\n"
        f"   TP1 : <b>${tp1:,.4f}</b> (+{t1pct:.1f}%) — R:R {rr1:.1f}:1\n"
        f"   TP2 : <b>${tp2:,.4f}</b> (+{t2pct:.1f}%) — R:R {rr2:.1f}:1\n\n"
        f"🛡️ <b>RISK MANAGEMENT:</b>\n"
        f"   SL Initial : <b>${sl:,.4f}</b> (-{s_pct:.1f}%)\n"
        f"   BE Trigger : <b>${be:,.4f}</b> → SL geser ke entry\n"
        f"   Trailing   : aktif setelah TP1, jarak {trade['trail_dist']:.4f}\n\n"
        f"📐 <i>{meth}</i>\n\n"
        f"💡 Bot monitor tiap scan & kirim alert saat level kena.\n"
        f"   Tutup posisi kapan saja dengan /close — bot tidak auto-close.\n"
        f"   /close {sym} → full close (masuk compound + journal)\n"
        f"   /trades → lihat semua posisi aktif"
    )


def format_trades_list(trades: list) -> str:
    if not trades:
        return "📭 <b>Tidak ada posisi aktif saat ini.</b>\n\nBuka posisi dengan:\n<code>/trade BTC LONG 95000 60</code>"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>POSISI AKTIF</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for t in trades:
        sym    = t["symbol"].replace("USDT", "")
        d      = t["direction"]
        entry  = t["entry_price"]
        size   = t["size_usdt"]
        sl     = t["sl"]
        tp1    = t["tp1"]
        tp2    = t["tp2"]
        price  = _fetch_price(t["symbol"]) or entry
        tp1hit = t["tp1_hit"]

        pnl_pct = (price - entry) / entry * 100 if d == "LONG" else (entry - price) / entry * 100
        pnl_usd = size * (pnl_pct / 100)
        pnl_e   = "🟢" if pnl_pct >= 0 else "🔴"
        d_emoji = "↗" if d == "LONG" else "↘"
        status  = "TP1 ✅ trailing aktif" if tp1hit else (
            "BE aktif 🛡️" if t.get("sl_at_be") else "monitoring"
        )

        opened_iso = t.get("opened_at", "")
        try:
            opened_dt = datetime.fromisoformat(opened_iso)
            age_h = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 3600
            age_str = f"{age_h:.1f}h"
        except Exception:
            age_str = "?"

        lines.append(
            f"{d_emoji} <b>{sym} {d}</b> | {age_str} lalu | {status}\n"
            f"  Entry: ${entry:,.4f} | Now: ${price:,.4f}\n"
            f"  P&L: {pnl_e} {pnl_pct:+.2f}% (${pnl_usd:+.2f})\n"
            f"  SL: ${sl:,.4f} | TP1: ${tp1:,.4f} | TP2: ${tp2:,.4f}"
        )
        if t.get("trailing_stop"):
            lines.append(f"  Trailing stop: ${t['trailing_stop']:,.4f}")
        lines.append("")

    lines.append("💡 /close BTC [harga] → manual close")
    return "\n".join(lines)


def format_closed_trade(trade: dict) -> str:
    sym    = trade["symbol"].replace("USDT", "")
    d      = trade["direction"]
    entry  = trade["entry_price"]
    exit_p = trade["exit_price"]
    reason = trade["exit_reason"]
    pnl_u  = trade["pnl_usdt"] or 0
    pnl_p  = trade["pnl_pct"] or 0
    size   = trade["size_usdt"]

    win    = pnl_u >= 0
    emoji  = "✅" if win else "🔴"
    result = "PROFIT" if win else "LOSS"

    partial_note = ""
    if trade.get("partial_done"):
        partial_note = f"\n💡 TP1 partial sudah diambil di ${trade['partial_price']:,.4f}"

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>TRADE CLOSED — {sym} {d} | {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Entry  : ${entry:,.4f}\n"
        f"🏁 Exit   : ${exit_p:,.4f}\n"
        f"📌 Reason : <b>{reason}</b>\n"
        f"📦 Size   : ${size:.2f}\n\n"
        f"📊 <b>PnL: {pnl_p:+.2f}% (${pnl_u:+.2f})</b>\n"
        f"{partial_note}\n\n"
        f"📝 Auto-logged ke trade journal."
    )


# ── Internal alert formatters ─────────────────

def _fmt_be_alert(trade: dict, price: float) -> str:
    sym = trade["symbol"].replace("USDT", "")
    return (
        f"🛡️ <b>{sym} {trade['direction']} — BREAKEVEN AKTIF</b>\n\n"
        f"Price: <b>${price:,.4f}</b> ≥ BE trigger ${trade['be_trigger']:,.4f}\n\n"
        f"✅ SL dipindah ke entry (<b>${trade['entry_price']:,.4f}</b>)\n"
        f"→ Worst case sekarang: <b>break even</b>, tidak ada loss\n"
        f"→ Target berikutnya: TP1 ${trade['tp1']:,.4f}"
    )


def _fmt_tp1_alert(trade: dict, price: float) -> str:
    sym     = trade["symbol"].replace("USDT", "")
    half    = trade["size_usdt"] * 0.5
    pnl_p1  = half * (trade["tp1_pct"] / 100)
    t_stop  = trade.get("trailing_stop")
    return (
        f"🎯 <b>{sym} {trade['direction']} — TP1 TERCAPAI!</b>\n\n"
        f"Price: <b>${price:,.4f}</b> (+{trade['tp1_pct']:.1f}%)\n\n"
        f"✅ Ambil profit 50% sekarang → +${pnl_p1:.2f}\n"
        f"✅ SL dipindah ke entry (break even)\n"
        f"✅ Trailing stop aktif: ${t_stop:,.4f}\n\n"
        f"🎯 Target selanjutnya: TP2 ${trade['tp2']:,.4f} (+{trade['tp2_pct']:.1f}%)\n"
        f"⚠️ Sisanya (50%) jalan otomatis dengan trailing"
    )


def _fmt_tp2_alert(trade: dict, price: float) -> str:
    sym  = trade["symbol"].replace("USDT", "")
    pnl  = trade.get("pnl_usdt", 0) or 0
    pnl_p= trade.get("pnl_pct", 0) or 0
    return (
        f"🏆 <b>{sym} {trade['direction']} — TP2 TERCAPAI! FULL CLOSE</b>\n\n"
        f"Price: <b>${price:,.4f}</b>\n\n"
        f"💰 <b>Total PnL: {pnl_p:+.2f}% (${pnl:+.2f})</b>\n\n"
        f"📝 Otomatis di-log ke trade journal."
    )


def _fmt_trailing_alert(trade: dict, price: float, pnl_now: float) -> str:
    sym  = trade["symbol"].replace("USDT", "")
    pnl  = trade.get("pnl_usdt", 0) or 0
    return (
        f"⚡ <b>{sym} {trade['direction']} — TRAILING STOP HIT</b>\n\n"
        f"Price: <b>${price:,.4f}</b> menyentuh trailing stop\n"
        f"Trailing High: ${trade.get('trailing_high') or trade.get('trailing_low', 0):,.4f}\n\n"
        f"💰 <b>PnL: {pnl_now:+.2f}% (${pnl:+.2f})</b> (blended TP1 + trailing)\n\n"
        f"📝 Otomatis di-log ke trade journal."
    )


def _fmt_sl_alert(trade: dict, price: float, pnl_now: float) -> str:
    sym   = trade["symbol"].replace("USDT", "")
    pnl   = trade.get("pnl_usdt", 0) or 0
    sl_type = "Breakeven" if trade.get("sl_at_be") else "Initial SL"
    return (
        f"🛑 <b>{sym} {trade['direction']} — {sl_type} HIT</b>\n\n"
        f"Price: <b>${price:,.4f}</b> menyentuh SL ${trade['sl']:,.4f}\n\n"
        f"💸 <b>PnL: {pnl_now:+.2f}% (${pnl:+.2f})</b>\n\n"
        f"📝 Otomatis di-log ke trade journal."
    )


# ─────────────────────────────────────────────
# COMMAND PARSER
# ─────────────────────────────────────────────

def parse_trade_command(args: str) -> dict:
    """
    Parse args dari /trade command.
    Format: SYMBOL DIRECTION ENTRY [SIZE]
    Contoh: BTC LONG 95000 60
            BTCUSDT SHORT 95000
    """
    parts = args.strip().split()
    if len(parts) < 3:
        return {"error": "Format: /trade SYMBOL DIRECTION ENTRY [SIZE_USD]\nContoh: /trade BTC LONG 95000 60"}

    sym   = parts[0].upper()
    if not sym.endswith("USDT"):
        sym = sym + "USDT"
    direc = parts[1].upper()
    try:
        entry = float(parts[2].replace(",", ""))
    except ValueError:
        return {"error": f"Entry price tidak valid: '{parts[2]}'"}

    size = None  # None = auto dari compound balance
    if len(parts) >= 4:
        try:
            size = float(parts[3])
        except ValueError:
            return {"error": f"Size tidak valid: '{parts[3]}'"}

    if entry <= 0:
        return {"error": "Entry price harus > 0"}
    if size is not None and size <= 0:
        return {"error": "Size harus > 0"}
    if direc not in ("LONG", "SHORT"):
        return {"error": f"Direction harus LONG atau SHORT, bukan '{direc}'"}

    return {"symbol": sym, "direction": direc, "entry": entry, "size": size}
