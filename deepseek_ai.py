#!/usr/bin/env python3
"""
DEEPSEEK AI MODULE
==================
Engine AI utama untuk analisa sinyal, /analyze, /ask, dan /chart.
Menggantikan Groq untuk signal insight dan menjadi primary AI strategist.

Flow utama:
  signal data + news context → deepseek_signal_review() → adjusted entry/TP/SL + insight
  /analyze command           → deepseek_analyze_coin()   → full coin analysis
  /ask command               → deepseek_free_ask()        → free Q&A

DeepSeek API compatible dengan format OpenAI (chat completions).
Model: deepseek-chat (DeepSeek-V3) — reasoning kuat, fast, murah.

Requires .env:
  DEEPSEEK_API_KEY=sk-...
  DEEPSEEK_MODEL=deepseek-chat   (opsional, default deepseek-chat)
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("deepseek_ai")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL   = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Batas maksimum penyesuaian AI terhadap level harga bot (dalam persen)
# Kalau AI adjust > batas ini, pakai angka bot saja
MAX_PRICE_ADJUST_PCT = 3.0   # 3% dari harga asli bot
MAX_SCORE_ADJUST     = 10    # ±10 poin dari skor original


# ─────────────────────────────────────────────
# CORE REQUEST
# ─────────────────────────────────────────────

def _deepseek_request(
    messages: list,
    max_tokens: int = 1500,
    temperature: float = 0.3,
    response_format: str = "text",   # "text" | "json_object"
) -> str:
    """
    Call DeepSeek API (OpenAI-compatible endpoint).
    Retry up to 3x dengan exponential backoff.
    """
    if not DEEPSEEK_API_KEY:
        return ""

    payload = {
        "model":       DEEPSEEK_MODEL,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    if response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}

    for attempt in range(3):
        try:
            r = requests.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json=payload,
                timeout=40,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            elif r.status_code == 429:
                wait = 20 * (2 ** attempt)
                log.warning(f"DeepSeek 429 rate limit (attempt {attempt+1}), retry in {wait}s...")
                time.sleep(wait)
            elif r.status_code in (502, 503):
                time.sleep(12 * (attempt + 1))
            else:
                log.warning(f"DeepSeek API error {r.status_code}: {r.text[:200]}")
                return ""
        except Exception as e:
            log.warning(f"DeepSeek exception (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(10)

    log.warning("DeepSeek: max retries reached.")
    return ""


# ─────────────────────────────────────────────
# SIGNAL REVIEW & PRICE ADJUSTMENT
# ─────────────────────────────────────────────

def deepseek_signal_review(
    symbol: str,
    direction: str,
    trade: dict,
    master_score: int,
    reasons: list,
    oi_data: dict,
    tf_4h: dict,
    tf_1h: dict,
    tf_15m: dict,
    news_context: dict = None,
    signal_type: str = "CONFIRMED",
) -> dict:
    """
    Review sinyal oleh DeepSeek SEBELUM dikirim ke Telegram.

    DeepSeek bisa:
    1. Menyesuaikan level harga (entry, TP1, TP2, SL) berdasarkan analisa AI
    2. Memberikan AI insight (3 baris: edge, konflik, invalidasi)
    3. Menaikkan/menurunkan confidence (score adjustment kecil ±10)

    Return dict:
      entry          : float (adjusted atau original)
      tp1            : float
      tp2            : float
      sl             : float
      score_adj      : int   (adjustment ke master_score, -10 to +10)
      insight        : str   (3-baris AI commentary plain text)
      was_adjusted   : bool  (True kalau AI ubah angka)
      ai_verdict     : str   ("CONFIRM" | "CAUTION" | "SKIP")
      error          : str   (isi kalau ada error, kosong kalau sukses)
    """
    if not DEEPSEEK_API_KEY:
        return _passthrough(trade, "DEEPSEEK_API_KEY not set")

    coin     = symbol.replace("USDT", "")
    is_long  = direction in ("LONG", "PUMP")
    bias     = "LONG/bullish" if is_long else "SHORT/bearish"

    entry = float(trade.get("entry") or 0)
    tp1   = float(trade.get("tp1")   or 0)
    tp2   = float(trade.get("tp2")   or 0)
    sl    = float(trade.get("sl")    or 0)

    if entry <= 0:
        return _passthrough(trade, "entry price = 0, skip AI review")

    # Susun konteks teknikal
    s4  = tf_4h.get("structure",  {}) if tf_4h  else {}
    s1  = tf_1h.get("structure",  {}) if tf_1h  else {}
    s15 = tf_15m.get("structure", {}) if tf_15m else {}
    mf4 = tf_4h.get("money_flow", {}) if tf_4h  else {}
    mf1 = tf_1h.get("money_flow", {}) if tf_1h  else {}
    cp1 = tf_1h.get("candle_patterns", {}) if tf_1h  else {}
    cp15= tf_15m.get("candle_patterns",{}) if tf_15m else {}
    ob4 = tf_4h.get("order_blocks",    []) if tf_4h  else []
    ob1 = tf_1h.get("order_blocks",    []) if tf_1h  else []
    fvg4= tf_4h.get("fvg",             []) if tf_4h  else []

    funding   = oi_data.get("funding_rate",  "N/A") if oi_data else "N/A"
    oi_chg    = oi_data.get("oi_change_pct", "N/A") if oi_data else "N/A"
    ls_bias   = oi_data.get("ls_bias",       "N/A") if oi_data else "N/A"
    ls_ratio  = oi_data.get("ls_ratio",      "N/A") if oi_data else "N/A"

    reasons_str = "\n".join(f"- {r}" for r in reasons[:6])

    # Susun news context (gabungkan news_context + active lessons dari news_agent)
    news_block = ""
    if news_context:
        hi_events = news_context.get("high_impact_events", [])
        sentiment = news_context.get("sentiment_label", "NEUTRAL")
        session   = news_context.get("trading_session", "")
        unlocks   = news_context.get("upcoming_unlocks", [])
        news_headlines = news_context.get("headlines", [])
        coin_lesson    = news_context.get("coin_lesson", "")
        urgency        = news_context.get("urgency", "LOW")

        parts = []
        if sentiment:
            parts.append(f"News Sentiment: {sentiment} (urgency: {urgency})")
        if session:
            parts.append(f"Trading Session: {session}")
        if hi_events:
            parts.append("High-Impact Events: " + " | ".join(str(e) for e in hi_events[:3]))
        if unlocks:
            parts.append("Token Unlock Events: " + " | ".join(str(u) for u in unlocks[:2]))
        if news_headlines:
            parts.append("Headlines: " + " | ".join(news_headlines[:3]))
        if coin_lesson:
            parts.append(f"AI News Lesson: {coin_lesson}")

        # X (Twitter) sentiment dari news_agent cache
        x_sent     = news_context.get("x_sentiment", "")
        x_kol_cnt  = news_context.get("x_kol_count", 0)
        x_euphoria = news_context.get("x_euphoria", False)
        x_kol_ments= news_context.get("x_kol_mentions", [])
        if x_sent:
            x_line = f"X/Twitter Sentiment: {x_sent}"
            if x_kol_cnt:
                x_line += f" | KOL aktif: {x_kol_cnt}"
            if x_euphoria:
                x_line += " | ⚠️ EUPHORIA TERDETEKSI"
            parts.append(x_line)
        if x_kol_ments:
            parts.append("X KOL: " + " | ".join(str(m)[:80] for m in x_kol_ments[:2]))

        news_block = "\n".join(parts)

    # Tambahkan active lessons dari news_agent (jika tersedia)
    active_news_lessons = []
    try:
        from news_agent import get_active_lessons_from_news
        active_news_lessons = get_active_lessons_from_news()
    except ImportError:
        pass
    if active_news_lessons:
        lessons_str = "\n".join(f"- {l}" for l in active_news_lessons[:5])
        news_block += f"\n\nActive News Lessons (dari hourly fetch):\n{lessons_str}"

    tp1_r = round((tp1 - entry) / abs(entry - sl), 2) if sl and entry and sl != entry else 0
    tp2_r = round((tp2 - entry) / abs(entry - sl), 2) if sl and entry and sl != entry else 0

    user_msg = f"""Review sinyal {signal_type} — {coin} {direction} (score {master_score}/100)

