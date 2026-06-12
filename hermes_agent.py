#!/usr/bin/env python3
"""
HERMES AGENT — Final Arbiter ("Sang Manusia di Meja Trading")
=============================================================
Agent KETIGA dalam pipeline validasi sinyal. Setelah dua AI berdebat
(🐂 Bull DeepSeek vs 🐻 Bear Groq) dan 🧑‍⚖️ Head Trader DeepSeek mengeluarkan
verdict + level, Hermes (NousResearch) bertindak sebagai PENGAMBIL KEPUTUSAN
FINAL — perannya seperti MANUSIA yang duduk di meja: membaca seluruh debat,
menimbang, lalu mengetuk palu GO / NO-GO.

MANDAT UTAMA — JANGAN JADI EXIT LIQUIDITY
-----------------------------------------
Tugas khusus Hermes: memastikan user TIDAK menjadi *exit liquidity* —
yaitu masuk trade tepat saat smart money sedang keluar (distribusi) dan
retail (kita) yang menampung. Ciri klasik exit-liquidity LONG:
  - harga sudah pump jauh (ngejar lilin hijau / FOMO entry)
  - funding sangat positif (long crowded, bayar mahal untuk long)
  - L/S ratio timpang ke long (retail sudah max long)
  - euphoria di sentimen sosial
  - OI melonjak bareng harga (long telat numpuk di puncak)
  - CVD divergence (harga naik tapi CVD turun = distribusi ke kekuatan)
(SHORT = cerminannya: dump jauh, funding sangat negatif, L/S timpang short,
panic/kapitulasi, CVD naik saat harga turun = absorpsi.)

Hermes boleh MENURUNKAN verdict (CONFIRM→CAUTION→SKIP) dan punya HAK VETO
penuh atas risiko exit-liquidity. Hermes TIDAK boleh menaikkan SKIP→CONFIRM
(menghormati bear). Default: capital preservation.

Drop-in: dipanggil dari ai_debate.run_signal_debate() sebagai ronde ke-4.
Kalau HERMES_API_KEY kosong / API mati, fungsi tetap mengembalikan keputusan
deterministik dari heuristik exit-liquidity (defense in depth), sehingga
proteksi exit-liquidity TETAP jalan tanpa key.

Requires .env (semua opsional kecuali key untuk pakai LLM):
  HERMES_API_KEY=...                 (Nous Portal / OpenRouter / custom)
  HERMES_MODEL=Hermes-4-405B         (opsional)
  HERMES_API_URL=https://inference-api.nousresearch.com/v1/chat/completions
  HERMES_AGENT_ENABLED=1             (opsional, 0 untuk matikan)
  HERMES_EXITLIQ_VETO=1              (opsional, 0 untuk matikan veto deterministik)
"""

import os
import re
import json
import time
import logging

import requests

log = logging.getLogger("hermes_agent")

HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
HERMES_MODEL   = os.getenv("HERMES_MODEL", "Hermes-4-405B")
HERMES_API_URL = os.getenv(
    "HERMES_API_URL",
    "https://inference-api.nousresearch.com/v1/chat/completions",
)

HERMES_ENABLED = os.getenv("HERMES_AGENT_ENABLED", "1") not in ("0", "false", "False")
# Veto deterministik jalan walau LLM mati (proteksi exit-liquidity inti).
HERMES_EXITLIQ_VETO = os.getenv("HERMES_EXITLIQ_VETO", "1") not in ("0", "false", "False")

# Ambang skor exit-liquidity (0–100): di atas ini = bahaya nyata.
EXITLIQ_CAUTION = 45    # ≥ → minimal CAUTION
EXITLIQ_SKIP    = 70    # ≥ → veto SKIP (kita kemungkinan besar jadi exit liquidity)


# ─────────────────────────────────────────────
# LOW-LEVEL CALLER (OpenAI-compatible)
# ─────────────────────────────────────────────

