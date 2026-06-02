"""
LEARNING ENGINE — Crypto Screener Bot v10
==========================================
Ported & adapted dari Meridian (yunus-0x/meridian) ke Python.

Fitur:
  1. Decision Log     — setiap signal/alert/trade dicatat struktural
  2. Lesson Engine    — lessons diderive dari outcome, di-inject ke AI prompt
  3. Threshold Evol.  — auto-tune bobot scoring berdasarkan win/loss history
  4. Daily Analysis   — DeepSeek AI analisa signal outcomes, kirim recommendation

File yang dihasilkan:
  decision_log.json   — log setiap keputusan screening
  lessons.json        — lessons + performance history
"""

import json
import os
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# FILE PATHS
# ─────────────────────────────────────────────
LESSONS_FILE     = "lessons.json"
DECISION_LOG_FILE = "decision_log.json"

# Threshold evolution
# Sampel minimal dinaikkan supaya tidak overfit: beberapa kekalahan sial tidak
# boleh permanen menaikkan gate entry yang dibaca screener live.
MIN_EVOLVE_POSITIONS = 30   # minimal total closed signals sebelum evolve
MIN_LOSERS_PER_TYPE  = 8    # minimal loser per signal_type sebelum adjust threshold-nya
EVOLVE_INTERVAL      = 10    # jalankan auto-evolve tiap +N closed signals baru
MAX_CHANGE_PER_STEP  = 0.20 # max 20% perubahan per step
MAX_MANUAL_LESSON_LEN = 400

# Lesson injection caps
PINNED_CAP = 5
ROLE_CAP   = 8
RECENT_CAP = 15

# Role-aware tag mapping (adapted untuk crypto screener)
ROLE_TAGS = {
    "SCREENER": ["screening", "entry", "confluence", "signal", "volume", "prepump", "predump", "fvg", "ob"],
    "SCALP":    ["scalp", "15m", "1h", "entry", "sweep", "rejection"],
    "SWING":    ["swing", "4h", "1h", "structure", "liquidity", "target"],
    "GENERAL":  [],
}


# ─────────────────────────────────────────────
# I/O HELPERS
# ─────────────────────────────────────────────

def _load_lessons() -> dict:
    if not os.path.exists(LESSONS_FILE):
        return {"lessons": [], "performance": []}
    try:
        with open(LESSONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"lessons": [], "performance": []}


def _save_lessons(data: dict):
    tmp = LESSONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LESSONS_FILE)   # atomic


def _load_decision_log() -> list:
    if not os.path.exists(DECISION_LOG_FILE):
        return []
    try:
        with open(DECISION_LOG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_decision_log(entries: list):
    tmp = DECISION_LOG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, DECISION_LOG_FILE)   # atomic


# ─────────────────────────────────────────────
# DECISION LOG
# ─────────────────────────────────────────────

def log_decision(
    actor: str,           # "SCREENER" | "PREPUMP" | "PREDUMP" | "SCALP" | "SWING"
    symbol: str,
    decision: str,        # "ALERT" | "SKIP" | "WATCH"
    summary: str,
    score: int,
    confluence_level: str = "",
    direction: str = "",
    reasons: list = None,
    trade_plan: dict = None,
    metadata: dict = None,
):
    """
    Catat setiap keputusan screening ke decision_log.json.
    Di-inject ke AI prompt supaya Gemini tau konteks keputusan sebelumnya.
    """
    entry = {
        "id": int(time.time() * 1000),
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "symbol": symbol,
        "decision": decision,
        "summary": summary,
        "score": score,
        "confluence_level": confluence_level,
        "direction": direction,
        "top_reasons": (reasons or [])[:3],
        "trade_plan": trade_plan or {},
        "metadata": metadata or {},
    }

    entries = _load_decision_log()
    entries.append(entry)

    # Keep last 200 decisions only
    if len(entries) > 200:
        entries = entries[-200:]

    _save_decision_log(entries)
    log.info(f"📝 Decision logged: [{actor}] {symbol} → {decision} (score={score})")
    return entry


def get_recent_decisions(limit: int = 10, actor: str = None, symbol: str = None) -> list:
    """Ambil keputusan terbaru, optional filter by actor/symbol."""
    entries = _load_decision_log()

    if actor:
        entries = [e for e in entries if e.get("actor") == actor]
    if symbol:
        entries = [e for e in entries if e.get("symbol") == symbol]

    return entries[-limit:]


