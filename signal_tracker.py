#!/usr/bin/env python3
"""
SIGNAL OUTCOME TRACKER v1.0
============================
Auto-track semua sinyal yang dikirim bot.
Di setiap scan berikutnya, bot cek apakah sinyal sebelumnya hit TP atau SL.

Kalau sinyal ternyata SALAH (SL hit):
  → Trigger mini-backtest otomatis untuk strategy itu
  → Derive lesson dan inject ke learning engine
  → Kirim Telegram notif: "Signal X ternyata salah, ini yang perlu diperbaiki"

Flow:
  run_scan() → send_signal() → _record_pending_signal()
             ↓
  run_scan() berikutnya → _check_pending_signals()
             ↓
  SL hit?  → run_mini_backtest() + derive_lesson() + notify_telegram()
  TP hit?  → record_win() + derive_lesson()
  Expired? → record_neutral() (signal tidak bergerak)
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

log = logging.getLogger("signal_tracker")

BINANCE_BASE      = "https://api.binance.com/api/v3"
BINANCE_FUTURES   = "https://fapi.binance.com"
PENDING_FILE      = "pending_signals.json"
OUTCOME_FILE      = "signal_outcomes.json"

# Timeout: signal expired kalau belum hit TP/SL dalam X jam
SIGNAL_TIMEOUT_HOURS = {
    "SCREENER": 24,
    "PREPUMP":  12,
    "PREDUMP":  12,
    "SCALP":     2,
    "SWING":    24,
}

# Mini-backtest trigger: kalau X sinyal berturut-turut salah
AUTOBT_ON_CONSECUTIVE_LOSSES = 3   # 3 SL berturut-turut → auto backtest
AUTOBT_ON_WIN_RATE_DROP      = 40  # win rate < 40% dalam 10 terakhir → auto backtest
AUTOBT_DAYS                  = 14  # backtest 14 hari terakhir
AUTOBT_MIN_INTERVAL_HOURS    = 6   # jangan auto-backtest lebih dari 1x per 6 jam per strategy

# ─────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────

def _load_pending() -> list:
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE) as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Load pending corrupt/error ({PENDING_FILE}): {e}")
    return []


def _save_pending(data: list):
    try:
        tmp = PENDING_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, PENDING_FILE)   # atomic
    except Exception as e:
        log.warning(f"Save pending error: {e}")


def _load_outcomes() -> list:
    try:
        if os.path.exists(OUTCOME_FILE):
            with open(OUTCOME_FILE) as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Load outcomes corrupt/error ({OUTCOME_FILE}): {e}")
    return []


def _save_outcomes(data: list):
    try:
        tmp = OUTCOME_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data[-500:], f, indent=2)  # keep last 500
        os.replace(tmp, OUTCOME_FILE)   # atomic
    except Exception as e:
        log.warning(f"Save outcomes error: {e}")


# ─────────────────────────────────────────────
# RECORD PENDING SIGNAL
# ─────────────────────────────────────────────

def record_pending_signal(
    symbol: str,
    signal_type: str,   # SCREENER | PREPUMP | PREDUMP | SCALP | SWING | CONFIRMED
    direction: str,     # LONG | SHORT
    entry_price: float,
    tp: float,
    sl: float,
    score: int,
    confluence_level: str = "",
    reasons: list = None,
    strategy: str = "CONFIRMED",  # Strategi yang generate: scalp, prepump, predump, swing, atau CONFIRMED
):
    """
    Catat sinyal yang baru dikirim bot ke pending_signals.json.
    Dipanggil otomatis setiap kali bot kirim signal ke Telegram.
    """
    # ── SANITY GUARD: TP & SL harus di sisi yang benar relatif entry ──
    # Mencegah record cacat (mis. LONG dengan TP di bawah entry) yang bikin
    # outcome langsung ke-mark "TP_HIT" padahal sebetulnya rugi.
    try:
        e = float(entry_price); t = float(tp); s = float(sl)
    except (TypeError, ValueError):
        log.warning(f"⚠️ Reject signal {symbol} {direction}: harga non-numerik (entry={entry_price} tp={tp} sl={sl})")
        return
    if e <= 0 or t <= 0 or s <= 0:
        log.warning(f"⚠️ Reject signal {symbol} {direction}: harga <= 0 (entry={e} tp={t} sl={s})")
        return
    if direction == "LONG" and not (t > e > s):
        log.warning(f"⚠️ Reject signal {symbol} LONG: TP/SL sisi salah (tp={t} entry={e} sl={s}) — harus tp>entry>sl")
        return
    if direction == "SHORT" and not (t < e < s):
        log.warning(f"⚠️ Reject signal {symbol} SHORT: TP/SL sisi salah (tp={t} entry={e} sl={s}) — harus tp<entry<sl")
        return

    entry = {
        "id":               f"{symbol}_{int(time.time()*1000)}",
        "symbol":           symbol,
        "signal_type":      signal_type,
        "strategy":         strategy,
        "direction":        direction,
        "entry_price":      entry_price,
        "tp":               tp,
        "sl":               sl,
        "score":            score,
        "confluence_level": confluence_level,
        "reasons":          (reasons or [])[:3],
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "status":           "PENDING",
        "timeout_hours":    SIGNAL_TIMEOUT_HOURS.get(signal_type, 24),
    }
    pending = _load_pending()
    # Hindari duplikat symbol+direction dalam 30 menit
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    existing = [p for p in pending
                if p["symbol"] == symbol and p["direction"] == direction
                and p["created_at"] > cutoff and p["status"] == "PENDING"]
    if existing:
        log.debug(f"Duplikat signal {symbol} {direction} dalam 30m, skip")
        return

    pending.append(entry)
    _save_pending(pending)
    log.info(f"📌 Signal tracked: {symbol} {direction} @ {entry_price:.4f} | TP:{tp:.4f} SL:{sl:.4f}")


def _get_current_price(symbol: str) -> Optional[float]:
    """Fetch harga terkini dari Binance."""
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price",
                         params={"symbol": symbol}, timeout=5)
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception as e:
        log.warning(f"Price fetch error {symbol}: {e}")
    return None


def _get_price_history(symbol: str, hours_back: int = 24) -> list:
    """
    Ambil candle 15m sejak signal dibuat sampai sekarang.
    Untuk cek apakah TP/SL pernah kena di periode itu.
    """
    try:
        limit  = min(1000, int(hours_back * 4) + 10)  # 4 candles/jam untuk 15m
        r = requests.get(f"{BINANCE_BASE}/klines",
            params={"symbol": symbol, "interval": "15m", "limit": limit},
            timeout=10)
        if r.status_code == 200:
            return [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                     "low": float(c[3]), "close": float(c[4])} for c in r.json()]
    except Exception as e:
        log.warning(f"History fetch error {symbol}: {e}")
    return []


# ─────────────────────────────────────────────
# CHECK PENDING SIGNALS
# Dipanggil di setiap run_scan()
# ─────────────────────────────────────────────

def check_pending_signals(send_telegram_fn=None) -> list:
    """
    Cek semua pending signals — apakah sudah TP, SL, atau expired.
    Return list of resolved signals (bisa kosong).

    Dipanggil di awal setiap run_scan().
    Kalau ada yang resolve → trigger learning + optional auto-backtest.
    """
    pending   = _load_pending()
    outcomes  = _load_outcomes()
    still_pending = []
    resolved      = []

    now = datetime.now(timezone.utc)

    for sig in pending:
        if sig["status"] != "PENDING":
            continue

        # Guard: timestamp naive/legacy/korup tidak boleh meledakkan seluruh
        # loop (dulu TypeError "naive vs aware" / ValueError → semua pending
        # berhenti diproses). Normalisasi ke UTC; kalau gagal, biarkan pending.
        try:
            created_at = datetime.fromisoformat(sig["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError, TypeError) as e:
            log.warning(f"signal_tracker: created_at invalid {sig.get('symbol','?')}: {e} — skip")
            still_pending.append(sig)
            continue
        age_hours  = (now - created_at).total_seconds() / 3600
        timeout_h  = sig.get("timeout_hours", 24)

        symbol    = sig["symbol"]
        direction = sig["direction"]
        entry     = sig["entry_price"]
        tp        = sig["tp"]
        sl        = sig["sl"]

        # Fetch candles dari waktu signal dibuat
        candles = _get_price_history(symbol, hours_back=max(age_hours + 1, 4))

        # Filter candles setelah signal dibuat
        created_ts = int(created_at.timestamp() * 1000)
        future_candles = [c for c in candles if c["time"] > created_ts]

        outcome       = None
        exit_price    = None
        exit_time     = None

        # Iterate candle-by-candle (conservative: SL dulu)
        for c in future_candles:
            c_time = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
            if direction == "LONG":
                if c["low"] <= sl:
                    outcome, exit_price, exit_time = "SL_HIT", sl, c_time; break
                if c["high"] >= tp:
                    outcome, exit_price, exit_time = "TP_HIT", tp, c_time; break
            else:  # SHORT
                if c["high"] >= sl:
                    outcome, exit_price, exit_time = "SL_HIT", sl, c_time; break
                if c["low"] <= tp:
                    outcome, exit_price, exit_time = "TP_HIT", tp, c_time; break

        # Kalau belum hit, cek timeout
        if outcome is None:
            if age_hours >= timeout_h:
                # Expired: cek current price untuk final PnL
                curr_price = _get_current_price(symbol)
                if curr_price:
                    if direction == "LONG":
                        pnl_pct = (curr_price - entry) / entry * 100
                    else:
                        pnl_pct = (entry - curr_price) / entry * 100
                    outcome    = "EXPIRED_WIN" if pnl_pct > 0 else "EXPIRED_LOSS"
                    exit_price = curr_price
                else:
                    outcome    = "EXPIRED"
                    exit_price = entry
                exit_time = now
            else:
                # Masih dalam timeout → tetap pending
                still_pending.append(sig)
                continue

        # ── Resolved! ───────────────────────────
        hold_hours = (exit_time - created_at).total_seconds() / 3600 if exit_time else 0
        if exit_price and entry > 0:
            if direction == "LONG":
                pnl_pct = (exit_price - entry) / entry * 100
            else:
                pnl_pct = (entry - exit_price) / entry * 100
        else:
            pnl_pct = 0.0

        result = {**sig,
            "status":      outcome,
            "exit_price":  exit_price,
            "exit_time":   exit_time.isoformat() if exit_time else None,
            "pnl_pct":     round(pnl_pct, 3),
            "hold_hours":  round(hold_hours, 2),
            "resolved_at": now.isoformat(),
        }
        outcomes.append(result)
        resolved.append(result)
        log.info(f"✅ Signal resolved: {symbol} {direction} → {outcome} | PnL: {pnl_pct:+.2f}%")

    _save_pending(still_pending)
    _save_outcomes(outcomes)

    if resolved:
        _process_resolved_signals(resolved, send_telegram_fn)

    return resolved


# ─────────────────────────────────────────────
# PROCESS RESOLVED SIGNALS
# ─────────────────────────────────────────────

def _process_resolved_signals(resolved: list, send_telegram_fn=None):
    """
    Proses setiap resolved signal:
    1. Inject ke learning engine (record_signal_outcome)
    2. Cek apakah perlu trigger auto-backtest
    3. Kirim Telegram notif kalau relevan
    """
    try:
        from learning_engine import record_signal_outcome
        LEARNING_AVAILABLE = True
    except ImportError:
        LEARNING_AVAILABLE = False

    try:
        from symbol_memory import record_symbol_outcome
        SYMBOL_MEMORY_AVAILABLE = True
    except ImportError:
        SYMBOL_MEMORY_AVAILABLE = False

    for sig in resolved:
        outcome    = sig["status"]
        symbol     = sig["symbol"]
        sig_type   = sig["signal_type"]
        direction  = sig["direction"]
        entry      = sig["entry_price"]
        exit_p     = sig.get("exit_price", entry)
        score      = sig.get("score", 0)
        conf_level = sig.get("confluence_level", "")
        pnl        = sig.get("pnl_pct", 0)
        hold_h     = sig.get("hold_hours", 0)

        # Map outcome ke learning engine format
        le_outcome = {
            "TP_HIT":       "TP1_HIT",
            "SL_HIT":       "SL_HIT",
            "EXPIRED_WIN":  "MANUAL_CLOSE",
            "EXPIRED_LOSS": "EXPIRED",
            "EXPIRED":      "EXPIRED",
        }.get(outcome, "EXPIRED")

        # 1. Inject ke learning engine
        if LEARNING_AVAILABLE:
            try:
                record_signal_outcome(
                    symbol          = symbol,
                    signal_type     = sig_type,
                    direction       = direction,
                    entry_price     = entry,
                    score           = score,
                    confluence_level= conf_level,
                    outcome         = le_outcome,
                    exit_price      = exit_p,
                    hold_minutes    = int(hold_h * 60),
                    pnl_pct         = pnl,
                    notes           = f"Auto-tracked by signal_tracker",
                    reasons         = sig.get("reasons", []),
                )
                log.info(f"📚 Learning engine updated: {symbol} {le_outcome}")
            except Exception as e:
                log.warning(f"Learning engine update error: {e}")

        # 1b. Inject ke symbol memory (per-coin win/loss + auto-blacklist).
        # outcome di-pass apa adanya: _compute_stats mendeteksi TP/SL via
        # substring + pnl_pct, jadi "TP_HIT"/"SL_HIT"/"EXPIRED_*" valid.
        if SYMBOL_MEMORY_AVAILABLE:
            try:
                record_symbol_outcome(
                    symbol           = symbol,
                    signal_type      = sig_type,
                    direction        = direction,
                    outcome          = outcome,
                    pnl_pct          = pnl,
                    score            = score,
                    hold_minutes     = int(hold_h * 60),
                    entry_mode       = sig.get("entry_mode", ""),
                    confluence_level = conf_level,
                    notes            = "Auto-tracked by signal_tracker",
                )
                log.info(f"📊 Symbol memory updated: {symbol} {outcome}")
            except Exception as e:
                log.warning(f"Symbol memory update error: {e}")

        # 2. Kirim notif ke Telegram
        if send_telegram_fn:
            _send_outcome_notification(sig, send_telegram_fn)

    # 3. Cek trigger auto-backtest
    _check_autobacktest_trigger(resolved, send_telegram_fn)


def _send_outcome_notification(sig: dict, send_telegram_fn):
    """Kirim notif ke topic Market Update saat signal resolve (TP/SL/Expired)."""
    outcome   = sig["status"]
    symbol    = sig["symbol"].replace("USDT", "")
    direction = sig["direction"]
    pnl       = sig["pnl_pct"]
    hold_h    = sig.get("hold_hours", 0)
    sig_type  = sig["signal_type"]
    score     = sig.get("score", 0)

    if outcome == "TP_HIT":
        header  = f"✅ <b>TP HIT — {symbol}</b>"
        pnl_str = f"🟢 <b>+{pnl:.2f}%</b>"
    elif outcome == "SL_HIT":
        header  = f"❌ <b>SL HIT — {symbol}</b>"
        pnl_str = f"🔴 <b>{pnl:.2f}%</b>"
    elif outcome == "EXPIRED_WIN":
        header  = f"⏱️ <b>EXPIRED (profit) — {symbol}</b>"
        pnl_str = f"🟡 <b>+{pnl:.2f}%</b>"
    elif outcome == "EXPIRED_LOSS":
        header  = f"⏱️ <b>EXPIRED (loss) — {symbol}</b>"
        pnl_str = f"🟠 <b>{pnl:.2f}%</b>"
    else:
        return  # EXPIRED neutral → tidak notif

    dir_emoji = "🟢" if direction == "LONG" else "🔴"
    msg = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} {direction} | {sig_type} | Score: {score}\n"
        f"Entry  : <code>${sig['entry_price']:.4f}</code>\n"
        f"Exit   : <code>${sig.get('exit_price', 0):.4f}</code>\n"
        f"PnL    : {pnl_str}\n"
        f"Hold   : {hold_h:.1f}h\n"
    )
    if outcome == "SL_HIT":
        msg += "\n<i>🔄 Mini-backtest akan berjalan jika diperlukan.</i>"

    try:
        send_telegram_fn(msg)
    except Exception as e:
        log.warning(f"Notif send error: {e}")


# ─────────────────────────────────────────────
# AUTO-BACKTEST TRIGGER
# ─────────────────────────────────────────────

def _check_autobacktest_trigger(resolved: list, send_telegram_fn=None):
    """
    Cek apakah kondisi terpenuhi untuk trigger auto-backtest:
    1. X SL berturut-turut untuk 1 strategy
    2. Win rate < threshold dalam 10 sinyal terakhir per strategy

    Kalau trigger → run mini-backtest di background thread.
    """
    outcomes = _load_outcomes()
    if not outcomes:
        return

    # Group by strategy
    by_strategy = {}
    for o in outcomes:
        s = o.get("signal_type", "SCREENER")
        by_strategy.setdefault(s, []).append(o)

    for strategy, history in by_strategy.items():
        # Ambil 10 terbaru
        recent = sorted(history, key=lambda x: x.get("created_at", ""))[-10:]
        if len(recent) < 3:
            continue

        wins  = sum(1 for o in recent if o["status"] in ("TP_HIT", "EXPIRED_WIN"))
        total = len(recent)
        wr    = wins / total * 100

        # Cek consecutive losses (dari yang terbaru)
        consec_loss = 0
        for o in reversed(recent):
            if o["status"] in ("SL_HIT", "EXPIRED_LOSS"):
                consec_loss += 1
            else:
                break

        # Ambil symbol paling sering muncul di recent losses untuk backtest
        loss_symbols = [o["symbol"] for o in recent if o["status"] in ("SL_HIT", "EXPIRED_LOSS")]
        if not loss_symbols:
            continue
        target_symbol = max(set(loss_symbols), key=loss_symbols.count)

        should_bt  = False
        bt_reason  = ""

        if consec_loss >= AUTOBT_ON_CONSECUTIVE_LOSSES:
            should_bt = True
            bt_reason = f"{consec_loss} SL berturut-turut pada strategy {strategy}"

        if wr < AUTOBT_ON_WIN_RATE_DROP and total >= 5:
            should_bt = True
            bt_reason = f"Win rate turun ke {wr:.0f}% (dari {total} sinyal terakhir) — strategy {strategy}"

        if not should_bt:
            continue

        # Cek cooldown: jangan backtest lebih dari 1x per 6 jam per strategy
        if _is_in_cooldown(strategy):
            log.info(f"Auto-BT cooldown aktif untuk {strategy}, skip")
            continue

        log.info(f"🔄 Auto-backtest trigger: {strategy} | reason: {bt_reason}")
        _set_cooldown(strategy)

        # Kirim notif dulu
        if send_telegram_fn:
            sym_display = target_symbol.replace("USDT", "")
            try:
                send_telegram_fn(
                    f"🔄 *AUTO-BACKTEST TRIGGERED*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📋 Strategy : *{strategy.upper()}*\n"
                    f"💎 Coin     : *{sym_display}*\n"
                    f"📅 Period   : {AUTOBT_DAYS} hari\n"
                    f"⚠️ Trigger  : _{bt_reason}_\n\n"
                    f"⏳ _Memulai evaluasi otomatis..._"
                )
            except Exception:
                pass

        # Run backtest di background
        threading.Thread(
            target=_run_autobacktest,
            args=(target_symbol, strategy, bt_reason, send_telegram_fn),
            daemon=True
        ).start()


def _run_autobacktest(symbol: str, strategy: str, trigger_reason: str, send_telegram_fn=None):
    """
    Jalankan mini-backtest dan kirim hasilnya ke Telegram.
    Dipanggil dari background thread.
    """
    try:
        from backtest_engine import run_backtest, format_backtest_result
    except ImportError:
        log.warning("backtest_engine tidak tersedia untuk auto-backtest")
        return

    # Map strategy name ke backtest engine strategy name
    strategy_map = {
        "SCREENER": "scalp",
        "PREPUMP":  "prepump",
        "PREDUMP":  "predump",
        "SCALP":    "scalp",
        "SWING":    "swing",
    }
    bt_strategy = strategy_map.get(strategy.upper(), "scalp")

    log.info(f"🔍 Running auto-backtest: {symbol} {bt_strategy} {AUTOBT_DAYS}d")

    try:
        stats = run_backtest(symbol, bt_strategy, days=AUTOBT_DAYS)

        if "error" in stats:
            if send_telegram_fn:
                send_telegram_fn(
                    f"⚠️ Auto-backtest *{symbol} {bt_strategy}* gagal:\n"
                    f"`{stats['error']}`"
                )
            return

        # Format hasil + tambah context kenapa dijalankan
        result_msg = format_backtest_result(stats)
        sym_display = symbol.replace("USDT", "")

        header = (
            f"🔬 *AUTO-BACKTEST RESULT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ *Trigger:* _{trigger_reason}_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        # Generate rekomendasi berdasarkan hasil
        recommendation = _generate_recommendation(stats, strategy)
        footer = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 *REKOMENDASI:*\n"
            f"{recommendation}"
        )

        full_msg = header + result_msg + footer

        if send_telegram_fn:
            send_telegram_fn(full_msg)

        # Inject rekomendasi ke learning engine sebagai lesson
        try:
            from learning_engine import _load_lessons, _save_lessons
            data = _load_lessons()
            lesson = {
                "id":         int(time.time() * 1000),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "rule":       f"AUTO-BACKTEST [{sym_display}/{strategy}]: {recommendation}",
                "category":   "auto_backtest",
                "tags":       ["auto_backtest", strategy.lower(), sym_display.lower()],
                "confidence": 0.7,
                "pinned":     False,
                "source":     "auto_backtest",
                "win_rate":   stats.get("win_rate", 0),
                "profit_factor": stats.get("profit_factor", 0),
            }
            data["lessons"].append(lesson)
            # Keep max 50 lessons
            if len(data["lessons"]) > 50:
                # Hapus yang non-pinned paling lama
                non_pinned = [l for l in data["lessons"] if not l.get("pinned")]
                pinned     = [l for l in data["lessons"] if l.get("pinned")]
                data["lessons"] = pinned + non_pinned[-(50 - len(pinned)):]
            _save_lessons(data)
            log.info(f"📚 Auto-backtest lesson saved untuk {symbol} {strategy}")
        except Exception as e:
            log.warning(f"Lesson inject error: {e}")

    except Exception as e:
        log.error(f"Auto-backtest error: {e}", exc_info=True)
        if send_telegram_fn:
            send_telegram_fn(f"❌ Auto-backtest error: `{str(e)[:200]}`")


def _generate_recommendation(stats: dict, strategy: str) -> str:
    """
    Generate rekomendasi actionable berdasarkan hasil backtest.
    Singkat, langsung ke poin.
    """
    wr  = stats.get("win_rate", 0)
    pf  = stats.get("profit_factor", 0)
    dd  = stats.get("max_drawdown_pct", 0)
    n   = stats.get("total_trades", 0)
    avg_hold = stats.get("avg_hold_hours", 0)
    tp_count = stats.get("tp_count", 0)
    sl_count = stats.get("sl_count", 0)
    to_count = stats.get("timeout_count", 0)

    recs = []

    if n == 0:
        return "⚠️ Tidak ada trades — score threshold terlalu tinggi. Pertimbangkan turunkan min_score."

    if pf < 1.0 and n >= 5:
        recs.append(f"❌ Strategy *{strategy.upper()}* tidak profitable di 14 hari terakhir (PF={pf:.2f}). Pertimbangkan pause strategy ini sampai market condition berubah.")
    elif pf >= 1.5 and wr >= 50:
        recs.append(f"✅ Strategy masih solid (PF={pf:.2f}, WR={wr:.0f}%). Loss streak kemungkinan noise, bukan masalah strategy.")

    if sl_count > tp_count and sl_count > 0:
        sl_ratio = sl_count / n * 100
        recs.append(f"⚠️ SL rate tinggi ({sl_ratio:.0f}%). Coba perlebar SL sedikit atau tunggu konfirmasi lebih kuat sebelum entry.")

    if to_count > n * 0.5 and n > 3:
        recs.append(f"⏱️ Banyak timeout ({to_count}/{n}). TP mungkin terlalu jauh untuk kondisi market sekarang. Pertimbangkan TP lebih dekat.")

    if dd > 20:
        recs.append(f"📉 Max drawdown tinggi ({dd:.1f}%). Kurangi stake per trade atau tambah filter konfluensi.")

    if avg_hold > 18 and strategy in ("SCALP", "scalp"):
        recs.append(f"⏰ Rata-rata hold {avg_hold:.0f}h terlalu panjang untuk scalp. Strategi mungkin tidak cocok dengan market saat ini.")

    if not recs:
        recs.append(f"📊 Backtest normal (PF={pf:.2f}, WR={wr:.0f}%). Loss streak kemungkinan random — tetap ikuti strategy.")

    return "\n".join(recs)


# ─────────────────────────────────────────────
# COOLDOWN MANAGEMENT
# ─────────────────────────────────────────────

COOLDOWN_FILE = "autobt_cooldown.json"

def _is_in_cooldown(strategy: str) -> bool:
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE) as f:
                data = json.load(f)
            last = data.get(strategy)
            if last:
                last_time = datetime.fromisoformat(last)
                elapsed   = (datetime.now(timezone.utc) - last_time).total_seconds() / 3600
                return elapsed < AUTOBT_MIN_INTERVAL_HOURS
    except Exception:
        pass
    return False


def _set_cooldown(strategy: str):
    try:
        data = {}
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE) as f:
                data = json.load(f)
        data[strategy] = datetime.now(timezone.utc).isoformat()
        tmp = COOLDOWN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, COOLDOWN_FILE)   # atomic
    except Exception as e:
        log.warning(f"Cooldown set error: {e}")


# ─────────────────────────────────────────────
# STATS & SUMMARY
# ─────────────────────────────────────────────

def get_tracker_stats() -> dict:
    """Summary statistik dari semua signal yang pernah ditrack."""
    outcomes = _load_outcomes()
    pending  = _load_pending()

    if not outcomes:
        return {"total": 0, "pending": len(pending)}

    by_type   = {}
    by_symbol = {}
    for o in outcomes:
        s   = o.get("signal_type", "UNKNOWN")
        sym = o.get("symbol", "UNKNOWN")
        st  = o.get("status", "")
        pnl = o.get("pnl_pct", 0)

        by_type.setdefault(s, {"tp": 0, "sl": 0, "exp": 0, "pnl": []})
        by_symbol.setdefault(sym, {"tp": 0, "sl": 0, "exp": 0, "pnl": [], "recent": []})

        for bucket in (by_type[s], by_symbol[sym]):
            if st == "TP_HIT":   bucket["tp"] += 1
            elif st == "SL_HIT": bucket["sl"] += 1
            else:                bucket["exp"] += 1
            if pnl != 0:         bucket["pnl"].append(pnl)

        by_symbol[sym]["recent"].append(st)

    stats = {
        "total":      len(outcomes),
        "pending":    len(pending),
        "by_type":    {},
        "by_symbol":  {},
    }

    for stype, data in by_type.items():
        total_s = data["tp"] + data["sl"] + data["exp"]
        wr      = data["tp"] / total_s * 100 if total_s > 0 else 0
        avg_pnl = np.mean(data["pnl"]) if data["pnl"] else 0
        stats["by_type"][stype] = {
            "total": total_s,
            "tp": data["tp"], "sl": data["sl"],
            "win_rate": round(wr, 1),
            "avg_pnl": round(avg_pnl, 2),
        }

    for sym, data in by_symbol.items():
        total_s  = data["tp"] + data["sl"] + data["exp"]
        wr       = data["tp"] / total_s * 100 if total_s > 0 else 0
        avg_pnl  = np.mean(data["pnl"]) if data["pnl"] else 0
        recent5  = data["recent"][-5:]
        recent_wr = sum(1 for s in recent5 if s == "TP_HIT") / len(recent5) * 100 if recent5 else 0
        stats["by_symbol"][sym] = {
            "total": total_s,
            "tp": data["tp"], "sl": data["sl"],
            "win_rate": round(wr, 1),
            "recent_wr": round(recent_wr, 1),
            "avg_pnl": round(avg_pnl, 2),
        }

    return stats


def get_coin_signal_stats(symbol: str) -> Optional[dict]:
    """Return live signal win rate stats for a specific coin from signal_outcomes.json."""
    stats = get_tracker_stats()
    return stats.get("by_symbol", {}).get(symbol.upper())


def format_tracker_summary() -> str:
    """Format tracker stats untuk Telegram (/signals command)."""
    stats   = get_tracker_stats()
    pending = _load_pending()
    now     = datetime.now(timezone.utc)

    ts = now.strftime("%d %b %Y %H:%M UTC")
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📡 *SIGNAL TRACKER*",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📊 Total resolved: *{stats['total']}*",
        f"⏳ Pending: *{stats['pending']}*",
        "",
    ]

    if stats.get("by_type"):
        lines.append("─── BY STRATEGY ───")
        for stype, data in sorted(stats["by_type"].items()):
            wr    = data["win_rate"]
            n     = data["total"]
            avg_p = data["avg_pnl"]
            tp    = data["tp"]
            sl    = data["sl"]
            wr_emoji = "✅" if wr >= 55 else "⚠️" if wr >= 40 else "🔴"
            lines.append(
                f"{wr_emoji} *{stype}* ({n} signals)\n"
                f"  WR: {wr:.0f}% | TP:{tp} SL:{sl} | Avg:{avg_p:+.2f}%"
            )
        lines.append("")

    # ── Per-coin stats (sort by most signals, show top 8) ──
    by_sym = stats.get("by_symbol", {})
    if by_sym:
        sorted_syms = sorted(by_sym.items(), key=lambda x: x[1]["total"], reverse=True)[:8]
        lines.append("─── BY COIN (top signals) ───")
        for sym, data in sorted_syms:
            wr     = data["win_rate"]
            rwr    = data["recent_wr"]
            n      = data["total"]
            avg_p  = data["avg_pnl"]
            trend  = "↑" if rwr >= wr else ("↓" if rwr < wr - 10 else "→")
            wr_e   = "✅" if wr >= 55 else "⚠️" if wr >= 40 else "🔴"
            name   = sym.replace("USDT", "")
            lines.append(
                f"  {wr_e} *{name}* ({n}) WR:{wr:.0f}%{trend} Avg:{avg_p:+.2f}%"
            )
        lines.append("")

    if pending:
        lines.append("─── PENDING SIGNALS ───")
        for p in pending[:5]:
            sym   = p["symbol"].replace("USDT", "")
            direc = p["direction"]
            age_h = (now - datetime.fromisoformat(p["created_at"])).total_seconds() / 3600
            timeout_h = p.get("timeout_hours", 24)
            remaining = max(0, timeout_h - age_h)
            dir_e = "🟢" if direc == "LONG" else "🔴"
            lines.append(
                f"  {dir_e} {sym} {direc} | {p['signal_type']} "
                f"| Entry:${p['entry_price']:.4f} "
                f"| {remaining:.0f}h left"
            )
        if len(pending) > 5:
            lines.append(f"  _...dan {len(pending)-5} lainnya_")

    lines.append("\n💡 _Signals ditrack otomatis. /btall untuk batch backtest semua coins._")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# INTEGRATION HOOK — Dipanggil dari run_scan()
# ─────────────────────────────────────────────

def take_lesson_snapshot() -> int:
    """
    Tiap 12 jam: ambil unrealized P&L semua pending signals dan inject ke learning engine.
    Sinyal TIDAK diclose — tracking tetap jalan sampai TP/SL/expiry asli.
    Return jumlah sinyal yang di-snapshot.
    """
    try:
        from learning_engine import record_signal_outcome
    except ImportError:
        log.warning("take_lesson_snapshot: learning_engine tidak tersedia")
        return 0

    pending = _load_pending()
    now     = datetime.now(timezone.utc)
    snapped = 0
    changed = False

    for sig in pending:
        if sig.get("status") != "PENDING":
            continue

        # Cek apakah sudah >= 12 jam sejak snapshot terakhir (atau belum pernah snapshot)
        last_snap_raw = sig.get("last_snapshot_at")
        if last_snap_raw:
            try:
                last_snap_dt = datetime.fromisoformat(last_snap_raw)
                if last_snap_dt.tzinfo is None:
                    last_snap_dt = last_snap_dt.replace(tzinfo=timezone.utc)
                if (now - last_snap_dt).total_seconds() < 12 * 3600:
                    continue
            except Exception:
                pass

        symbol    = sig.get("symbol", "")
        direction = sig.get("direction", "")
        entry     = sig.get("entry_price", 0.0)
        if not symbol or not direction or entry <= 0:
            continue

        curr_price = _get_current_price(symbol)
        if not curr_price:
            continue

        if direction == "LONG":
            pnl_pct = (curr_price - entry) / entry * 100
        else:
            pnl_pct = (entry - curr_price) / entry * 100

        try:
            created_at = datetime.fromisoformat(sig["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            hold_min = int((now - created_at).total_seconds() / 60)
        except Exception:
            hold_min = 0

        try:
            record_signal_outcome(
                symbol           = symbol,
                signal_type      = sig.get("signal_type", "SCREENER"),
                direction        = direction,
                entry_price      = entry,
                score            = sig.get("score", 0),
                confluence_level = sig.get("confluence_level", ""),
                outcome          = "SNAPSHOT_12H",
                exit_price       = curr_price,
                hold_minutes     = hold_min,
                pnl_pct          = round(pnl_pct, 3),
                notes            = "12h unrealized snapshot",
                indicators       = sig.get("indicators", {}),
                reasons          = sig.get("reasons", []),
            )
            sig["last_snapshot_at"] = now.isoformat()
            snapped += 1
            changed  = True
            log.info(f"📸 12h snapshot: {symbol} {direction} | unrealized PnL: {pnl_pct:+.2f}%")
        except Exception as e:
            log.warning(f"take_lesson_snapshot error {symbol}: {e}")

    if changed:
        _save_pending(pending)

    if snapped:
        log.info(f"📸 Lesson snapshot selesai: {snapped} sinyal di-snapshot")
    return snapped


def on_scan_start(send_telegram_fn=None) -> list:
    """
    Dipanggil di AWAL setiap run_scan().
    Cek pending signals, resolve yang sudah selesai, trigger auto-backtest kalau perlu.
    Return list sinyal yang baru resolved.
    """
    try:
        return check_pending_signals(send_telegram_fn)
    except Exception as e:
        log.warning(f"Signal tracker check error: {e}")
        return []


def on_signal_sent(symbol: str, signal_type: str, direction: str,
                   entry_price: float, tp: float, sl: float,
                   score: int, confluence_level: str = "", reasons: list = None,
                   strategy: str = "CONFIRMED"):
    """
    Dipanggil setelah bot kirim signal ke Telegram.
    Record signal ke pending list.

    Args:
        strategy: Strategy yang generate sinyal (scalp, prepump, predump, swing, atau CONFIRMED)
    """
    try:
        record_pending_signal(
            symbol=symbol, signal_type=signal_type, direction=direction,
            entry_price=entry_price, tp=tp, sl=sl, score=score,
            confluence_level=confluence_level, reasons=reasons, strategy=strategy
        )
    except Exception as e:
        log.warning(f"on_signal_sent error: {e}")