Entry: {entry} | TP1: {tp1} ({tp1_r}R) | TP2: {tp2} ({tp2_r}R) | SL: {sl}
Mode: {trade.get("entry_mode","?")}

Alasan: {reasons_str}

Structure: 4H {s4.get("trend","?")} CVD{mf4.get("cvd_pct",0):+.1f}% | 1H {s1.get("trend","?")} CVD{mf1.get("cvd_pct",0):+.1f}% | 15M {s15.get("trend","?")}
Candle: 1H {cp1.get("pattern","NONE")} | 15M {cp15.get("pattern","NONE")}
OB: 4H={len(ob4)} 1H={len(ob1)} | FVG 4H={len(fvg4)}
Funding: {funding}% | OI: {oi_chg}% | L/S: {ls_ratio} ({ls_bias})
News: {news_block if news_block else "tidak ada"}

TUGAS:
1. Sesuaikan entry/TP/SL kalau ada alasan teknikal SMC yang jelas (OB/FVG/swing). Kalau sudah optimal, pertahankan.
2. Verdict: CONFIRM / CAUTION / SKIP (skip kalau ada risiko besar: unlock, macro, struktur kontra)

Balas JSON (angka numerik, tanpa markdown):
{{
  "entry": {entry},
  "tp1": {tp1},
  "tp2": {tp2},
  "sl": {sl},
  "score_adj": 0,
  "ai_verdict": "CONFIRM",
  "insight_edge": "Satu kalimat — edge utama setup ini (sebut data konkret).",
  "insight_entry": "Satu kalimat — strategi entry: market/tunggu retest/level spesifik.",
  "insight_risk": "Satu kalimat — risiko terbesar yang harus diwaspadai.",
  "insight_invalid": "Level harga SPESIFIK di mana thesis ini batal (pakai angka).",
  "adjustment_reason": "Satu kalimat kenapa disesuaikan atau 'Level sudah optimal'."
}}

