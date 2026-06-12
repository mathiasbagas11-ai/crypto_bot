#!/usr/bin/env python3
"""
AUTO MARKET CONTEXT VALIDATOR v1.0
=====================================
Validasi otomatis multi-layer sebelum sinyal dikirim.
Tidak butuh feedback manual — bot sendiri yang cek semua kondisi.

Layer validasi (dijalankan urut, satu fail = signal ditahan):

  LAYER 1 — HTF Trend Alignment
    Cek Weekly/Daily candle trend searah dengan signal.
    LONG signal harus: Daily bullish ATAU setidaknya tidak strongly bearish.
    SHORT signal harus: Daily bearish ATAU setidaknya tidak strongly bullish.
    → Fetch 1W dan 1D candles dari Binance, analyze structure.

  LAYER 2 — BTC Macro Alignment
    Cek apakah BTC sedang mendukung atau melawan signal.
    LONG: BTC 4H trend harus tidak bearish, RSI tidak < 35.
    SHORT: BTC 4H trend harus tidak strongly bullish.
    Bonus kalau BTC structure confirming (BoS up untuk LONG, dll).

  LAYER 3 — Ecosystem Season Fit
    Kalau coin ecosystemnya lagi bearish season → LONG signal diblock.
    Kalau lagi bullish season → SHORT signal dapat penalty.
    Data dari ecosystem_detector.py (cached, tidak re-fetch).

  LAYER 4 — OI/Funding Sanity Check
    Cek divergence antara OI movement dan price movement.
    OI naik tapi price tidak naik dalam 4H = bearish divergence → SKIP LONG.
    Funding extreme (+0.05% atau -0.05%) = crowded trade → penalty.

  LAYER 5 — Multi-TF Structure Depth
    Cek berapa TF yang confirm signal:
    Ideal: 4H + 1H + 15M semua searah (deep confluence).
    Minimal: setidaknya 2 dari 3 TF confirm.
    Kalau hanya 1 TF confirm = weak setup, skip.

  LAYER 6 — Liquidity Context (SMC)
    Apakah entry price ada di zona yang logis?
    LONG: entry dekat OB bullish atau FVG bullish, bukan di resistance HTF.
    SHORT: entry dekat OB bearish atau resistance HTF.
    Entry di "middle of nowhere" = skip.

  LAYER 7 — Volatility Regime
    Cek ATR relatif terhadap harga (ATR%).
    Terlalu volatile (ATR% > 5%) = risiko slippage tinggi, skip.
    Terlalu flat (ATR% < 0.3%) = tidak ada momentum, skip.

Auto-learning:
  Setiap validasi hasil dicatat (pass/fail per layer).
  Kalau signal yang lolos semua layer ternyata gagal (via signal_tracker) →
  layer mana yang "missed" diboost sensitivity-nya.
  Self-calibrating over time.

Output per signal:
  validation_score: 0-100
  passed_layers: [list]
  failed_layers: [list]
  gate_decision: PASS | SOFT_BLOCK | HARD_BLOCK
  adjustments: score modifier (positif = boost, negatif = penalty)
"""

import time
import logging
import requests
import numpy as np
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("auto_validator")

BINANCE_BASE = "https://api.binance.com/api/v3"

# ── Gate thresholds ─────────────────────────
VALIDATION_PASS_SCORE      = 60   # >= 60 → PASS
VALIDATION_SOFT_BLOCK_MIN  = 40   # 40-59 → SOFT_BLOCK (score penalty ke confirmed_signal)
# < 40 → HARD_BLOCK (signal tidak dikirim)

# ── Layer weights (total 100) ───────────────
LAYER_WEIGHTS = {
    "htf_trend":      20,   # Daily/Weekly alignment
    "btc_alignment":  18,   # BTC macro support
    "cb_premium":     16,   # Coinbase Premium (institutional bias)
    "ecosystem":      12,   # Season fit
    "oi_sanity":      12,   # OI/Funding divergence
    "mtf_depth":      10,   # Multi-TF confluence depth
    "liquidity":      10,   # SMC liquidity context + volume at zone (dinaikkan)
    "volatility":      2,   # ATR regime
}

# ── Sensitivity config (auto-evolves) ───────
import json, os
SENSITIVITY_FILE = "validator_sensitivity.json"

def _load_sensitivity() -> dict:
    defaults = {
        "btc_bearish_long_block":    True,
        "btc_rsi_min_long":          35,
        "btc_rsi_max_short":         65,
        "daily_trend_required":      True,
        "oi_divergence_sensitivity": 0.5,  # multiplier for OI checks
        "funding_block_threshold":   0.05, # % absolute
        "min_tf_confirm":            2,    # minimum TFs that must agree
        "atr_min_pct":               0.3,
        "atr_max_pct":               5.0,
        "evolve_count":              0,
    }
    try:
        if os.path.exists(SENSITIVITY_FILE):
            with open(SENSITIVITY_FILE) as f:
                saved = json.load(f)
                defaults.update(saved)
    except Exception:
        pass
    return defaults

def _save_sensitivity(data: dict):
    try:
        with open(SENSITIVITY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"Save sensitivity error: {e}")


# ─────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────

def _fetch_candles(symbol: str, interval: str, limit: int = 50) -> list:
    try:
        r = requests.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        if r.status_code == 200:
            return [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                     "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
                    for c in r.json()]
    except Exception as e:
        log.debug(f"Fetch {symbol} {interval} error: {e}")
    return []


