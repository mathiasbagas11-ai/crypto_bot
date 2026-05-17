#!/usr/bin/env python3
"""
FEEDBACK ENGINE v1.0
=====================
Natural language feedback parser untuk Crypto Bot v12.

Lo bisa komen apapun tentang sinyal yang salah atau benar,
bot langsung parse, extract rule, dan inject ke learning engine.

Command:
  /feedback <komentar bebas>

Contoh:
  /feedback tadi sinyal long BTC padahal BTC lagi downtrend, keseret turun
  /feedback SOL signal bagus, TP1 kena dalam 30 menit
  /feedback jangan kasih long kalau coinbase premium negatif
  /feedback funding rate positif tapi harga ga naik, bearish divergence
  /feedback prepump ETH tadi salah, OI naik tapi harga justru dump karena BTC jelek

Flow:
  1. Parse input → deteksi coin, direction, outcome, dan kondisi market
  2. Cek apakah ada pending signal yang relevan di signal_tracker
  3. Gunakan Gemini (kalau tersedia) atau rule-based parser
  4. Generate structured lesson
  5. Inject ke learning_engine
  6. Optionally update confirmed_signal thresholds

Tanpa Gemini: rule-based keyword extraction (tetap jalan)
Dengan Gemini: lebih nuanced, bisa handle bahasa campuran & konteks kompleks
"""

import re
import json
import time
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("feedback")

FEEDBACK_FILE = "feedback_log.json"
MAX_FEEDBACK  = 500

# ─────────────────────────────────────────────
# KEYWORD PATTERNS
# Untuk rule-based parsing (fallback tanpa Gemini)
# ─────────────────────────────────────────────

# Outcome keywords
WRONG_KEYWORDS = [
    "salah", "wrong", "gagal", "failed", "jelek", "rugi", "minus", "mc", "margin call",
    "sl hit", "kena sl", "stop loss", "loss", "turun padahal", "naik padahal",
    "keseret", "kebawa", "misleading", "false signal", "false", "jangan",
    "avoid", "hindari", "ga bagus", "gak bagus", "tidak bagus", "buruk",
]
CORRECT_KEYWORDS = [
    "bagus", "good", "profit", "tp", "target", "kena tp", "tp kena", "berhasil",
    "works", "jalan", "sukses", "oke", "bener", "benar", "correct", "mantap",
    "works well", "valid", "solid",
]

# Direction keywords
LONG_KEYWORDS  = ["long", "buy", "beli", "pump", "naik", "up", "bullish", "bull"]
SHORT_KEYWORDS = ["short", "sell", "jual", "dump", "turun", "down", "bearish", "bear"]

# Market condition keywords → map ke rule tags
CONDITION_PATTERNS = {
    "btc_down":         ["btc turun", "btc jelek", "btc downtrend", "btc red",
                          "btc dropping", "btc dump", "bitcoin turun", "bitcoin jelek",
                          "btc lagi turun", "btc bearish"],
    "btc_up":           ["btc naik", "btc pump", "btc up", "btc bullish",
                          "bitcoin naik", "btc uptrend"],
    "btc_correlated":   ["keseret btc", "kebawa btc", "ikut btc", "diseret btc",
                          "dragged by btc", "btc drag", "correlation"],
    "coinbase_premium": ["coinbase premium", "cb premium", "coinbase negatif",
                          "coinbase negative", "cb negatif", "premium negatif"],
    "funding_high":     ["funding tinggi", "funding positif", "funding high",
                          "funding rate tinggi", "over-leveraged long"],
    "funding_low":      ["funding negatif", "funding rendah", "funding negative",
                          "funding low", "over-leveraged short"],
    "oi_rising":        ["oi naik", "oi tinggi", "open interest naik", "oi rising",
                          "oi increase"],
    "oi_dropping":      ["oi turun", "oi drop", "open interest turun", "oi falling"],
    "volume_fake":      ["volume palsu", "volume rendah", "low volume", "no volume",
                          "volume spike palsu", "fake volume"],
    "divergence":       ["divergence", "divergensi", "harga ga ikut", "price not following",
                          "bearish divergence", "bullish divergence"],
    "macro_bad":        ["macro jelek", "macro bearish", "fed", "cpi", "news jelek",
                          "bad news", "berita buruk", "sentiment negatif"],
    "macro_good":       ["macro bagus", "macro bullish", "good news", "berita bagus",
                          "sentiment positif", "etf", "institutional"],
    "structure_break":  ["choch", "bos palsu", "false bos", "struktur rusak",
                          "structure break", "level jebol"],
    "time_wrong":       ["timing salah", "too early", "terlalu awal", "belum waktunya",
                          "too late", "terlalu telat", "bad timing"],
    "season_wrong":     ["bukan seasonnya", "salah season", "wrong season",
                          "ecosystem jelek", "lagi bear season"],
}

