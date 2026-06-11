#!/usr/bin/env python3
"""
AI DEBATE ENGINE — Two-AI Bull vs Bear Signal Validation
=========================================================
Sebelum sebuah sinyal dikirim ke user, dua AI berbeda saling "berdebat"
untuk memvalidasi sinyal dari DUA SISI:

  1. BULL ANALYST  (DeepSeek) — argumen TERKUAT kenapa trade ini layak.
  2. BEAR ANALYST  (Groq)     — membaca argumen bull, lalu mencari SEMUA
                                celah, risiko tersembunyi, dan alasan gagal.
  3. HEAD TRADER   (DeepSeek) — membaca kedua argumen, menimbang, dan
                                mengeluarkan VERDICT final + level harga
                                yang sudah disesuaikan.

Tujuan: tidak ada sinyal yang lolos hanya dari analisa satu sisi. Setiap
sinyal harus bertahan melawan bantahan sebelum sampai ke user.

Output `run_signal_debate()` adalah dict yang KOMPATIBEL dengan output
`deepseek_signal_review()` (drop-in), plus field tambahan transcript debat:

  entry, tp1, tp2, sl          : float (level final, sudah di-clamp pemanggil)
  score_adj                    : int   (-10..+10)
  ai_verdict                   : "CONFIRM" | "CAUTION" | "SKIP"
  insight                      : str   (ringkasan actionable untuk user)
  was_adjusted                 : bool
  debate_bull                  : str   (argumen bull)
  debate_bear                  : str   (argumen bear)
  debate_winner                : "BULL" | "BEAR" | "MIXED"
  used_debate                  : bool
  error                        : str

Requires .env:
  DEEPSEEK_API_KEY=sk-...
  GROQ_API_KEY=gsk-...        (kalau kosong → bear di-handle DeepSeek sbg
                              devil's advocate, debat tetap jalan 1 model)
  DEEPSEEK_MODEL=deepseek-chat                (opsional)
  GROQ_MODEL=llama-3.3-70b-versatile         (opsional)
"""

import os
import json
import time
import logging

import requests

log = logging.getLogger("ai_debate")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL   = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Toggle global — set DEBATE_ENABLED=0 untuk matikan debat (fallback single-AI)
DEBATE_ENABLED = os.getenv("AI_DEBATE_ENABLED", "1") not in ("0", "false", "False")


# ─────────────────────────────────────────────
# LOW-LEVEL CALLERS
# ─────────────────────────────────────────────

