#!/usr/bin/env python3
"""
SIGNAL CHAT / DISCUSSION MODULE — v15
=====================================
Diskusi dua-arah soal sinyal lewat Telegram + capture "gaya trading" user.

Alur:
  1. Bot kirim confirmed signal → register_signal_message(message_id, signal)
  2. User REPLY ke pesan sinyal itu → handle_discussion_reply(...)
     - Bot jelaskan "kenapa" sinyal ini (dari component_scores: indikator mana
       yang mendorong, mana yang setuju, mana yang konflik)
     - Bot lanjut diskusi via AI, grounded ke data sinyal + gaya trading user
  3. Kalau diskusi memunculkan insight gaya trading → bot USULIN simpan,
     tunggu konfirmasi user (ya / skip) dulu sebelum disimpan.
  4. Gaya trading yang tersimpan dipakai untuk personalisasi sinyal berikutnya
     (di-inject ke prompt + saran penyesuaian entry/TP "analisa berdua").

Desain:
  - Logika murni (explain_signal, klasifikasi komponen, parsing marker, prompt
    builder) dipisah dari I/O supaya gampang di-test offline.
  - Persisten lewat JSON file lokal; path bisa di-override untuk testing.
  - AI dipanggil lewat callable `ai_fn(prompt) -> str` yang di-inject dari bot
    (Groq → Gemini → Claude chain), jadi modul ini tidak terikat ke 1 provider.
"""

import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Callable

log = logging.getLogger("signal_chat")

# ── File paths (bisa di-override saat testing) ───────────────────
SIGNAL_MAP_FILE = "signal_chat_map.json"      # message_id → signal snapshot
CONVO_FILE      = "signal_chat_convos.json"   # chat_id → discussion state
STYLE_FILE      = "trading_style.json"         # aturan gaya trading user
HISTORY_FILE    = "confirmed_signals_history.json"

MAX_SIGNAL_MAP   = 80     # simpan mapping untuk N sinyal terakhir
MAX_HISTORY_TURN = 12     # turn percakapan yang dibawa ke prompt

# Nama ramah untuk tiap komponen detektor (buat penjelasan ke pemula)
COMPONENT_NAMES = {
    "confluence": "Confluence (struktur + Order Block + FVG + money flow)",
    "prepump":    "Pre-Pump (funding squeeze + momentum + OI)",
    "predump":    "Pre-Dump (long squeeze + bearish momentum + OI)",
    "scalp":      "Scalp 15M (liquidity sweep + rejection)",
    "swing":      "Swing 4H/1H (HTF bias + trigger)",
}

# Kata konfirmasi
_YES = {"ya", "iya", "yes", "y", "ok", "oke", "okay", "simpan", "save",
        "setuju", "bener", "betul", "gas", "sip", "boleh"}
_NO  = {"skip", "no", "ga", "gak", "nggak", "tidak", "batal", "jangan", "engga"}


# ─────────────────────────────────────────────
# Low-level JSON store
# ─────────────────────────────────────────────

def _read(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"read {path} error: {e}")
    return default


def _write(path: str, data) -> bool:
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return True
    except Exception as e:
        log.error(f"write {path} error: {e}")
        return False


# ─────────────────────────────────────────────
# Signal ↔ message_id mapping
# ─────────────────────────────────────────────

def register_signal_message(message_id, signal: dict) -> None:
    """Catat bahwa pesan Telegram `message_id` adalah sinyal `signal`."""
    if not message_id or not signal:
        return
    data = _read(SIGNAL_MAP_FILE, {})
    data[str(message_id)] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "signal": signal,
    }
    # Bound: simpan hanya N terbaru (urut by ts)
    if len(data) > MAX_SIGNAL_MAP:
        items = sorted(data.items(), key=lambda kv: kv[1].get("ts", ""))
        data = dict(items[-MAX_SIGNAL_MAP:])
    _write(SIGNAL_MAP_FILE, data)


def get_signal_for_message(message_id) -> Optional[dict]:
    """Ambil snapshot sinyal untuk message_id, atau None kalau bukan sinyal."""
    if not message_id:
        return None
    entry = _read(SIGNAL_MAP_FILE, {}).get(str(message_id))
    return entry.get("signal") if entry else None