# Coin extraction — common ticker patterns
COIN_PATTERN = re.compile(
    r'\b(BTC|ETH|SOL|BNB|XRP|ADA|DOT|AVAX|MATIC|LINK|UNI|AAVE|'
    r'OP|ARB|APT|SUI|SEI|TIA|JUP|JTO|WIF|BONK|PEPE|DOGE|SHIB|'
    r'WLD|FET|TAO|RENDER|NEAR|TON|STX|RUNE|IMX|BEAM|INJ|TRX|'
    r'LTC|BCH|ETC|ATOM|ALGO|VET|SAND|MANA|ENS|GRT|LDO|RPL|'
    r'PYTH|POPCAT|BOME|WEN|NEIRO|MOG|BRETT|AERO|HIGHER)USDT?\b',
    re.IGNORECASE
)


# ─────────────────────────────────────────────
# RULE-BASED PARSER
# ─────────────────────────────────────────────

def _parse_feedback_rule_based(text: str) -> dict:
    """
    Parse feedback tanpa AI.
    Extract: coin, direction, outcome, conditions, lesson rule.
    """
    text_lower = text.lower()

    # Detect outcome
    wrong   = any(kw in text_lower for kw in WRONG_KEYWORDS)
    correct = any(kw in text_lower for kw in CORRECT_KEYWORDS)
    if wrong and not correct:
        outcome = "BAD"
    elif correct and not wrong:
        outcome = "GOOD"
    elif wrong and correct:
        outcome = "MIXED"
    else:
        outcome = "OBSERVATION"

    # Detect direction
    is_long  = any(kw in text_lower for kw in LONG_KEYWORDS)
    is_short = any(kw in text_lower for kw in SHORT_KEYWORDS)
    if is_long and not is_short:
        direction = "LONG"
    elif is_short and not is_long:
        direction = "SHORT"
    else:
        direction = "UNKNOWN"

    # Extract coin
    coins = [m.group(1).upper() for m in COIN_PATTERN.finditer(text.upper())]
    # Also check for bare tickers like "btc" "sol" etc.
    bare = re.findall(
        r'\b(btc|eth|sol|bnb|xrp|ada|avax|link|uni|aave|op|arb|apt|sui|'
        r'wif|bonk|pepe|doge|shib|wld|fet|tao|near|ton|inj|pyth)\b',
        text_lower
    )
    coins += [b.upper() for b in bare]
    coins = list(dict.fromkeys(coins))  # dedup, preserve order

    # Detect conditions
    active_conditions = []
    for cond_key, patterns in CONDITION_PATTERNS.items():
        if any(p in text_lower for p in patterns):
            active_conditions.append(cond_key)

    # Build lesson rule from conditions
    condition_rules = {
        "btc_down":         "SKIP LONG kalau BTC sedang downtrend — altcoins diseret turun",
        "btc_up":           "LONG bias lebih valid kalau BTC uptrend — tailwind",
        "btc_correlated":   "Coin ini highly correlated dengan BTC — cek BTC trend dulu sebelum entry",
        "coinbase_premium": "Coinbase premium negatif = institutional selling — hindari LONG",
        "funding_high":     "Funding rate positif tinggi = longs crowded — LONG risky, potensi squeeze",
        "funding_low":      "Funding rate negatif = shorts crowded — SHORT risky, potensi short squeeze",
        "oi_rising":        "OI naik tapi harga tidak follow = bearish divergence, hati-hati LONG",
        "oi_dropping":      "OI turun = posisi ditutup, momentum melemah",
        "volume_fake":      "Volume spike tanpa follow-through = trap, tunggu konfirmasi",
        "divergence":       "Price-indicator divergence = potential reversal, hindari directional bias",
        "macro_bad":        "Macro sentiment negatif — reduce position size, prefer SKIP",
        "macro_good":       "Macro sentiment positif — LONG bias lebih valid",
        "structure_break":  "Market structure break setelah entry = invalid setup, keluar lebih awal",
        "time_wrong":       "Timing entry terlalu awal/telat — tunggu konfirmasi lebih kuat",
        "season_wrong":     "Ecosystem sedang bear season — hindari LONG di coin ini",
    }

    derived_rules = [condition_rules[c] for c in active_conditions if c in condition_rules]

    # Build main lesson rule
    coin_str = "/".join(coins[:2]) if coins else "general"
    dir_str  = f" {direction}" if direction != "UNKNOWN" else ""

    if outcome == "BAD":
        if derived_rules:
            main_rule = (
                f"AVOID{dir_str} signal pada {coin_str} ketika: "
                + "; ".join(derived_rules[:3])
                + f" — [user feedback: \"{text[:100]}\"]"
            )
        else:
            main_rule = (
                f"REVIEW{dir_str} signal pada {coin_str}: {text[:150]} "
                f"[outcome: signal gagal menurut user]"
            )
    elif outcome == "GOOD":
        if derived_rules:
            main_rule = (
                f"GOOD{dir_str} signal pada {coin_str} ketika: "
                + "; ".join(derived_rules[:2])
                + f" — [user feedback: \"{text[:80]}\"]"
            )
        else:
            main_rule = f"SETUP{dir_str} {coin_str} ini effective: {text[:150]}"
    elif outcome == "OBSERVATION":
        main_rule = f"OBSERVATION: {text[:200]} [user note, no clear outcome]"
    else:
        main_rule = f"MIXED: {text[:200]} [perlu diperhatikan lebih lanjut]"

    # Tags
    tags = ["user_feedback", outcome.lower()]
    tags += [c.lower() for c in coins[:3]]
    if direction != "UNKNOWN": tags.append(direction.lower())
    tags += active_conditions[:5]

    return {
        "outcome":    outcome,
        "direction":  direction,
        "coins":      coins,
        "conditions": active_conditions,
        "main_rule":  main_rule,
        "sub_rules":  derived_rules[:3],
        "tags":       list(set(tags)),
        "confidence": 0.80 if active_conditions else 0.55,
        "method":     "rule_based",
    }