def _ema(values: list, period: int) -> list:
    result = [None] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    result[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        result[i] = ema
    return result


def _rsi(candles: list, period: int = 14) -> float:
    # Wilder smoothing — konsisten dengan calculate_rsi() di screener &
    # detect_rsi_divergence() (sebelumnya SMA atas period delta terakhir).
    if len(candles) < period + 1:
        return 50.0
    closes = [c["close"] for c in candles]
    gains  = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[:period]) / period     # seed = SMA pertama
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def _trend_from_candles(candles: list) -> str:
    """Simple trend detection: EMA20 vs EMA50 + recent price action."""
    if len(candles) < 50:
        return "UNKNOWN"
    closes = [c["close"] for c in candles]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    e20 = next((v for v in reversed(ema20) if v), None)
    e50 = next((v for v in reversed(ema50) if v), None)
    price = closes[-1]
    if e20 and e50:
        if e20 > e50 and price > e20:
            return "BULLISH"
        elif e20 < e50 and price < e20:
            return "BEARISH"
        elif e20 > e50:
            return "MILD_BULLISH"
        else:
            return "MILD_BEARISH"
    return "UNKNOWN"


def _choch_bos(candles: list) -> dict:
    """Detect CHoCH/BoS in last 20 candles."""
    if len(candles) < 20:
        return {"choch": False, "bos": False, "direction": "NONE"}
    c = candles[-20:]
    highs  = [x["high"]  for x in c]
    lows   = [x["low"]   for x in c]
    closes = [x["close"] for x in c]
    prev_hh = max(highs[:-5])
    prev_ll = min(lows[:-5])
    last_close = closes[-1]
    bos_up   = last_close > prev_hh
    bos_down = last_close < prev_ll
    # CHoCH: price crosses prev structure in opposite direction
    choch = (bos_up and closes[-5] < prev_ll) or (bos_down and closes[-5] > prev_hh)
    direction = "BULLISH" if bos_up else "BEARISH" if bos_down else "NONE"
    return {"choch": choch, "bos": bos_up or bos_down, "direction": direction}


# ─────────────────────────────────────────────
# LAYER 1: HTF TREND ALIGNMENT
# ─────────────────────────────────────────────

def _check_htf_trend(symbol: str, direction: str, sens: dict) -> dict:
    """
    Fetch 1D dan 1W candles, cek apakah trend searah dengan signal.
    LONG needs: Daily not strongly bearish
    SHORT needs: Daily not strongly bullish
    """
    score    = 0
    notes    = []
    blocking = False

    daily = _fetch_candles(symbol, "1d", limit=60)
    weekly = _fetch_candles(symbol, "1w", limit=20)

    daily_trend  = _trend_from_candles(daily)  if daily  else "UNKNOWN"
    weekly_trend = _trend_from_candles(weekly) if weekly else "UNKNOWN"
    daily_rsi    = _rsi(daily)  if daily  else 50
    weekly_rsi   = _rsi(weekly) if weekly else 50
    daily_struct = _choch_bos(daily) if daily else {}

    if direction == "LONG":
        if daily_trend == "BULLISH":
            score += 100
            notes.append(f"✅ Daily BULLISH (EMA20>EMA50, price above) — HTF tailwind")
        elif daily_trend == "MILD_BULLISH":
            score += 70
            notes.append(f"🟡 Daily MILD_BULLISH — weak tailwind")
        elif daily_trend == "UNKNOWN":
            score += 50
            notes.append(f"⚪ Daily trend unclear")
        elif daily_trend == "MILD_BEARISH":
            score += 30
            notes.append(f"⚠️ Daily MILD_BEARISH — counter-trend LONG, risky")
        else:  # BEARISH
            score += 0
            blocking = sens.get("daily_trend_required", True)
            notes.append(f"❌ Daily BEARISH — LONG is counter-trend (Daily RSI={daily_rsi:.0f})")

        if weekly_trend in ("BULLISH", "MILD_BULLISH"):
            score = min(100, score + 15)
            notes.append(f"✅ Weekly {weekly_trend} supports LONG")
        elif weekly_trend in ("BEARISH", "MILD_BEARISH"):
            score = max(0, score - 20)
            notes.append(f"⚠️ Weekly {weekly_trend} — macro headwind for LONG")

        if daily_struct.get("direction") == "BULLISH" and daily_struct.get("bos"):
            score = min(100, score + 10)
            notes.append(f"✅ Daily BoS BULLISH — structure break up confirmed")

    else:  # SHORT
        if daily_trend == "BEARISH":
            score += 100
            notes.append(f"✅ Daily BEARISH — HTF tailwind for SHORT")
        elif daily_trend == "MILD_BEARISH":
            score += 70
            notes.append(f"🟡 Daily MILD_BEARISH — weak tailwind")
        elif daily_trend == "UNKNOWN":
            score += 50
        elif daily_trend == "MILD_BULLISH":
            score += 30
            notes.append(f"⚠️ Daily MILD_BULLISH — counter-trend SHORT, risky")
        else:  # BULLISH
            score += 0
            blocking = sens.get("daily_trend_required", True)
            notes.append(f"❌ Daily BULLISH — SHORT is counter-trend (RSI={daily_rsi:.0f})")

        if weekly_trend in ("BEARISH", "MILD_BEARISH"):
            score = min(100, score + 15)
        elif weekly_trend in ("BULLISH", "MILD_BULLISH"):
            score = max(0, score - 20)
            notes.append(f"⚠️ Weekly {weekly_trend} — macro headwind for SHORT")

    return {
        "layer":      "htf_trend",
        "score":      round(score),
        "blocking":   blocking,
        "notes":      notes,
        "detail":     {"daily_trend": daily_trend, "weekly_trend": weekly_trend,
                       "daily_rsi": daily_rsi, "weekly_rsi": weekly_rsi},
    }


# ─────────────────────────────────────────────
# LAYER 2: BTC MACRO ALIGNMENT
# ─────────────────────────────────────────────