def _hermes_call(messages: list, max_tokens: int = 500,
                 temperature: float = 0.25, json_mode: bool = False) -> str:
    """Call Hermes (OpenAI-compatible chat completions). Return content str / ''."""
    if not HERMES_API_KEY:
        return ""
    payload = {
        "model":       HERMES_MODEL,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    for attempt in range(3):
        try:
            r = requests.post(
                HERMES_API_URL,
                headers={
                    "Authorization": f"Bearer {HERMES_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json=payload,
                timeout=45,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            elif r.status_code == 429:
                time.sleep(8 * (2 ** attempt))
            elif r.status_code in (502, 503):
                time.sleep(6 * (attempt + 1))
            else:
                log.warning(f"Hermes error {r.status_code}: {r.text[:160]}")
                return ""
        except Exception as e:
            log.warning(f"Hermes exception (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(6)
    return ""


def is_available() -> bool:
    """True kalau Hermes LLM bisa dipanggil (key ada + enabled)."""
    return bool(HERMES_ENABLED and HERMES_API_KEY)


def is_active() -> bool:
    """True kalau Hermes punya peran apapun (LLM ATAU veto deterministik)."""
    return bool(HERMES_ENABLED and (HERMES_API_KEY or HERMES_EXITLIQ_VETO))


# ─────────────────────────────────────────────
# EXIT-LIQUIDITY HEURISTIC (deterministik)
# ─────────────────────────────────────────────

def _num(val):
    """Ekstrak angka dari int/float/str (mis. '0.052%', '+12.3'). None kalau gagal."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    m = re.search(r"-?\d+(?:\.\d+)?", str(val).replace(",", ""))
    return float(m.group()) if m else None


def assess_exit_liquidity(risk: dict, is_long: bool) -> dict:
    """
    Hitung skor risiko exit-liquidity (0–100) dari sinyal pasar terstruktur.

    `risk` (semua opsional): funding, oi_change_pct, ls_ratio, ls_bias,
        euphoria(bool), cvd_4h, cvd_1h, recent_move_pct (≈% gerak baru-baru ini
        searah trade), extended(bool/ngejar).

    Skor tinggi = kemungkinan besar kita jadi exit liquidity (smart money keluar,
    kita menampung). Mengembalikan dict: {score, flags(list), level}.
    """
    risk = risk or {}
    score = 0
    flags = []

    funding   = _num(risk.get("funding"))
    oi_chg    = _num(risk.get("oi_change_pct"))
    ls_ratio  = _num(risk.get("ls_ratio"))
    cvd_4h    = _num(risk.get("cvd_4h"))
    cvd_1h    = _num(risk.get("cvd_1h"))
    move      = _num(risk.get("recent_move_pct"))
    euphoria  = bool(risk.get("euphoria"))
    extended  = bool(risk.get("extended"))

    # 1) Funding crowded searah trade → kita bayar mahal, jadi bahan bakar exit.
    if funding is not None:
        if is_long and funding >= 0.08:
            score += 22; flags.append(f"funding sangat positif ({funding:.3f}%) — long crowded")
        elif is_long and funding >= 0.04:
            score += 12; flags.append(f"funding positif tinggi ({funding:.3f}%)")
        elif (not is_long) and funding <= -0.08:
            score += 22; flags.append(f"funding sangat negatif ({funding:.3f}%) — short crowded")
        elif (not is_long) and funding <= -0.04:
            score += 12; flags.append(f"funding negatif tinggi ({funding:.3f}%)")

    # 2) L/S ratio timpang searah trade → retail sudah satu sisi (kita liquidity).
    if ls_ratio is not None and ls_ratio > 0:
        if is_long and ls_ratio >= 2.5:
            score += 18; flags.append(f"L/S {ls_ratio:.2f} — retail max long")
        elif is_long and ls_ratio >= 1.8:
            score += 10; flags.append(f"L/S {ls_ratio:.2f} — long-skewed")
        elif (not is_long) and ls_ratio <= 0.4:
            score += 18; flags.append(f"L/S {ls_ratio:.2f} — retail max short")
        elif (not is_long) and ls_ratio <= 0.55:
            score += 10; flags.append(f"L/S {ls_ratio:.2f} — short-skewed")

    # 3) Euphoria / panic sosial → puncak emosi, klasik distribusi.
    if euphoria:
        score += 16
        flags.append("euphoria/panic sosial terdeteksi — emosi di puncak")

    # 4) OI melonjak bareng harga → posisi telat numpuk di area extended.
    if oi_chg is not None and oi_chg >= 12 and (extended or (move is not None and abs(move) >= 8)):
        score += 14
        flags.append(f"OI +{oi_chg:.0f}% saat harga extended — posisi telat numpuk")

    # 5) Ngejar gerakan besar (FOMO entry) → beli puncak / jual dasar.
    if move is not None:
        if is_long and move >= 12:
            score += 16; flags.append(f"harga sudah pump +{move:.0f}% — ngejar puncak")
        elif (not is_long) and move <= -12:
            score += 16; flags.append(f"harga sudah dump {move:.0f}% — ngejar dasar")
    elif extended:
        score += 10; flags.append("entry extended dari nilai wajar — risiko ngejar")

    # 6) CVD divergence → distribusi ke kekuatan (long) / absorpsi (short).
    for label, cvd in (("4H", cvd_4h), ("1H", cvd_1h)):
        if cvd is None:
            continue
        if is_long and cvd <= -1.5 and (move is None or move > 0):
            score += 9; flags.append(f"CVD {label} {cvd:+.1f}% saat harga naik — distribusi")
            break
        elif (not is_long) and cvd >= 1.5 and (move is None or move < 0):
            score += 9; flags.append(f"CVD {label} {cvd:+.1f}% saat harga turun — absorpsi")
            break

    score = max(0, min(100, score))
    if score >= EXITLIQ_SKIP:
        level = "HIGH"
    elif score >= EXITLIQ_CAUTION:
        level = "MEDIUM"
    elif score > 0:
        level = "LOW"
    else:
        level = "NONE"

    return {"score": score, "flags": flags, "level": level}


def _downgrade(verdict: str, target: str) -> str:
    """Kembalikan verdict yang lebih konservatif di antara verdict & target."""
    order = {"CONFIRM": 0, "CAUTION": 1, "SKIP": 2}
    a = order.get(str(verdict).upper(), 0)
    b = order.get(str(target).upper(), 0)
    inv = {0: "CONFIRM", 1: "CAUTION", 2: "SKIP"}
    return inv[max(a, b)]


# ─────────────────────────────────────────────
# FINAL ARBITER
# ─────────────────────────────────────────────

def final_arbiter(
    setup_block: str,
    bull_case: str,
    bear_case: str,
    judge_verdict: str,
    judge_reason: str,
    coin: str,
    direction: str,
    is_long: bool,
    risk_signals: dict = None,
) -> dict | None:
    """
    Keputusan FINAL ala-manusia atas sinyal, setelah Bull/Bear/Head Trader.

    Return dict (None kalau Hermes nonaktif total):
      verdict            : "CONFIRM" | "CAUTION" | "SKIP"  (≤ judge_verdict)
      exit_liquidity     : bool   (True = risiko kita jadi exit liquidity)
      exitliq_score      : int    (0–100)
      exitliq_level      : "NONE"|"LOW"|"MEDIUM"|"HIGH"
      decision_line      : str    (kalimat keputusan ala-manusia)
      note               : str    (catatan/insight tambahan, boleh kosong)
      source             : "hermes_llm" | "heuristic"
    """
    if not is_active():
        return None

    # ── Lapis 1: heuristik deterministik (selalu jalan) ──────────────
    el = assess_exit_liquidity(risk_signals, is_long)
    el_score = el["score"]
    el_flags = el["flags"]

    # Veto deterministik — proteksi inti, tidak bergantung pada LLM.
    heur_verdict = judge_verdict
    if HERMES_EXITLIQ_VETO:
        if el_score >= EXITLIQ_SKIP:
            heur_verdict = _downgrade(judge_verdict, "SKIP")
        elif el_score >= EXITLIQ_CAUTION:
            heur_verdict = _downgrade(judge_verdict, "CAUTION")

    # ── Lapis 2: Hermes LLM sebagai pengambil keputusan ala-manusia ──
    if is_available():
        flags_txt = ("\n".join(f"- {f}" for f in el_flags)) if el_flags else "- (tidak ada sinyal exit-liquidity kuat)"
        user_msg = f"""{setup_block}

═══ ARGUMEN PRO-TRADE (Bull) ═══
{bull_case}

═══ ARGUMEN KONTRA-TRADE (Bear) ═══
{bear_case}

═══ VERDICT HEAD TRADER ═══
{judge_verdict} — {judge_reason}

═══ AUDIT EXIT-LIQUIDITY (skor {el_score}/100, level {el['level']}) ═══
{flags_txt}

Kamu MANUSIA pengambil keputusan final. Dua AI sudah berdebat, head trader
sudah memberi verdict. Sekarang kamu mengetuk palu.

PRIORITAS #1 — JANGAN biarkan kita jadi EXIT LIQUIDITY: jangan masuk saat
smart money sedang distribusi/keluar dan kita yang menampung. Kalau ciri
exit-liquidity nyata (funding crowded, L/S timpang, euphoria, ngejar
pump/dump, OI telat, CVD divergence) → turunkan verdict / SKIP.

ATURAN:
- Verdict final TIDAK boleh lebih agresif dari head trader ({judge_verdict}).
  Kamu hanya boleh menyamai atau menurunkan (CONFIRM→CAUTION→SKIP).
- Kalau exit-liquidity HIGH → SKIP. Kalau MEDIUM → minimal CAUTION.
- Bicara seperti manusia trader berpengalaman, singkat, tanpa markdown.

Balas JSON murni:
{{
  "verdict": "CONFIRM|CAUTION|SKIP",
  "exit_liquidity": true/false,
  "decision_line": "Satu kalimat keputusan ala-manusia (kenapa GO/NO-GO).",
  "note": "Satu kalimat saran eksekusi / hal yang diawasi (boleh kosong)."
}}"""

        raw = _hermes_call(
            messages=[
                {"role": "system", "content": (
                    "Kamu Hermes — trader manusia senior yang jadi pengambil keputusan "
                    "FINAL di meja. Obsesimu satu: jangan pernah jadi exit liquidity untuk "
                    "smart money. Tegas, capital-preservation, tidak FOMO. Kamu boleh "
                    "menolak sinyal yang sudah lolos AI lain kalau baumu bilang kita yang "
                    "ditampung. JSON valid saja, tanpa markdown."
                )},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=320,
            temperature=0.2,
            json_mode=True,
        )

        data = None
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Coba tarik blok JSON pertama kalau model membungkus teks.
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group())
                    except json.JSONDecodeError:
                        data = None

        if data:
            v = str(data.get("verdict", judge_verdict)).upper()
            if v not in ("CONFIRM", "CAUTION", "SKIP"):
                v = judge_verdict
            # Guardrail: verdict final = paling konservatif antara head trader,
            # keputusan Hermes, dan veto heuristik (tidak boleh lebih agresif).
            final_v = _downgrade(_downgrade(judge_verdict, v), heur_verdict)

            el_flag = bool(data.get("exit_liquidity")) or el_score >= EXITLIQ_CAUTION
            return {
                "verdict":        final_v,
                "exit_liquidity": el_flag,
                "exitliq_score":  el_score,
                "exitliq_level":  el["level"],
                "decision_line":  str(data.get("decision_line", "")).strip(),
                "note":           str(data.get("note", "")).strip(),
                "source":         "hermes_llm",
            }

    # ── Fallback: hanya heuristik (LLM mati/tidak ada key) ───────────
    el_flag = el_score >= EXITLIQ_CAUTION
    if el_score >= EXITLIQ_SKIP:
        dl = f"NO-GO — risiko exit-liquidity tinggi ({el_score}/100); kita yang ditampung."
    elif el_score >= EXITLIQ_CAUTION:
        dl = f"Hati-hati — ada bau exit-liquidity ({el_score}/100); kecilkan size / tunggu konfirmasi."
    else:
        dl = ""
    return {
        "verdict":        heur_verdict,
        "exit_liquidity": el_flag,
        "exitliq_score":  el_score,
        "exitliq_level":  el["level"],
        "decision_line":  dl,
        "note":           "",
        "source":         "heuristic",
    }
