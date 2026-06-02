"""
SYMBOL MEMORY ENGINE
====================
Inspired by Meridian (yunus-0x/meridian) pool-memory.js → ported to Python.

Per-symbol tracking:
  - Win rate, avg hold, common failure patterns per coin
  - Auto-derived lessons dari closed trades per symbol
  - Blacklist per symbol (terlalu sering fakeout, dll)
  - Inject ke /analyze prompt sebagai context historis

File: symbol_memory.json
"""

import json
import os
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

SYMBOL_MEMORY_FILE = "symbol_memory.json"

# Min trades per symbol sebelum lessons dianggap reliable
MIN_TRADES_FOR_LESSON = 3
# Blacklist kalau SL rate > threshold ini
BLACKLIST_SL_THRESHOLD = 0.75  # 75% SL rate → blacklist sementara
BLACKLIST_MIN_TRADES   = 5     # minimal trades sebelum bisa diblacklist


# ─────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────

def _load() -> dict:
    if not os.path.exists(SYMBOL_MEMORY_FILE):
        return {}
    try:
        with open(SYMBOL_MEMORY_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    try:
        with open(SYMBOL_MEMORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"symbol_memory save error: {e}")


# ─────────────────────────────────────────────
# RECORD TRADE OUTCOME
# ─────────────────────────────────────────────

def record_symbol_outcome(
    symbol: str,
    signal_type: str,     # PREPUMP | PREDUMP | SCALP | SWING | SCREENER
    direction: str,       # LONG | SHORT
    outcome: str,         # TP1_HIT | TP2_HIT | SL_HIT | EXPIRED_WIN | EXPIRED_LOSS
    pnl_pct: float,
    score: int,
    hold_minutes: int = 0,
    entry_mode: str = "",        # MOMENTUM_NOW | RETEST_WAIT
    confluence_level: str = "",
    notes: str = "",
    indicators: dict = None,
):
    """
    Catat satu trade outcome ke symbol memory.
    Dipanggil dari /logoutcome atau signal_tracker auto-resolve.
    """
    data = _load()

    sym_key = symbol.upper().replace("USDT", "")
    if sym_key not in data:
        data[sym_key] = {
            "symbol": sym_key,
            "trades": [],
            "stats": {},
            "lessons": [],
            "blacklisted": False,
            "blacklist_reason": "",
            "last_updated": "",
        }

    trade_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "signal_type": signal_type,
        "direction": direction,
        "outcome": outcome,
        "pnl_pct": round(pnl_pct, 2),
        "score": score,
        "hold_minutes": hold_minutes,
        "entry_mode": entry_mode,
        "confluence_level": confluence_level,
        "notes": notes,
        "indicators": indicators or {},
    }

    data[sym_key]["trades"].append(trade_entry)
    data[sym_key]["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Keep last 50 trades per symbol
    if len(data[sym_key]["trades"]) > 50:
        data[sym_key]["trades"] = data[sym_key]["trades"][-50:]

    # Recompute stats
    data[sym_key]["stats"] = _compute_stats(data[sym_key]["trades"])

    # Derive lessons
    new_lessons = _derive_symbol_lessons(sym_key, data[sym_key]["trades"])
    data[sym_key]["lessons"] = new_lessons

    # Auto-blacklist check
    _check_blacklist(data, sym_key)

    _save(data)
    log.info(f"📊 Symbol memory updated: {sym_key} → {outcome} ({pnl_pct:+.1f}%)")
    return data[sym_key]


# ─────────────────────────────────────────────
# STATS COMPUTATION
# ─────────────────────────────────────────────

def _compute_stats(trades: list) -> dict:
    if not trades:
        return {}

    total   = len(trades)
    wins    = [t for t in trades if t["pnl_pct"] > 0]
    losses  = [t for t in trades if t["pnl_pct"] <= 0]
    sl_hits = [t for t in trades if "SL_HIT" in t.get("outcome", "")]
    tp_hits = [t for t in trades if "TP" in t.get("outcome", "")]

    avg_pnl    = sum(t["pnl_pct"] for t in trades) / total
    avg_hold   = sum(t.get("hold_minutes", 0) for t in trades) / total
    avg_score  = sum(t.get("score", 0) for t in trades) / total

    # Per signal_type breakdown
    by_type = {}
    for t in trades:
        st = t.get("signal_type", "UNKNOWN")
        if st not in by_type:
            by_type[st] = {"total": 0, "wins": 0, "sl_hits": 0}
        by_type[st]["total"] += 1
        if t["pnl_pct"] > 0:
            by_type[st]["wins"] += 1
        if "SL_HIT" in t.get("outcome", ""):
            by_type[st]["sl_hits"] += 1

    # Per entry_mode breakdown
    momentum_trades = [t for t in trades if t.get("entry_mode") == "MOMENTUM_NOW"]
    retest_trades   = [t for t in trades if t.get("entry_mode") == "RETEST_WAIT"]

    momentum_wr = (sum(1 for t in momentum_trades if t["pnl_pct"] > 0) / len(momentum_trades) * 100) if momentum_trades else None
    retest_wr   = (sum(1 for t in retest_trades   if t["pnl_pct"] > 0) / len(retest_trades)   * 100) if retest_trades   else None

    return {
        "total_trades":   total,
        "win_rate_pct":   round(len(wins) / total * 100, 1),
        "sl_rate_pct":    round(len(sl_hits) / total * 100, 1),
        "avg_pnl_pct":    round(avg_pnl, 2),
        "avg_hold_min":   round(avg_hold, 0),
        "avg_score":      round(avg_score, 1),
        "tp_hits":        len(tp_hits),
        "sl_hits":        len(sl_hits),
        "by_type":        by_type,
        "momentum_wr":    round(momentum_wr, 1) if momentum_wr is not None else None,
        "retest_wr":      round(retest_wr, 1)   if retest_wr   is not None else None,
    }


# ─────────────────────────────────────────────
# LESSON DERIVATION PER SYMBOL
# ─────────────────────────────────────────────

def _derive_symbol_lessons(symbol: str, trades: list) -> list:
    """
    Derive lessons otomatis dari trade history per symbol.
    Mirip Meridian studyTopLPers tapi untuk futures trading.
    """
    if len(trades) < MIN_TRADES_FOR_LESSON:
        return []

    lessons = []
    wins    = [t for t in trades if t["pnl_pct"] > 0]
    losses  = [t for t in trades if t["pnl_pct"] <= 0]
    sl_hits = [t for t in trades if "SL_HIT" in t.get("outcome", "")]

    total = len(trades)
    wr    = len(wins) / total * 100

    # ── Lesson 1: Overall performance summary ──
    lessons.append({
        "type": "PERFORMANCE",
        "text": f"{symbol}: {total} trades, WR {wr:.0f}%, avg PnL {sum(t['pnl_pct'] for t in trades)/total:+.1f}%",
        "confidence": "HIGH" if total >= 10 else "MEDIUM",
    })

    # ── Lesson 2: Signal type yang paling bagus/jelek ──
    by_type = {}
    for t in trades:
        st = t.get("signal_type", "?")
        by_type.setdefault(st, []).append(t)

    for st, ts in by_type.items():
        if len(ts) >= 3:
            type_wr = sum(1 for t in ts if t["pnl_pct"] > 0) / len(ts) * 100
            type_sl = sum(1 for t in ts if "SL_HIT" in t.get("outcome", "")) / len(ts) * 100
            if type_wr >= 70:
                lessons.append({
                    "type": "SIGNAL_TYPE_WIN",
                    "text": f"{symbol} + {st}: WR {type_wr:.0f}% dari {len(ts)} trades → high confidence setup",
                    "confidence": "HIGH",
                })
            elif type_sl >= 60:
                lessons.append({
                    "type": "SIGNAL_TYPE_AVOID",
                    "text": f"{symbol} + {st}: SL rate {type_sl:.0f}% dari {len(ts)} trades → hindari atau skip",
                    "confidence": "HIGH",
                })

    # ── Lesson 3: Score threshold pattern ──
    if len(losses) >= 3:
        avg_loss_score = sum(t.get("score", 0) for t in losses) / len(losses)
        avg_win_score  = sum(t.get("score", 0) for t in wins) / len(wins) if wins else 0
        if avg_loss_score < 70 and len(losses) >= 3:
            lessons.append({
                "type": "SCORE_THRESHOLD",
                "text": f"{symbol}: rata-rata loss terjadi di score {avg_loss_score:.0f} (win avg: {avg_win_score:.0f}) → naikkan min score ke {min(80, int(avg_loss_score)+8)}",
                "confidence": "MEDIUM",
            })

    # ── Lesson 4: Entry mode effectiveness ──
    momentum = [t for t in trades if t.get("entry_mode") == "MOMENTUM_NOW"]
    retest   = [t for t in trades if t.get("entry_mode") == "RETEST_WAIT"]
    if len(momentum) >= 3 and len(retest) >= 3:
        mom_wr = sum(1 for t in momentum if t["pnl_pct"] > 0) / len(momentum) * 100
        ret_wr = sum(1 for t in retest   if t["pnl_pct"] > 0) / len(retest)   * 100
        better = "MOMENTUM_NOW" if mom_wr > ret_wr else "RETEST_WAIT"
        diff   = abs(mom_wr - ret_wr)
        if diff >= 15:
            lessons.append({
                "type": "ENTRY_MODE",
                "text": f"{symbol}: {better} lebih efektif ({max(mom_wr,ret_wr):.0f}% vs {min(mom_wr,ret_wr):.0f}%) → prefer {better} untuk coin ini",
                "confidence": "MEDIUM",
            })

    # ── Lesson 5: Confluence level pattern ──
    poor_losses = [t for t in losses if t.get("confluence_level") in ("POOR", "FAIR")]
    if len(poor_losses) >= 2 and len(poor_losses) / max(1, len(losses)) >= 0.6:
        lessons.append({
            "type": "CONFLUENCE_WARNING",
            "text": f"{symbol}: {len(poor_losses)}/{len(losses)} losses terjadi di confluence POOR/FAIR → skip jika level < GOOD",
            "confidence": "HIGH",
        })

    # ── Lesson 6: Waktu hold optimal ──
    if wins:
        avg_hold_win  = sum(t.get("hold_minutes", 0) for t in wins) / len(wins)
        avg_hold_loss = sum(t.get("hold_minutes", 0) for t in losses) / len(losses) if losses else 0
        if avg_hold_win < 120:  # < 2 jam
            lessons.append({
                "type": "HOLD_TIME",
                "text": f"{symbol}: rata-rata winner selesai dalam {avg_hold_win:.0f} menit → jangan hold terlalu lama",
                "confidence": "LOW" if len(wins) < 5 else "MEDIUM",
            })

    return lessons


# ─────────────────────────────────────────────
# BLACKLIST
# ─────────────────────────────────────────────

def _check_blacklist(data: dict, sym_key: str):
    """Auto-blacklist kalau SL rate terlalu tinggi."""
    sym_data = data[sym_key]
    trades   = sym_data["trades"]
    stats    = sym_data["stats"]

    if len(trades) < BLACKLIST_MIN_TRADES:
        return

    # Cek last 10 trades (recent performance)
    recent = trades[-10:]
    recent_sl = sum(1 for t in recent if "SL_HIT" in t.get("outcome", "")) / len(recent)

    if recent_sl >= BLACKLIST_SL_THRESHOLD:
        sym_data["blacklisted"] = True
        sym_data["blacklist_reason"] = (
            f"SL rate {recent_sl*100:.0f}% dalam {len(recent)} trades terakhir "
            f"(threshold: {BLACKLIST_SL_THRESHOLD*100:.0f}%)"
        )
        log.warning(f"⛔ {sym_key} auto-blacklisted: {sym_data['blacklist_reason']}")
    else:
        # Auto-unblacklist kalau sudah membaik
        if sym_data.get("blacklisted") and recent_sl < 0.5:
            sym_data["blacklisted"] = False
            sym_data["blacklist_reason"] = ""
            log.info(f"✅ {sym_key} removed from blacklist: SL rate improved to {recent_sl*100:.0f}%")


def is_blacklisted(symbol: str) -> tuple[bool, str]:
    """
    Cek apakah symbol ada di blacklist.
    Return (True, reason) atau (False, "")
    """
    data    = _load()
    sym_key = symbol.upper().replace("USDT", "")
    sym_data = data.get(sym_key, {})
    return sym_data.get("blacklisted", False), sym_data.get("blacklist_reason", "")


def manual_blacklist(symbol: str, reason: str = "Manual blacklist"):
    data    = _load()
    sym_key = symbol.upper().replace("USDT", "")
    if sym_key not in data:
        data[sym_key] = {
            "symbol": sym_key, "trades": [], "stats": {},
            "lessons": [], "blacklisted": True,
            "blacklist_reason": reason, "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    else:
        data[sym_key]["blacklisted"] = True
        data[sym_key]["blacklist_reason"] = reason
    _save(data)
    return f"⛔ {sym_key} diblacklist: {reason}"


def manual_unblacklist(symbol: str):
    data    = _load()
    sym_key = symbol.upper().replace("USDT", "")
    if sym_key in data:
        data[sym_key]["blacklisted"] = False
        data[sym_key]["blacklist_reason"] = ""
        _save(data)
    return f"✅ {sym_key} dihapus dari blacklist"


# ─────────────────────────────────────────────
# QUERY & INJECT TO PROMPT
# ─────────────────────────────────────────────

def get_symbol_context(symbol: str) -> dict:
    """
    Ambil context lengkap untuk satu symbol.
    Dipakai untuk inject ke /analyze prompt.
    """
    data    = _load()
    sym_key = symbol.upper().replace("USDT", "")
    return data.get(sym_key, {})


def get_symbol_memory(symbol: str) -> dict:
    """Ringkasan memori per-symbol untuk inject ke prompt AI (DeepSeek).

    Mengembalikan shape yang dipakai deepseek_analyze_coin:
      {win_rate, total_trades, best_signal_type, lessons: [str], ...}
    Kosong {} kalau belum ada histori.
    """
    ctx = get_symbol_context(symbol)
    if not ctx or not ctx.get("trades"):
        return {}

    stats   = ctx.get("stats", {})
    by_type = stats.get("by_type", {})

    # Signal type dengan win-rate terbaik (minimal 2 trade)
    best, best_wr = "?", -1.0
    for st, d in by_type.items():
        if d.get("total", 0) >= 2:
            wr = d["wins"] / d["total"] * 100
            if wr > best_wr:
                best_wr, best = wr, st

    lessons_txt = [l.get("text", "") for l in ctx.get("lessons", [])
                   if l.get("confidence") in ("HIGH", "MEDIUM")][:4]

    return {
        "win_rate":         stats.get("win_rate_pct", 0),
        "total_trades":     stats.get("total_trades", 0),
        "best_signal_type": best,
        "sl_rate":          stats.get("sl_rate_pct", 0),
        "avg_pnl":          stats.get("avg_pnl_pct", 0),
        "lessons":          lessons_txt,
        "blacklisted":      ctx.get("blacklisted", False),
    }


def build_symbol_context_block(symbol: str) -> str:
    """
    Format context block untuk inject ke Gemini prompt.
    Singkat, actionable, langsung ke poin.
    """
    ctx     = get_symbol_context(symbol)
    sym_key = symbol.upper().replace("USDT", "")

    if not ctx or not ctx.get("trades"):
        return ""

    stats   = ctx.get("stats", {})
    lessons = ctx.get("lessons", [])
    trades  = ctx.get("trades", [])
    bl      = ctx.get("blacklisted", False)
    bl_r    = ctx.get("blacklist_reason", "")

    lines = [f"═══ SYMBOL MEMORY: {sym_key} ═══"]

    if bl:
        lines.append(f"⛔ BLACKLISTED: {bl_r}")

    if stats:
        lines.append(
            f"📊 History: {stats['total_trades']} trades | "
            f"WR {stats['win_rate_pct']}% | SL rate {stats['sl_rate_pct']}% | "
            f"Avg PnL {stats['avg_pnl_pct']:+.1f}%"
        )
        if stats.get("momentum_wr") is not None and stats.get("retest_wr") is not None:
            lines.append(
                f"🎯 Entry Mode: MOMENTUM WR={stats['momentum_wr']}% | RETEST WR={stats['retest_wr']}%"
            )

    # Top lessons (max 4)
    high_conf = [l for l in lessons if l.get("confidence") in ("HIGH", "MEDIUM")]
    for l in high_conf[:4]:
        t = l.get("type", "")
        icon = {"PERFORMANCE": "📈", "SIGNAL_TYPE_WIN": "✅", "SIGNAL_TYPE_AVOID": "⚠️",
                "SCORE_THRESHOLD": "🎯", "ENTRY_MODE": "⚡", "CONFLUENCE_WARNING": "🔴",
                "HOLD_TIME": "⏱"}.get(t, "•")
        lines.append(f"{icon} {l['text']}")

    # Last 3 trades quick summary
    if trades:
        recent_3 = trades[-3:]
        recent_str = " | ".join(
            f"{t['signal_type']} {t['direction']} {t['pnl_pct']:+.1f}%"
            for t in reversed(recent_3)
        )
        lines.append(f"📝 Recent: {recent_str}")

    return "\n".join(lines)


def get_all_stats_summary() -> str:
    """Summary semua symbol yang ada di memory. Untuk /symbolstats command."""
    data = _load()
    if not data:
        return "📭 Belum ada symbol memory. Gunakan /logoutcome untuk mencatat hasil trade."

    lines = ["📊 <b>Symbol Memory Summary</b>\n"]
    # Sort by total trades desc
    sorted_syms = sorted(data.items(), key=lambda x: x[1].get("stats", {}).get("total_trades", 0), reverse=True)

    for sym_key, sym_data in sorted_syms[:15]:  # max 15 di tampilan
        stats = sym_data.get("stats", {})
        bl    = sym_data.get("blacklisted", False)
        if not stats:
            continue
        bl_icon = "⛔ " if bl else ""
        wr      = stats.get("win_rate_pct", 0)
        wr_icon = "🟢" if wr >= 60 else "🟡" if wr >= 40 else "🔴"
        lines.append(
            f"{bl_icon}{wr_icon} <b>{sym_key}</b> — "
            f"{stats['total_trades']}T | WR {wr}% | Avg {stats['avg_pnl_pct']:+.1f}%"
        )

    blacklisted = [k for k, v in data.items() if v.get("blacklisted")]
    if blacklisted:
        lines.append(f"\n⛔ Blacklisted: {', '.join(blacklisted)}")

    lines.append(f"\n💡 /symbolmemory BTC — detail per coin")
    return "\n".join(lines)


def get_symbol_detail(symbol: str) -> str:
    """Detail lengkap untuk satu symbol. Untuk /symbolmemory BTC command."""
    ctx     = get_symbol_context(symbol)
    sym_key = symbol.upper().replace("USDT", "")

    if not ctx or not ctx.get("trades"):
        return f"📭 Belum ada data untuk <b>{sym_key}</b>. Catat trade dulu via /logoutcome."

    stats   = ctx.get("stats", {})
    lessons = ctx.get("lessons", [])
    trades  = ctx.get("trades", [])
    bl      = ctx.get("blacklisted", False)
    bl_r    = ctx.get("blacklist_reason", "")
    updated = ctx.get("last_updated", "")[:16].replace("T", " ")

    lines = [f"📊 <b>Symbol Memory: {sym_key}</b>", f"🕐 Updated: {updated}\n"]

    if bl:
        lines.append(f"⛔ <b>BLACKLISTED</b>: {bl_r}\n")

    if stats:
        lines.append("─── STATISTIK ───")
        lines.append(f"Total Trades   : {stats['total_trades']}")
        lines.append(f"Win Rate       : {stats['win_rate_pct']}%")
        lines.append(f"SL Rate        : {stats['sl_rate_pct']}%")
        lines.append(f"Avg PnL        : {stats['avg_pnl_pct']:+.1f}%")
        lines.append(f"Avg Hold       : {stats['avg_hold_min']:.0f} menit")
        lines.append(f"Avg Score      : {stats['avg_score']:.0f}/100")

        if stats.get("momentum_wr") is not None:
            lines.append(f"MOMENTUM WR    : {stats['momentum_wr']}% ({len([t for t in trades if t.get('entry_mode')=='MOMENTUM_NOW'])} trades)")
        if stats.get("retest_wr") is not None:
            lines.append(f"RETEST WR      : {stats['retest_wr']}% ({len([t for t in trades if t.get('entry_mode')=='RETEST_WAIT'])} trades)")

        # Per signal type
        if stats.get("by_type"):
            lines.append("\n─── PER SIGNAL TYPE ───")
            for st, d in stats["by_type"].items():
                st_wr = d["wins"] / d["total"] * 100 if d["total"] > 0 else 0
                lines.append(f"  {st}: {d['total']}T WR {st_wr:.0f}% SL {d['sl_hits']}")

    if lessons:
        lines.append("\n─── LESSONS ───")
        for l in lessons[:6]:
            conf_icon = "🔴" if l["confidence"] == "HIGH" else "🟡"
            lines.append(f"{conf_icon} {l['text']}")

    # Last 5 trades
    if trades:
        lines.append("\n─── 5 TRADE TERAKHIR ───")
        for t in reversed(trades[-5:]):
            ts    = t["ts"][:10]
            pnl   = t["pnl_pct"]
            icon  = "✅" if pnl > 0 else "❌"
            em    = t.get("entry_mode", "?")[:3]
            lines.append(f"  {icon} {ts} {t['signal_type']} {t['direction']} {pnl:+.1f}% [{em}]")

    return "\n".join(lines)