def _check_btc_alignment(direction: str, btc_tf4h: dict, btc_tf1d: dict, sens: dict) -> dict:
    """
    Cek apakah BTC macro mendukung signal direction.
    Pakai data yang sudah di-fetch (btc_tf4h dari run_scan).
    """
    score    = 0
    notes    = []
    blocking = False

    btc_trend_4h = btc_tf4h.get("structure", {}).get("trend", "UNKNOWN") if btc_tf4h else "UNKNOWN"
    btc_rsi_4h   = btc_tf4h.get("rsi", 50) if btc_tf4h else 50
    btc_rsi_1d   = btc_tf1d.get("rsi", 50) if btc_tf1d else 50
    btc_choch    = btc_tf4h.get("structure", {}).get("choch", False) if btc_tf4h else False
    btc_bos      = btc_tf4h.get("structure", {}).get("bos", False) if btc_tf4h else False

    min_rsi_long  = sens.get("btc_rsi_min_long", 35)
    max_rsi_short = sens.get("btc_rsi_max_short", 65)

    if direction == "LONG":
        if btc_trend_4h == "BULLISH":
            score += 100
            notes.append(f"✅ BTC 4H BULLISH — macro tailwind for LONG")
        elif btc_trend_4h == "TRANSITIONING":
            score += 60
            notes.append(f"🟡 BTC 4H TRANSITIONING — neutral for LONG")
        elif btc_trend_4h in ("BEARISH", "DOWNTREND"):
            score += 0
            blocking = sens.get("btc_bearish_long_block", True)
            notes.append(f"❌ BTC 4H {btc_trend_4h} — LONG akan keseret turun")
        else:
            score += 50

        if btc_rsi_4h < min_rsi_long:
            score = max(0, score - 20)
            notes.append(f"⚠️ BTC RSI4H={btc_rsi_4h:.0f} < {min_rsi_long} — BTC oversold/dropping fast")
        elif btc_rsi_4h > 70:
            score = min(100, score + 10)
            notes.append(f"✅ BTC RSI4H={btc_rsi_4h:.0f} — strong momentum")

        if btc_choch:
            score = max(0, score - 15)
            notes.append(f"⚠️ BTC 4H CHoCH detected — potential trend flip, caution LONG")
        elif btc_bos:
            score = min(100, score + 10)
            notes.append(f"✅ BTC 4H BoS up — structure confirming LONG")

    else:  # SHORT
        if btc_trend_4h in ("BEARISH", "DOWNTREND"):
            score += 100
            notes.append(f"✅ BTC 4H {btc_trend_4h} — macro tailwind for SHORT")
        elif btc_trend_4h == "TRANSITIONING":
            score += 60
            notes.append(f"🟡 BTC 4H TRANSITIONING — neutral for SHORT")
        elif btc_trend_4h == "BULLISH":
            score += 10
            blocking = False   # SHORT during BTC bull = risky but not always blocked
            notes.append(f"⚠️ BTC 4H BULLISH — SHORT is counter-trend, high risk")
        else:
            score += 50

        if btc_rsi_4h > max_rsi_short:
            score = max(0, score - 20)
            notes.append(f"⚠️ BTC RSI4H={btc_rsi_4h:.0f} > {max_rsi_short} — BTC overbought, SHORT riskier")
        elif btc_rsi_4h < 40:
            score = min(100, score + 10)

        if btc_choch:
            score = min(100, score + 15)
            notes.append(f"✅ BTC 4H CHoCH bearish — trend flip signal for SHORT")

    return {
        "layer":    "btc_alignment",
        "score":    round(score),
        "blocking": blocking,
        "notes":    notes,
        "detail":   {"btc_trend_4h": btc_trend_4h, "btc_rsi_4h": btc_rsi_4h,
                     "btc_rsi_1d": btc_rsi_1d, "btc_choch": btc_choch},
    }


# ─────────────────────────────────────────────
# LAYER 3: ECOSYSTEM SEASON FIT
# ─────────────────────────────────────────────

def _check_ecosystem(symbol: str, direction: str) -> dict:
    score = 50   # neutral default
    notes = []
    blocking = False

    try:
        from ecosystem_detector import (
            get_coin_ecosystem, get_ecosystem_boost,
            get_dump_ecosystem_penalty, get_active_seasons,
            get_market_phase, _season_cache, _cache_is_fresh
        )

        if not _cache_is_fresh():
            return {"layer": "ecosystem", "score": 50, "blocking": False,
                    "notes": ["⚪ Season cache stale — skip ecosystem check"], "detail": {}}

        ticker  = symbol.replace("USDT", "").lower()
        eco     = get_coin_ecosystem(ticker)
        phase   = get_market_phase()
        actives = get_active_seasons(top_n=5)

        if not eco:
            return {"layer": "ecosystem", "score": 50, "blocking": False,
                    "notes": ["⚪ Ecosystem tidak dikenal — neutral"], "detail": {}}

        scores = _season_cache.get("scores", {})
        eco_sc = scores.get(eco, 0)

        if direction == "LONG":
            boost = get_ecosystem_boost(ticker)
            if boost >= 3.0:
                score = 100
                notes.append(f"🔥🔥 {eco} DOMINANT season — LONG perfectly aligned")
            elif boost >= 2.0:
                score = 85
                notes.append(f"🔥 {eco} top-3 season aktif — LONG aligned")
            elif boost >= 1.0:
                score = 70
                notes.append(f"📈 {eco} season aktif — LONG supported")
            elif boost >= 0:
                score = 50
                notes.append(f"⚪ {eco} neutral season")
            else:
                score = 15
                blocking = True
                notes.append(f"❌ {eco} bearish season — LONG melawan ecosystem trend (score={eco_sc:+.0f})")

            if phase == "BTC_SEASON" and eco != "BTC":
                score = max(0, score - 20)
                notes.append(f"⚠️ BTC Season aktif — non-BTC altcoin underperform")
            elif phase == "ALTSEASON":
                score = min(100, score + 10)
                notes.append(f"✅ Altseason — semua altcoin dapat tailwind")
            elif phase == "BEAR":
                score = max(0, score - 30)
                blocking = True
                notes.append(f"❌ Bear market phase — LONG risky")

        else:  # SHORT
            dump_bonus = get_dump_ecosystem_penalty(ticker)
            if dump_bonus >= 2.0:
                score = 100
                notes.append(f"📉📉 {eco} strongly bearish — SHORT perfectly aligned")
            elif dump_bonus >= 1.0:
                score = 80
                notes.append(f"📉 {eco} bearish ecosystem — SHORT supported")
            elif dump_bonus >= 0:
                score = 50
            else:  # dump_bonus negative = ecosystem bullish, penalty for short
                score = 20
                notes.append(f"⚠️ {eco} bullish season — SHORT counter-trend")

            if phase == "ALTSEASON":
                score = max(0, score - 20)
                notes.append(f"⚠️ Altseason — SHORT altcoin berisiko, banyak squeeze")
            elif phase == "BEAR":
                score = min(100, score + 20)
                notes.append(f"✅ Bear market — SHORT aligned dengan macro")

    except ImportError:
        return {"layer": "ecosystem", "score": 50, "blocking": False,
                "notes": ["⚪ ecosystem_detector tidak tersedia"], "detail": {}}
    except Exception as e:
        log.debug(f"Ecosystem check error: {e}")
        return {"layer": "ecosystem", "score": 50, "blocking": False, "notes": [], "detail": {}}

    return {"layer": "ecosystem", "score": round(score), "blocking": blocking,
            "notes": notes, "detail": {"eco": eco, "phase": phase}}