def _deepseek_call(messages: list, max_tokens: int = 500,
                   temperature: float = 0.3, json_mode: bool = False) -> str:
    """Call DeepSeek (OpenAI-compatible). Return content str atau '' on error."""
    if not DEEPSEEK_API_KEY:
        return ""
    payload = {
        "model":       DEEPSEEK_MODEL,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    if json_mode:
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
                time.sleep(8 * (2 ** attempt))
            elif r.status_code in (502, 503):
                time.sleep(6 * (attempt + 1))
            else:
                log.warning(f"DeepSeek debate error {r.status_code}: {r.text[:160]}")
                return ""
        except Exception as e:
            log.warning(f"DeepSeek debate exception (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(6)
    return ""


def _groq_call(messages: list, max_tokens: int = 500,
               temperature: float = 0.4) -> str:
    """Call Groq (OpenAI-compatible). Return content str atau '' on error."""
    if not GROQ_API_KEY:
        return ""
    payload = {
        "model":       GROQ_MODEL,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    for attempt in range(3):
        try:
            r = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json=payload,
                timeout=40,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            elif r.status_code == 429:
                time.sleep(8 * (2 ** attempt))
            elif r.status_code in (502, 503):
                time.sleep(6 * (attempt + 1))
            else:
                log.warning(f"Groq debate error {r.status_code}: {r.text[:160]}")
                return ""
        except Exception as e:
            log.warning(f"Groq debate exception (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(6)
    return ""


# ─────────────────────────────────────────────
# AVAILABILITY
# ─────────────────────────────────────────────

def is_available() -> bool:
    """Debat butuh minimal DeepSeek. Groq opsional (kalau ada → 2 AI penuh)."""
    return bool(DEBATE_ENABLED and DEEPSEEK_API_KEY)


def has_two_ai() -> bool:
    """True kalau kedua AI tersedia (debat dua-otak penuh)."""
    return bool(DEEPSEEK_API_KEY and GROQ_API_KEY)


# ─────────────────────────────────────────────
# DEBATE ORCHESTRATOR
# ─────────────────────────────────────────────

def run_signal_debate(
    context_text: str,
    coin: str,
    direction: str,
    master_score: int,
    entry: float,
    tp1: float,
    tp2: float,
    sl: float,
    is_long: bool,
    signal_type: str = "SIGNAL",
    sector_brief: str = "",
) -> dict | None:
    """
    Jalankan debat Bull vs Bear lalu sintesa verdict final.

    `context_text` : blok teknikal + news yang sudah diformat pemanggil.
    Return dict kompatibel deepseek_signal_review, atau None kalau debat
    tidak bisa jalan (pemanggil harus fallback ke single-AI path).
    """
    if not is_available():
        return None

    bias = "LONG/bullish" if is_long else "SHORT/bearish"
    tp1_r = round((tp1 - entry) / abs(entry - sl), 2) if (sl and entry and sl != entry) else 0
    tp2_r = round((tp2 - entry) / abs(entry - sl), 2) if (sl and entry and sl != entry) else 0

    sector_line = f"\nKONTEKS SEKTOR (24h):\n{sector_brief}" if sector_brief else ""

    setup_block = f"""SETUP {signal_type} — {coin} {direction} (skor bot {master_score}/100)
Entry: {entry} | TP1: {tp1} ({tp1_r}R) | TP2: {tp2} ({tp2_r}R) | SL: {sl}

DATA TEKNIKAL & NEWS:
{context_text}{sector_line}"""

    # ── ROUND 1: BULL (DeepSeek) ──────────────────────────────
    bull_raw = _deepseek_call(
        messages=[
            {"role": "system", "content": (
                "Kamu adalah BULL ANALYST di meja trading crypto. Tugasmu membangun "
                "argumen TERKUAT yang mendukung trade ini. Jujur pada data — pakai angka "
                "konkret (OB/FVG/funding/OI/CVD/struktur). Maksimal 5 poin, padat, tanpa "
                "markdown. Akhiri dengan 'CONVICTION: x/10'."
            )},
            {"role": "user", "content": (
                f"{setup_block}\n\nBangun kasus BULLISH untuk {bias} ini. "
                f"Kenapa setup ini layak diambil sekarang? Apa edge konkretnya?"
            )},
        ],
        max_tokens=450,
        temperature=0.35,
    )
    bull_case = (bull_raw or "").strip()
    if not bull_case:
        # DeepSeek mati total → tidak bisa debat, fallback ke single-AI
        return None

    # ── ROUND 2: BEAR (Groq kalau ada, else DeepSeek devil's advocate) ──
    bear_prompt_user = (
        f"{setup_block}\n\n"
        f"Argumen BULL analyst:\n\"\"\"\n{bull_case}\n\"\"\"\n\n"
        f"Kamu skeptis. Bongkar setiap kelemahan: risiko tersembunyi, struktur "
        f"kontra-arah, funding/OI yang menipu, news/unlock, R:R yang buruk, atau "
        f"kemungkinan ini fakeout/stop-hunt. Apa yang bikin trade ini GAGAL?"
    )
    bear_sys = (
        "Kamu adalah BEAR ANALYST / risk skeptic di meja trading crypto. Tugasmu "
        "MEMBANTAH argumen bull dan menemukan alasan trade ini gagal. Pakai angka "
        "konkret. Maksimal 5 poin, padat, tanpa markdown. Sebut level harga spesifik "
        "yang membatalkan thesis. Akhiri dengan 'RISK: x/10'."
    )
    if GROQ_API_KEY:
        bear_raw = _groq_call(
            messages=[
                {"role": "system", "content": bear_sys},
                {"role": "user",   "content": bear_prompt_user},
            ],
            max_tokens=450,
            temperature=0.45,
        )
        bear_engine = "Groq"
    else:
        bear_raw = ""
        bear_engine = "DeepSeek"

    if not bear_raw:
        # Groq gagal/absen → DeepSeek main devil's advocate biar debat tetap utuh
        bear_raw = _deepseek_call(
            messages=[
                {"role": "system", "content": bear_sys},
                {"role": "user",   "content": bear_prompt_user},
            ],
            max_tokens=450,
            temperature=0.5,
        )
        bear_engine = "DeepSeek(DA)"

    bear_case = (bear_raw or "").strip() or "(tidak ada bantahan tersedia)"

    # ── ROUND 3: JUDGE / HEAD TRADER (DeepSeek, JSON) ─────────
    judge_user = f"""{setup_block}

═══ ARGUMEN PRO-TRADE ═══
{bull_case}

═══ ARGUMEN KONTRA-TRADE ═══
{bear_case}

Kamu KEPALA TRADER. Timbang kedua argumen secara objektif. Putuskan apakah
sinyal ini boleh dikirim ke trader. Kalau argumen kontra menemukan cacat fatal → SKIP.
Kalau valid tapi ada risiko nyata → CAUTION. Kalau argumen pro menang telak → CONFIRM.
Sesuaikan level harga HANYA kalau ada alasan teknikal jelas dari debat.

Balas JSON murni (angka numerik, tanpa markdown):
{{
  "entry": {entry},
  "tp1": {tp1},
  "tp2": {tp2},
  "sl": {sl},
  "score_adj": 0,
  "ai_verdict": "CONFIRM",
  "winner": "PRO",
  "verdict_reason": "Satu kalimat — kenapa verdict ini (sebut argumen pemenang).",
  "insight_edge": "Satu kalimat — edge utama yang bertahan dari debat.",
  "insight_entry": "Satu kalimat — strategi entry: market/tunggu retest/level.",
  "insight_risk": "Satu kalimat — risiko terbesar dari argumen kontra-trade.",
  "insight_invalid": "Level harga SPESIFIK di mana thesis batal (angka)."
}}

winner: "PRO" (argumen pro-trade menang) | "KONTRA" (argumen kontra menang, curigai sinyal) | "MIXED".
score_adj: -10..+10 (negatif kalau kontra kuat)."""

    judge_raw = _deepseek_call(
        messages=[
            {"role": "system", "content": (
                "Kamu kepala trading desk — pengambil keputusan final dan gatekeeper "
                "sinyal. Objektif, tegas, utamakan capital preservation. Kalau ragu, "
                "pilih CAUTION atau SKIP. JSON valid saja, tanpa markdown."
            )},
            {"role": "user", "content": judge_user},
        ],
        max_tokens=400,
        temperature=0.2,
        json_mode=True,
    )

    if not judge_raw:
        return None

    try:
        data = json.loads(judge_raw)
    except json.JSONDecodeError as e:
        log.warning(f"Debate judge JSON parse error: {e} | raw={judge_raw[:160]}")
        return None

    verdict = str(data.get("ai_verdict", "CONFIRM")).upper()
    if verdict not in ("CONFIRM", "CAUTION", "SKIP"):
        verdict = "CAUTION"

    winner = str(data.get("winner", "MIXED")).upper()
    if winner not in ("PRO", "KONTRA", "MIXED"):
        winner = "MIXED"

    try:
        score_adj = int(data.get("score_adj", 0) or 0)
    except (TypeError, ValueError):
        score_adj = 0
    score_adj = max(-10, min(10, score_adj))

    def _f(key, fallback):
        try:
            v = float(data.get(key))
            return v if v > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    out_entry = _f("entry", entry)
    out_tp1   = _f("tp1",   tp1)
    out_tp2   = _f("tp2",   tp2)
    out_sl    = _f("sl",    sl)

    was_adjusted = (
        abs(out_entry - entry) > 1e-9 or abs(out_tp1 - tp1) > 1e-9 or
        abs(out_tp2 - tp2) > 1e-9 or abs(out_sl - sl) > 1e-9
    )

    # Build insight untuk user
    vr     = str(data.get("verdict_reason", "")).strip()
    edge   = str(data.get("insight_edge", "")).strip()
    entry_t= str(data.get("insight_entry", "")).strip()
    risk   = str(data.get("insight_risk", "")).strip()
    invalid= str(data.get("insight_invalid", "")).strip()

    parts = []
    if vr:      parts.append(f"⚖️ VERDIKT — {vr} (pemenang: {winner})")
    if edge:    parts.append(f"⚡ EDGE — {edge}")
    if entry_t: parts.append(f"📍 ENTRY — {entry_t}")
    if risk:    parts.append(f"⚠️ RISIKO (bear) — {risk}")
    if invalid: parts.append(f"🚫 INVALID JIKA — {invalid}")
    insight = "\n".join(parts)

    log.info(
        f"🥊 Debate {coin} {direction}: verdict={verdict} winner={winner} "
        f"score_adj={score_adj:+d} bear={bear_engine} adjusted={was_adjusted}"
    )

    return {
        "entry":        out_entry,
        "tp1":          out_tp1,
        "tp2":          out_tp2,
        "sl":           out_sl,
        "score_adj":    score_adj,
        "insight":      insight,
        "was_adjusted": was_adjusted,
        "ai_verdict":   verdict,
        "debate_bull":  bull_case,
        "debate_bear":  bear_case,
        "debate_winner": winner,
        "bear_engine":  bear_engine,
        "used_debate":  True,
        "error":        "",
    }