score_adj: -10 sampai +10."""

    raw = _deepseek_request(
        messages=[
            {"role": "system", "content": (
                "Kamu adalah trader crypto senior dan gatekeeper sinyal. "
                "Gaya: singkat, langsung, pakai angka konkret. "
                "Kalau setup valid konfirmasi, kalau ada risiko tersembunyi sebutkan. "
                "JSON valid saja, tanpa markdown."
            )},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=400,
        temperature=0.25,
        response_format="json_object",
    )

    if not raw:
        return _passthrough(trade, "DeepSeek returned empty response")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"DeepSeek JSON parse error: {e} | raw={raw[:200]}")
        return _passthrough(trade, f"JSON parse error: {e}")

    # Validasi dan apply adjustments
    adj_entry = _safe_float(data.get("entry"), entry)
    adj_tp1   = _safe_float(data.get("tp1"),   tp1)
    adj_tp2   = _safe_float(data.get("tp2"),   tp2)
    adj_sl    = _safe_float(data.get("sl"),     sl)
    score_adj = int(max(-MAX_SCORE_ADJUST, min(MAX_SCORE_ADJUST, data.get("score_adj", 0) or 0)))
    ai_verdict= str(data.get("ai_verdict", "CONFIRM")).upper()
    if ai_verdict not in ("CONFIRM", "CAUTION", "SKIP"):
        ai_verdict = "CONFIRM"

    # Sanity check: jangan biarkan AI ubah harga lebih dari MAX_PRICE_ADJUST_PCT
    adj_entry = _clamp_price(adj_entry, entry, MAX_PRICE_ADJUST_PCT)
    adj_tp1   = _clamp_price(adj_tp1,   tp1,   MAX_PRICE_ADJUST_PCT)
    adj_tp2   = _clamp_price(adj_tp2,   tp2,   MAX_PRICE_ADJUST_PCT)
    adj_sl    = _clamp_price(adj_sl,    sl,     MAX_PRICE_ADJUST_PCT)

    # Sanity direction: TP harus di arah yang benar
    if is_long:
        if adj_tp1 <= adj_entry: adj_tp1 = tp1
        if adj_tp2 <= adj_tp1:   adj_tp2 = tp2
        if adj_sl  >= adj_entry: adj_sl  = sl
    else:
        if adj_tp1 >= adj_entry: adj_tp1 = tp1
        if adj_tp2 >= adj_tp1:   adj_tp2 = tp2
        if adj_sl  <= adj_entry: adj_sl  = sl

    was_adjusted = (
        abs(adj_entry - entry) > 0.0001 or
        abs(adj_tp1   - tp1)   > 0.0001 or
        abs(adj_tp2   - tp2)   > 0.0001 or
        abs(adj_sl    - sl)    > 0.0001
    )

    # Build insight teks — format singkat actionable
    edge     = str(data.get("insight_edge",    "")).strip()
    entry_tip= str(data.get("insight_entry",   "")).strip()
    risk     = str(data.get("insight_risk",    "")).strip()
    invalid  = str(data.get("insight_invalid", "")).strip()
    adj_rsn  = str(data.get("adjustment_reason","")).strip()

    insight_parts = []
    if edge:      insight_parts.append(f"⚡ EDGE — {edge}")
    if entry_tip: insight_parts.append(f"📍 ENTRY — {entry_tip}")
    if risk:      insight_parts.append(f"⚠️ RISIKO — {risk}")
    if invalid:   insight_parts.append(f"🚫 INVALID JIKA — {invalid}")
    if was_adjusted and adj_rsn:
        insight_parts.append(f"🔧 ADJUST — {adj_rsn}")

    insight = "\n".join(insight_parts)

    log.info(
        f"DeepSeek review {symbol}: verdict={ai_verdict} adj={was_adjusted} "
        f"score_adj={score_adj:+d} entry={adj_entry:.4f} tp1={adj_tp1:.4f} sl={adj_sl:.4f}"
    )

    return {
        "entry":        adj_entry,
        "tp1":          adj_tp1,
        "tp2":          adj_tp2,
        "sl":           adj_sl,
        "score_adj":    score_adj,
        "insight":      insight,
        "was_adjusted": was_adjusted,
        "ai_verdict":   ai_verdict,
        "error":        "",
    }


# ─────────────────────────────────────────────
# /analyze COMMAND — FULL COIN ANALYSIS
# ─────────────────────────────────────────────

def deepseek_analyze_coin(
    symbol: str,
    confluence: dict,
    tf_4h: dict,
    tf_1h: dict,
    tf_15m: dict,
    oi_data: dict,
    price: float,
    prepump: dict   = None,
    predump: dict   = None,
    scalp: dict     = None,
    swing: dict     = None,
    news_context: dict = None,
    symbol_memory: dict = None,
) -> str:
    """
    Full analisa coin untuk command /analyze <COIN>.
    Menggantikan groq_analyze_coin dan gemini_analyze_coin.
    Menggabungkan SMC data + news + symbol memory untuk analisa komprehensif.
    """
    if not DEEPSEEK_API_KEY:
        return "⚠️ DEEPSEEK_API_KEY belum diset di .env"

    coin = symbol.replace("USDT", "")

    s4  = tf_4h.get("structure",     {}) if tf_4h  else {}
    s1  = tf_1h.get("structure",     {}) if tf_1h  else {}
    mf4 = tf_4h.get("money_flow",    {}) if tf_4h  else {}
    mf1 = tf_1h.get("money_flow",    {}) if tf_1h  else {}
    mf15= tf_15m.get("money_flow",   {}) if tf_15m else {}
    cp4 = tf_4h.get("candle_patterns",{}) if tf_4h  else {}
    cp1 = tf_1h.get("candle_patterns",{}) if tf_1h  else {}
    cp15= tf_15m.get("candle_patterns",{})if tf_15m else {}
    ob4 = tf_4h.get("order_blocks",  []) if tf_4h  else []
    ob1 = tf_1h.get("order_blocks",  []) if tf_1h  else []
    fvg4= tf_4h.get("fvg",           []) if tf_4h  else []
    fvg1= tf_1h.get("fvg",           []) if tf_1h  else []
    liq = tf_1h.get("liquidity",     {}) if tf_1h  else {}

    conf_level = confluence.get("level",     "?")    if confluence else "?"
    conf_score = confluence.get("score",      0)     if confluence else 0
    conf_dir   = confluence.get("direction", "NEUTRAL") if confluence else "NEUTRAL"

    pp_score = prepump.get("total_score", 0) if prepump else 0
    pd_score = predump.get("total_score", 0) if predump else 0
    sc_score = scalp.get("score",         0) if scalp   else 0
    sw_score = swing.get("score",         0) if swing   else 0

    funding  = oi_data.get("funding_rate",  "N/A") if oi_data else "N/A"
    oi_chg   = oi_data.get("oi_change_pct", "N/A") if oi_data else "N/A"
    ls_ratio = oi_data.get("ls_ratio",      "N/A") if oi_data else "N/A"
    ls_bias  = oi_data.get("ls_bias",       "N/A") if oi_data else "N/A"

    # News block
    news_block = "Tidak ada data news."
    if news_context:
        parts = []
        if news_context.get("sentiment_label"):
            parts.append(f"Sentiment: {news_context['sentiment_label']}")
        if news_context.get("trading_session"):
            parts.append(f"Session: {news_context['trading_session']}")
        hi = news_context.get("high_impact_events", [])
        if hi:
            parts.append("Events: " + " | ".join(str(e) for e in hi[:4]))
        heads = news_context.get("headlines", [])
        if heads:
            parts.append("Headlines: " + " | ".join(heads[:4]))
        news_block = "\n".join(parts) if parts else "Tidak ada data news."

    # Symbol memory block
    mem_block = ""
    if symbol_memory:
        wr   = symbol_memory.get("win_rate", 0)
        n    = symbol_memory.get("total_trades", 0)
        best = symbol_memory.get("best_signal_type", "?")
        lessons = symbol_memory.get("lessons", [])
        mem_block = (
            f"Histori {coin}: {n} trades, WR {wr:.0f}%, best signal: {best}\n"
            + "\n".join(f"- {l}" for l in lessons[:3])
        )

    user_msg = f"""Data trading untuk {coin} (harga sekarang: {price}).