def find_latest_signal(symbol: Optional[str] = None) -> Optional[dict]:
    """Ambil confirmed signal terakhir dari history (opsional filter symbol)."""
    hist = _read(HISTORY_FILE, [])
    if not isinstance(hist, list) or not hist:
        return None
    if symbol:
        sym = symbol.upper().replace("USDT", "")
        for sig in reversed(hist):
            if sig.get("symbol", "").upper().replace("USDT", "") == sym:
                return sig
        return None
    return hist[-1]


# ─────────────────────────────────────────────
# PURE: jelaskan "kenapa" sinyal ini
# ─────────────────────────────────────────────

def _component_side(cdir: str) -> Optional[str]:
    """Map arah komponen ('LONG'/'LONG_WEAK'/'SHORT'/...) → 'LONG'|'SHORT'|None."""
    if "LONG" in cdir:
        return "LONG"
    if "SHORT" in cdir:
        return "SHORT"
    return None


def classify_components(signal: dict) -> dict:
    """
    Klasifikasi tiap komponen relatif ke arah akhir sinyal.

    Return dict:
      drivers : komponen yang menyumbang bobot (weight > 0)
      agree   : komponen searah dengan sinyal akhir
      conflict: komponen berlawanan arah
      neutral : komponen tanpa arah
    Tiap entry: {"key", "name", "direction", "score", "weight"}
    """
    direction = signal.get("direction", "NONE")
    comps = signal.get("component_scores", {})
    out = {"drivers": [], "agree": [], "conflict": [], "neutral": []}

    for key in COMPONENT_NAMES:
        c = comps.get(key)
        if not c:
            continue
        cdir = c.get("direction", "NONE")
        score = c.get("score", 0)
        weight = c.get("weight", 0) or 0
        side = _component_side(cdir)
        entry = {"key": key, "name": COMPONENT_NAMES[key],
                 "direction": cdir, "score": score, "weight": weight}

        if weight > 0:
            out["drivers"].append(entry)
        if side is None:
            out["neutral"].append(entry)
        elif side == direction:
            out["agree"].append(entry)
        else:
            out["conflict"].append(entry)
    return out


def explain_signal(signal: dict) -> str:
    """Penjelasan deterministik 'kenapa sinyal ini' — tanpa AI."""
    symbol = signal.get("symbol", "?")
    direction = signal.get("direction", "NONE")
    score = signal.get("master_score", 0)
    conf = signal.get("confidence", "LOW")
    cls = classify_components(signal)

    dir_emoji = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"
    lines = [
        f"{dir_emoji} <b>{symbol} {direction}</b> — master score {score}/100 ({conf})",
        "",
    ]

    drivers = cls["drivers"]
    if drivers:
        lines.append("📌 <b>Yang mendorong sinyal ini</b> (punya bobot):")
        for d in sorted(drivers, key=lambda x: x["weight"], reverse=True):
            lines.append(f"  • {d['name']} — score {d['score']}/100, bobot {d['weight']}")
    else:
        lines.append("📌 Tidak ada komponen dengan bobot dominan.")

    # Jawab eksplisit: cuma satu atau banyak yang setuju?
    n_drivers = len(drivers)
    n_agree = len(cls["agree"])
    if n_drivers <= 1:
        lines.append(f"\n⚠️ Sinyal ini praktis ditopang <b>1 indikator</b> "
                     f"({drivers[0]['name'] if drivers else 'tidak jelas'}) — "
                     f"konfirmasi lemah, hati-hati.")
    else:
        lines.append(f"\n✅ Ada <b>{n_drivers} indikator</b> yang menyumbang, "
                     f"{n_agree} searah dengan sinyal — konfirmasi lebih kuat.")

    if cls["conflict"]:
        names = ", ".join(c["name"].split(" (")[0] for c in cls["conflict"])
        lines.append(f"\n🔴 <b>Yang berlawanan:</b> {names}")

    reasons = signal.get("reasons", [])
    if reasons:
        lines.append("\n🧠 <b>Alasan utama:</b>")
        for r in reasons[:4]:
            lines.append(f"  • {r}")

    conflicts = signal.get("conflict_reasons", [])
    if conflicts:
        lines.append("\n⚠️ <b>Catatan konflik:</b>")
        for r in conflicts[:3]:
            lines.append(f"  • {r}")

    trade = signal.get("trade", {})
    if trade:
        lines.append(
            f"\n🎯 Rencana: entry <code>{trade.get('entry', '?')}</code> · "
            f"SL <code>{trade.get('sl', '?')}</code> · "
            f"TP1 <code>{trade.get('tp1', '?')}</code> · "
            f"TP2 <code>{trade.get('tp2', '?')}</code>"
            + (f" (R:R {trade.get('rr')})" if trade.get("rr") else "")
        )

    return "\n".join(lines)