# ─────────────────────────────────────────────
# LAYER 4: OI/FUNDING SANITY CHECK
# ─────────────────────────────────────────────

def _check_oi_sanity(direction: str, oi_data: dict, tf_4h: dict, sens: dict) -> dict:
    score    = 70   # default pass
    notes    = []
    blocking = False

    if not oi_data:
        return {"layer": "oi_sanity", "score": 70, "blocking": False,
                "notes": ["⚪ No OI data — neutral"], "detail": {}}

    funding    = oi_data.get("funding_rate", 0) or 0
    oi_chg     = oi_data.get("oi_change_pct", 0) or 0
    ls_bias    = oi_data.get("ls_bias", "BALANCED")
    ls_ratio   = oi_data.get("ls_ratio", 1.0) or 1.0
    fund_block = sens.get("funding_block_threshold", 0.05)
    oi_sens    = sens.get("oi_divergence_sensitivity", 0.5)

    # Price momentum dari 4H (proxy divergence check)
    price_change_4h = 0.0
    if tf_4h and tf_4h.get("candles"):
        candles_4h = tf_4h["candles"]
        if len(candles_4h) >= 4:
            p_now  = candles_4h[-1]["close"]
            p_prev = candles_4h[-4]["close"]
            price_change_4h = (p_now - p_prev) / p_prev * 100 if p_prev > 0 else 0

    if direction == "LONG":
        # OI naik + price naik = healthy long accumulation
        if oi_chg > 3 and price_change_4h > 0.5:
            score = 90
            notes.append(f"✅ OI+{oi_chg:.1f}% + price up → healthy accumulation")
        # OI naik tapi price flat/turun = bearish divergence
        elif oi_chg > 5 and price_change_4h < -0.5:
            score = 20
            blocking = oi_chg * oi_sens > 4
            notes.append(f"❌ OI+{oi_chg:.1f}% tapi price {price_change_4h:+.1f}% → bearish OI divergence")
        # Funding extreme positif = longs crowded → squeeze risk
        if funding > fund_block:
            score = max(0, score - 30)
            notes.append(f"⚠️ Funding={funding:+.3f}% extreme positif — longs crowded, squeeze risk")
        elif funding > 0.02:
            score = max(0, score - 10)
            notes.append(f"⚠️ Funding={funding:+.3f}% elevated")
        # L/S ratio: terlalu banyak longs = potential long squeeze
        if ls_bias == "LONG_HEAVY" and ls_ratio > 1.5:
            score = max(0, score - 15)
            notes.append(f"⚠️ L/S ratio={ls_ratio:.2f} LONG_HEAVY — over-leveraged longs")
        elif ls_bias == "SHORT_HEAVY":
            score = min(100, score + 10)
            notes.append(f"✅ L/S SHORT_HEAVY — short squeeze fuel for LONG")

    else:  # SHORT
        if oi_chg > 3 and price_change_4h < -0.5:
            score = 90
            notes.append(f"✅ OI+{oi_chg:.1f}% + price down → healthy short accumulation")
        elif oi_chg > 5 and price_change_4h > 0.5:
            score = 20
            blocking = oi_chg * oi_sens > 4
            notes.append(f"❌ OI+{oi_chg:.1f}% tapi price {price_change_4h:+.1f}% → bullish OI divergence")
        # Funding extreme negatif = shorts crowded → squeeze risk
        if funding < -fund_block:
            score = max(0, score - 30)
            notes.append(f"⚠️ Funding={funding:+.3f}% extreme negatif — shorts crowded, squeeze risk")
        elif funding < -0.02:
            score = max(0, score - 10)
        if ls_bias == "SHORT_HEAVY" and ls_ratio < 0.6:
            score = max(0, score - 15)
            notes.append(f"⚠️ L/S SHORT_HEAVY — over-leveraged shorts, squeeze risk")
        elif ls_bias == "LONG_HEAVY":
            score = min(100, score + 10)
            notes.append(f"✅ LONG_HEAVY — liquidation fuel for SHORT")

    return {"layer": "oi_sanity", "score": round(score), "blocking": blocking,
            "notes": notes,
            "detail": {"funding": funding, "oi_chg": oi_chg, "ls_bias": ls_bias,
                       "price_change_4h": price_change_4h}}