CONFLUENCE: {conf_dir} | {conf_level} | Score {conf_score}/100
Pre-Pump {pp_score} | Pre-Dump {pd_score} | Scalp {sc_score} | Swing {sw_score}

STRUCTURE:
4H: {s4.get("trend","?")} CVD{mf4.get("cvd_pct",0):+.1f}% {mf4.get("strength","")}
1H: {s1.get("trend","?")} CVD{mf1.get("cvd_pct",0):+.1f}%
15M: {mf15.get("bias","?")} | Candle 1H:{cp1.get("pattern","NONE")} 15M:{cp15.get("pattern","NONE")}
OB: 4H={len(ob4)} blok, 1H={len(ob1)} blok | FVG: 4H={len(fvg4)}, 1H={len(fvg1)}
Liq: {liq}
Funding: {funding}% | OI: {oi_chg}% | L/S: {ls_ratio} ({ls_bias})

NEWS: {news_block}

HISTORI: {mem_block if mem_block else "Belum ada histori."}

Berikan analisa singkat, padat, LANGSUNG KE STRATEGI. Gunakan format ini PERSIS (satu baris per poin, tanpa paragraf panjang):

🎯 BIAS — [LONG/SHORT/WAIT] + satu kalimat alasan utama
📍 ENTRY — harga konkret atau zona entry (contoh: 9.50–9.65)
🎯 TP1 — harga target 1 + alasan level
🎯 TP2 — harga target 2 (runner)
🛑 SL — harga konkret + kenapa di sini (swing low/OB/FVG)
⚡ EDGE — satu kalimat kenapa setup ini punya keunggulan sekarang
⚠️ RISIKO — satu kalimat faktor terbesar yang bisa bikin setup gagal
🚫 INVALID JIKA — level harga spesifik yang batalkan thesis