# ─────────────────────────────────────────────
# GEMINI-POWERED PARSER
# ─────────────────────────────────────────────

def _parse_feedback_with_gemini(text: str, api_key: str,
                                 pending_signals: list = None) -> Optional[dict]:
    """
    Parse feedback dengan Gemini — lebih akurat, handle bahasa campur.
    Fallback ke rule-based kalau Gemini gagal.
    """
    try:
        import requests as req

        context_block = ""
        if pending_signals:
            recent = pending_signals[-3:]
            ctx_lines = []
            for s in recent:
                ctx_lines.append(
                    f"- {s.get('symbol','')} {s.get('direction','')} "
                    f"@ ${s.get('entry_price',0):.4f} "
                    f"[{s.get('signal_type','')} score={s.get('score',0)}] "
                    f"status={s.get('status','PENDING')}"
                )
            context_block = "Signal terbaru:\n" + "\n".join(ctx_lines)

        prompt = f"""Kamu adalah parser feedback untuk crypto trading bot.
User memberikan komentar tentang sinyal yang diterima. Tugasmu:
1. Extract informasi terstruktur dari feedback
2. Generate lesson/rule yang actionable untuk bot

{context_block}

Feedback user: "{text}"

Respond HANYA dengan JSON (tanpa markdown/backtick), format:
{{
  "outcome": "BAD" | "GOOD" | "MIXED" | "OBSERVATION",
  "direction": "LONG" | "SHORT" | "UNKNOWN",
  "coins": ["BTC", "SOL"],
  "conditions": ["btc_down", "funding_high", "coinbase_premium", "oi_divergence", "macro_bad", "ecosystem_wrong", "timing_bad", "volume_fake", "btc_correlated"],
  "main_rule": "Rule utama yang harus diapply bot ke depannya (max 200 char, actionable)",
  "sub_rules": ["rule tambahan 1", "rule tambahan 2"],
  "tags": ["user_feedback", "btc", "long", "btc_correlated"],
  "confidence": 0.85,
  "explanation": "Kenapa lo parse begini (1-2 kalimat)"
}}

Aturan:
- main_rule harus ACTIONABLE — bot bisa langsung apply ke screening logic
- Kalau user bilang "jangan long kalau btc turun" → rule: "SKIP LONG kalau BTC_TREND=BEARISH"
- Kalau tidak jelas outcomenya → OBSERVATION
- confidence: 0.9 kalau kondisi sangat jelas, 0.6 kalau ambigu
- Jawab dalam Bahasa Indonesia untuk main_rule/sub_rules"""

        r = req.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.1, "maxOutputTokens": 600}},
            timeout=20
        )

        if r.status_code != 200:
            log.warning(f"Gemini feedback parse error: {r.status_code}")
            return None

        raw = r.json()
        text_out = raw["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Strip markdown fences kalau ada
        text_out = re.sub(r"```json\s*|\s*```", "", text_out).strip()

        parsed = json.loads(text_out)
        parsed["method"] = "gemini"
        return parsed

    except json.JSONDecodeError as e:
        log.warning(f"Gemini response JSON parse error: {e}")
        return None
    except Exception as e:
        log.warning(f"Gemini feedback parse exception: {e}")
        return None


# ─────────────────────────────────────────────
# THRESHOLD ADJUSTMENTS
# Apply lesson ke confirmed_signal thresholds
# ─────────────────────────────────────────────

THRESHOLDS_FILE = "feedback_thresholds.json"

def _load_thresholds() -> dict:
    defaults = {
        "skip_long_if_btc_bearish":    False,
        "skip_long_if_coinbase_neg":   False,
        "skip_long_if_funding_high":   False,   # threshold %
        "btc_trend_check_required":    False,
        "min_btc_rsi_for_long":        0,        # 0 = disabled
        "oi_divergence_penalty":       False,
        "adjustments_count":           0,
        "last_updated":                None,
    }
    try:
        if os.path.exists(THRESHOLDS_FILE):
            with open(THRESHOLDS_FILE) as f:
                saved = json.load(f)
                defaults.update(saved)
    except Exception:
        pass
    return defaults


def _save_thresholds(data: dict):
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(THRESHOLDS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"Save thresholds error: {e}")


def _apply_threshold_adjustments(parsed: dict) -> list:
    """
    Translate conditions dari feedback ke threshold adjustments.
    Return list of changes applied.
    """
    conditions = parsed.get("conditions", [])
    outcome    = parsed.get("outcome", "OBSERVATION")
    changes    = []

    if outcome not in ("BAD", "MIXED"):
        return changes

    thresholds = _load_thresholds()

    if "btc_down" in conditions or "btc_correlated" in conditions:
        if not thresholds["skip_long_if_btc_bearish"]:
            thresholds["skip_long_if_btc_bearish"] = True
            thresholds["btc_trend_check_required"]  = True
            changes.append("✅ Rule aktif: SKIP LONG kalau BTC trend BEARISH")

    if "coinbase_premium" in conditions:
        if not thresholds["skip_long_if_coinbase_neg"]:
            thresholds["skip_long_if_coinbase_neg"] = True
            changes.append("✅ Rule aktif: SKIP LONG kalau Coinbase Premium negatif")

    if "funding_high" in conditions:
        if not thresholds["skip_long_if_funding_high"]:
            thresholds["skip_long_if_funding_high"] = True
            changes.append("✅ Rule aktif: SKIP LONG kalau Funding Rate > +0.03%")

    if "oi_rising" in conditions or "divergence" in conditions:
        if not thresholds["oi_divergence_penalty"]:
            thresholds["oi_divergence_penalty"] = True
            changes.append("✅ Rule aktif: Penalty kalau OI naik tapi harga tidak follow")

    if changes:
        thresholds["adjustments_count"] = thresholds.get("adjustments_count", 0) + 1
        _save_thresholds(thresholds)

    return changes


def get_active_thresholds() -> dict:
    """Ambil threshold yang aktif saat ini — dipanggil dari confirmed_signal."""
    return _load_thresholds()


# ─────────────────────────────────────────────
# MAIN: PROCESS FEEDBACK
# ─────────────────────────────────────────────

def process_feedback(text: str, api_key: str = None) -> dict:
    """
    Main entry point. Parse feedback, inject lesson, adjust thresholds.
    Return result dict untuk Telegram response.
    """
    if len(text.strip()) < 5:
        return {"error": "Feedback terlalu pendek. Contoh: /feedback sinyal long BTC salah, BTC lagi downtrend"}

    # 1. Load pending signals untuk context
    pending_signals = []
    try:
        import json, os
        if os.path.exists("pending_signals.json"):
            with open("pending_signals.json") as f:
                pending_signals = json.load(f)
    except Exception:
        pass

    # 2. Parse feedback
    parsed = None
    if api_key:
        parsed = _parse_feedback_with_gemini(text, api_key, pending_signals)

    if parsed is None:
        parsed = _parse_feedback_rule_based(text)

    # 3. Inject ke learning engine
    lesson_id = None
    try:
        from learning_engine import add_manual_lesson

        full_rule = parsed["main_rule"]
        if parsed.get("sub_rules"):
            full_rule += " | " + " | ".join(parsed["sub_rules"][:2])

        lesson = add_manual_lesson(
            rule   = full_rule[:400],
            tags   = parsed.get("tags", []),
            pinned = parsed.get("confidence", 0) >= 0.85,
            role   = "SCREENER",
        )
        lesson_id = lesson.get("id")
        log.info(f"📚 Feedback lesson injected: {full_rule[:80]}...")
    except Exception as e:
        log.warning(f"Learning engine inject error: {e}")

    # 4. Apply threshold adjustments kalau outcome BAD/MIXED
    threshold_changes = _apply_threshold_adjustments(parsed)

    # 5. If matched a pending signal, record its outcome
    outcome_recorded = None
    if parsed.get("coins") and parsed["outcome"] in ("BAD", "GOOD"):
        for coin in parsed["coins"][:1]:
            sym = coin + "USDT"
            matching = [s for s in pending_signals
                        if s.get("symbol") == sym and s.get("status") == "PENDING"]
            if matching:
                sig = matching[-1]
                le_outcome = "SL_HIT" if parsed["outcome"] == "BAD" else "TP1_HIT"
                try:
                    from learning_engine import record_signal_outcome
                    record_signal_outcome(
                        symbol           = sym,
                        signal_type      = sig.get("signal_type", "SCREENER"),
                        direction        = sig.get("direction", "LONG"),
                        entry_price      = sig.get("entry_price", 0),
                        score            = sig.get("score", 0),
                        confluence_level = sig.get("confluence_level", ""),
                        outcome          = le_outcome,
                        exit_price       = sig.get("entry_price", 0),
                        hold_minutes     = 0,
                        pnl_pct          = -2.0 if parsed["outcome"] == "BAD" else 2.0,
                        notes            = f"user_feedback: {text[:100]}",
                    )
                    outcome_recorded = f"{sym} {le_outcome}"
                except Exception as e:
                    log.debug(f"Record outcome error: {e}")

    # 6. Persist feedback log
    _save_feedback_log(text, parsed, threshold_changes, lesson_id)

    return {
        "parsed":             parsed,
        "lesson_id":          lesson_id,
        "threshold_changes":  threshold_changes,
        "outcome_recorded":   outcome_recorded,
    }


def _save_feedback_log(text: str, parsed: dict, changes: list, lesson_id):
    try:
        existing = []
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE) as f:
                existing = json.load(f)
        entry = {
            "id":         int(time.time() * 1000),
            "ts":         datetime.now(timezone.utc).isoformat(),
            "text":       text[:300],
            "outcome":    parsed.get("outcome"),
            "coins":      parsed.get("coins", []),
            "conditions": parsed.get("conditions", []),
            "main_rule":  parsed.get("main_rule", ""),
            "changes":    changes,
            "lesson_id":  lesson_id,
            "method":     parsed.get("method", "rule_based"),
        }
        existing.append(entry)
        if len(existing) > MAX_FEEDBACK:
            existing = existing[-MAX_FEEDBACK:]
        with open(FEEDBACK_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        log.warning(f"Save feedback log error: {e}")


# ─────────────────────────────────────────────
# TELEGRAM RESPONSE FORMATTER
# ─────────────────────────────────────────────

def format_feedback_response(result: dict, original_text: str) -> str:
    if "error" in result:
        return f"❌ {result['error']}"

    parsed   = result["parsed"]
    changes  = result.get("threshold_changes", [])
    recorded = result.get("outcome_recorded")
    method   = parsed.get("method", "rule_based")

    outcome   = parsed.get("outcome", "OBSERVATION")
    direction = parsed.get("direction", "UNKNOWN")
    coins     = parsed.get("coins", [])
    conds     = parsed.get("conditions", [])
    rule      = parsed.get("main_rule", "")
    sub_rules = parsed.get("sub_rules", [])
    conf      = parsed.get("confidence", 0)

    outcome_emoji = {"BAD": "❌", "GOOD": "✅", "MIXED": "⚠️", "OBSERVATION": "📝"}.get(outcome, "📝")
    dir_emoji     = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else ""
    conf_bar      = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))

    cond_labels = {
        "btc_down":         "📉 BTC downtrend",
        "btc_up":           "📈 BTC uptrend",
        "btc_correlated":   "🔗 BTC correlation",
        "coinbase_premium": "🏦 Coinbase premium negatif",
        "funding_high":     "💸 Funding rate tinggi",
        "funding_low":      "💸 Funding rate rendah",
        "oi_rising":        "📊 OI naik (divergence)",
        "oi_dropping":      "📊 OI dropping",
        "volume_fake":      "📉 Volume tidak valid",
        "divergence":       "↔️ Price divergence",
        "macro_bad":        "🌍 Macro negatif",
        "macro_good":       "🌍 Macro positif",
        "structure_break":  "💥 Structure break",
        "time_wrong":       "⏰ Timing salah",
        "season_wrong":     "🌍 Wrong ecosystem season",
    }

    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{outcome_emoji} *FEEDBACK DITERIMA*",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📝 _\"{original_text[:100]}{'...' if len(original_text)>100 else ''}\"_",
        "",
        "─────── ANALISA ───────",
        f"Outcome    : *{outcome}* {outcome_emoji}",
        f"Direction  : {dir_emoji} *{direction}*" if direction != "UNKNOWN" else "Direction  : —",
        f"Coins      : *{', '.join(coins)}*" if coins else "Coins      : General",
        f"Confidence : {conf_bar} {conf*100:.0f}%",
        f"Parser     : {'🤖 Gemini' if method == 'gemini' else '📋 Rule-based'}",
    ]

    if conds:
        lines.append("")
        lines.append("─────── KONDISI TERDETEKSI ───────")
        for c in conds[:5]:
            lines.append(f"  • {cond_labels.get(c, c)}")

    lines += [
        "",
        "─────── LESSON YANG DITAMBAHKAN ───────",
        f"📚 _{rule[:200]}_",
    ]
    for sr in sub_rules[:2]:
        lines.append(f"   + _{sr}_")

    if changes:
        lines += [
            "",
            "─────── RULE UPDATE ───────",
        ]
        for ch in changes:
            lines.append(f"  {ch}")

    if recorded:
        lines += ["", f"📊 Signal outcome recorded: `{recorded}`"]

    lines += [
        "",
        "─────── EFEK KE BOT ───────",
    ]

    if changes or result.get("lesson_id"):
        effects = []
        if result.get("lesson_id"):
            effects.append(f"✅ Lesson #{result['lesson_id']} disimpan ke learning engine")
        if changes:
            effects.append(f"✅ {len(changes)} rule threshold diupdate")
        effects.append("✅ Akan diterapkan mulai scan berikutnya")
        lines.extend(effects)
    else:
        lines.append("📝 Observation disimpan — tidak ada rule change (confidence rendah)")

    lines += [
        "",
        "💡 _Kasih feedback lebih spesifik untuk hasil lebih akurat_",
        "_Contoh: `/feedback long SOL salah, BTC lagi downtrend dan coinbase premium negatif`_",
    ]

    return "\n".join(lines)