# ─────────────────────────────────────────────
# LAYER 8 (inserted as cb_premium): COINBASE PREMIUM
# ─────────────────────────────────────────────

def _check_cb_premium(direction: str) -> dict:
    """
    Fetch Coinbase Premium Index dan score untuk direction.
    Institutional bias indicator — strongest for BTC-correlated moves.
    """
    try:
        from coinbase_premium import get_premium_score, fetch_premium
        premium_data = fetch_premium()
        result = get_premium_score(direction, premium_data)
        return {
            "layer":    "cb_premium",
            "score":    result["score"],
            "blocking": result["blocking"],
            "notes":    result["notes"],
            "detail":   {
                "premium_pct": result.get("premium_pct"),
                "signal":      result.get("signal"),
                "momentum":    result.get("momentum"),
                "strength":    result.get("strength"),
            },
        }
    except ImportError:
        return {"layer": "cb_premium", "score": 50, "blocking": False,
                "notes": ["⚪ coinbase_premium.py tidak tersedia — skip"], "detail": {}}
    except Exception as e:
        log.debug(f"CB premium layer error: {e}")
        return {"layer": "cb_premium", "score": 50, "blocking": False,
                "notes": [f"⚪ CB Premium error: {str(e)[:50]}"], "detail": {}}

# ─────────────────────────────────────────────
# LAYER 5: MULTI-TF STRUCTURE DEPTH
# ─────────────────────────────────────────────

def _check_mtf_depth(direction: str, tf_4h: dict, tf_1h: dict, tf_15m: dict, sens: dict) -> dict:
    score    = 0
    notes    = []
    blocking = False
    confirmed_tfs = []
    conflicting   = []

    min_confirm = sens.get("min_tf_confirm", 2)

    def _tf_vote(tf: dict, label: str):
        if not tf or tf.get("error"):
            return
        struct = tf.get("structure", {})
        trend  = struct.get("trend", "UNKNOWN")
        rsi    = tf.get("rsi", 50) or 50
        sweep  = tf.get("sweep", {}) or {}
        ob     = tf.get("order_blocks", {}) or {}
        fvg    = tf.get("fvg", {}) or {}

        bullish_signals = 0
        bearish_signals = 0

        if trend == "BULLISH":               bullish_signals += 2
        elif trend == "MILD_BULLISH":        bullish_signals += 1
        elif trend == "BEARISH":             bearish_signals += 2
        elif trend == "MILD_BEARISH":        bearish_signals += 1

        if sweep.get("swept") and sweep.get("direction") == "UP":   bullish_signals += 1
        if sweep.get("swept") and sweep.get("direction") == "DOWN":  bearish_signals += 1
        if ob.get("bullish_ob"):    bullish_signals += 1
        if ob.get("bearish_ob"):    bearish_signals += 1
        if fvg.get("fvg_type") == "BULLISH": bullish_signals += 1
        if fvg.get("fvg_type") == "BEARISH": bearish_signals += 1
        if rsi > 55:   bullish_signals += 1
        elif rsi < 45: bearish_signals += 1

        if direction == "LONG":
            if bullish_signals >= bearish_signals + 2:
                confirmed_tfs.append(label)
            elif bearish_signals >= bullish_signals + 2:
                conflicting.append(label)
        else:
            if bearish_signals >= bullish_signals + 2:
                confirmed_tfs.append(label)
            elif bullish_signals >= bearish_signals + 2:
                conflicting.append(label)

    _tf_vote(tf_4h,  "4H")
    _tf_vote(tf_1h,  "1H")
    _tf_vote(tf_15m, "15M")

    n_confirm = len(confirmed_tfs)
    n_conflict = len(conflicting)

    if n_confirm >= 3:
        score = 100
        notes.append(f"✅ 4H+1H+15M semua confirm {direction} — deep MTF confluence")
    elif n_confirm == 2:
        score = 75
        notes.append(f"🟡 2/3 TF confirm: {', '.join(confirmed_tfs)} — acceptable")
    elif n_confirm == 1:
        score = 35
        blocking = n_confirm < min_confirm
        notes.append(f"⚠️ Hanya {confirmed_tfs[0] if confirmed_tfs else '1 TF'} confirm — weak MTF setup")
    else:
        score = 10
        blocking = True
        notes.append(f"❌ Tidak ada TF yang confirm {direction} — no MTF alignment")

    if n_conflict > 0:
        score = max(0, score - n_conflict * 15)
        notes.append(f"⚠️ {', '.join(conflicting)} conflicting — mixed structure signal")

    return {"layer": "mtf_depth", "score": round(score), "blocking": blocking,
            "notes": notes,
            "detail": {"confirmed_tfs": confirmed_tfs, "conflicting": conflicting,
                       "n_confirm": n_confirm}}


# ─────────────────────────────────────────────
# LAYER 6: LIQUIDITY CONTEXT (SMC)
# ─────────────────────────────────────────────