PENTING: Sebut harga konkret di setiap poin. Tidak perlu paragraf panjang. Maksimal 2 baris per poin."""

    result = _deepseek_request(
        messages=[
            {"role": "system", "content": (
                "Kamu adalah trader crypto profesional dengan keahlian SMC. "
                "Gaya komunikasi: singkat, langsung, actionable seperti mentor trading. "
                "Selalu sebut level harga konkret. Bahasa Indonesia. "
                "DILARANG menulis paragraf panjang — maksimal 2 baris per poin. "
                "DILARANG bold/italic markdown."
            )},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=600,
        temperature=0.3,
    )

    return result or "⚠️ DeepSeek tidak bisa menganalisa saat ini. Coba lagi."


# ─────────────────────────────────────────────
# /ask COMMAND — FREE Q&A
# ─────────────────────────────────────────────

def deepseek_free_ask(question: str) -> str:
    """
    Jawab pertanyaan bebas tentang crypto/trading via DeepSeek.
    Menggantikan groq_free_ask dan gemini_free_ask untuk command /ask.
    """
    if not DEEPSEEK_API_KEY:
        return "⚠️ DEEPSEEK_API_KEY belum diset di .env"

    result = _deepseek_request(
        messages=[
            {"role": "system", "content": (
                "Kamu adalah asisten trading crypto yang ahli di SMC, teknikal analisis, "
                "DeFi, dan market structure. Jawab dalam Bahasa Indonesia, "
                "gunakan emoji sebagai bullet/header, langsung ke poin. "
                "Kalau tidak tahu, bilang terus terang."
            )},
            {"role": "user", "content": question},
        ],
        max_tokens=900,
        temperature=0.5,
    )
    return result or "⚠️ DeepSeek tidak memberikan respons. Coba lagi."


# ─────────────────────────────────────────────
# /macro COMMAND — MACRO ANALYSIS
# ─────────────────────────────────────────────

def deepseek_macro_analysis(news_context: dict = None) -> str:
    """
    Analisa makro pasar crypto (BTC, altcoin, sentiment global).
    Menggantikan Gemini untuk /macro command.
    """
    if not DEEPSEEK_API_KEY:
        return "⚠️ DEEPSEEK_API_KEY belum diset di .env"

    news_block = ""
    if news_context:
        hi = news_context.get("high_impact_events", [])
        heads = news_context.get("headlines", [])
        session = news_context.get("trading_session", "")
        parts = []
        if session: parts.append(f"Trading Session: {session}")
        if hi: parts.append("High-Impact Events: " + " | ".join(str(e) for e in hi[:5]))
        if heads: parts.append("Headlines: " + " | ".join(heads[:5]))
        news_block = "\n".join(parts)

    result = _deepseek_request(
        messages=[
            {"role": "system", "content": (
                "Kamu adalah makro analis crypto senior. "
                "Fokus pada BTC market structure, altcoin season, sentiment, dan risiko macro. "
                "Bahasa Indonesia, emoji sebagai header, concise tapi informatif."
            )},
            {"role": "user", "content": (
                f"Berikan analisa makro crypto market saat ini.\n\n"
                f"Data yang tersedia:\n{news_block if news_block else 'Tidak ada data tambahan.'}\n\n"
                f"Format:\n"
                f"🌐 BTC MACRO — kondisi dan bias BTC saat ini\n"
                f"🔄 ALTCOIN SEASON — apakah season altcoin atau belum\n"
                f"📰 RISK EVENTS — event makro yang perlu diwaspadai\n"
                f"🎯 OUTLOOK — bias 24-48 jam ke depan\n"
                f"⚠️ RISIKO UTAMA — satu faktor yang bisa flip market"
            )},
        ],
        max_tokens=800,
        temperature=0.4,
    )
    return result or "⚠️ DeepSeek tidak bisa analisa makro saat ini."


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _passthrough(trade: dict, reason: str) -> dict:
    """Return sinyal tanpa perubahan kalau AI tidak tersedia."""
    return {
        "entry":        float(trade.get("entry") or 0),
        "tp1":          float(trade.get("tp1")   or 0),
        "tp2":          float(trade.get("tp2")   or 0),
        "sl":           float(trade.get("sl")    or 0),
        "score_adj":    0,
        "insight":      "",
        "was_adjusted": False,
        "ai_verdict":   "CONFIRM",
        "error":        reason,
    }


def _safe_float(val, fallback: float) -> float:
    """Convert val ke float, fallback kalau gagal."""
    try:
        f = float(val)
        return f if f > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _clamp_price(ai_price: float, original: float, max_pct: float) -> float:
    """Batasi adjustments harga AI dalam batas max_pct dari original."""
    if original <= 0:
        return original
    pct_diff = abs(ai_price - original) / original * 100
    if pct_diff > max_pct:
        return original
    return ai_price


def is_available() -> bool:
    """Check apakah DeepSeek API key tersedia."""
    return bool(DEEPSEEK_API_KEY)