# ─────────────────────────────────────────────
# PURE: prompt builders + style marker parsing
# ─────────────────────────────────────────────

def _signal_context_text(signal: dict) -> str:
    """Ringkasan sinyal yang padat untuk konteks AI."""
    cls = classify_components(signal)
    drivers = "; ".join(f"{d['key']}={d['score']}(w{d['weight']})" for d in cls["drivers"]) or "none"
    agree = ", ".join(c["key"] for c in cls["agree"]) or "none"
    conflict = ", ".join(c["key"] for c in cls["conflict"]) or "none"
    trade = signal.get("trade", {})
    return (
        f"Symbol: {signal.get('symbol')} | Arah: {signal.get('direction')} | "
        f"MasterScore: {signal.get('master_score')}/100 ({signal.get('confidence')})\n"
        f"Indikator pendorong: {drivers}\n"
        f"Searah: {agree} | Berlawanan: {conflict}\n"
        f"Alasan: {' | '.join(signal.get('reasons', [])[:4])}\n"
        f"Konflik: {' | '.join(signal.get('conflict_reasons', [])[:3]) or 'none'}\n"
        f"Rencana: entry={trade.get('entry')} sl={trade.get('sl')} "
        f"tp1={trade.get('tp1')} tp2={trade.get('tp2')} rr={trade.get('rr')}"
    )


_STYLE_INSTRUCTION = (
    "Kalau dari obrolan ini kamu menangkap PREFERENSI / GAYA TRADING user yang "
    "layak diingat untuk sinyal berikutnya (mis. suka entry retest bukan market, "
    "mau R:R minimal tertentu, hindari kondisi tertentu, suka TP bertahap), "
    "tulis SATU baris di paling akhir dengan format persis: [STYLE: <aturan singkat>]. "
    "Kalau tidak ada, jangan tulis baris itu."
)


_PERSONA = (
    "Lo asisten trading crypto pribadi-nya user — ngobrol santai kayak temen yang jago "
    "trading, BUKAN textbook. Pakai Bahasa Indonesia santai (boleh 'gue/lo'), langsung ke "
    "poin, maksimal ~5 kalimat. ATURAN PENTING:\n"
    "- Jawab SPESIFIK soal sinyal/coin yang lagi dibahas. JANGAN kasih definisi umum atau "
    "teori dasar (mis. 'apa itu short selling') kecuali diminta.\n"
    "- INGAT: bot ini (lo) yang ngasih sinyal. Kalau user nanya 'kenapa disuruh short/long', "
    "jawab kenapa LO ngasih call itu berdasarkan indikator/alasan di data — bukan ngomong "
    "soal 'orang lain' atau definisi.\n"
    "- Kalau harga keliatan lawan arah sinyal, jelasin logikanya (mis. short karena overextended/"
    "resistance/funding, bukan karena 'harga bakal turun' doang).\n"
    "- Jujur: kalau sinyalnya emang lemah/cuma 1 indikator, ngaku. Boleh koreksi user kalau keliru."
)


def build_discussion_prompt(signal: dict, history: list, user_msg: str,
                            style_rules: list, replied_text: str = None) -> str:
    """Susun prompt diskusi untuk AI dengan persona santai & grounded ke data."""
    style_txt = "\n".join(f"- {r}" for r in style_rules) if style_rules else "(belum ada)"
    convo = "\n".join(
        f"{'User' if t.get('role') == 'user' else 'Bot'}: {t.get('text', '')}"
        for t in history[-MAX_HISTORY_TURN:]
    ) or "(belum ada)"

    if signal and signal.get("component_scores"):
        sig_ctx = _signal_context_text(signal)
    elif signal:
        sig_ctx = (f"Symbol: {signal.get('symbol', '?')} | Arah: {signal.get('direction', '?')} "
                   f"(data ringkas)")
    else:
        sig_ctx = "(tidak ada data sinyal terstruktur — pakai isi pesan bot yang di-reply)"

    replied_block = (f"\n=== PESAN BOT YANG DI-REPLY USER (konteks utama!) ===\n{replied_text}\n"
                     if replied_text else "")

    return (
        f"{_PERSONA}\n\n"
        f"=== DATA SINYAL ===\n{sig_ctx}\n"
        f"{replied_block}\n"
        f"=== GAYA TRADING USER (tersimpan) ===\n{style_txt}\n\n"
        f"=== RIWAYAT DISKUSI ===\n{convo}\n\n"
        f"=== PESAN USER SEKARANG ===\n{user_msg}\n\n"
        f"{_STYLE_INSTRUCTION}"
    )