def _check_liquidity_context(direction: str, price: float,
                              tf_4h: dict, tf_1h: dict, tf_15m: dict) -> dict:
    """Layer 6: SMC zone quality + volume at zone + sweep trend alignment.

    Tiga sub-check:
      A) Apakah entry di OB/FVG yang valid (FRESH/TESTED, bukan MITIGATED)?
      B) Apakah ada volume anomaly di zona itu? (konfirmasi minat)
      C) Apakah liquidity sweep (equal highs/lows) searah dengan HTF trend?
         Sweep counter-trend = trap — dikena penalti.
    """
    score = 50
    notes = []
    blocking = False

    def _has_valid_ob(tf: dict, direc: str) -> bool:
        """OB valid: FRESH atau TESTED (max 2x disentuh), dalam 2% dari price."""
        ob = (tf or {}).get("order_blocks", {}) or {}
        if direc == "LONG":
            bull_ob = ob.get("bullish_ob")
            if bull_ob and isinstance(bull_ob, dict):
                ob_high  = bull_ob.get("high", 0)
                ob_low   = bull_ob.get("low", 0)
                status   = bull_ob.get("status", "")
                touches  = bull_ob.get("touches", 0)
                if ob_low <= price <= ob_high * 1.02 and status in ("FRESH", "TESTED") and touches <= 2:
                    return True
        else:
            bear_ob = ob.get("bearish_ob")
            if bear_ob and isinstance(bear_ob, dict):
                ob_high  = bear_ob.get("high", 0)
                ob_low   = bear_ob.get("low", 0)
                status   = bear_ob.get("status", "")
                touches  = bear_ob.get("touches", 0)
                if ob_low * 0.98 <= price <= ob_high and status in ("FRESH", "TESTED") and touches <= 2:
                    return True
        return False

    def _has_fvg(tf: dict, direc: str) -> bool:
        fvg = (tf or {}).get("fvg", {}) or {}
        fvg_type = fvg.get("fvg_type", "NONE")
        if direc == "LONG"  and fvg_type == "BULLISH": return True
        if direc == "SHORT" and fvg_type == "BEARISH":  return True
        return False

    def _has_sweep(tf: dict, direc: str) -> bool:
        sweep = (tf or {}).get("sweep", {}) or {}
        if not sweep.get("swept"): return False
        sw_dir = sweep.get("direction", "")
        if direc == "LONG"  and sw_dir == "UP":   return True
        if direc == "SHORT" and sw_dir == "DOWN":  return True
        return False

    def _has_volume_at_zone(tf: dict) -> bool:
        """Volume anomaly (≥1.5x MA) di TF ini — konfirmasi ada minat di zona."""
        vol = (tf or {}).get("volume_anomaly") or (tf or {}).get("volume", {})
        if isinstance(vol, dict):
            if vol.get("is_anomaly") and vol.get("multiplier", 1) >= 1.5:
                return True
        # Fallback: cek langsung di money_flow
        mf = (tf or {}).get("money_flow", {})
        if mf.get("vol_anomaly") or mf.get("volume_spike"):
            return True
        return False

    def _has_counter_sweep(tf: dict, direc: str) -> bool:
        """Sweep yang berlawanan dengan arah trade — tanda potential trap."""
        sweep = (tf or {}).get("sweep", {}) or {}
        if not sweep.get("swept"): return False
        sw_dir = sweep.get("direction", "")
        if direc == "LONG"  and sw_dir == "DOWN": return True
        if direc == "SHORT" and sw_dir == "UP":   return True
        return False

    # ── A) Zone quality check ────────────────────────────────────
    ob_1h  = _has_valid_ob(tf_1h,  direction)
    ob_15m = _has_valid_ob(tf_15m, direction)
    ob_4h  = _has_valid_ob(tf_4h,  direction)
    fvg_15m = _has_fvg(tf_15m, direction)
    fvg_1h  = _has_fvg(tf_1h,  direction)
    sweep_1h  = _has_sweep(tf_1h,  direction)
    sweep_15m = _has_sweep(tf_15m, direction)
    quality_zones = sum([ob_1h, ob_15m, ob_4h, fvg_15m, fvg_1h, sweep_1h, sweep_15m])

    if quality_zones >= 3:
        score = 95
        notes.append(f"✅ Entry di zona SMC kuat ({quality_zones} confluences: OB/FVG/Sweep)")
    elif quality_zones == 2:
        score = 75
        notes.append(f"🟡 Entry di zona SMC cukup ({quality_zones} zones confirmed)")
    elif quality_zones == 1:
        score = 45
        notes.append(f"⚠️ Hanya 1 zona SMC — entry kurang ideal")
    else:
        score = 20
        notes.append(f"❌ Entry tidak di zona SMC yang jelas — 'middle of nowhere'")

    # ── B) Volume at zone — reward kalau ada, penalti kalau sunyi ────
    vol_1h  = _has_volume_at_zone(tf_1h)
    vol_15m = _has_volume_at_zone(tf_15m)
    if vol_1h or vol_15m:
        score = min(100, score + 10)
        notes.append(f"✅ Volume anomaly di zona ({'1H' if vol_1h else '15M'}) — ada minat beli/jual")
    elif quality_zones >= 2:
        # Di zona bagus tapi volume sepi — kurangi kepercayaan
        score = max(20, score - 15)
        notes.append(f"⚠️ Entry di zona SMC tapi volume sepi — konfirmasi lemah")

    # ── C) Counter-trend sweep penalty ──────────────────────────────
    # Equal highs/lows hunting yang berlawanan HTF trend = risiko trap.
    # Contoh: sinyal LONG tapi sweep DOWN masih ada di 1H (sell-side liquidity
    # baru diambil) = kemungkinan harga masih mau lanjut turun sebentar.
    ctr_sweep_1h  = _has_counter_sweep(tf_1h,  direction)
    ctr_sweep_15m = _has_counter_sweep(tf_15m, direction)
    # HTF trend dari 4H structure
    htf_trend = ((tf_4h or {}).get("structure", {}) or {}).get("trend", "NEUTRAL")
    htf_aligns = (
        (direction == "LONG"  and htf_trend in ("BULLISH", "HH_HL")) or
        (direction == "SHORT" and htf_trend in ("BEARISH", "LH_LL"))
    )
    if ctr_sweep_1h and not htf_aligns:
        score = max(15, score - 20)
        notes.append(f"🚨 Sweep counter-trend di 1H + HTF tidak align — risiko trap tinggi")
    elif ctr_sweep_15m and not htf_aligns:
        score = max(20, score - 10)
        notes.append(f"⚠️ Sweep counter-trend di 15M + HTF tidak align — hati-hati fakeout")

    return {"layer": "liquidity", "score": round(score), "blocking": blocking,
            "notes": notes,
            "detail": {"ob_count": sum([ob_1h, ob_15m, ob_4h]),
                       "fvg": fvg_15m or fvg_1h,
                       "sweep": sweep_1h or sweep_15m,
                       "vol_at_zone": vol_1h or vol_15m,
                       "counter_sweep": ctr_sweep_1h or ctr_sweep_15m}}