def format_feedback_history() -> str:
    """Format feedback history untuk /feedbacklog command."""
    try:
        if not os.path.exists(FEEDBACK_FILE):
            return "📭 Belum ada feedback. Coba: `/feedback sinyal tadi salah karena BTC turun`"
        with open(FEEDBACK_FILE) as f:
            history = json.load(f)
    except Exception:
        return "❌ Error membaca feedback log."

    if not history:
        return "📭 Belum ada feedback history."

    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📋 *FEEDBACK HISTORY*",
        f"🕐 {ts}",
        f"Total: *{len(history)}* feedback",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # Stats
    bad_count  = sum(1 for f in history if f.get("outcome") == "BAD")
    good_count = sum(1 for f in history if f.get("outcome") == "GOOD")
    obs_count  = len(history) - bad_count - good_count

    lines += [
        f"❌ Wrong signals: *{bad_count}*",
        f"✅ Good signals : *{good_count}*",
        f"📝 Observations : *{obs_count}*",
        "",
        "─── LAST 5 FEEDBACK ───",
    ]

    for f in reversed(history[-5:]):
        ts_short  = f.get("ts", "")[:16].replace("T", " ")
        outcome_e = {"BAD": "❌", "GOOD": "✅", "MIXED": "⚠️", "OBSERVATION": "📝"}.get(
            f.get("outcome",""), "📝")
        coins_str = ", ".join(f.get("coins", []))[:20] or "general"
        rule_short = f.get("main_rule", "")[:60]
        lines.append(
            f"{outcome_e} `{ts_short}` | {coins_str}\n"
            f"   _{rule_short}..._"
        )

    # Active thresholds
    thresholds = get_active_thresholds()
    active = [k for k, v in thresholds.items()
              if isinstance(v, bool) and v and k != "btc_trend_check_required"]
    if active:
        lines += ["", "─── ACTIVE RULES ───"]
        rule_labels = {
            "skip_long_if_btc_bearish":  "SKIP LONG kalau BTC bearish",
            "skip_long_if_coinbase_neg": "SKIP LONG kalau Coinbase Premium negatif",
            "skip_long_if_funding_high": "SKIP LONG kalau Funding > +0.03%",
            "oi_divergence_penalty":     "Penalty jika OI-price divergence",
        }
        for rule in active:
            lines.append(f"  ✅ {rule_labels.get(rule, rule)}")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# CONFIRMED SIGNAL INTEGRATION