def format_decisions_for_prompt(limit: int = 5) -> str:
    """Format recent decisions untuk inject ke Gemini/Claude prompt."""
    recent = get_recent_decisions(limit=limit)
    if not recent:
        return ""

    lines = ["[KEPUTUSAN SCREENING TERBARU]"]
    for d in reversed(recent):
        ts_short = d["ts"][:16].replace("T", " ")
        lines.append(
            f"• [{ts_short}] {d['actor']} | {d['symbol']} → {d['decision']} "
            f"(score={d['score']}, {d.get('confluence_level','')}, {d.get('direction','')}): "
            f"{d['summary']}"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────
# SIGNAL OUTCOME RECORDING
# ─────────────────────────────────────────────

def record_signal_outcome(
    symbol: str,
    signal_type: str,       # "SCREENER" | "PREPUMP" | "PREDUMP" | "SCALP" | "SWING"
    direction: str,         # "LONG" | "SHORT"
    entry_price: float,
    score: int,
    confluence_level: str,
    outcome: str,           # "TP1_HIT" | "TP2_HIT" | "SL_HIT" | "EXPIRED" | "MANUAL_CLOSE"
    exit_price: float,
    hold_minutes: int,
    pnl_pct: float,
    notes: str = "",
    indicators: dict = None,
):
    """
    Catat hasil signal setelah posisi ditutup.
    Data ini yang dipakai buat derive lessons dan threshold evolution.

    Panggil via command /logoutcome dari Telegram.
    """
    data = _load_lessons()

    perf_entry = {
        "id": int(time.time() * 1000),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "signal_type": signal_type,
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "score": score,
        "confluence_level": confluence_level,
        "outcome": outcome,
        "pnl_pct": round(pnl_pct, 2),
        "hold_minutes": hold_minutes,
        "notes": notes,
        "indicators": indicators or {},
    }

    data["performance"].append(perf_entry)

    # Derive lesson dari outcome
    lesson = _derive_lesson(perf_entry)
    if lesson:
        data["lessons"].append(lesson)
        log.info(f"📚 New lesson derived: {lesson['rule'][:80]}...")

    _save_lessons(data)

    # Auto-evolve tiap +EVOLVE_INTERVAL closed signals BARU sejak evolve terakhir.
    # (Dulu pakai `perf_count % MIN_EVOLVE_POSITIONS == 0` yang gampang ke-skip
    # kalau ada path yang menambah >1 entry sekaligus.)
    perf_count = len(data["performance"])
    last_evolve = data.get("_last_evolve_count", 0)
    if perf_count >= MIN_EVOLVE_POSITIONS and (perf_count - last_evolve) >= EVOLVE_INTERVAL:
        result = evolve_thresholds(data["performance"])
        data["_last_evolve_count"] = perf_count
        _save_lessons(data)
        if result and result.get("changes"):
            log.info(f"🧬 Auto-evolved thresholds: {result['changes']}")

    return perf_entry


# ─────────────────────────────────────────────
# LESSON DERIVATION
# ─────────────────────────────────────────────

def _derive_lesson(perf: dict) -> Optional[dict]:
    """
    Derive lesson dari performance entry.
    Hanya generate kalau outcome jelas bagus atau jelas jelek.
    """
    outcome      = perf.get("outcome", "")
    pnl          = perf.get("pnl_pct", 0)
    score        = perf.get("score", 0)
    confluence   = perf.get("confluence_level", "")
    signal_type  = perf.get("signal_type", "")
    symbol       = perf.get("symbol", "")
    direction    = perf.get("direction", "")
    hold_min     = perf.get("hold_minutes", 0)
    indicators   = perf.get("indicators", {})

    # Kategorisasi
    if outcome in ("TP1_HIT", "TP2_HIT") and pnl > 0:
        category = "good"
    elif outcome == "SL_HIT" or pnl < -3:
        category = "bad"
    elif outcome == "EXPIRED" and abs(pnl) < 1:
        category = "neutral"
    elif pnl > 1:
        category = "good"
    else:
        category = "poor"

    if category == "neutral":
        return None  # tidak ada yang menarik untuk dipelajari

    # Build rule text
    rule = ""
    tags = [signal_type.lower(), direction.lower(), confluence.lower()]

    context_parts = [
        f"symbol={symbol}",
        f"type={signal_type}",
        f"score={score}",
        f"confluence={confluence}",
        f"direction={direction}",
        f"hold={hold_min}m",
    ]

    # Tambah indicator context kalau ada
    if indicators.get("funding_rate") is not None:
        context_parts.append(f"funding={indicators['funding_rate']:.3f}%")
        tags.append("funding")
    if indicators.get("ls_ratio") is not None:
        context_parts.append(f"ls_ratio={indicators['ls_ratio']:.2f}")
    if indicators.get("rsi_1h") is not None:
        context_parts.append(f"rsi1h={indicators['rsi_1h']:.0f}")

    context = ", ".join(context_parts)

    if category == "good":
        if score >= 80:
            rule = f"WORKED WELL: {context} → PnL +{pnl:.1f}% ({outcome}). Score 80+ di {confluence} confluence reliable."
            tags.extend(["screening", "high_score"])
        elif pnl > 3:
            rule = f"WORKED: {context} → PnL +{pnl:.1f}% ({outcome}). Setup ini effective, replicate kalau kondisi sama."
            tags.append("worked")
        else:
            rule = f"SMALL WIN: {context} → PnL +{pnl:.1f}% ({outcome}). Cukup tapi bisa lebih baik."
            tags.append("worked")

    elif category == "bad":
        if confluence in ("POOR", "FAIR") and outcome == "SL_HIT":
            rule = f"AVOID: {context} → SL hit, PnL {pnl:.1f}%. Confluence {confluence} terlalu rendah untuk entry. Skip signal dengan score < 50 atau confluence POOR."
            tags.extend(["risk", "confluence"])
        elif hold_min > 120 and outcome == "SL_HIT":
            rule = f"FAILED SLOW: {context} → SL hit setelah {hold_min}m. Hold terlalu lama tanpa momentum. Pertimbangkan time-based exit kalau setup tidak bergerak dalam 2 jam."
            tags.extend(["risk", "hold_time"])
        else:
            rule = f"FAILED: {context} → {outcome}, PnL {pnl:.1f}%. Hindari kondisi serupa."
            tags.append("failed")

    elif category == "poor":
        rule = f"WEAK: {context} → {outcome}, PnL {pnl:.1f}%. Setup ini tidak efisien, cari yang lebih kuat."
        tags.append("weak")

    if not rule:
        return None

    # Confidence scoring (lebih tinggi = lebih pasti)
    confidence = 0.35
    if category == "good" and pnl > 3:
        confidence = 0.85
    elif category == "good":
        confidence = 0.65
    elif category == "bad" and outcome == "SL_HIT":
        confidence = 0.88
    elif category == "bad":
        confidence = 0.70
    elif category == "poor":
        confidence = 0.50

    return {
        "id": int(time.time() * 1000) + 1,
        "rule": rule,
        "tags": list(set(tags)),
        "outcome": category,
        "source_type": "performance",
        "confidence": round(confidence, 2),
        "context": context,
        "pnl_pct": pnl,
        "signal_type": signal_type,
        "confluence_level": confluence,
        "hold_minutes": hold_min,
        "pinned": False,
        "role": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────
# MANUAL LESSONS
# ─────────────────────────────────────────────

def add_manual_lesson(rule: str, tags: list = None, pinned: bool = False, role: str = None) -> dict:
    """Tambah lesson secara manual via /addlesson command."""
    # Sanitize
    rule = rule.strip()[:MAX_MANUAL_LESSON_LEN]
    if not rule:
        return {}

    data = _load_lessons()
    lesson = {
        "id": int(time.time() * 1000),
        "rule": rule,
        "tags": tags or [],
        "outcome": "manual",
        "source_type": "manual",
        "confidence": 0.75,
        "pinned": pinned,
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    data["lessons"].append(lesson)
    _save_lessons(data)
    log.info(f"📌 Manual lesson added: {rule[:60]}...")
    return lesson


def pin_lesson(lesson_id: int) -> bool:
    data = _load_lessons()
    for l in data["lessons"]:
        if l.get("id") == lesson_id:
            l["pinned"] = True
            _save_lessons(data)
            return True
    return False


def delete_lesson(lesson_id: int) -> bool:
    data = _load_lessons()
    before = len(data["lessons"])
    data["lessons"] = [l for l in data["lessons"] if l.get("id") != lesson_id]
    _save_lessons(data)
    return len(data["lessons"]) < before


def list_lessons(role: str = None, pinned_only: bool = False, limit: int = 20) -> list:
    data = _load_lessons()
    lessons = data["lessons"]

    if pinned_only:
        lessons = [l for l in lessons if l.get("pinned")]
    if role:
        lessons = [l for l in lessons if not l.get("role") or l.get("role") == role]

    return lessons[-limit:]


# ─────────────────────────────────────────────
# LESSON INJECTION (for AI prompts)
# ─────────────────────────────────────────────

def get_lessons_for_prompt(agent_type: str = "GENERAL", max_lessons: int = None) -> Optional[str]:
    """
    Ambil lessons terformat untuk inject ke Gemini/Claude system prompt.
    3-tier: pinned → role-matched → recent.
    """
    data = _load_lessons()
    all_lessons = data.get("lessons", [])

    if not all_lessons:
        return None

    _max = max_lessons or RECENT_CAP

    outcome_priority = {"bad": 0, "poor": 1, "failed": 1, "good": 2, "worked": 2, "manual": 1, "neutral": 3}
    def by_priority(l):
        return outcome_priority.get(l.get("outcome", "neutral"), 3)

    # Tier 1: Pinned
    pinned = [l for l in all_lessons if l.get("pinned") and (not l.get("role") or l.get("role") == agent_type or agent_type == "GENERAL")]
    pinned.sort(key=by_priority)
    pinned = pinned[:PINNED_CAP]
    used_ids = {l["id"] for l in pinned}

    # Tier 2: Role-matched
    role_tags = ROLE_TAGS.get(agent_type, [])
    def is_role_match(l):
        if l["id"] in used_ids: return False
        role_ok = not l.get("role") or l.get("role") == agent_type or agent_type == "GENERAL"
        tag_ok  = not role_tags or not l.get("tags") or any(t in role_tags for t in l.get("tags", []))
        return role_ok and tag_ok

    role_matched = [l for l in all_lessons if is_role_match(l)]
    role_matched.sort(key=by_priority)
    role_matched = role_matched[:ROLE_CAP]
    used_ids.update(l["id"] for l in role_matched)

    # Tier 3: Recent fill
    remaining = _max - len(pinned) - len(role_matched)
    recent = [l for l in all_lessons if l["id"] not in used_ids]
    recent.sort(key=lambda l: l.get("created_at", ""), reverse=True)
    recent = recent[:max(0, remaining)]

    selected = pinned + role_matched + recent
    if not selected:
        return None

    def fmt_lesson(l):
        ts    = (l.get("created_at") or "")[:10]
        pin   = "📌 " if l.get("pinned") else ""
        cat   = l.get("outcome", "?").upper()
        conf  = f" [{l.get('confidence', 0)*100:.0f}%]" if l.get("confidence") else ""
        return f"{pin}[{cat}]{conf}[{ts}] {l['rule']}"

    sections = []
    if pinned:       sections.append(f"── PINNED ({len(pinned)}) ──\n" + "\n".join(fmt_lesson(l) for l in pinned))
    if role_matched: sections.append(f"── {agent_type} ({len(role_matched)}) ──\n" + "\n".join(fmt_lesson(l) for l in role_matched))
    if recent:       sections.append(f"── RECENT ({len(recent)}) ──\n" + "\n".join(fmt_lesson(l) for l in recent))

    return "\n\n".join(sections)


def get_performance_summary() -> Optional[dict]:
    """Summary stats dari semua signal yang di-track."""
    data = _load_lessons()
    perf = data.get("performance", [])
    if not perf:
        return None

    total   = len(perf)
    wins    = [p for p in perf if p.get("pnl_pct", 0) > 0]
    losses  = [p for p in perf if p.get("pnl_pct", 0) < 0]
    avg_pnl = sum(p.get("pnl_pct", 0) for p in perf) / total
    tp_hits = [p for p in perf if "TP" in p.get("outcome", "")]
    sl_hits = [p for p in perf if "SL_HIT" in p.get("outcome", "")]

    return {
        "total_signals":  total,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate_pct":   round(len(wins) / total * 100, 1),
        "avg_pnl_pct":    round(avg_pnl, 2),
        "tp1_tp2_hits":   len(tp_hits),
        "sl_hits":        len(sl_hits),
        "total_lessons":  len(data.get("lessons", [])),
    }


# ─────────────────────────────────────────────
# THRESHOLD EVOLUTION
# ─────────────────────────────────────────────

def evolve_thresholds(perf_data: list = None) -> Optional[dict]:
    """
    Analisis performance history → auto-adjust scoring thresholds.
    Dipanggil auto tiap 5 closed signals, atau manual via /evolve.

    Returns dict of changes made ke thresholds.
    """
    if perf_data is None:
        data = _load_lessons()
        perf_data = data.get("performance", [])

    if len(perf_data) < MIN_EVOLVE_POSITIONS:
        return {"error": f"Minimal {MIN_EVOLVE_POSITIONS} signal diperlukan, baru {len(perf_data)}"}

    winners = [p for p in perf_data if p.get("pnl_pct", 0) > 0]
    losers  = [p for p in perf_data if p.get("pnl_pct", 0) < -3]

    if len(losers) < MIN_LOSERS_PER_TYPE:
        return {"error": f"Belum cukup loser untuk evolve (perlu {MIN_LOSERS_PER_TYPE}, baru {len(losers)})"}

    changes   = {}
    rationale = {}

    # ── 1. PREPUMP_ALERT_THRESHOLD ──────────────────────────────
    # Kalau banyak prepump signal dengan score rendah yang SL hit → naikkan threshold
    prepump_losers  = [p for p in losers  if p.get("signal_type") == "PREPUMP"]
    prepump_winners = [p for p in winners if p.get("signal_type") == "PREPUMP"]
    if len(prepump_losers) >= MIN_LOSERS_PER_TYPE:
        avg_loser_score = sum(p.get("score", 0) for p in prepump_losers) / len(prepump_losers)
        if avg_loser_score < 75:
            new_thresh = min(85, round(avg_loser_score + 5))
            changes["PREPUMP_ALERT_THRESHOLD"]   = new_thresh
            rationale["PREPUMP_ALERT_THRESHOLD"] = (
                f"Avg loser prepump score={avg_loser_score:.0f} → naikkan threshold ke {new_thresh}"
            )

    # ── 2. PREDUMP_ALERT_THRESHOLD ─────────────────────────────
    predump_losers = [p for p in losers if p.get("signal_type") == "PREDUMP"]
    if len(predump_losers) >= MIN_LOSERS_PER_TYPE:
        avg_loser_score = sum(p.get("score", 0) for p in predump_losers) / len(predump_losers)
        if avg_loser_score < 75:
            new_thresh = min(85, round(avg_loser_score + 5))
            changes["PREDUMP_ALERT_THRESHOLD"]   = new_thresh
            rationale["PREDUMP_ALERT_THRESHOLD"] = (
                f"Avg loser predump score={avg_loser_score:.0f} → naikkan threshold ke {new_thresh}"
            )

    # ── 3. SCALP_MIN_SCORE ──────────────────────────────────────
    scalp_losers  = [p for p in losers  if p.get("signal_type") == "SCALP"]
    scalp_winners = [p for p in winners if p.get("signal_type") == "SCALP"]
    if len(scalp_losers) >= MIN_LOSERS_PER_TYPE:
        avg_loser_score = sum(p.get("score", 0) for p in scalp_losers) / len(scalp_losers)
        avg_win_score   = sum(p.get("score", 0) for p in scalp_winners) / max(1, len(scalp_winners))
        if avg_loser_score < avg_win_score - 10:
            new_min = min(75, round(avg_loser_score + 8))
            changes["SCALP_MIN_SCORE"]   = new_min
            rationale["SCALP_MIN_SCORE"] = (
                f"Scalp loser avg={avg_loser_score:.0f} vs winner avg={avg_win_score:.0f} → raise min ke {new_min}"
            )

    # ── 4. Confluence filter suggestion ────────────────────────
    poor_conf_losses = [p for p in losers if p.get("confluence_level") in ("POOR", "FAIR")]
    if len(poor_conf_losses) >= 2 and len(poor_conf_losses) / max(1, len(losers)) > 0.6:
        changes["_suggestion_confluence"] = "Hindari entry di POOR/FAIR confluence — terlalu banyak SL hits"
        rationale["_suggestion_confluence"] = f"{len(poor_conf_losses)} dari {len(losers)} losses terjadi di confluence rendah"

    # ── Record sebagai lesson ───────────────────────────────────
    if changes:
        lesson_text = (
            f"[AUTO-EVOLVED @ {len(perf_data)} signals] "
            + " | ".join(f"{k}={v}" for k, v in changes.items() if not k.startswith("_"))
            + " — "
            + "; ".join(rationale.values())
        )
        add_manual_lesson(rule=lesson_text, tags=["evolution", "config_change"], pinned=False)
        log.info(f"🧬 Threshold evolution: {changes}")

        # Tulis ke dynamic_thresholds.json agar screener bisa baca langsung
        _write_dynamic_thresholds(changes)

    return {"changes": changes, "rationale": rationale, "total_analyzed": len(perf_data)}


DYNAMIC_THRESHOLDS_FILE = "dynamic_thresholds.json"


def _write_dynamic_thresholds(changes: dict):
    """Tulis threshold overrides ke JSON agar bisa dibaca screener tanpa restart."""
    try:
        existing = {}
        if os.path.exists(DYNAMIC_THRESHOLDS_FILE):
            with open(DYNAMIC_THRESHOLDS_FILE) as f:
                existing = json.load(f)
        existing.update({k: v for k, v in changes.items() if not k.startswith("_")})
        existing["_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = DYNAMIC_THRESHOLDS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(existing, f, indent=2)
        os.replace(tmp, DYNAMIC_THRESHOLDS_FILE)   # atomic
        # Invalidate cache supaya screener langsung pakai nilai baru.
        _dyn_cache["ts"] = 0.0
        log.info(f"🧬 dynamic_thresholds.json diupdate: {list(changes.keys())}")
    except Exception as e:
        log.warning(f"Gagal tulis dynamic_thresholds: {e}")


_dyn_cache = {"data": None, "ts": 0.0}
_DYN_TTL   = 30.0   # detik — file hanya berubah saat /evolve, jadi cache aman


def get_dynamic_thresholds() -> dict:
    """
    Baca threshold overrides dari dynamic_thresholds.json (cached, TTL 30s).
    Keys yang dipakai screener: PREPUMP_ALERT_THRESHOLD, PREDUMP_ALERT_THRESHOLD,
    SCALP_MIN_SCORE. Returns empty dict jika file tidak ada atau error.
    """
    import time as _t
    now = _t.time()
    if _dyn_cache["data"] is not None and (now - _dyn_cache["ts"]) < _DYN_TTL:
        return _dyn_cache["data"]
    data = {}
    try:
        if os.path.exists(DYNAMIC_THRESHOLDS_FILE):
            with open(DYNAMIC_THRESHOLDS_FILE) as f:
                data = json.load(f)
    except Exception:
        data = {}
    _dyn_cache["data"] = data
    _dyn_cache["ts"]   = now
    return data


# ─────────────────────────────────────────────
# TELEGRAM COMMAND HANDLERS
# ─────────────────────────────────────────────

def handle_logoutcome_command(args: str, send_fn) -> str:
    """
    Command: /logoutcome SYMBOL TYPE OUTCOME PNL [notes]
    Contoh:  /logoutcome BTCUSDT SCALP TP1_HIT +2.3 setup bersih
             /logoutcome SOLUSDT PREPUMP SL_HIT -1.5 false breakout
    """
    parts = args.strip().split(maxsplit=4)
    if len(parts) < 4:
        return (
            "❌ Format: <code>/logoutcome SYMBOL TYPE OUTCOME PNL [notes]</code>\n\n"
            "<b>TYPE:</b> SCREENER | PREPUMP | PREDUMP | SCALP | SWING\n"
            "<b>OUTCOME:</b> TP1_HIT | TP2_HIT | SL_HIT | EXPIRED | MANUAL_CLOSE\n"
            "<b>PNL:</b> angka dalam % (e.g. +2.5 atau -1.2)\n\n"
            "Contoh: <code>/logoutcome BTCUSDT SCALP TP1_HIT +2.3 setup clean</code>"
        )

    symbol  = parts[0].upper().replace("USDT", "") + "USDT"
    sig_type = parts[1].upper()
    outcome  = parts[2].upper()
    try:
        pnl = float(parts[3].replace("+", ""))
    except ValueError:
        return "❌ PnL harus angka, contoh: +2.3 atau -1.5"
    notes = parts[4] if len(parts) > 4 else ""

    if sig_type not in ("SCREENER", "PREPUMP", "PREDUMP", "SCALP", "SWING"):
        return "❌ TYPE tidak valid. Gunakan: SCREENER | PREPUMP | PREDUMP | SCALP | SWING"
    if outcome not in ("TP1_HIT", "TP2_HIT", "SL_HIT", "EXPIRED", "MANUAL_CLOSE"):
        return "❌ OUTCOME tidak valid. Gunakan: TP1_HIT | TP2_HIT | SL_HIT | EXPIRED | MANUAL_CLOSE"

    entry = record_signal_outcome(
        symbol=symbol,
        signal_type=sig_type,
        direction="LONG" if pnl >= 0 else "SHORT",  # simplified
        entry_price=0,  # tidak required di command
        exit_price=0,
        score=0,
        confluence_level="",
        outcome=outcome,
        pnl_pct=pnl,
        hold_minutes=0,
        notes=notes,
    )

    emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "⚪"
    summary = get_performance_summary()

    msg = (
        f"{emoji} <b>Outcome Logged</b>\n"
        f"📍 {symbol} [{sig_type}] → <b>{outcome}</b>\n"
        f"💹 PnL: <b>{pnl:+.2f}%</b>\n"
        f"📝 Notes: {notes or '-'}\n\n"
    )
    if summary:
        msg += (
            f"📊 <b>Signal Stats ({summary['total_signals']} total):</b>\n"
            f"  Win Rate: {summary['win_rate_pct']}%\n"
            f"  Avg PnL : {summary['avg_pnl_pct']:+.2f}%\n"
            f"  TP Hits : {summary['tp1_tp2_hits']} | SL Hits: {summary['sl_hits']}\n"
            f"  Lessons : {summary['total_lessons']}"
        )
    return msg


def handle_lessons_command(args: str) -> str:
    """Command: /lessons [all|pinned|recent]"""
    arg = args.strip().lower() if args else "recent"
    pinned_only = arg == "pinned"
    lessons = list_lessons(pinned_only=pinned_only, limit=15)

    if not lessons:
        return "📚 Belum ada lessons tersimpan.\nGunakan /logoutcome untuk mencatat hasil signal."

    lines = [f"📚 <b>Lessons ({arg.upper()}) — {len(lessons)} entries</b>\n"]
    for i, l in enumerate(reversed(lessons), 1):
        ts   = (l.get("created_at") or "")[:10]
        pin  = "📌 " if l.get("pinned") else ""
        cat  = l.get("outcome", "?").upper()
        conf = f" [{l.get('confidence', 0)*100:.0f}%]" if l.get("confidence") else ""
        rule = l.get("rule", "")[:120]
        lines.append(f"{i}. {pin}<b>[{cat}]{conf}</b> [{ts}]\n   {rule}")
        if i >= 10:
            lines.append(f"   ... +{len(lessons)-10} lebih")
            break

    lines.append("\n💡 <i>/addlesson [teks] — tambah lesson manual</i>")
    return "\n".join(lines)


def handle_decisions_command(args: str) -> str:
    """Command: /decisions [N] — lihat N keputusan terbaru"""
    try:
        limit = int(args.strip()) if args.strip() else 8
    except ValueError:
        limit = 8

    decisions = get_recent_decisions(limit=limit)
    if not decisions:
        return "📝 Belum ada keputusan tersimpan."

    lines = [f"📝 <b>{limit} Keputusan Terbaru:</b>\n"]
    for d in reversed(decisions):
        ts    = d.get("ts", "")[:16].replace("T", " ")
        sym   = d.get("symbol", "?")
        actor = d.get("actor", "?")
        dec   = d.get("decision", "?")
        score = d.get("score", 0)
        conf  = d.get("confluence_level", "")
        summ  = d.get("summary", "")[:80]
        emoji = {"ALERT": "🔔", "SKIP": "⏭", "WATCH": "👁"}.get(dec, "•")
        lines.append(f"{emoji} [{ts}] <b>{sym}</b> [{actor}] → {dec} (score={score}, {conf})\n   {summ}")

    lines.append("\n💡 <i>/decisions 15 — tampilkan 15 terbaru</i>")
    return "\n".join(lines)


def handle_evolve_command() -> str:
    """Command: /evolve — trigger threshold evolution manual"""
    data    = _load_lessons()
    perf    = data.get("performance", [])

    if len(perf) < MIN_EVOLVE_POSITIONS:
        return (
            f"⚠️ Minimal <b>{MIN_EVOLVE_POSITIONS} signal</b> dibutuhkan untuk evolve.\n"
            f"Saat ini: <b>{len(perf)} signal</b> tercatat.\n\n"
            f"Gunakan /logoutcome untuk mencatat hasil trade dulu."
        )

    result = evolve_thresholds(perf)

    if not result or not result.get("changes"):
        return (
            f"✅ Evolve selesai — <b>tidak ada perubahan</b> diperlukan.\n"
            f"Data dianalisis: {len(perf)} signals.\n"
            f"Thresholds sudah optimal berdasarkan data saat ini."
        )

    changes   = result["changes"]
    rationale = result["rationale"]

    lines = [f"🧬 <b>Threshold Evolution ({len(perf)} signals)</b>\n"]
    for key, val in changes.items():
        if key.startswith("_"):
            lines.append(f"💡 Saran: {val}")
        else:
            reason = rationale.get(key, "")
            lines.append(f"📊 <b>{key}</b> → <code>{val}</code>\n   └ {reason}")

    lines.append(
        f"\n⚠️ <i>Update nilai ini manual di config bot kamu ya bro.</i>\n"
        f"Copy angka di atas → paste ke bagian CONFIG di v10.py"
    )
    return "\n".join(lines)


def handle_addlesson_command(args: str) -> str:
    """Command: /addlesson [teks lesson]"""
    rule = args.strip()
    if not rule or len(rule) < 10:
        return "❌ Lesson terlalu pendek. Min 10 karakter.\nContoh: /addlesson SOL sering fakeout di 15M kalau funding masih positif"

    lesson = add_manual_lesson(rule=rule, tags=["manual"], pinned=False)
    return (
        f"📌 <b>Lesson ditambahkan!</b>\n"
        f"ID: <code>{lesson.get('id', '?')}</code>\n"
        f"Rule: {rule[:120]}\n\n"
        f"Lesson ini akan di-inject ke AI prompt saat /analyze dipanggil."
    )


# ─────────────────────────────────────────────
# UTILITY: Inject ke AI Prompt
# ─────────────────────────────────────────────

def build_ai_context_block(agent_type: str = "SCREENER") -> str:
    """
    Build context block untuk inject ke Gemini/Claude prompt.
    Gabungan: recent decisions + lessons.
    """
    parts = []

    lessons_text = get_lessons_for_prompt(agent_type=agent_type, max_lessons=12)
    if lessons_text:
        parts.append(f"═══ LESSONS FROM PAST SIGNALS ═══\n{lessons_text}")

    decisions_text = format_decisions_for_prompt(limit=4)
    if decisions_text:
        parts.append(decisions_text)

    return "\n\n".join(parts)


def handle_freetext_logpnl(user_text: str, gemini_fn) -> str:
    """
    /logpnl dengan free-text natural language.
    Gemini parse teks lo → extract lesson → simpan ke lessons.json.

    user_text   : apapun yang lo tulis setelah /logpnl
    gemini_fn   : callable(prompt: str) -> str  (pakai gemini_analyze dari bot)
    """
    if not user_text or len(user_text.strip()) < 5:
        return (
            "❓ Tulis apapun setelah /logpnl, contoh:\n"
            "• <code>/logpnl BTC entry ga kesentuh, harga naik duluan</code>\n"
            "• <code>/logpnl SOL TP1 hit tapi TP2 miss, spread terlalu lebar</code>\n"
            "• <code>/logpnl POPCAT SL kena padahal setup bagus, timing salah</code>"
        )

    # ── Step 1: Gemini parse freetext ────────────────────────
    parse_prompt = f"""Kamu adalah sistem logging crypto trading. Parse catatan trader berikut dan extract informasi terstruktur.

CATATAN TRADER:
"{user_text}"

Tugas:
1. Extract informasi yang ada (tidak semua harus ada)
2. Derive lesson konkret untuk trading bot

Respond HANYA dengan JSON valid, tanpa markdown, tanpa penjelasan:
{{
  "coin": "symbol koin atau null",
  "pnl_pct": angka_float_atau_null,
  "pnl_usdt": angka_float_atau_null,
  "outcome": "TP1_HIT|TP2_HIT|SL_HIT|ENTRY_MISSED|PARTIAL|MANUAL_CLOSE|OBSERVATION|null",
  "signal_type": "SCALP|SWING|PREPUMP|PREDUMP|SCREENER|null",
  "issue": "entry_too_low|entry_too_high|entry_zone_too_narrow|sl_too_tight|sl_too_wide|tp_too_aggressive|tp_too_conservative|timing|false_signal|spread|funding|null",
  "lesson": "1 kalimat konkret lesson untuk bot — spesifik, actionable",
  "adjustment": "entry_zone_wider|entry_zone_higher|entry_zone_lower|sl_wider|sl_tighter|tp1_lower|tp2_higher|wait_for_pullback|check_funding|null",
  "confidence": 0.0_sampai_1.0,
  "sentiment": "positive|negative|neutral"
}}"""

    try:
        raw = gemini_fn(parse_prompt)
    except Exception as e:
        log.error(f"Gemini parse error: {e}")
        raw = ""

    # ── Step 2: Parse JSON response ──────────────────────────
    parsed = {}
    if raw:
        import re as _re
        json_match = _re.search(r'\{[\s\S]+\}', raw)
        if json_match:
            try:
                import json as _json
                parsed = _json.loads(json_match.group())
            except Exception:
                pass

    # ── Step 3: Fallback kalau Gemini gagal parse ─────────────
    if not parsed:
        # Simple fallback: simpan sebagai manual lesson langsung
        lesson_text = user_text.strip()[:MAX_MANUAL_LESSON_LEN]
        lesson = add_manual_lesson(
            rule=lesson_text,
            tags=["freetext", "manual"],
            pinned=False
        )
        return (
            f"📝 <b>Catatan tersimpan</b> (mode manual)\n\n"
            f"💬 <i>{lesson_text[:150]}</i>\n\n"
            f"⚠️ Gemini tidak bisa parse — disimpan sebagai lesson mentah."
        )

    # ── Step 4: Simpan ke lessons + performance ───────────────
    coin      = parsed.get("coin") or "UNKNOWN"
    outcome   = parsed.get("outcome") or "OBSERVATION"
    lesson_txt = parsed.get("lesson") or user_text[:200]
    adjustment = parsed.get("adjustment")
    issue      = parsed.get("issue")
    confidence = parsed.get("confidence", 0.7)
    pnl_pct    = parsed.get("pnl_pct")
    pnl_usdt   = parsed.get("pnl_usdt")
    signal_type = parsed.get("signal_type") or "SCREENER"

    # Build tags
    tags = ["freetext"]
    if coin and coin != "UNKNOWN":
        tags.append(coin.lower())
    if issue:
        tags.append(issue)
    if adjustment:
        tags.append(adjustment)

    # Simpan lesson
    full_lesson = lesson_txt
    if adjustment:
        full_lesson += f" → ADJUSTMENT: {adjustment}"
    if issue:
        full_lesson = f"[{issue.upper()}] " + full_lesson

    lesson_obj = add_manual_lesson(
        rule=full_lesson[:MAX_MANUAL_LESSON_LEN],
        tags=tags,
        pinned=(confidence >= 0.85)
    )

    # Simpan ke performance kalau ada pnl
    if pnl_pct is not None:
        record_signal_outcome(
            symbol=f"{coin.upper().replace('USDT','')}USDT" if coin != "UNKNOWN" else "UNKNOWN",
            signal_type=signal_type,
            direction="LONG" if (pnl_pct or 0) >= 0 else "SHORT",
            entry_price=0,
            exit_price=0,
            score=0,
            confluence_level="",
            outcome=outcome,
            pnl_pct=pnl_pct,
            hold_minutes=0,
            notes=user_text[:200],
        )

    # ── Step 5: Build response ────────────────────────────────
    outcome_emoji = {
        "TP1_HIT": "🟢", "TP2_HIT": "🎯", "SL_HIT": "🔴",
        "ENTRY_MISSED": "⚠️", "PARTIAL": "🟡", "MANUAL_CLOSE": "⚪",
        "OBSERVATION": "📝"
    }.get(outcome, "📝")

    adj_messages = {
        "entry_zone_wider":  "📦 Entry zone akan diperlebar next time",
        "entry_zone_higher": "📈 Entry zone akan dinaikkan (lebih dekat ke harga)",
        "entry_zone_lower":  "📉 Entry zone akan diturunkan (lebih dalam)",
        "sl_wider":          "📏 SL akan diperlebar biar ada ruang napas",
        "sl_tighter":        "✂️ SL akan diperketat",
        "tp1_lower":         "🎯 TP1 akan diturunkan biar lebih realistis",
        "tp2_higher":        "🚀 TP2 akan dinaikkan untuk max profit",
        "wait_for_pullback": "⏳ Next time tunggu pullback dulu sebelum entry",
        "check_funding":     "💰 Cek funding rate sebelum entry next time",
    }
    adj_msg = adj_messages.get(adjustment, "") if adjustment else ""

    lines = [
        f"{outcome_emoji} <b>Logged & Learned</b>",
        f"📍 Coin: <b>{coin.upper()}</b>  |  Outcome: <b>{outcome}</b>",
    ]

    if pnl_pct is not None:
        pnl_emoji = "🟢" if pnl_pct > 0 else "🔴"
        lines.append(f"{pnl_emoji} PnL: <b>{pnl_pct:+.2f}%</b>")
    if pnl_usdt is not None:
        pnl_emoji = "🟢" if pnl_usdt > 0 else "🔴"
        lines.append(f"{pnl_emoji} PnL USDT: <b>{pnl_usdt:+.2f}</b>")

    lines.append(f"\n📚 <b>Lesson tersimpan:</b>")
    lines.append(f"<i>{full_lesson[:200]}</i>")

    if adj_msg:
        lines.append(f"\n🔧 <b>Bot Adjustment:</b> {adj_msg}")

    if lesson_obj.get("pinned"):
        lines.append("📌 <i>Di-pin karena confidence tinggi</i>")

    lines.append(f"\n💡 <i>/lessons — lihat semua lessons</i>")

    return "\n".join(lines)


def get_performance_stats_text() -> str:
    """Text summary performance buat /status command."""
    summary = get_performance_summary()
    if not summary:
        return "📊 Belum ada signal history. Gunakan /logoutcome untuk mulai tracking."

    data = _load_lessons()
    perf = data.get("performance", [])

    # By type breakdown
    types = {}
    for p in perf:
        t = p.get("signal_type", "?")
        if t not in types:
            types[t] = {"total": 0, "wins": 0}
        types[t]["total"] += 1
        if p.get("pnl_pct", 0) > 0:
            types[t]["wins"] += 1

    lines = [
        f"📊 <b>Signal Performance ({summary['total_signals']} total)</b>",
        f"  Win Rate : <b>{summary['win_rate_pct']}%</b>",
        f"  Avg PnL  : <b>{summary['avg_pnl_pct']:+.2f}%</b>",
        f"  TP Hits  : {summary['tp1_tp2_hits']}  |  SL Hits: {summary['sl_hits']}",
        f"  Lessons  : {summary['total_lessons']}",
        "",
        "<b>By Type:</b>",
    ]
    for t, stats in types.items():
        wr = round(stats["wins"] / max(1, stats["total"]) * 100, 0)
        lines.append(f"  {t}: {stats['total']} signals, WR={wr:.0f}%")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# DAILY LEARNING ANALYSIS (DeepSeek AI)
# ─────────────────────────────────────────────

def analyze_signal_outcomes_daily(send_telegram_fn=None) -> str:
    """
    Daily analysis of signal_outcomes.json:
    1. Compute stats per-strategy (win-rate, avg PnL, total trades)
    2. Compare vs backtest expectations (dari btall_results.json)
    3. Call DeepSeek API untuk analisa discrepancy + recommendations
    4. Send summary ke Telegram
    Return formatted message.
    """
    try:
        # Load signal outcomes
        if not os.path.exists("signal_outcomes.json"):
            return "📊 Belum ada signal outcomes."

        with open("signal_outcomes.json") as f:
            outcomes = json.load(f)

        if not outcomes:
            return "📊 Signal outcomes masih kosong."

        # Group by strategy
        stats_by_strategy = {}
        for sig in outcomes:
            if sig.get("status") not in ("TP_HIT", "SL_HIT") and \
               not sig.get("status", "").startswith("EXPIRED"):
                continue  # Skip unresolved

            strategy = sig.get("strategy", "UNKNOWN")
            pnl = sig.get("pnl_pct", 0)

            if strategy not in stats_by_strategy:
                stats_by_strategy[strategy] = {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "pnls": [],
                    "avg_pnl": 0.0,
                    "win_rate": 0.0,
                }

            stats_by_strategy[strategy]["trades"] += 1
            stats_by_strategy[strategy]["pnls"].append(pnl)
            if pnl > 0:
                stats_by_strategy[strategy]["wins"] += 1
            elif pnl < 0:
                stats_by_strategy[strategy]["losses"] += 1

        # Compute aggregates
        for strat, data in stats_by_strategy.items():
            if data["trades"] > 0:
                data["win_rate"] = round(data["wins"] / data["trades"] * 100, 1)
                data["avg_pnl"] = round(sum(data["pnls"]) / data["trades"], 2)

        # Build analysis prompt
        analysis_text = "📊 SIGNAL OUTCOMES ANALYSIS\n\n"
        for strat, data in sorted(stats_by_strategy.items()):
            analysis_text += f"{strat}: {data['trades']} trades | WR={data['win_rate']:.0f}% | AvgPnL={data['avg_pnl']:+.2f}%\n"

        # Call DeepSeek API
        summary = _call_deepseek_analysis(analysis_text, stats_by_strategy)

        # Format stats tabel dengan rata kanan
        stats_lines = []
        for strat, data in sorted(stats_by_strategy.items(), key=lambda x: -x[1]["win_rate"]):
            wr    = data["win_rate"]
            pnl   = data["avg_pnl"]
            n     = data["trades"]
            wr_em = "🟢" if wr >= 55 else "🟡" if wr >= 40 else "🔴"
            pnl_s = f"{pnl:+.2f}%"
            stats_lines.append(f"  {wr_em} <b>{strat}</b>: {n} trade | WR {wr:.0f}% | PnL {pnl_s}")

        ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📚 <b>DAILY LEARNING SUMMARY</b>\n"
            f"🕐 {ts}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 <b>Signal Outcomes:</b>\n"
            + "\n".join(stats_lines) + "\n\n"
            "─────────────────────\n"
            "🤖 <b>AI Analysis:</b>\n"
            f"{summary}\n\n"
            "⚠️ <i>Data otomatis dari signal_outcomes.json</i>"
        )

        if send_telegram_fn:
            try:
                send_telegram_fn(msg, parse_mode="HTML")
            except Exception as e:
                log.warning(f"Telegram send error: {e}")

        return msg

    except Exception as e:
        log.error(f"Daily analysis error: {e}", exc_info=True)
        return f"❌ Daily analysis error: {e}"


def _strip_markdown(text: str) -> str:
    """Bersihkan markdown bold/italic agar aman dikirim sebagai plain text di Telegram."""
    import re
    # Hapus **bold** dan *italic*
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*',     r'\1', text)
    # Hapus __underline__ dan _italic_
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'_(.+?)_',   r'\1', text)
    # Hapus `code`
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Bersihkan spasi berlebih
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _call_deepseek_analysis(analysis_text: str, stats: dict) -> str:
    """Call DeepSeek API untuk analisa signal outcomes + recommendations."""
    try:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            return "⚠️ DEEPSEEK_API_KEY belum diset di .env"

        prompt = f"""Analisa performa trading bot crypto berikut:

{analysis_text}

Berikan analisa singkat dan actionable dalam Bahasa Indonesia. Format WAJIB:

📈 TERBAIK: [strategy] — [alasan singkat, 1 kalimat]
📉 TERBURUK: [strategy] — [alasan singkat, 1 kalimat]
🔍 POLA: [insight pattern yang terdeteksi, 1-2 kalimat]
💡 REKOMENDASI:
• [action item 1]
• [action item 2]
• [action item 3]
⚠️ PERINGATAN: [kondisi yang perlu diwaspadai, jika ada]

ATURAN FORMAT:
- Gunakan emoji sebagai pengganti bold/header
- DILARANG pakai **bold**, *italic*, atau markdown apapun
- Plain text saja + emoji
- Maksimal 150 kata total"""

        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Kamu adalah analis performa bot trading crypto. "
                            "Jawab selalu dalam Bahasa Indonesia menggunakan emoji sebagai penanda section. "
                            "JANGAN PERNAH gunakan **bold**, *italic*, atau format markdown apapun. "
                            "Hanya plain text dan emoji. Singkat dan actionable."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 400,
            },
            timeout=30,
        )

        if response.status_code != 200:
            return f"⚠️ DeepSeek API error: {response.status_code}"

        data = response.json()
        raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return _strip_markdown(raw) if raw else "⚠️ Tidak ada respons dari DeepSeek"

    except Exception as e:
        log.warning(f"DeepSeek API error: {e}")
        return f"⚠️ DeepSeek error: {e}"