# ─────────────────────────────────────────────
# LAYER 7: VOLATILITY REGIME
# ─────────────────────────────────────────────

def _check_volatility(price: float, tf_1h: dict, tf_4h: dict, sens: dict) -> dict:
    score  = 70
    notes  = []
    blocking = False

    atr_1h  = (tf_1h or {}).get("atr", 0) or 0
    atr_4h  = (tf_4h or {}).get("atr", 0) or 0
    atr_pct = (atr_1h / price * 100) if (price > 0 and atr_1h > 0) else 0

    min_atr = sens.get("atr_min_pct", 0.3)
    max_atr = sens.get("atr_max_pct", 5.0)

    if atr_pct == 0:
        return {"layer": "volatility", "score": 60, "blocking": False,
                "notes": ["⚪ ATR tidak tersedia — skip volatility check"], "detail": {}}

    if atr_pct < min_atr:
        score = 25
        notes.append(f"⚠️ ATR%={atr_pct:.2f}% terlalu rendah — pasar terlalu flat, tidak ada momentum")
    elif atr_pct > max_atr:
        score = 20
        notes.append(f"⚠️ ATR%={atr_pct:.2f}% terlalu tinggi — volatile ekstrim, slippage besar")
    elif 0.5 <= atr_pct <= 2.5:
        score = 90
        notes.append(f"✅ ATR%={atr_pct:.2f}% optimal — volatility healthy untuk entry")
    else:
        score = 65
        notes.append(f"🟡 ATR%={atr_pct:.2f}% — acceptable")

    return {"layer": "volatility", "score": round(score), "blocking": blocking,
            "notes": notes, "detail": {"atr_pct": atr_pct, "atr_1h": atr_1h}}


# ─────────────────────────────────────────────
# MAIN VALIDATOR
# ─────────────────────────────────────────────

def run_auto_validation(
    symbol:    str,
    direction: str,
    price:     float,
    tf_4h:     dict,
    tf_1h:     dict,
    tf_15m:    dict,
    oi_data:   dict,
    btc_tf4h:  dict = None,
) -> dict:
    """
    Jalankan semua 7 layer validasi.
    Return comprehensive validation result.

    dipanggil dari confirmed_signal.generate_confirmed_signal()
    setelah master_score check, sebelum backtest.
    """
    sens = _load_sensitivity()

    # Fetch BTF Daily untuk layer 1 (1D candles coin itu sendiri)
    # Layer 2 pakai btc_tf4h yang sudah di-pass dari run_scan
    # Fetch BTC 1D untuk layer 2 tambahan
    btc_tf1d = {}
    try:
        btc_daily = _fetch_candles("BTCUSDT", "1d", limit=60)
        if btc_daily:
            btc_tf1d = {
                "rsi": _rsi(btc_daily),
                "trend": _trend_from_candles(btc_daily),
                "candles": btc_daily,
            }
    except Exception:
        pass

    # Run all layers
    results = {}
    results["htf_trend"]    = _check_htf_trend(symbol, direction, sens)
    results["btc_alignment"]= _check_btc_alignment(direction, btc_tf4h, btc_tf1d, sens)
    results["cb_premium"]   = _check_cb_premium(direction)
    results["ecosystem"]    = _check_ecosystem(symbol, direction)
    results["oi_sanity"]    = _check_oi_sanity(direction, oi_data, tf_4h, sens)
    results["mtf_depth"]    = _check_mtf_depth(direction, tf_4h, tf_1h, tf_15m, sens)
    results["liquidity"]    = _check_liquidity_context(direction, price, tf_4h, tf_1h, tf_15m)
    results["volatility"]   = _check_volatility(price, tf_1h, tf_4h, sens)

    # Weighted total score
    total_score = 0.0
    for layer_id, weight in LAYER_WEIGHTS.items():
        layer_result = results.get(layer_id, {})
        layer_score  = layer_result.get("score", 50)
        total_score += layer_score * (weight / 100)

    total_score = round(total_score)

    # Hard blocks
    hard_blocked = [lid for lid, r in results.items() if r.get("blocking")]

    # Gate decision
    if hard_blocked:
        gate = "HARD_BLOCK"
    elif total_score >= VALIDATION_PASS_SCORE:
        gate = "PASS"
    elif total_score >= VALIDATION_SOFT_BLOCK_MIN:
        gate = "SOFT_BLOCK"
    else:
        gate = "HARD_BLOCK"

    passed  = [lid for lid, r in results.items() if r.get("score", 0) >= 60]
    failed  = [lid for lid, r in results.items() if r.get("score", 0) < 60]

    # Score adjustment for confirmed_signal master_score
    adjustment = 0
    if gate == "PASS":
        if total_score >= 80:
            adjustment = +10   # Boost kalau semua layer bagus
        elif total_score >= 70:
            adjustment = +5
    elif gate == "SOFT_BLOCK":
        adjustment = -15       # Penalty kalau banyak layer fail
    # HARD_BLOCK → signal tidak sampai sini

    result = {
        "symbol":        symbol,
        "direction":     direction,
        "gate":          gate,
        "total_score":   total_score,
        "adjustment":    adjustment,
        "passed_layers": passed,
        "failed_layers": failed,
        "hard_blocked":  hard_blocked,
        "layers":        results,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }

    log.info(
        f"  [{symbol} {direction}] AutoVal: score={total_score} gate={gate} "
        f"pass={passed} fail={failed} block={hard_blocked}"
    )

    # Persist for auto-learning
    _save_validation_record(result)
    return result