# Cek thresholds sebelum kirim signal
# ─────────────────────────────────────────────

def check_feedback_rules(symbol: str, direction: str, btc_context: dict, oi_data: dict) -> dict:
    """
    Cek apakah ada feedback-derived rules yang block signal ini.
    Dipanggil dari confirmed_signal.generate_confirmed_signal() sebelum kirim.

    Returns: {blocked: bool, reason: str, penalty: int}
    """
    thresholds = get_active_thresholds()
    blocked    = False
    reasons    = []
    penalty    = 0

    if direction == "LONG":
        # Rule: skip long kalau BTC bearish
        if thresholds.get("skip_long_if_btc_bearish"):
            btc_env = btc_context.get("environment", "")
            btc_trend = btc_context.get("trend", "")
            if btc_env in ("BEARISH",) or btc_trend in ("BEARISH", "DOWNTREND"):
                blocked = True
                reasons.append("🚫 Rule aktif: SKIP LONG — BTC sedang bearish (dari feedback lo)")

        # Rule: penalty kalau funding tinggi
        if thresholds.get("skip_long_if_funding_high"):
            funding = oi_data.get("funding_rate", 0) or 0
            if funding > 0.03:
                penalty += 15
                reasons.append(f"⚠️ Funding rate tinggi (+{funding:.3f}%) — LONG risky")

        # Rule: penalty OI divergence
        if thresholds.get("oi_divergence_penalty"):
            oi_chg = oi_data.get("oi_change_pct", 0) or 0
            # OI naik tapi harga tidak follow = divergence signal
            if oi_chg > 5:
                penalty += 10
                reasons.append(f"⚠️ OI naik tapi belum terkonfirmasi — potensi divergence")

    return {
        "blocked": blocked,
        "reasons": reasons,
        "penalty": penalty,
    }
