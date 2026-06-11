#!/usr/bin/env python3
"""
CONFIRMED ENTRY SIGNAL ENGINE v1.0
====================================
Gabungan semua detector + backtest validasi sebelum kirim sinyal.

Flow:
  1. Gabungkan semua detector (confluence + prepump + predump + scalp + swing)
     → Hitung MASTER SCORE dari semua input
     → Tentukan direction LONG/SHORT dengan confidence

  2. Validasi historis (quick backtest 7 hari)
     → Kalau strategy ini profitable di 7 hari terakhir → lanjut
     → Kalau tidak → sinyal DITAHAN, tidak dikirim

  3. Kirim ke Telegram kalau:
     → Master score >= threshold (CONFIRMED)
     → Backtest profit factor >= 1.0 (tidak rugi secara historis)
     → Semua sinyal selaras (prepump + scalp + confluence searah)

Command Telegram:
  Tidak ada command — ini berjalan OTOMATIS di setiap scan.
  Kalau ada confirmed signal → kirim otomatis
  Kalau tidak ada → diam (tidak spam)

Threshold:
  MASTER_SCORE >= 75 → CONFIRMED ENTRY
  MASTER_SCORE 60-74 → WATCH (tidak kirim, tapi log)
  MASTER_SCORE < 60  → SKIP
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

log = logging.getLogger("confirmed_signal")

# ── Market Context ──────────────────────────
try:
    from market_context import get_market_context, apply_market_context_to_score, format_market_context_block
    MARKET_CONTEXT_MODULE = True
except ImportError:
    MARKET_CONTEXT_MODULE = False
    log.warning("market_context.py tidak ditemukan — market context filter dinonaktifkan")

# ── News Gate ───────────────────────────────
try:
    from news_sentiment import get_news_gate, get_structured_news_for_ai
    NEWS_GATE_MODULE = True
except ImportError:
    NEWS_GATE_MODULE = False

# ── Social Gate (Reddit + HackerNews via last30days-skill) ──────────────────
try:
    from social_sentiment import get_social_gate
    SOCIAL_GATE_MODULE = True
except ImportError:
    SOCIAL_GATE_MODULE = False

# ── DeepSeek AI ─────────────────────────────
try:
    from deepseek_ai import deepseek_signal_review
    DEEPSEEK_MODULE = True
except ImportError:
    DEEPSEEK_MODULE = False

# ── Auto Validator ─────────────────────────
try:
    from auto_validator import (
        run_auto_validation, format_validation_summary,
        evolve_sensitivity, VALIDATION_PASS_SCORE,
    )
    AUTO_VALIDATOR_ENABLED = True
except ImportError:
    AUTO_VALIDATOR_ENABLED = False
    log.warning("auto_validator.py tidak ditemukan — auto validation dinonaktifkan")

CONFIRMED_SIGNAL_FILE = "confirmed_signals_history.json"

# ── Threshold ──────────────────────────────
MASTER_SCORE_CONFIRMED  = 75   # kirim ke Telegram
MASTER_SCORE_WATCH      = 60   # log tapi tidak kirim
BT_MIN_PROFIT_FACTOR    = 1.0  # backtest harus >= break even
BT_DAYS                 = 7    # quick backtest 7 hari
BT_MIN_TRADES           = 3    # minimal 3 trades di backtest agar statistik valid

# ── Bobot per detector (total harus = 100) ─
WEIGHT = {
    "confluence": 30,   # SMC market structure (4H/1H/15M)
    "prepump":    25,   # Funding squeeze + OI + momentum
    "predump":    25,   # Long squeeze + bearish momentum + OI
    "scalp":      10,   # 15M setup (entry trigger)
    "swing":      10,   # 4H/1H swing setup (hold time)
}

# ── Cooldown: jangan kirim signal sama dalam X menit ──
SIGNAL_COOLDOWN_MINUTES = 60
_last_signal_time: dict = {}  # symbol → datetime

# ── Signal Persistence Cache ────────────────────────
# Track skor sinyal dari scan sebelumnya.
# Sinyal yang tiba-tiba muncul (score rendah → tinggi dalam 1 scan) = suspicious.
# Sinyal yang konsisten dari scan sebelumnya = lebih dipercaya.
_prev_scores: dict = {}   # symbol → {"score": int, "direction": str, "ts": datetime}


def _update_prev_score(symbol: str, direction: str, score: int):
    _prev_scores[symbol] = {
        "score":     score,
        "direction": direction,
        "ts":        datetime.now(timezone.utc),
    }


def _persistence_adjustment(symbol: str, direction: str, current_score: int) -> int:
    """
    Cek konsistensi sinyal antar scan (signal persistence).
    Professional systems hanya kirim sinyal yang muncul 2+ scan berturut-turut.

    Return: adjustment integer
      +5  → sinyal persisten (juga kuat di scan sebelumnya)
      0   → scan pertama atau netral
      -12 → sinyal tiba-tiba spike dari skor rendah (suspicious noise)
      -5  → arah berubah dibanding scan sebelumnya
    """
    prev = _prev_scores.get(symbol)
    if not prev:
        return 0

    age_min = (datetime.now(timezone.utc) - prev["ts"]).total_seconds() / 60
    if age_min > 45:
        return 0  # Cache terlalu lama, tidak relevan

    prev_dir   = prev.get("direction", "NONE")
    prev_score = prev.get("score", 0)

    if prev_dir == direction:
        if prev_score >= 70:
            return +5   # Konsisten kuat → bonus kepercayaan
        elif prev_score >= 50:
            return 0    # Konsisten moderat → netral
        else:
            return -12  # Tiba-tiba muncul dari score rendah → suspicious spike
    else:
        return -5       # Arah flip → ketidakpastian


def _is_in_cooldown(symbol: str) -> bool:
    last = _last_signal_time.get(symbol)
    if not last:
        return False
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return elapsed < SIGNAL_COOLDOWN_MINUTES


def _set_signal_cooldown(symbol: str):
    _last_signal_time[symbol] = datetime.now(timezone.utc)


# ─────────────────────────────────────────────
# MASTER SCORE ENGINE
# ─────────────────────────────────────────────

def compute_master_score(
    symbol: str,
    confluence: dict,
    prepump: dict,
    predump: dict,
    scalp: dict,
    swing: dict,
    oi_data: dict,
) -> dict:
    """
    Gabungkan semua detector jadi satu MASTER SCORE.

    Aturan:
    - Semua detector harus searah (LONG atau SHORT)
    - Semakin banyak yang agree → skor makin tinggi
    - Ada satu yang kuat kontra-arah → skor dikurangi (conflict penalty)

    Return dict:
      direction: LONG | SHORT | CONFLICT
      master_score: 0-100
      confidence: HIGH | MEDIUM | LOW
      signal_count: berapa detector yang agree
      reasons: list alasan utama
      conflict_reasons: list alasan kontra
    """
    result = {
        "symbol":           symbol,
        "direction":        "NONE",
        "master_score":     0,
        "confidence":       "LOW",
        "signal_count":     0,
        "agreement_count":  0,
        "conflict_count":   0,
        "reasons":          [],
        "conflict_reasons": [],
        "component_scores": {},
        "bt_validated":     False,
        "bt_profit_factor": 0.0,
        "bt_win_rate":      0.0,
    }

    long_votes  = 0
    short_votes = 0
    weighted_long  = 0.0
    weighted_short = 0.0
    reasons        = []
    conflict_r     = []
    components     = {}

    # ── 1. CONFLUENCE (structure + OB + FVG) ────
    conf_dir   = confluence.get("direction", "NEUTRAL")
    conf_score = confluence.get("score", 0)
    conf_level = confluence.get("level", "POOR")

    if conf_dir == "PUMP" and conf_level not in ("POOR",):
        w = WEIGHT["confluence"] * (conf_score / 100)
        weighted_long += w
        long_votes    += 1
        components["confluence"] = {"direction": "LONG", "score": conf_score, "weight": round(w, 1)}
        reasons.append(f"📐 Confluence LONG ({conf_level}, {conf_score}/100) — structure + OB + FVG aligned")
    elif conf_dir == "DUMP" and conf_level not in ("POOR",):
        w = WEIGHT["confluence"] * (conf_score / 100)
        weighted_short += w
        short_votes    += 1
        components["confluence"] = {"direction": "SHORT", "score": conf_score, "weight": round(w, 1)}
        reasons.append(f"📐 Confluence SHORT ({conf_level}, {conf_score}/100) — structure + OB + FVG aligned")
    else:
        components["confluence"] = {"direction": "NEUTRAL", "score": conf_score, "weight": 0}

    # ── 2. PRE-PUMP detector ─────────────────────
    pp_score = prepump.get("total_score", 0) if prepump else 0
    pp_label = prepump.get("label", "") if prepump else ""

    if pp_score >= 55:
        w = WEIGHT["prepump"] * (pp_score / 100)
        weighted_long += w
        long_votes    += 1
        components["prepump"] = {"direction": "LONG", "score": pp_score, "weight": round(w, 1)}
        reasons.append(f"🎯 Pre-Pump {pp_score}/100 ({pp_label}) — funding squeeze + momentum")
    elif pp_score >= 35:
        # Weak pump signal — hanya beri setengah bobot
        w = WEIGHT["prepump"] * (pp_score / 100) * 0.5
        weighted_long += w
        components["prepump"] = {"direction": "LONG_WEAK", "score": pp_score, "weight": round(w, 1)}
    else:
        components["prepump"] = {"direction": "NONE", "score": pp_score, "weight": 0}

    # ── 3. PRE-DUMP detector ─────────────────────
    pd_score = predump.get("total_score", 0) if predump else 0
    pd_label = predump.get("label", "") if predump else ""

    if pd_score >= 55:
        w = WEIGHT["predump"] * (pd_score / 100)
        weighted_short += w
        short_votes    += 1
        components["predump"] = {"direction": "SHORT", "score": pd_score, "weight": round(w, 1)}
        reasons.append(f"💀 Pre-Dump {pd_score}/100 ({pd_label}) — long squeeze + bearish momentum")
    elif pd_score >= 35:
        w = WEIGHT["predump"] * (pd_score / 100) * 0.5
        weighted_short += w
        components["predump"] = {"direction": "SHORT_WEAK", "score": pd_score, "weight": round(w, 1)}
    else:
        components["predump"] = {"direction": "NONE", "score": pd_score, "weight": 0}

    # ── 4. SCALP detector ───────────────────────
    sc_score = scalp.get("score", 0) if scalp else 0
    sc_dir   = scalp.get("direction", "NONE") if scalp else "NONE"

    if sc_score >= 60 and sc_dir == "LONG":
        w = WEIGHT["scalp"] * (sc_score / 100)
        weighted_long += w
        long_votes    += 1
        components["scalp"] = {"direction": "LONG", "score": sc_score, "weight": round(w, 1)}
        reasons.append(f"⚡ Scalp LONG {sc_score}/100 — sweep + rejection confirmed 15M")
    elif sc_score >= 60 and sc_dir == "SHORT":
        w = WEIGHT["scalp"] * (sc_score / 100)
        weighted_short += w
        short_votes    += 1
        components["scalp"] = {"direction": "SHORT", "score": sc_score, "weight": round(w, 1)}
        reasons.append(f"⚡ Scalp SHORT {sc_score}/100 — sweep + rejection confirmed 15M")
    else:
        components["scalp"] = {"direction": "NONE", "score": sc_score, "weight": 0}

    # ── 5. SWING detector ───────────────────────
    sw_score = swing.get("score", 0) if swing else 0
    sw_dir   = swing.get("direction", "NONE") if swing else "NONE"

    if sw_score >= 60 and sw_dir == "LONG":
        w = WEIGHT["swing"] * (sw_score / 100)
        weighted_long += w
        long_votes    += 1
        components["swing"] = {"direction": "LONG", "score": sw_score, "weight": round(w, 1)}
        reasons.append(f"📈 Swing LONG {sw_score}/100 — 4H bias + 1H trigger aligned")
    elif sw_score >= 60 and sw_dir == "SHORT":
        w = WEIGHT["swing"] * (sw_score / 100)
        weighted_short += w
        short_votes    += 1
        components["swing"] = {"direction": "SHORT", "score": sw_score, "weight": round(w, 1)}
        reasons.append(f"📉 Swing SHORT {sw_score}/100 — 4H bias + 1H trigger aligned")
    else:
        components["swing"] = {"direction": "NONE", "score": sw_score, "weight": 0}

    # ── 6. OI/Funding overlay ────────────────────
    funding = oi_data.get("funding_rate", 0) or 0
    ls_bias = oi_data.get("ls_bias", "BALANCED")
    oi_chg  = oi_data.get("oi_change_pct", 0) or 0

    # Funding rate extreme → strong directional signal
    if funding < -0.03:
        weighted_long += 5
        reasons.append(f"🔥 Funding extreme negative ({funding:.3f}%) — short squeeze imminent")
    elif funding < -0.01:
        weighted_long += 2
    elif funding > 0.03:
        weighted_short += 5
        reasons.append(f"🔥 Funding extreme positive (+{funding:.3f}%) — long squeeze imminent")
    elif funding > 0.01:
        weighted_short += 2

    if ls_bias == "SHORT_HEAVY":
        weighted_long += 3
        reasons.append(f"⚖️ L/S short-heavy → squeeze fuel LONG")
    elif ls_bias == "LONG_HEAVY":
        weighted_short += 3
        reasons.append(f"⚖️ L/S long-heavy → liquidation fuel SHORT")

    # ── 6b. Ecosystem Season Alignment ──────────────
    try:
        from ecosystem_detector import (
            get_coin_ecosystem, get_ecosystem_boost,
            get_dump_ecosystem_penalty, get_active_seasons,
        )
        ticker = symbol.replace("USDT", "").lower()
        eco    = get_coin_ecosystem(ticker)
        if eco:
            actives = get_active_seasons(top_n=5)
            boost   = get_ecosystem_boost(ticker)
            dump_b  = get_dump_ecosystem_penalty(ticker)
            # Prediksi direction belum pasti di sini, tapi hitung untuk kedua arah
            # LONG boost: ecosystem lagi season
            if boost > 0:
                weighted_long += boost * 2
                reasons.append(f"🌍 {eco} season aktif → LONG aligned (+{boost*2:.0f}pt)")
            elif boost < 0:
                weighted_long += boost       # penalty untuk LONG
                conflict_r.append(f"⚠️ Ecosystem {eco} bearish — LONG melawan ecosystem trend")
            # SHORT boost: ecosystem lagi bearish
            if dump_b > 0:
                weighted_short += dump_b * 2
                reasons.append(f"🌍 {eco} ecosystem bearish → SHORT aligned (+{dump_b*2:.0f}pt)")
            elif dump_b < 0:
                weighted_short += dump_b     # penalty untuk SHORT
                conflict_r.append(f"⚠️ Ecosystem {eco} season aktif — SHORT melawan trend")
    except ImportError:
        pass
    except Exception:
        pass

    # ── 6c. Coinbase Premium (Institutional Bias) ───────────────────────
    # NOTE: `direction` final baru dihitung di blok #7 di bawah. Pakai provisional
    # direction dari weighted_long/short saat ini supaya kontribusi CB tetap ikut
    # menentukan arah final. (Sebelumnya blok ini memakai `direction` yang belum
    # di-assign → UnboundLocalError yang ketelan except → institutional bias tidak
    # pernah berkontribusi sama sekali.)
    try:
        from coinbase_premium import get_premium_master_contribution
        if weighted_long > weighted_short:
            prov_dir = "LONG"
        elif weighted_short > weighted_long:
            prov_dir = "SHORT"
        else:
            prov_dir = "NONE"
        cb = get_premium_master_contribution(prov_dir if prov_dir != "NONE" else "LONG")
        if cb.get("weighted_long_add", 0) > 0:
            weighted_long += cb["weighted_long_add"]
            if cb.get("reason"):
                reasons.append(cb["reason"])
        if cb.get("weighted_short_add", 0) > 0:
            weighted_short += cb["weighted_short_add"]
            if cb.get("reason"):
                reasons.append(cb["reason"])
        # Hard counter-institutional penalty
        p_val = cb.get("premium_pct")
        if p_val is not None:
            if abs(p_val) > 0.15:
                if p_val > 0 and prov_dir == "SHORT":
                    conflict_r.append(
                        f"🏦 CB Premium {p_val:+.4f}% STRONGLY POSITIVE — SHORT melawan institutional"
                    )
                elif p_val < 0 and prov_dir == "LONG":
                    conflict_r.append(
                        f"🏦 CB Premium {p_val:+.4f}% STRONGLY NEGATIVE — LONG melawan institutional"
                    )
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"CB premium master score error: {e}")

    # ── 7. Conflict detection ────────────────────
    # Kalau satu arah ada tapi arah lain juga ada sinyal kuat → conflict
    if weighted_long > 5 and weighted_short > 5:
        conflict_ratio = min(weighted_long, weighted_short) / max(weighted_long, weighted_short)
        if conflict_ratio > 0.5:
            conflict_r.append(
                f"⚠️ Conflict: LONG score={weighted_long:.1f} vs SHORT score={weighted_short:.1f} "
                f"({conflict_ratio*100:.0f}% conflicting) — sinyal tidak solid"
            )

    # ── Determine final direction ────────────────
    total_weight = weighted_long + weighted_short

    if total_weight < 5:
        direction = "NONE"
        master_score = 0
    elif weighted_long > weighted_short:
        direction    = "LONG"
        master_score = min(100, int((weighted_long / total_weight) * 100 + weighted_long * 0.5))
    else:
        direction    = "SHORT"
        master_score = min(100, int((weighted_short / total_weight) * 100 + weighted_short * 0.5))

    # Conflict penalty
    if conflict_r:
        master_score = int(master_score * 0.75)

    # Minimum strong component requirement
    # Mencegah 1 sinyal lemah yang sendirian trigger confirmed signal via ratio dominance
    if direction in ("LONG", "SHORT"):
        strong_comps = sum(
            1 for c in components.values()
            if c.get("score", 0) >= 50
            and (
                (direction == "LONG"  and "LONG"  in c.get("direction", ""))
             or (direction == "SHORT" and "SHORT" in c.get("direction", ""))
            )
        )
        if strong_comps < 2:
            master_score = int(master_score * 0.65)
            conflict_r.append(
                f"⚠️ Hanya {strong_comps}/5 komponen score ≥50 — "
                f"sinyal kurang solid, score dikurangi 35%"
            )

    # Agreement bonus: semua detector agree → bonus
    total_votes = long_votes + short_votes
    if direction == "LONG":
        agree_ratio = long_votes / total_votes if total_votes > 0 else 0
    else:
        agree_ratio = short_votes / total_votes if total_votes > 0 else 0

    if agree_ratio >= 0.8 and total_votes >= 3:
        master_score = min(100, master_score + 5)
        reasons.append(f"✅ {int(agree_ratio*100)}% detector agreement ({total_votes} signals) — high conviction")

    # Confidence level
    if master_score >= 80 and total_votes >= 3 and not conflict_r:
        confidence = "HIGH"
    elif master_score >= 65 and not conflict_r:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    result.update({
        "direction":        direction,
        "master_score":     master_score,
        "confidence":       confidence,
        "signal_count":     total_votes,
        "agreement_count":  long_votes if direction == "LONG" else short_votes,
        "conflict_count":   len(conflict_r),
        "reasons":          reasons,
        "conflict_reasons": conflict_r,
        "component_scores": components,
        "weighted_long":    round(weighted_long, 1),
        "weighted_short":   round(weighted_short, 1),
    })
    return result


# ─────────────────────────────────────────────
# QUICK BACKTEST VALIDATION
# ─────────────────────────────────────────────

def _quick_backtest_validate(symbol: str, direction: str) -> dict:
    """
    Validasi backtest untuk coin.
    Priority: cached /btall result (≤7 hari) → live 7-hari backtest.
    Cached result jauh lebih cepat dan tidak spam API.

    Filosofi: backtest itu untuk MENGUMPULKAN data dulu. Kalau data belum cukup
    (<BT_MIN_TRADES) → JANGAN blok (insufficient_data=True), biar coin tetap bisa
    lolos sambil ngumpulin track record. Baru kalau data SUDAH cukup tapi PF jelek
    → signal ditahan (valid=False).
    """
    # ── 1. Cek cached btall result terlebih dahulu ──
    try:
        from backtest_engine import get_coin_bt_grade
        cached = get_coin_bt_grade(symbol, max_age_hours=168)
        if cached and not cached.get("error") and cached.get("total_trades", 0) >= BT_MIN_TRADES:
            pf    = cached.get("profit_factor", 0)
            wr    = cached.get("win_rate", 0)
            n     = cached.get("total_trades", 0)
            grade = cached.get("_grade", "UNKNOWN")
            valid = pf >= BT_MIN_PROFIT_FACTOR
            reason = (
                f"Cache btall ({grade}): PF={pf:.2f}, WR={wr:.0f}%, {n} trades"
                if valid else
                f"Cache btall ({grade}) GAGAL: PF={pf:.2f} < {BT_MIN_PROFIT_FACTOR}"
            )
            log.info(f"  {symbol}: backtest from cache — {reason}")
            return {"valid": valid, "profit_factor": pf, "win_rate": wr,
                    "trades": n, "strategy": "cached_combined",
                    "insufficient_data": False, "reason": reason}
    except Exception as e:
        log.debug(f"Cache lookup error: {e}")

    # ── 2. Fallback: live quick backtest ──
    try:
        from backtest_engine import run_backtest
    except ImportError:
        return {"valid": True, "profit_factor": 1.0, "win_rate": 50.0,
                "strategy": "none", "insufficient_data": True,
                "reason": "backtest_engine unavailable, bypassed"}

    strategies = ["scalp", "prepump"] if direction == "LONG" else ["scalp", "predump"]
    best_result = None
    best_pf     = 0.0

    for strat in strategies:
        try:
            r = run_backtest(symbol, strat, days=BT_DAYS, stake_usdt=100)
            if "error" in r:
                continue
            pf = r.get("profit_factor", 0)
            n  = r.get("total_trades", 0)
            if n >= BT_MIN_TRADES and pf > best_pf:
                best_pf     = pf
                best_result = r
                best_result["_strategy"] = strat
        except Exception as e:
            log.debug(f"Quick BT {strat} error: {e}")

    if best_result is None:
        # Data belum cukup → bypass (lagi ngumpulin data), JANGAN blok.
        return {"valid": True, "profit_factor": 0.0, "win_rate": 0.0,
                "strategy": "none", "insufficient_data": True,
                "reason": f"Data BT belum cukup (<{BT_MIN_TRADES} trades) — lolos sambil ngumpulin data"}

    pf    = best_result.get("profit_factor", 0)
    wr    = best_result.get("win_rate", 0)
    n     = best_result.get("total_trades", 0)
    strat = best_result.get("_strategy", "?")
    valid = pf >= BT_MIN_PROFIT_FACTOR
    reason = (
        f"BT {strat} {BT_DAYS}d: PF={pf:.2f}, WR={wr:.0f}%, {n} trades"
        if valid else
        f"BT {strat} {BT_DAYS}d GAGAL: PF={pf:.2f} < {BT_MIN_PROFIT_FACTOR} — signal ditahan"
    )
    return {"valid": valid, "profit_factor": pf, "win_rate": wr,
            "trades": n, "strategy": strat,
            "insufficient_data": False, "reason": reason}


# ─────────────────────────────────────────────
# CONFIRMED SIGNAL GENERATOR
# ─────────────────────────────────────────────

def generate_confirmed_signal(
    symbol: str,
    price: float,
    confluence: dict,
    prepump: dict,
    predump: dict,
    scalp: dict,
    swing: dict,
    oi_data: dict,
    tf_4h: dict,
    tf_1h: dict,
    tf_15m: dict,
) -> Optional[dict]:
    """
    Entry point utama. Dipanggil dari run_scan() per coin.

    Return:
      dict signal kalau CONFIRMED
      None kalau tidak ada signal valid
    """
    # 1. Compute master score
    master = compute_master_score(
        symbol, confluence, prepump, predump, scalp, swing, oi_data
    )

    direction    = master["direction"]
    master_score = master["master_score"]

    log.info(f"  {symbol}: master={master_score} dir={direction} "
             f"long={master['weighted_long']} short={master['weighted_short']}")

    # 2. Filter awal
    # Selalu update persistence cache dulu (even jika score di bawah threshold)
    _update_prev_score(symbol, direction, master_score)

    if direction == "NONE" or master_score < MASTER_SCORE_WATCH:
        return None

    # Signal persistence adjustment
    persist_adj = _persistence_adjustment(symbol, direction, master_score)
    if persist_adj != 0:
        master["master_score"] = max(0, min(100, master["master_score"] + persist_adj))
        master_score = master["master_score"]
        if persist_adj > 0:
            master["reasons"].append(
                f"✅ Sinyal persisten dari scan sebelumnya ({persist_adj:+d}pt)"
            )
        elif persist_adj == -12:
            master["conflict_reasons"].append(
                f"⚠️ Sinyal tiba-tiba spike — tidak ada di scan sebelumnya ({persist_adj:+d}pt)"
            )
        elif persist_adj == -5:
            master["conflict_reasons"].append(
                f"⚠️ Arah sinyal berubah dari scan sebelumnya ({persist_adj:+d}pt)"
            )
        log.info(f"  {symbol}: persistence adj={persist_adj:+d} → score={master_score}")

    if master_score < MASTER_SCORE_CONFIRMED:
        log.info(f"  {symbol}: WATCH zone ({master_score}) — below confirmed threshold {MASTER_SCORE_CONFIRMED}")
        return None

    # 3. Cooldown check
    if _is_in_cooldown(symbol):
        log.info(f"  {symbol}: in cooldown, skip")
        return None

    # 3d. Macro trend filter — EMA9 vs EMA21 pada 4H candles
    # Sinyal yang berlawanan dengan macro trend kena penalty besar
    try:
        _c4h = tf_4h.get("candles", [])
        if _c4h and len(_c4h) >= 22:
            _cls = [c["close"] for c in _c4h]
            # EMA9
            _k9 = 2 / 10; _e9 = sum(_cls[:9]) / 9
            for _v in _cls[9:]:
                _e9 = _v * _k9 + _e9 * (1 - _k9)
            # EMA21
            _k21 = 2 / 22; _e21 = sum(_cls[:21]) / 21
            for _v in _cls[21:]:
                _e21 = _v * _k21 + _e21 * (1 - _k21)
            _macro_bull  = _e9 > _e21
            _macro_label = "BULLISH" if _macro_bull else "BEARISH"
            _contra = (direction == "LONG" and not _macro_bull) or \
                      (direction == "SHORT" and _macro_bull)
            if _contra:
                _m_penalty = 15
                master["master_score"] = max(0, master["master_score"] - _m_penalty)
                master["conflict_reasons"].append(
                    f"📉 Macro 4H EMA: {_macro_label} — sinyal {direction} melawan macro trend (-{_m_penalty}pt)"
                )
                log.info(f"  {symbol}: macro contra {_macro_label} -{_m_penalty}pt → score={master['master_score']}")
                if master["master_score"] < MASTER_SCORE_CONFIRMED:
                    log.info(f"  {symbol}: dropped below threshold after macro filter, skip")
                    return None
    except Exception as _me:
        log.debug(f"Macro filter error {symbol}: {_me}")

    # 3e. MARKET CONTEXT FILTER (Fear&Greed + BTC Regime + Breadth + Vol + Dominance)
    if MARKET_CONTEXT_MODULE:
        try:
            ctx = get_market_context()
            adj_score, ctx_blocked, ctx_reasons = apply_market_context_to_score(
                direction, master["master_score"], ctx
            )
            if ctx_blocked:
                log.info(f"  {symbol}: MARKET CONTEXT HARD BLOCK ({direction})")
                return None
            if adj_score != master["master_score"]:
                penalty = master["master_score"] - adj_score
                master["master_score"] = adj_score
                master["conflict_reasons"].append(
                    f"🌐 Market context penalty -{penalty}pt "
                    f"(bias={ctx.get('overall_bias','?')})"
                )
                log.info(f"  {symbol}: market ctx penalty -{penalty}pt → score={master['master_score']}")
                if master["master_score"] < MASTER_SCORE_CONFIRMED:
                    log.info(f"  {symbol}: dropped below threshold after market context, skip")
                    return None
            # Store ctx in master for formatter
            master["market_context"] = ctx
        except Exception as _mce:
            log.debug(f"Market context error {symbol}: {_mce}")

    # 3f. NEWS GATE (high-impact event filter)
    if NEWS_GATE_MODULE:
        try:
            news_pen, news_blocked, news_reasons = get_news_gate(symbol, direction)
            if news_blocked:
                log.info(f"  {symbol}: NEWS GATE HARD BLOCK — {news_reasons}")
                return None
            if news_pen > 0:
                master["master_score"] = max(0, master["master_score"] - news_pen)
                master["conflict_reasons"].append(
                    f"📰 News gate penalty -{news_pen}pt"
                )
                master["conflict_reasons"].extend(news_reasons[:2])
                log.info(f"  {symbol}: news gate penalty -{news_pen}pt → score={master['master_score']}")
                if master["master_score"] < MASTER_SCORE_CONFIRMED:
                    log.info(f"  {symbol}: dropped below threshold after news gate, skip")
                    return None
        except Exception as _nge:
            log.debug(f"News gate error {symbol}: {_nge}")

    # 3f-2. SOCIAL GATE (Reddit + HackerNews sentiment — cached, no API key)
    if SOCIAL_GATE_MODULE:
        try:
            soc_adj, soc_blocked, soc_reasons = get_social_gate(symbol, direction)
            if soc_blocked:
                log.info(f"  {symbol}: SOCIAL GATE HARD BLOCK — {soc_reasons}")
                return None
            if soc_adj != 0:
                old_score = master["master_score"]
                master["master_score"] = max(0, min(100, old_score + soc_adj))
                if soc_adj > 0:
                    master["confluence_reasons"] = master.get("confluence_reasons", [])
                    master["confluence_reasons"].extend(soc_reasons[:2])
                else:
                    master["conflict_reasons"].extend(soc_reasons[:2])
                log.info(
                    f"  {symbol}: social gate adj {soc_adj:+d}pt "
                    f"→ score {old_score}→{master['master_score']}"
                )
                if master["master_score"] < MASTER_SCORE_CONFIRMED and soc_adj < 0:
                    log.info(f"  {symbol}: dropped below threshold after social gate, skip")
                    return None
        except Exception as _sge:
            log.debug(f"Social gate error {symbol}: {_sge}")

    # 3b. AUTO MARKET CONTEXT VALIDATION (7-layer check)
    val_result = None
    if AUTO_VALIDATOR_ENABLED:
        try:
            # Fetch BTC 4H context untuk validator
            btc_tf4h_ctx = None
            try:
                from crypto_screening_bot_v13 import analyze_timeframe
                btc_tf4h_ctx = analyze_timeframe("BTCUSDT", "4h")
            except Exception:
                pass

            val_result = run_auto_validation(
                symbol=symbol, direction=direction, price=price,
                tf_4h=tf_4h, tf_1h=tf_1h, tf_15m=tf_15m,
                oi_data=oi_data, btc_tf4h=btc_tf4h_ctx,
            )

            gate = val_result.get("gate", "PASS")
            if gate == "HARD_BLOCK":
                hard_reasons = []
                for lid in val_result.get("hard_blocked", []):
                    lr = val_result.get("layers", {}).get(lid, {})
                    if lr.get("notes"):
                        hard_reasons.append(lr["notes"][0])
                log.info(f"  {symbol}: AUTO-VALIDATION HARD_BLOCK — {hard_reasons}")
                return None

            # Adjust master_score berdasarkan validation
            adj = val_result.get("adjustment", 0)
            if adj != 0:
                master["master_score"] = max(0, min(100, master["master_score"] + adj))
                log.info(f"  {symbol}: validation adj={adj:+d} → new master_score={master['master_score']}")

            # Re-check threshold after adjustment
            if master["master_score"] < MASTER_SCORE_CONFIRMED:
                log.info(f"  {symbol}: score dropped after validation adj, skip")
                return None

        except Exception as e:
            log.warning(f"Auto validation error for {symbol}: {e}")

    # 4. Quick backtest validation (background — non-blocking untuk flow)
    bt_result = _quick_backtest_validate(symbol, direction)
    master["bt_validated"]     = bt_result["valid"]
    master["bt_profit_factor"] = bt_result.get("profit_factor", 0)
    master["bt_win_rate"]      = bt_result.get("win_rate", 0)
    master["bt_reason"]        = bt_result.get("reason", "")

    if not bt_result["valid"]:
        log.info(f"  {symbol}: BT validation FAILED — {bt_result['reason']}")
        # Cek apakah ada cached signal accuracy yang bisa jadi override
        # (run /signalbt untuk update cache)
        cached_acc = _load_signal_bt_cache(symbol)
        if cached_acc and cached_acc.get("accuracy_4h", 0) >= 60 and bt_result.get("profit_factor", 0) >= 0.85:
            log.info(f"  {symbol}: Trade BT borderline tapi signal accuracy {cached_acc['accuracy_4h']:.0f}% >= 60% — allow")
            master["bt_validated"]     = True
            master["bt_profit_factor"] = bt_result.get("profit_factor", 0)
            master["bt_win_rate"]      = bt_result.get("win_rate", 0)
            master["bt_reason"]        = bt_result.get("reason", "") + f" | Signal acc {cached_acc['accuracy_4h']:.0f}%@4H"
        else:
            return None

    # 4b. Feedback-derived rules check
    try:
        from feedback_engine import check_feedback_rules
        fb_check = check_feedback_rules(symbol, direction, tf_4h, oi_data)
        if fb_check.get("blocked"):
            for r in fb_check.get("reasons", []):
                log.info(f"  {symbol}: BLOCKED by feedback rule — {r}")
            return None
        if fb_check.get("penalty", 0) > 0:
            master["master_score"] = max(0, master["master_score"] - fb_check["penalty"])
            master["reasons"].extend(fb_check.get("reasons", []))
            if master["master_score"] < MASTER_SCORE_CONFIRMED:
                log.info(f"  {symbol}: dropped below threshold after feedback penalty, skip")
                return None
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"Feedback check error: {e}")
    try:
        from crypto_screening_bot_v13 import calculate_trade_plan
        atr_1h = tf_1h.get("atr", 0)
        bt_dir = "PUMP" if direction == "LONG" else "DUMP"
        trade  = calculate_trade_plan(price, bt_dir, atr_1h, tf_4h, tf_1h, tf_15m)
    except Exception as e:
        log.warning(f"calculate_trade_plan error: {e}")
        # Fallback trade plan
        if direction == "LONG":
            trade = {
                "direction": "LONG", "entry": price,
                "tp1": round(price * 1.04, 8), "tp2": round(price * 1.07, 8),
                "sl":  round(price * 0.975, 8),
                "tp1_r": 2.0, "tp2_r": 3.5,
                "tp1_basis": "4% target", "tp2_basis": "7% target",
                "entry_type": "MARKET", "rr": 2.0,
            }
        else:
            trade = {
                "direction": "SHORT", "entry": price,
                "tp1": round(price * 0.96, 8), "tp2": round(price * 0.93, 8),
                "sl":  round(price * 1.025, 8),
                "tp1_r": 2.0, "tp2_r": 3.5,
                "tp1_basis": "-4% target", "tp2_basis": "-7% target",
                "entry_type": "MARKET", "rr": 2.0,
            }

    # 5b. DeepSeek strategic review — adjust entry/TP/SL + news context
    ai_review = None
    if DEEPSEEK_MODULE:
        try:
            import os as _os
            _ds_key = _os.getenv("DEEPSEEK_API_KEY", "")
            if _ds_key:
                _news_ctx = None
                if NEWS_GATE_MODULE:
                    try:
                        _news_ctx = get_structured_news_for_ai(symbol)
                    except Exception:
                        pass
                # Lessons dari sinyal lalu (closed-loop self-learning)
                _learn_ctx = None
                try:
                    from learning_engine import build_ai_context_block
                    _learn_ctx = build_ai_context_block("SCREENER") or None
                except Exception:
                    _learn_ctx = None
                ai_review = deepseek_signal_review(
                    symbol       = symbol,
                    direction    = direction,
                    trade        = trade,
                    master_score = master["master_score"],
                    reasons      = master.get("reasons", []),
                    oi_data      = oi_data,
                    tf_4h        = tf_4h, tf_1h = tf_1h, tf_15m = tf_15m,
                    news_context = _news_ctx,
                    signal_type  = "CONFIRMED",
                    learning_context = _learn_ctx,
                )
                if ai_review:
                    # SKIP verdict → batalkan sinyal
                    if ai_review.get("ai_verdict") == "SKIP":
                        log.info(f"  {symbol}: DeepSeek SKIP — AI tidak konfirmasi sinyal confirmed")
                        return None
                    # Apply price adjustments
                    if ai_review.get("was_adjusted"):
                        trade = dict(trade)
                        trade["entry"] = ai_review["entry"]
                        trade["tp1"]   = ai_review["tp1"]
                        trade["tp2"]   = ai_review["tp2"]
                        trade["sl"]    = ai_review["sl"]
                        log.info(
                            f"  {symbol}: DeepSeek adjusted prices "
                            f"entry={ai_review['entry']:.4f} tp1={ai_review['tp1']:.4f}"
                        )
                    # Apply score adjustment
                    if ai_review.get("score_adj", 0) != 0:
                        master["master_score"] = max(
                            0, min(100, master["master_score"] + ai_review["score_adj"])
                        )
                    # Store insight in master for formatter
                    if ai_review.get("insight"):
                        master["ai_insight"]  = ai_review["insight"]
                        master["ai_verdict"]  = ai_review.get("ai_verdict", "CONFIRM")
                        master["ai_adjusted"] = ai_review.get("was_adjusted", False)
        except Exception as _ds_e:
            log.warning(f"DeepSeek review error {symbol}: {_ds_e}")

    # Guard: pastikan TP/SL di sisi benar (terutama setelah override AI) sebelum
    # sinyal dibangun/dikirim/di-track — cegah sinyal malformed (TP sisi salah).
    try:
        from crypto_screening_bot_v13 import _sanitize_trade_levels
        trade = _sanitize_trade_levels(trade, direction)
    except Exception as _san_e:
        log.debug(f"sanitize trade levels error: {_san_e}")

    # 6. Set cooldown
    _set_signal_cooldown(symbol)

    # 7. Build final signal
    signal = {
        **master,
        "price":        price,
        "trade":        trade,
        "oi_data":      oi_data,
        "val_result":   val_result,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # 8. Persist ke history
    _save_confirmed_signal(signal)

    log.info(f"🚀 CONFIRMED SIGNAL: {symbol} {direction} "
             f"score={master_score} BT-PF={bt_result.get('profit_factor', 0):.2f}")
    return signal


SIGNAL_BT_CACHE_FILE = "signal_bt_cache.json"


def _load_signal_bt_cache(symbol: str) -> Optional[dict]:
    """Load cached signal backtest accuracy for a symbol (saved by /signalbt)."""
    try:
        if not os.path.exists(SIGNAL_BT_CACHE_FILE):
            return None
        with open(SIGNAL_BT_CACHE_FILE) as f:
            cache = json.load(f)
        entry = cache.get(symbol)
        if not entry:
            return None
        # Cache expires after 48h
        cached_at = datetime.fromisoformat(entry.get("cached_at", "2000-01-01T00:00:00+00:00"))
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_h > 48:
            return None
        return entry
    except Exception:
        return None


def save_signal_bt_cache(symbol: str, stats: dict):
    """Save signal backtest result to cache (called from backtest_engine after /signalbt)."""
    try:
        cache = {}
        if os.path.exists(SIGNAL_BT_CACHE_FILE):
            with open(SIGNAL_BT_CACHE_FILE) as f:
                cache = json.load(f)
        cache[symbol] = {
            "accuracy_4h":  stats.get("accuracy_4h", 0),
            "accuracy_24h": stats.get("accuracy_24h", 0),
            "signals_fired": stats.get("signals_fired", 0),
            "avg_score":    stats.get("avg_score", 0),
            "cached_at":    datetime.now(timezone.utc).isoformat(),
        }
        tmp = SIGNAL_BT_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, SIGNAL_BT_CACHE_FILE)   # atomic
    except Exception as e:
        log.debug(f"Signal BT cache save error: {e}")


def _save_confirmed_signal(signal: dict):
    try:
        history = []
        if os.path.exists(CONFIRMED_SIGNAL_FILE):
            try:
                with open(CONFIRMED_SIGNAL_FILE) as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    raise ValueError("history bukan list")
            except Exception as e:
                # ANTI DEATH-SPIRAL: kalau file corrupt, JANGAN gagal & berhenti
                # nyimpen selamanya. Backup file rusak lalu mulai dari kosong,
                # supaya recording sinyal tetap jalan.
                bak = f"{CONFIRMED_SIGNAL_FILE}.corrupt-{int(time.time())}"
                try:
                    os.replace(CONFIRMED_SIGNAL_FILE, bak)
                    log.warning(f"⚠️ {CONFIRMED_SIGNAL_FILE} corrupt ({e}) — di-backup ke {bak}, mulai history baru")
                except Exception:
                    log.warning(f"⚠️ {CONFIRMED_SIGNAL_FILE} corrupt ({e}) — reset history")
                history = []
        # Hapus data besar sebelum save
        to_save = {k: v for k, v in signal.items() if k not in ("tf_4h", "tf_1h", "tf_15m")}
        history.append(to_save)
        if len(history) > 200:
            history = history[-200:]
        tmp = CONFIRMED_SIGNAL_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp, CONFIRMED_SIGNAL_FILE)   # atomic
    except Exception as e:
        log.warning(f"Save confirmed signal error: {e}")


# ─────────────────────────────────────────────
# TELEGRAM MESSAGE FORMATTER
# ─────────────────────────────────────────────

def format_confirmed_signal_message(signal: dict) -> str:
    """
    Format pesan Telegram untuk confirmed signal.
    Info-dense, actionable, langsung ke poin.
    """
    symbol   = signal["symbol"].replace("USDT", "")
    price    = signal["price"]
    direc    = signal["direction"]
    score    = signal["master_score"]
    conf     = signal["confidence"]
    trade    = signal["trade"]
    oi       = signal.get("oi_data", {})
    bt_pf    = signal.get("bt_profit_factor", 0)
    bt_wr    = signal.get("bt_win_rate", 0)
    bt_strat = signal.get("bt_reason", "")
    comp     = signal.get("component_scores", {})
    reasons  = signal.get("reasons", [])
    conflicts = signal.get("conflict_reasons", [])
    _wib      = timezone(timedelta(hours=7))
    ts        = datetime.now(_wib).strftime("%d %b %Y %H:%M WIB")

    dir_emoji  = "🟢" if direc == "LONG"  else "🔴"
    conf_emoji = "🔥" if conf == "HIGH"   else "✅" if conf == "MEDIUM" else "🟡"
    dir_label  = "LONG  ▲ MARKET ENTRY" if direc == "LONG" else "SHORT ▼ MARKET ENTRY"

    # Trade plan
    entry  = trade.get("entry", price)
    tp1    = trade.get("tp1")
    tp2    = trade.get("tp2")
    sl     = trade.get("sl")
    tp1_r  = trade.get("tp1_r", 0)
    tp2_r  = trade.get("tp2_r", 0)
    tp1_b  = trade.get("tp1_basis", "")
    tp2_b  = trade.get("tp2_basis", "")
    rr     = trade.get("rr", tp1_r)

    def _f(v):
        if v is None: return "N/A"
        if v >= 1000:    return f"${v:,.2f}"
        elif v >= 1:     return f"${v:.4f}"
        else:            return f"${v:.6f}"

    def _pct(entry, target):
        if not entry or not target: return ""
        return f"({abs(target-entry)/entry*100:.1f}%)"

    # Component summary
    comp_lines = []
    icons = {"confluence":"📐","prepump":"🎯","predump":"💀","scalp":"⚡","swing":"📈"}
    for k, c in comp.items():
        d = c.get("direction","NONE")
        s = c.get("score", 0)
        w = c.get("weight", 0)
        if d in ("NONE",): continue
        agree = "✅" if (direc=="LONG" and "LONG" in d) or (direc=="SHORT" and "SHORT" in d) else "⚠️"
        weak  = " (weak)" if "WEAK" in d else ""
        comp_lines.append(f"  {agree} {icons.get(k,'')} {k.upper()}: {s}/100 → +{w:.0f}pt{weak}")

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{conf_emoji} *CONFIRMED ENTRY SIGNAL*",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💎 *{symbol}*   {dir_emoji} *{dir_label}*",
        f"💰 Harga   : {_f(price)}",
        f"🎯 Master Score : *{score}/100* — Confidence: *{conf}*",
        "",
        "─────── TRADE PLAN ───────",
        f"{'🟢' if direc == 'LONG' else '🔴'} Entry   : *{_f(entry)}*  ← MARKET",
        f"🟡 TP1    : *{_f(tp1)}*  {_pct(entry,tp1)}  ({tp1_r}R) ← {tp1_b} | close 50%",
        f"🟢 TP2    : *{_f(tp2)}*  {_pct(entry,tp2)}  ({tp2_r}R) ← {tp2_b} | runner",
        f"🔴 SL     : *{_f(sl)}*  {_pct(entry,sl)}",
        f"📐 R:R    : *{rr:.1f}:1*  {'✅' if rr >= 2.0 else '⚠️ < 2R'}",
        "",
        "─────── SIGNAL BREAKDOWN ───────",
    ]
    lines.extend(comp_lines)

    # OI/Funding
    lines += ["", "─────── MARKET CONTEXT ───────"]
    fr = oi.get("funding_rate")
    ls = oi.get("ls_ratio")
    oi_c = oi.get("oi_change_pct")
    if fr is not None:
        fr_e = "🔥" if fr < -0.01 else "⚠️" if fr > 0.01 else "  "
        lines.append(f"{fr_e} Funding Rate  : {fr:+.3f}%")
    if ls is not None:
        lines.append(f"⚖️ L/S Ratio    : {ls:.2f} ({oi.get('ls_bias','?')})")
    if oi_c is not None:
        oi_e = "📈" if oi_c > 0 else "📉"
        lines.append(f"{oi_e} OI Change     : {oi_c:+.1f}%")

    # Market context block (Fear&Greed + BTC Regime + Breadth + Vol + Dominance)
    ctx = signal.get("market_context")
    if ctx and MARKET_CONTEXT_MODULE:
        try:
            lines.append("")
            lines.append(format_market_context_block(ctx, compact=False))
        except Exception:
            pass

    # Top reasons
    lines += ["", "─────── ALASAN UTAMA ───────"]
    for r in reasons[:5]:
        lines.append(f"  {r}")

    # Conflicts
    if conflicts:
        lines += ["", "─────── ⚠️ KONFLIK SINYAL ───────"]
        for c in conflicts:
            lines.append(f"  {c}")

    # Coinbase Premium
    try:
        from coinbase_premium import get_premium_context_string, detect_premium_divergence
        premium_str = get_premium_context_string()
        if premium_str and "N/A" not in premium_str:
            lines += ["", "─────── COINBASE PREMIUM ───────", f"  {premium_str}"]
            # Check divergence
            price_1h_change = 0
            if signal.get("tf_1h") and signal["tf_1h"].get("candles"):
                c = signal["tf_1h"]["candles"]
                if len(c) >= 2:
                    price_1h_change = (c[-1]["close"] - c[-2]["close"]) / c[-2]["close"] * 100
            div_warn = detect_premium_divergence(price_1h_change)
            if div_warn:
                lines.append(f"  {div_warn}")
    except ImportError:
        pass
    except Exception:
        pass

    # Backtest validation
    lines += [
        "",
        "─────── BACKTEST VALIDASI ───────",
        f"{'✅' if bt_pf >= 1.0 else '⚠️'} {bt_strat}",
        f"   PF: {bt_pf:.2f} | WR: {bt_wr:.0f}%",
    ]

    # Risk disclaimer + position sizing hint
    # Auto-validation summary
    val = signal.get("val_result")
    if val and AUTO_VALIDATOR_ENABLED:
        lines += ["", format_validation_summary(val)]

    # Signal accuracy cache (from /signalbt)
    sym_key = signal.get("symbol", "")
    sig_cache = _load_signal_bt_cache(sym_key)
    if sig_cache and sig_cache.get("signals_fired", 0) >= 3:
        acc4  = sig_cache.get("accuracy_4h",  0)
        acc24 = sig_cache.get("accuracy_24h", 0)
        acc_emoji = "✅" if acc4 >= 60 else "🟡"
        lines += [
            "",
            "─────── SIGNAL ACCURACY ───────",
            f"{acc_emoji} Historical accuracy: {acc4:.0f}%@4H | {acc24:.0f}%@24H",
            f"   (dari {sig_cache['signals_fired']} sinyal serupa, avg score {sig_cache.get('avg_score', 0):.0f})",
        ]
    else:
        lines += [
            "",
            f"💡 _Jalankan `/signalbt {sym_key.replace('USDT', '')} 30` untuk lihat historical signal accuracy_",
        ]

    # v15: DeepSeek AI insight
    ai_insight = signal.get("ai_insight", "")
    if ai_insight:
        ai_verdict  = signal.get("ai_verdict", "CONFIRM")
        ai_adjusted = signal.get("ai_adjusted", False)
        _v_emoji    = {"CONFIRM": "✅", "CAUTION": "⚠️"}.get(ai_verdict, "🤖")
        lines += [
            "",
            "─────── 🤖 DeepSeek AI ───────",
            f"{_v_emoji} <b>{ai_verdict}</b>",
        ]
        for _il in ai_insight.split("\n"):
            if _il.strip():
                lines.append(f"  {_il.strip()}")
        if ai_adjusted:
            lines.append("  🔧 <i>Level harga disesuaikan oleh AI</i>")

    lines += [
        "",
        "─────── MANAJEMEN RISIKO ───────",
        f"💡 SL wajib! Entry market, TP partial di TP1 (50%)",
        f"   Gunakan `/risk` untuk hitung ukuran posisi",
        "",
        f"⚠️ _Sinyal ini divalidasi backtest {BT_DAYS}h + 7-layer auto-check + DeepSeek AI. DYOR._",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────
# INTEGRATION: dipanggil dari run_scan()
# ─────────────────────────────────────────────

def run_confirmed_signal_scan(
    coins_data: list,
    send_telegram_fn,
    tracker_on_signal_sent=None,
    register_signal_fn=None,
    personalize_fn=None,
):
    """
    Dipanggil di run_scan() setelah screen_coins().
    Iterasi setiap coin yang lolos screening, coba generate confirmed signal.

    Kalau ada confirmed signal → kirim ke Telegram.
    Kalau tidak ada → tidak kirim apapun.

    Args:
        coins_data: list hasil build dari screen_coins() yang sudah punya tf data
        send_telegram_fn: fungsi send_telegram dari bot
        tracker_on_signal_sent: fungsi on_signal_sent dari signal_tracker (optional)
    """
    confirmed_count = 0

    for coin in coins_data:
        symbol = coin.get("symbol", "")
        if not symbol:
            continue

        try:
            price      = coin.get("price", 0)
            confluence = coin.get("confluence", {})
            prepump    = coin.get("prepump", {})
            predump    = coin.get("predump", {})
            scalp      = coin.get("scalp", {})
            swing      = coin.get("swing", {})
            oi_data    = coin.get("oi", {})
            tf_4h      = coin.get("tf_4h", {})
            tf_1h      = coin.get("tf_1h", {})
            tf_15m     = coin.get("tf_15m", {})

            if not price or not confluence:
                continue

            signal = generate_confirmed_signal(
                symbol=symbol, price=price,
                confluence=confluence,
                prepump=prepump, predump=predump,
                scalp=scalp, swing=swing,
                oi_data=oi_data,
                tf_4h=tf_4h, tf_1h=tf_1h, tf_15m=tf_15m,
            )

            if signal is None:
                continue

            # Kirim ke Telegram
            msg = format_confirmed_signal_message(signal)

            # v15: tempel penyesuaian gaya trading user (kalau ada)
            if personalize_fn:
                try:
                    extra = personalize_fn(signal)
                    if extra:
                        msg += "\n\n" + extra
                except Exception as e:
                    log.debug(f"personalize_fn error: {e}")

            msg_id = send_telegram_fn(msg)
            confirmed_count += 1

            # v15: daftarkan message_id → signal supaya bisa didiskusikan via reply
            if register_signal_fn:
                try:
                    register_signal_fn(msg_id, signal)
                except Exception as e:
                    log.debug(f"register_signal_fn error: {e}")

            # Track ke signal_tracker kalau tersedia
            if tracker_on_signal_sent:
                trade = signal.get("trade", {})
                try:
                    _dir    = signal["direction"]
                    _tp_val = float(trade.get("tp1", 0) or 0)
                    _sl_val = float(trade.get("sl", 0) or 0)
                    _entry  = float(trade.get("entry") or price)
                    _sane   = (_dir == "LONG"  and _tp_val > _entry) or \
                              (_dir == "SHORT" and _tp_val < _entry)
                    if _sane and _tp_val and _sl_val:
                        tracker_on_signal_sent(
                            symbol          = symbol,
                            signal_type     = "CONFIRMED",
                            direction       = _dir,
                            entry_price     = _entry,
                            tp              = _tp_val,
                            sl              = _sl_val,
                            score           = signal["master_score"],
                            confluence_level= signal["confidence"],
                            reasons         = signal.get("reasons", [])[:3],
                        )
                    else:
                        log.warning(f"⚠️ CONFIRMED sanity fail {symbol}: dir={_dir} entry={_entry} tp={_tp_val}")
                except Exception as e:
                    log.debug(f"Tracker record error: {e}")

            # Max 1 confirmed signal per scan — quality over quantity
            if confirmed_count >= 1:
                break

        except Exception as e:
            log.warning(f"Confirmed signal error {symbol}: {e}", exc_info=True)

    if confirmed_count == 0:
        log.info("  No confirmed signals this scan")
    else:
        log.info(f"  🚀 {confirmed_count} confirmed signal(s) sent")

    # Auto-evolve sensitivity tiap 20 scan cycles (roughly)
    if AUTO_VALIDATOR_ENABLED:
        try:
            import random
            if random.random() < 0.05:  # ~5% chance per scan = tiap ~20 scan
                changes = evolve_sensitivity()
                if changes:
                    log.info(f"🧬 Auto-validator sensitivity evolved: {changes}")
        except Exception:
            pass

    return confirmed_count