def build_personalize_prompt(signal: dict, style_rules: list) -> str:
    """Prompt untuk saran penyesuaian entry/TP berdasarkan gaya user (advisory)."""
    style_txt = "\n".join(f"- {r}" for r in style_rules)
    return (
        "Lo asisten trading pribadi user, ngobrol santai (boleh 'gue/lo'). Berdasarkan DATA "
        "sinyal dan GAYA TRADING user, kasih saran penyesuaian entry/SL/TP yang lebih cocok "
        "buat gaya dia. Ini SARAN di atas angka mekanis bot — jelasin alasan tiap penyesuaian, "
        "spesifik & singkat (maks 5 kalimat). Jangan nyuruh all-in / over-leverage / nambah risiko.\n\n"
        f"=== DATA SINYAL ===\n{_signal_context_text(signal)}\n\n"
        f"=== GAYA TRADING USER ===\n{style_txt}\n"
    )


_STYLE_MARKER = re.compile(r"\[STYLE:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)


def parse_style_suggestion(ai_text: str) -> tuple:
    """
    Pisahkan marker [STYLE: ...] dari teks AI.
    Return (clean_text, rule_or_None).
    """
    if not ai_text:
        return "", None
    m = _STYLE_MARKER.search(ai_text)
    if not m:
        return ai_text.strip(), None
    rule = m.group(1).strip()
    clean = _STYLE_MARKER.sub("", ai_text).strip()
    return clean, (rule or None)


# ─────────────────────────────────────────────
# Conversation state (per chat)
# ─────────────────────────────────────────────

def _load_convos() -> dict:
    return _read(CONVO_FILE, {})


def _save_convos(data: dict) -> None:
    _write(CONVO_FILE, data)


def _get_convo(chat_id: str) -> dict:
    return _load_convos().get(str(chat_id), {})


DISCUSSION_WINDOW_MIN = 25   # follow-up tanpa reply masih lanjut dalam window ini


def start_convo(chat_id: str, message_id, context: dict) -> None:
    data = _load_convos()
    data[str(chat_id)] = {
        "message_id": str(message_id) if message_id else None,
        "symbol": (context or {}).get("symbol"),
        "context": context or {},
        "history": [],
        "pending_rule": None,
        "last_active": datetime.now(timezone.utc).isoformat(),
    }
    _save_convos(data)


def get_context(chat_id: str) -> dict:
    return _get_convo(chat_id).get("context", {})


def is_discussion_active(chat_id: str) -> bool:
    """True kalau ada diskusi yang masih hangat (dalam window) untuk chat ini."""
    convo = _get_convo(chat_id)
    if not convo.get("context"):
        return False
    last = convo.get("last_active")
    if not last:
        return False
    try:
        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
    except Exception:
        return False
    return age_min <= DISCUSSION_WINDOW_MIN


def end_convo(chat_id: str) -> None:
    data = _load_convos()
    if str(chat_id) in data:
        data[str(chat_id)]["context"] = {}
        data[str(chat_id)]["last_active"] = None
        _save_convos(data)


def append_turn(chat_id: str, role: str, text: str) -> None:
    data = _load_convos()
    convo = data.setdefault(str(chat_id), {"history": [], "pending_rule": None})
    convo.setdefault("history", []).append({"role": role, "text": text})
    convo["history"] = convo["history"][-(MAX_HISTORY_TURN * 2):]
    convo["last_active"] = datetime.now(timezone.utc).isoformat()
    _save_convos(data)


def get_history(chat_id: str) -> list:
    return _get_convo(chat_id).get("history", [])


def set_pending_rule(chat_id: str, rule: Optional[str]) -> None:
    data = _load_convos()
    convo = data.setdefault(str(chat_id), {"history": [], "pending_rule": None})
    convo["pending_rule"] = rule
    _save_convos(data)


def get_pending_rule(chat_id: str) -> Optional[str]:
    return _get_convo(chat_id).get("pending_rule")


def has_pending_rule(chat_id: str) -> bool:
    return bool(get_pending_rule(chat_id))


def is_confirm_answer(text: str) -> bool:
    return text.strip().lower() in (_YES | _NO)


def _is_yes(text: str) -> bool:
    return text.strip().lower() in _YES


# ─────────────────────────────────────────────
# Trading-style store
# ─────────────────────────────────────────────

def get_style_rules() -> list:
    """List string aturan gaya trading user."""
    data = _read(STYLE_FILE, {"rules": []})
    return [r.get("rule", "") for r in data.get("rules", []) if r.get("rule")]


def add_style_rule(rule: str, source: str = "discussion") -> bool:
    """Tambah aturan gaya trading (skip kalau duplikat). Return True kalau ditambah."""
    rule = (rule or "").strip()
    if not rule:
        return False
    data = _read(STYLE_FILE, {"rules": []})
    rules = data.setdefault("rules", [])
    if any(r.get("rule", "").strip().lower() == rule.lower() for r in rules):
        return False
    rules.append({
        "rule": rule,
        "source": source,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _write(STYLE_FILE, data)
    # Best-effort: push juga ke learning_engine supaya ikut ke prompt analisa lain
    try:
        import learning_engine
        learning_engine.add_manual_lesson(
            rule, tags=["trading_style"], pinned=False, role="TRADING_STYLE")
    except Exception:
        pass
    return True


def remove_style_rule(index: int) -> Optional[str]:
    """Hapus aturan ke-`index` (1-based). Return rule yang dihapus atau None."""
    data = _read(STYLE_FILE, {"rules": []})
    rules = data.get("rules", [])
    if 1 <= index <= len(rules):
        removed = rules.pop(index - 1)
        _write(STYLE_FILE, data)
        return removed.get("rule")
    return None


def format_style_list() -> str:
    rules = get_style_rules()
    if not rules:
        return ("🎚️ <b>Gaya Trading</b>\nBelum ada aturan tersimpan. "
                "Reply ke pesan sinyal dan diskusi — nanti bot usulin aturan dari obrolan kamu.")
    lines = ["🎚️ <b>Gaya Trading kamu</b> (dipakai untuk personalisasi sinyal):", ""]
    for i, r in enumerate(rules, 1):
        lines.append(f"  {i}. {r}")
    lines.append("\n<i>Hapus: /style del &lt;nomor&gt;</i>")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# STYLE ENGINE — terjemahkan aturan gaya → penyesuaian sinyal (deterministik)
# ─────────────────────────────────────────────
# Aturan gaya yang berupa teks bebas diparse jadi preferensi terstruktur, lalu
# diterapkan ke SALINAN rencana trade secara deterministik & transparan.
# Tujuannya bikin sinyal terasa "hasil analisa berdua" TANPA diam-diam menimpa
# angka mekanis bot (angka asli tetap ditampilkan; penyesuaian dijelaskan).

_RR_RE   = re.compile(r"(?:r[:\s/]*r|risk[\s:-]*reward)\D{0,15}(\d+(?:[.,]\d+)?)", re.I)
_SCORE_RE = re.compile(r"(?:score|skor|konviksi|conviction)\D{0,10}(\d{2,3})", re.I)
_INDMIN_RE = re.compile(r"(?:minimal|min|setidaknya|at\s*least)\s*(\d+)\s*"
                        r"(?:indikator|konfirmasi|indicator|confirmation)", re.I)
_INDNO1_RE = re.compile(r"(?:jangan|bukan|hindari|no|gak|nggak|cuma|hanya)"
                        r"[^\d]{0,15}(?:1|satu)\s*(?:indikator|konfirmasi)", re.I)


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def parse_style_to_prefs(rules: list) -> dict:
    """Parse aturan gaya (teks bebas) → preferensi terstruktur + catatan AI."""
    prefs = {
        "min_rr": None, "min_score": None, "min_indicators": None,
        "entry_style": None, "notes": list(rules or []),
    }
    blob = "  ".join(rules or []).lower()

    m = _RR_RE.search(blob)
    if m:
        v = _to_float(m.group(1))
        if v and 0.5 <= v <= 10:
            prefs["min_rr"] = v

    m = _SCORE_RE.search(blob)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 100:
            prefs["min_score"] = v

    if _INDMIN_RE.search(blob):
        prefs["min_indicators"] = int(_INDMIN_RE.search(blob).group(1))
    elif _INDNO1_RE.search(blob):
        prefs["min_indicators"] = 2

    if "retest" in blob or "pullback" in blob:
        prefs["entry_style"] = "RETEST"
    elif "market" in blob or "langsung" in blob or "chase" in blob:
        prefs["entry_style"] = "MARKET"

    return prefs


def _round_like(value: float, ref: float) -> float:
    """Bulatkan `value` mengikuti presisi harga `ref`."""
    if ref >= 100:
        return round(value, 2)
    if ref >= 1:
        return round(value, 4)
    return round(value, 8)


def apply_style_to_signal(signal: dict, prefs: dict) -> dict:
    """
    Terapkan preferensi ke salinan rencana trade. Deterministik & konservatif:
      - min_rr  → geser TP supaya R:R memenuhi target (math eksak)
      - min_score / min_indicators → warning + flag 'skip' (advisory, tidak memblok)
      - entry_style → catatan cara entry
    Return: {adjusted_trade, changes, warnings, suppress, entry_note}
    """
    direction = signal.get("direction") or signal.get("trade", {}).get("direction", "LONG")
    trade = signal.get("trade", {}) or {}
    entry = _to_float(str(trade.get("entry", signal.get("price", 0)))) or 0.0
    sl = _to_float(str(trade.get("sl", 0))) or 0.0

    out = {"adjusted_trade": {}, "changes": [], "warnings": [],
           "suppress": False, "entry_note": None}

    # ── min R:R → geser TP ───────────────────────
    risk = abs(entry - sl)
    if prefs.get("min_rr") and entry > 0 and sl > 0 and risk > 0:
        min_rr = prefs["min_rr"]
        for tp_key in ("tp1", "tp2"):
            tp = _to_float(str(trade.get(tp_key, 0))) or 0.0
            if tp <= 0:
                continue
            cur_rr = (tp - entry) / risk if direction == "LONG" else (entry - tp) / risk
            if cur_rr < min_rr - 0.01:
                new_tp = entry + min_rr * risk if direction == "LONG" else entry - min_rr * risk
                new_tp = _round_like(new_tp, entry)
                out["adjusted_trade"][tp_key] = new_tp
                out["changes"].append(
                    f"{tp_key.upper()} {tp} → <b>{new_tp}</b> "
                    f"(R:R {cur_rr:.2f} → {min_rr:.1f}, sesuai gaya kamu)")

    # ── min score ────────────────────────────────
    if prefs.get("min_score"):
        ms = signal.get("master_score", 0)
        if ms < prefs["min_score"]:
            out["warnings"].append(
                f"Master score {ms} di bawah ambang gaya kamu ({prefs['min_score']}) "
                f"— biasanya kamu skip yang segini.")
            out["suppress"] = True

    # ── min indikator pendorong ──────────────────
    if prefs.get("min_indicators"):
        n_drivers = len(classify_components(signal).get("drivers", []))
        if n_drivers < prefs["min_indicators"]:
            out["warnings"].append(
                f"Cuma {n_drivers} indikator pendorong, gaya kamu minta minimal "
                f"{prefs['min_indicators']} — konfirmasi kurang buat kamu.")
            out["suppress"] = True

    # ── entry style ──────────────────────────────
    if prefs.get("entry_style") == "RETEST" and entry > 0:
        out["entry_note"] = (f"Kamu suka entry retest: tunggu harga pullback ke "
                             f"zona <code>{entry}</code>, jangan market-chase.")
    elif prefs.get("entry_style") == "MARKET":
        out["entry_note"] = "Kamu suka entry cepat: boleh masuk di harga sekarang kalau setup masih valid."

    return out


def format_personalized_block(signal: dict, prefs: dict, res: dict) -> str:
    """Susun blok 'disesuaikan gaya kamu'. Return '' kalau tidak ada penyesuaian."""
    if not (res["changes"] or res["warnings"] or res["entry_note"]):
        return ""
    lines = ["🤝 <b>Disesuaikan gaya trading kamu</b>"]

    if res["suppress"]:
        lines.append("⛔ <b>Menurut gaya kamu, sinyal ini sebaiknya di-SKIP:</b>")
    for w in res["warnings"]:
        lines.append(f"  ⚠️ {w}")

    if res["changes"]:
        lines.append("\n🎯 <b>Rencana disesuaikan:</b>")
        for c in res["changes"]:
            lines.append(f"  • {c}")

    if res["entry_note"]:
        lines.append(f"\n📍 {res['entry_note']}")

    lines.append("\n<i>Penyesuaian dari aturan /style kamu — angka mekanis bot tetap di atas. "
                 "Keputusan akhir tetap di kamu.</i>")
    return "\n".join(lines)


def build_signal_personalization(signal: dict) -> str:
    """Helper untuk bot: load gaya → parse → apply → format. '' kalau tidak ada."""
    rules = get_style_rules()
    if not rules:
        return ""
    prefs = parse_style_to_prefs(rules)
    res = apply_style_to_signal(signal, prefs)
    return format_personalized_block(signal, prefs, res)


# ─────────────────────────────────────────────
# Orchestration handlers (pakai ai_fn & send_fn yang di-inject)
# ─────────────────────────────────────────────

def _guess_coin(text: str) -> Optional[str]:
    """Tebak coin dari teks berdasarkan symbol yang ada di history sinyal."""
    if not text:
        return None
    syms = set()
    for s in _read(HISTORY_FILE, []):
        if isinstance(s, dict):
            sym = s.get("symbol", "").upper().replace("USDT", "")
            if sym:
                syms.add(sym)
    up = text.upper()
    for s in sorted(syms, key=len, reverse=True):
        if re.search(r"\b" + re.escape(s) + r"\b", up):
            return s
    return None


def _resolve_context(message_id, user_msg: str, replied_text: str) -> Optional[dict]:
    """
    Tentukan konteks diskusi:
      1. sinyal terdaftar untuk message yang di-reply
      2. sinyal terakhir untuk coin yang disebut user / di pesan bot
      3. konteks ringan dari isi pesan bot yang di-reply
    """
    ctx = get_signal_for_message(message_id) if message_id else None
    if ctx:
        return ctx
    coin = _guess_coin(user_msg) or _guess_coin(replied_text or "")
    if coin:
        sig = find_latest_signal(coin)
        if sig:
            return sig
    if replied_text:
        return {"symbol": coin or "?", "summary": replied_text, "_light": True}
    return None


def _discuss(ctx: dict, user_msg: str, chat_id: str,
             ai_fn: Callable[[str], str], send_fn: Callable[..., None],
             replied_text: str = None) -> None:
    """Inti diskusi: bangun prompt grounded, jawab, sambung thread, usulkan rule."""
    is_full = bool(ctx and ctx.get("component_scores"))

    convo = _get_convo(chat_id)
    if (not convo.get("context")) or convo.get("symbol") != (ctx or {}).get("symbol"):
        start_convo(chat_id, None, ctx or {})

    append_turn(chat_id, "user", user_msg)

    prompt = build_discussion_prompt(ctx, get_history(chat_id), user_msg,
                                     get_style_rules(), replied_text=replied_text)
    try:
        ai_raw = ai_fn(prompt) or ""
    except Exception as e:
        log.warning(f"discuss ai_fn error: {e}")
        ai_raw = ""

    if not ai_raw.strip():
        send_fn("⚠️ AI-nya lagi error, coba lagi bentar ya.", chat_id)
        return

    clean, rule = parse_style_suggestion(ai_raw)
    append_turn(chat_id, "bot", clean)

    # Penjelasan deterministik hanya di awal & hanya kalau data sinyal lengkap
    if is_full and len(get_history(chat_id)) <= 2:
        send_fn(explain_signal(ctx), chat_id)

    mid = send_fn(f"💬 {clean}", chat_id)
    # Daftarkan balasan bot → reply ke jawaban ini lanjut diskusi dengan konteks sama
    if mid and ctx:
        try:
            register_signal_message(mid, ctx)
        except Exception:
            pass

    if rule:
        set_pending_rule(chat_id, rule)
        send_fn(
            f"📝 Gue nangkep gaya trading lo:\n<b>“{rule}”</b>\n\n"
            f"Simpan biar sinyal berikutnya nyesuaiin? Balas <b>ya</b> / <b>skip</b>.",
            chat_id,
        )


def handle_discussion_reply(message_id, user_msg: str, chat_id: str,
                            ai_fn: Callable[[str], str],
                            send_fn: Callable[..., None],
                            replied_text: str = None) -> bool:
    """
    Tangani reply user ke pesan bot. Grounded ke sinyal terdaftar / coin yang
    disebut / isi pesan yang di-reply. Return False hanya kalau benar-benar tidak
    ada konteks (biar bot bisa fallback).
    """
    ctx = _resolve_context(message_id, user_msg, replied_text)
    if not ctx:
        return False
    _discuss(ctx, user_msg, chat_id, ai_fn, send_fn, replied_text=replied_text)
    return True


def handle_followup(user_msg: str, chat_id: str,
                    ai_fn: Callable[[str], str],
                    send_fn: Callable[..., None]) -> bool:
    """Lanjutan diskusi tanpa reply (selama window masih aktif)."""
    if not is_discussion_active(chat_id):
        return False
    ctx = get_context(chat_id)
    if not ctx:
        return False
    _discuss(ctx, user_msg, chat_id, ai_fn, send_fn)
    return True


def handle_confirm(text: str, chat_id: str, send_fn: Callable[..., None]) -> bool:
    """Tangani jawaban ya/skip untuk pending rule. Return True kalau ditangani."""
    pending = get_pending_rule(chat_id)
    if not pending:
        return False
    if not is_confirm_answer(text):
        return False

    set_pending_rule(chat_id, None)
    if _is_yes(text):
        added = add_style_rule(pending, source="discussion")
        if added:
            send_fn(f"✅ Tersimpan: <b>“{pending}”</b>\n"
                    f"Sinyal berikutnya bakal menyesuaikan gaya ini. Lihat semua: /style", chat_id)
        else:
            send_fn("ℹ️ Aturan itu sudah ada sebelumnya — tidak digandakan.", chat_id)
    else:
        send_fn("👍 Oke, nggak disimpan.", chat_id)
    return True


def handle_why(symbol: Optional[str], chat_id: str,
               ai_fn: Optional[Callable[[str], str]],
               send_fn: Callable[..., None]) -> None:
    """Command /why [SYMBOL] — jelaskan sinyal terakhir + saran sesuai gaya user."""
    signal = find_latest_signal(symbol)
    if not signal:
        send_fn("ℹ️ Belum ada confirmed signal yang bisa dijelaskan."
                + (f" untuk {symbol.upper()}" if symbol else ""), chat_id)
        return

    send_fn(explain_signal(signal), chat_id)

    style_rules = get_style_rules()

    # Penyesuaian deterministik (angka entry/TP) dari gaya tersimpan
    det_block = build_signal_personalization(signal)
    if det_block:
        send_fn(det_block, chat_id)

    # Saran naratif dari AI (opsional, kalau ada AI key)
    if style_rules and ai_fn:
        try:
            take = ai_fn(build_personalize_prompt(signal, style_rules))
        except Exception as e:
            log.warning(f"personalize ai_fn error: {e}")
            take = ""
        if take and take.strip():
            send_fn("🤝 <b>Analisa berdua (disesuaikan gaya kamu):</b>\n" + take.strip(), chat_id)

    send_fn("💬 <i>Mau diskusi? Reply pesan sinyalnya dan tanya apa aja.</i>", chat_id)


def handle_style_command(args: str, chat_id: str, send_fn: Callable[..., None]) -> None:
    """Command /style — list, atau /style del <n> untuk hapus."""
    args = (args or "").strip()
    if args.lower().startswith("del"):
        parts = args.split()
        if len(parts) >= 2 and parts[1].isdigit():
            removed = remove_style_rule(int(parts[1]))
            if removed:
                send_fn(f"🗑️ Dihapus: “{removed}”", chat_id)
            else:
                send_fn("⚠️ Nomor tidak valid. Cek /style.", chat_id)
        else:
            send_fn("❓ Format: <code>/style del 2</code>", chat_id)
        return
    send_fn(format_style_list(), chat_id)