# ─────────────────────────────────────────────
# AUTO-LEARNING: EVOLVE SENSITIVITY
# ─────────────────────────────────────────────

VALIDATION_LOG_FILE = "validation_log.json"

def _save_validation_record(result: dict):
    try:
        existing = []
        if os.path.exists(VALIDATION_LOG_FILE):
            with open(VALIDATION_LOG_FILE) as f:
                existing = json.load(f)
        compact = {k: v for k, v in result.items() if k != "layers"}
        compact["layer_scores"] = {lid: r.get("score") for lid, r in result.get("layers", {}).items()}
        existing.append(compact)
        if len(existing) > 300:
            existing = existing[-300:]
        with open(VALIDATION_LOG_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        log.debug(f"Save validation record error: {e}")


def evolve_sensitivity():
    """
    Auto-evolve sensitivity berdasarkan signal outcomes.
    Dipanggil tiap 20 signal outcomes dari signal_tracker.

    Logic:
    - Kalau layer X sering PASS tapi signal akhirnya SL → tingkatkan sensitivity X
    - Kalau layer X sering BLOCK tapi signal seharusnya bagus → turunkan sensitivity X
    """
    try:
        val_log = []
        if os.path.exists(VALIDATION_LOG_FILE):
            with open(VALIDATION_LOG_FILE) as f:
                val_log = json.load(f)

        outcomes = []
        if os.path.exists("signal_outcomes.json"):
            with open("signal_outcomes.json") as f:
                outcomes = json.load(f)
    except Exception:
        return

    if len(outcomes) < 10:
        return

    # Match outcomes to validation records by symbol + approximate time
    # Count: for each layer, how many times it passed BUT signal failed (miss)
    # and how many times it blocked BUT signal would have been good (false block)

    changes    = []
    sens       = _load_sensitivity()
    recent_out = outcomes[-30:]  # last 30 outcomes

    wins_after_pass  = 0
    fails_after_pass = 0
    for out in recent_out:
        sym    = out.get("symbol", "")
        status = out.get("status", "")
        ts     = out.get("created_at", "")
        # Find matching validation record
        matching = [v for v in val_log
                    if v.get("symbol") == sym
                    and v.get("gate") == "PASS"
                    and abs((datetime.fromisoformat(v.get("timestamp","2000-01-01"))
                             - datetime.fromisoformat(ts.replace("Z",""))).total_seconds()) < 3600]
        if matching:
            if status in ("TP_HIT", "EXPIRED_WIN"):
                wins_after_pass += 1
            elif status in ("SL_HIT", "EXPIRED_LOSS"):
                fails_after_pass += 1

    total_pass_outcomes = wins_after_pass + fails_after_pass
    if total_pass_outcomes >= 5:
        pass_win_rate = wins_after_pass / total_pass_outcomes

        if pass_win_rate < 0.40:
            # Validator terlalu permissive → tighten
            old = sens.get("btc_rsi_min_long", 35)
            sens["btc_rsi_min_long"] = min(45, old + 2)
            changes.append(f"Tighten btc_rsi_min_long: {old} → {sens['btc_rsi_min_long']}")

            old = sens.get("min_tf_confirm", 2)
            sens["min_tf_confirm"] = min(3, old + 1)
            changes.append(f"Tighten min_tf_confirm: {old} → {sens['min_tf_confirm']}")

        elif pass_win_rate > 0.70 and total_pass_outcomes >= 10:
            # Validator working well, but maybe too strict → can loosen a bit
            old = sens.get("btc_rsi_min_long", 35)
            sens["btc_rsi_min_long"] = max(28, old - 1)
            if old != sens["btc_rsi_min_long"]:
                changes.append(f"Loosen btc_rsi_min_long: {old} → {sens['btc_rsi_min_long']}")

    if changes:
        sens["evolve_count"] = sens.get("evolve_count", 0) + 1
        _save_sensitivity(sens)
        log.info(f"🧬 Auto-validator evolved: {changes}")

    return changes


# ─────────────────────────────────────────────
# FORMATTER FOR TELEGRAM
# ─────────────────────────────────────────────

def format_validation_summary(val: dict) -> str:
    """Format validation result untuk ditambahkan ke confirmed signal message."""
    gate   = val.get("gate", "?")
    score  = val.get("total_score", 0)
    passed = val.get("passed_layers", [])
    failed = val.get("failed_layers", [])
    blocks = val.get("hard_blocked", [])

    gate_emoji = "✅" if gate == "PASS" else "⚠️" if gate == "SOFT_BLOCK" else "❌"

    layer_labels = {
        "htf_trend":     "HTF Trend (Daily/Weekly)",
        "btc_alignment": "BTC Macro Alignment",
        "cb_premium":    "Coinbase Premium (Institutional)",
        "ecosystem":     "Ecosystem Season",
        "oi_sanity":     "OI/Funding Sanity",
        "mtf_depth":     "Multi-TF Depth",
        "liquidity":     "SMC Liquidity Zone",
        "volatility":    "Volatility Regime",
    }

    lines = [
        "─────── AUTO-VALIDATION ───────",
        f"{gate_emoji} Gate: *{gate}* | Score: *{score}/100*",
    ]

    if passed:
        lines.append(f"✅ Pass: {' · '.join(layer_labels.get(l, l) for l in passed[:4])}")
    if failed:
        lines.append(f"❌ Fail: {' · '.join(layer_labels.get(l, l) for l in failed[:3])}")

    # Top notes from each layer
    layers = val.get("layers", {})
    key_notes = []
    for lid in (blocks or failed or passed)[:3]:
        lr = layers.get(lid, {})
        if lr.get("notes"):
            key_notes.append(lr["notes"][0])

    for note in key_notes[:3]:
        lines.append(f"  {note}")

    return "\n".join(lines)
