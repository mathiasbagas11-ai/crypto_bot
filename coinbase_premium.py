#!/usr/bin/env python3
"""
COINBASE PREMIUM INDEX v1.0
============================
Coinbase Premium = (BTC price di Coinbase Pro) - (BTC price di Binance)
Expressed sebagai percentage: (CB_price / BN_price - 1) * 100

Kenapa ini penting:
  Coinbase adalah exchange utama institutional US traders.
  Ketika institutional buyers aktif beli → CB price > BN price → premium positif.
  Ketika institutional sellers aktif jual → CB price < BN price → premium negatif.

Signal interpretation:

  LONG signals:
    Premium > +0.05%  → institutional accumulation aktif          → LONG BOOST
    Premium > +0.10%  → strong institutional buying               → STRONG LONG BOOST
    Premium > +0.20%  → very strong institutional demand          → VERY STRONG BOOST
    Premium turning + dari negatif (reversal)                    → EARLY LONG SIGNAL

  SHORT signals:
    Premium < -0.05%  → institutional distribution aktif          → SHORT BOOST
    Premium < -0.10%  → strong institutional selling              → STRONG SHORT BOOST
    Premium < -0.20%  → heavy institutional dump                  → VERY STRONG BOOST
    Premium turning - dari positif (distribution starts)         → EARLY SHORT SIGNAL

  NEUTRAL / CAUTION:
    -0.05% < premium < +0.05%   → no clear institutional bias
    Premium extreme + tapi price tidak naik → distribution trap  → SHORT WARNING
    Premium extreme - tapi price tidak turun → accumulation trap → LONG WARNING

Data sources:
  BTC/USD price dari Coinbase Advanced Trade API (public, no auth needed)
  BTC/USDT price dari Binance (already used by bot)

Cache: 5 menit (premium tidak berubah terlalu cepat)

Integration points:
  1. auto_validator.py → Layer 8 (Coinbase Premium)
  2. confirmed_signal.py → compute_master_score() bonus/penalty
  3. Telegram message → tampilkan premium di signal output

Also tracks:
  - Premium momentum: apakah premium sedang naik atau turun
  - Divergence: premium positif tapi price turun = distribution
  - Historical context: compare premium sekarang vs 1H lalu
"""

import time
import logging
import requests
import json
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("cb_premium")

# ── Cache ───────────────────────────────────
_cache = {
    "premium_pct":   None,
    "cb_price":      None,
    "bn_price":      None,
    "signal":        "NEUTRAL",
    "strength":      0,        # 0-100
    "last_premiums": [],       # last 12 readings (1 hour at 5m interval)
    "momentum":      "FLAT",   # RISING | FALLING | FLAT
    "last_update":   None,
}
CACHE_TTL_SECONDS = 300   # 5 menit

# ── Thresholds ──────────────────────────────
PREMIUM_STRONG_LONG   = +0.10
PREMIUM_MILD_LONG     = +0.05
PREMIUM_MILD_SHORT    = -0.05
PREMIUM_STRONG_SHORT  = -0.10
PREMIUM_EXTREME       = 0.20   # abs value

# ── API URLs ────────────────────────────────
COINBASE_TICKER_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINBASE_ADV_URL    = "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD"
BINANCE_PRICE_URL   = "https://api.binance.com/api/v3/ticker/price"

# ─────────────────────────────────────────────
# PRICE FETCHERS
# ─────────────────────────────────────────────

def _fetch_coinbase_btc_price() -> Optional[float]:
    """
    Fetch BTC/USD dari Coinbase.
    Try multiple endpoints — Coinbase v2 spot price is public.
    """
    # Method 1: Coinbase v2 spot (most reliable, no auth)
    try:
        r = requests.get(
            COINBASE_TICKER_URL,
            headers={"Accept": "application/json"},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            price = float(data.get("data", {}).get("amount", 0))
            if price > 0:
                return price
    except Exception as e:
        log.debug(f"CB v2 spot error: {e}")

    # Method 2: Coinbase Exchange public ticker
    try:
        r = requests.get(
            "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
            timeout=8
        )
        if r.status_code == 200:
            price = float(r.json().get("price", 0))
            if price > 0:
                return price
    except Exception as e:
        log.debug(f"CB exchange ticker error: {e}")

    return None


def _fetch_binance_btc_price() -> Optional[float]:
    """Fetch BTC/USDT dari Binance."""
    try:
        r = requests.get(
            BINANCE_PRICE_URL,
            params={"symbol": "BTCUSDT"},
            timeout=8
        )
        if r.status_code == 200:
            price = float(r.json().get("price", 0))
            if price > 0:
                return price
    except Exception as e:
        log.debug(f"Binance price error: {e}")
    return None


# ─────────────────────────────────────────────
# PREMIUM CALCULATION
# ─────────────────────────────────────────────

def _is_cache_fresh() -> bool:
    last = _cache["last_update"]
    if not last:
        return False
    return (time.time() - last) < CACHE_TTL_SECONDS


def fetch_premium(force: bool = False) -> dict:
    """
    Fetch dan calculate Coinbase Premium Index.
    Returns dict dengan semua context yang dibutuhkan.
    """
    if not force and _is_cache_fresh() and _cache["premium_pct"] is not None:
        return dict(_cache)

    cb_price = _fetch_coinbase_btc_price()
    bn_price = _fetch_binance_btc_price()

    if not cb_price or not bn_price:
        log.debug("Could not fetch prices for CB premium")
        return {
            "premium_pct": None,
            "cb_price": cb_price,
            "bn_price": bn_price,
            "signal": "UNKNOWN",
            "strength": 0,
            "momentum": "UNKNOWN",
            "last_premiums": _cache["last_premiums"],
            "available": False,
        }

    premium_pct = (cb_price / bn_price - 1) * 100

    # Update premium history
    history = _cache["last_premiums"].copy()
    history.append({
        "ts":      time.time(),
        "premium": premium_pct,
    })
    # Keep last 12 readings (1 hour kalau fetch tiap 5m)
    if len(history) > 12:
        history = history[-12:]

    # Calculate momentum: compare current vs 30m ago (6 readings ago)
    momentum = "FLAT"
    if len(history) >= 6:
        prev_premium = history[-6]["premium"]
        delta = premium_pct - prev_premium
        if delta > 0.03:
            momentum = "RISING"
        elif delta < -0.03:
            momentum = "FALLING"

    # Determine signal and strength
    signal, strength = _interpret_premium(premium_pct, momentum, history)

    # Update cache
    _cache.update({
        "premium_pct":   round(premium_pct, 4),
        "cb_price":      cb_price,
        "bn_price":      bn_price,
        "signal":        signal,
        "strength":      strength,
        "momentum":      momentum,
        "last_premiums": history,
        "last_update":   time.time(),
    })

    log.info(f"💰 CB Premium: {premium_pct:+.4f}% → {signal} (strength={strength})")
    return dict(_cache)


def _interpret_premium(premium_pct: float, momentum: str, history: list) -> tuple:
    """
    Interpret premium value + momentum → signal + strength (0-100).

    Returns: (signal_str, strength_int)
    signal: STRONG_LONG | MILD_LONG | NEUTRAL | MILD_SHORT | STRONG_SHORT
            | DIVERGENCE_LONG | DIVERGENCE_SHORT
    """
    abs_p = abs(premium_pct)

    # Base signal from premium level
    if premium_pct >= PREMIUM_EXTREME:
        signal   = "STRONG_LONG"
        strength = 95
    elif premium_pct >= PREMIUM_STRONG_LONG:
        signal   = "STRONG_LONG"
        strength = 80
    elif premium_pct >= PREMIUM_MILD_LONG:
        signal   = "MILD_LONG"
        strength = 60
    elif premium_pct <= -PREMIUM_EXTREME:
        signal   = "STRONG_SHORT"
        strength = 95
    elif premium_pct <= PREMIUM_STRONG_SHORT:
        signal   = "STRONG_SHORT"
        strength = 80
    elif premium_pct <= PREMIUM_MILD_SHORT:
        signal   = "MILD_SHORT"
        strength = 60
    else:
        signal   = "NEUTRAL"
        strength = 30

    # Momentum adjustment
    if momentum == "RISING":
        if "LONG" in signal:
            strength = min(100, strength + 10)
        elif "SHORT" in signal:
            # Premium rising from negative = potential reversal
            strength = max(0, strength - 10)
            if premium_pct > -0.03:
                signal = "DIVERGENCE_SHORT"  # was short, now neutralizing

    elif momentum == "FALLING":
        if "SHORT" in signal:
            strength = min(100, strength + 10)
        elif "LONG" in signal:
            strength = max(0, strength - 10)
            if premium_pct < 0.03:
                signal = "DIVERGENCE_LONG"   # was long, now neutralizing

    # Extreme premium can be a trap (mean reversion risk)
    if abs_p > PREMIUM_EXTREME:
        # Very extreme premium often means overextension
        if len(history) >= 3:
            # Check if premium has been extreme for a while
            recent_extreme = sum(1 for h in history[-3:] if abs(h["premium"]) > PREMIUM_EXTREME)
            if recent_extreme >= 3:
                strength = max(0, strength - 20)
                signal = f"OVEREXTENDED_{signal}"

    return signal, strength


# ─────────────────────────────────────────────
# SCORE PER SIGNAL DIRECTION
# ─────────────────────────────────────────────

def get_premium_score(direction: str, premium_data: dict = None) -> dict:
    """
    Convert premium data → score (0-100) + notes untuk satu direction.
    Dipanggil dari auto_validator Layer 8 dan confirmed_signal.

    Returns: {score, notes, blocking, premium_pct, signal, strength}
    """
    if premium_data is None:
        premium_data = fetch_premium()

    if not premium_data.get("available", True) or premium_data.get("premium_pct") is None:
        return {
            "score":    50,
            "notes":    ["⚪ Coinbase Premium tidak tersedia — skip"],
            "blocking": False,
            "premium_pct": None,
            "signal":   "UNKNOWN",
            "strength": 0,
        }

    premium_pct = premium_data["premium_pct"]
    signal      = premium_data["signal"]
    strength    = premium_data["strength"]
    momentum    = premium_data["momentum"]
    score       = 50   # neutral default
    notes       = []
    blocking    = False

    def _fmt_p(p): return f"{p:+.4f}%"

    if direction == "LONG":
        if "STRONG_LONG" in signal:
            score = 90 + min(10, strength - 80)
            mom_str = f" + momentum RISING 📈" if momentum == "RISING" else ""
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} STRONG POSITIVE{mom_str}\n"
                f"   → Institutional buyers aktif — LONG strongly confirmed"
            )
        elif "MILD_LONG" in signal:
            score = 72
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} mild positive\n"
                f"   → Institutional bias LONG — moderate support"
            )
        elif signal == "NEUTRAL":
            score = 50
            notes.append(f"⚪ CB Premium {_fmt_p(premium_pct)} — neutral, no institutional bias")
        elif "MILD_SHORT" in signal:
            score = 30
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} NEGATIVE\n"
                f"   → Institutional sellers aktif — LONG berisiko"
            )
        elif "STRONG_SHORT" in signal:
            score = 5
            blocking = abs(premium_pct) > 0.15
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} STRONGLY NEGATIVE\n"
                f"   → Heavy institutional selling — LONG sangat berisiko, counter-institutional"
            )
        elif "DIVERGENCE_LONG" in signal:
            score = 65
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} — reversal signal\n"
                f"   → Premium naik dari negatif, possible institutional accumulation starts"
            )
        elif "OVEREXTENDED" in signal:
            score = 55
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} OVEREXTENDED\n"
                f"   → Premium sudah extreme lama, mean reversion risk untuk LONG"
            )

        # Momentum bonus/penalty
        if momentum == "RISING" and score >= 60:
            score = min(100, score + 8)
            notes.append(f"   📈 Premium momentum RISING — institutional buying accelerating")
        elif momentum == "FALLING" and score >= 60:
            score = max(0, score - 8)
            notes.append(f"   📉 Premium momentum FALLING — institutional buying slowing")

    else:  # SHORT
        if "STRONG_SHORT" in signal:
            score = 90 + min(10, strength - 80)
            mom_str = f" + momentum FALLING 📉" if momentum == "FALLING" else ""
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} STRONG NEGATIVE{mom_str}\n"
                f"   → Institutional sellers aktif — SHORT strongly confirmed"
            )
        elif "MILD_SHORT" in signal:
            score = 72
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} mild negative\n"
                f"   → Institutional bias SHORT — moderate confirmation"
            )
        elif signal == "NEUTRAL":
            score = 50
            notes.append(f"⚪ CB Premium {_fmt_p(premium_pct)} — neutral, no clear bias")
        elif "MILD_LONG" in signal:
            score = 30
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} POSITIVE\n"
                f"   → Institutional buyers aktif — SHORT berisiko"
            )
        elif "STRONG_LONG" in signal:
            score = 5
            blocking = abs(premium_pct) > 0.15
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} STRONGLY POSITIVE\n"
                f"   → Heavy institutional buying — SHORT sangat berisiko, counter-institutional"
            )
        elif "DIVERGENCE_SHORT" in signal:
            score = 65
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} — reversal signal\n"
                f"   → Premium turun dari positif, possible institutional distribution starts"
            )
        elif "OVEREXTENDED" in signal:
            score = 55
            notes.append(
                f"🏦 CB Premium {_fmt_p(premium_pct)} OVEREXTENDED\n"
                f"   → Premium sudah extreme lama, mean reversion bisa reversal SHORT"
            )

        if momentum == "FALLING" and score >= 60:
            score = min(100, score + 8)
            notes.append(f"   📉 Premium momentum FALLING — institutional selling accelerating")
        elif momentum == "RISING" and score >= 60:
            score = max(0, score - 8)
            notes.append(f"   📈 Premium momentum RISING — institutional selling slowing")

    return {
        "score":       round(min(100, max(0, score))),
        "notes":       notes,
        "blocking":    blocking,
        "premium_pct": premium_pct,
        "signal":      signal,
        "strength":    strength,
        "momentum":    momentum,
    }


# ─────────────────────────────────────────────
# MASTER SCORE CONTRIBUTION
# Untuk confirmed_signal.compute_master_score()
# ─────────────────────────────────────────────

def get_premium_master_contribution(direction: str) -> dict:
    """
    Return contribution ke master_score dari CB premium.
    Dipakai langsung di compute_master_score() sebagai weighted input.

    Returns: {weighted_long_add, weighted_short_add, reason, premium_pct}
    """
    try:
        premium_data = fetch_premium()
        if premium_data.get("premium_pct") is None:
            return {"weighted_long_add": 0, "weighted_short_add": 0,
                    "reason": "", "premium_pct": None}

        p      = premium_data["premium_pct"]
        signal = premium_data["signal"]
        mom    = premium_data["momentum"]

        long_add  = 0.0
        short_add = 0.0
        reason    = ""

        if "STRONG_LONG" in signal and "OVEREXTENDED" not in signal:
            long_add = 8.0
            reason   = f"🏦 CB Premium {p:+.4f}% STRONG POSITIVE — institutional accumulation"
        elif "MILD_LONG" in signal:
            long_add = 4.0
            reason   = f"🏦 CB Premium {p:+.4f}% positive — institutional LONG bias"
        elif "STRONG_SHORT" in signal and "OVEREXTENDED" not in signal:
            short_add = 8.0
            reason    = f"🏦 CB Premium {p:+.4f}% STRONG NEGATIVE — institutional distribution"
        elif "MILD_SHORT" in signal:
            short_add = 4.0
            reason    = f"🏦 CB Premium {p:+.4f}% negative — institutional SHORT bias"

        # Counter-direction penalty
        if direction == "LONG" and short_add > 0:
            long_add -= short_add * 0.5
            reason = f"🏦 CB Premium {p:+.4f}% negative — LONG counter-institutional"
        elif direction == "SHORT" and long_add > 0:
            short_add -= long_add * 0.5
            reason = f"🏦 CB Premium {p:+.4f}% positive — SHORT counter-institutional"

        # Momentum modifier
        if mom == "RISING":
            long_add  = min(long_add * 1.2,  12.0)
        elif mom == "FALLING":
            short_add = min(short_add * 1.2, 12.0)

        return {
            "weighted_long_add":  round(long_add, 2),
            "weighted_short_add": round(short_add, 2),
            "reason":             reason,
            "premium_pct":        p,
            "signal":             signal,
            "momentum":           mom,
        }

    except Exception as e:
        log.debug(f"Premium master contribution error: {e}")
        return {"weighted_long_add": 0, "weighted_short_add": 0,
                "reason": "", "premium_pct": None}


# ─────────────────────────────────────────────
# HISTORY & DIVERGENCE ANALYSIS
# ─────────────────────────────────────────────

def get_premium_context_string() -> str:
    """
    Return short string untuk ditampilkan di Telegram signal message.
    Format: "CB Premium: +0.08% 📈 Institutional LONG"
    """
    data = fetch_premium()
    p = data.get("premium_pct")
    if p is None:
        return "CB Premium: N/A"

    signal = data.get("signal", "NEUTRAL")
    mom    = data.get("momentum", "FLAT")

    if "STRONG_LONG" in signal:   label = "Institutional ACCUMULATION 🏦🟢"
    elif "MILD_LONG" in signal:   label = "Institutional LONG bias 🏦"
    elif "STRONG_SHORT" in signal: label = "Institutional DISTRIBUTION 🏦🔴"
    elif "MILD_SHORT" in signal:  label = "Institutional SHORT bias 🏦"
    elif "DIVERGENCE" in signal:  label = "Institutional REVERSAL ⚠️"
    else:                         label = "Neutral"

    mom_icon = {"RISING": "📈", "FALLING": "📉", "FLAT": "➡️"}.get(mom, "")
    return f"CB Premium: {p:+.4f}% {mom_icon} {label}"


def detect_premium_divergence(price_change_1h_pct: float) -> Optional[str]:
    """
    Deteksi divergence antara CB premium dan price movement.
    Ini adalah advanced signal untuk early reversal detection.

    premium positif tapi price turun → distribution trap → SHORT warning
    premium negatif tapi price naik → accumulation absorb → LONG warning
    """
    data = fetch_premium()
    p = data.get("premium_pct")
    if p is None:
        return None

    if p > PREMIUM_MILD_LONG and price_change_1h_pct < -0.5:
        return (
            f"⚠️ PREMIUM DIVERGENCE: CB Premium positif ({p:+.4f}%) "
            f"tapi price -{abs(price_change_1h_pct):.2f}% dalam 1H\n"
            f"   → Kemungkinan distribution trap — institutional absorb selling"
        )
    elif p < PREMIUM_MILD_SHORT and price_change_1h_pct > 0.5:
        return (
            f"⚠️ PREMIUM DIVERGENCE: CB Premium negatif ({p:+.4f}%) "
            f"tapi price +{price_change_1h_pct:.2f}% dalam 1H\n"
            f"   → Kemungkinan accumulation trap — institutional absorb buying"
        )
    return None


# ─────────────────────────────────────────────
# TELEGRAM FORMATTER
# ─────────────────────────────────────────────

def format_premium_report() -> str:
    """Format full premium report untuk /premium command."""
    data = fetch_premium(force=True)
    ts   = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    p       = data.get("premium_pct")
    signal  = data.get("signal", "UNKNOWN")
    mom     = data.get("momentum", "UNKNOWN")
    cb_px   = data.get("cb_price")
    bn_px   = data.get("bn_price")
    history = data.get("last_premiums", [])

    if p is None:
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🏦 *COINBASE PREMIUM INDEX*\n"
            f"🕐 {ts}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "❌ Data tidak tersedia — Coinbase API timeout\n"
            "_Coba beberapa menit lagi_"
        )

    # Premium bar visualization
    bar_pos = int(min(10, max(0, (p + 0.3) / 0.6 * 10)))
    bar = "▓" * bar_pos + "░" * (10 - bar_pos)
    bar_label = f"[-0.30%] {bar} [+0.30%]"

    if "STRONG_LONG" in signal:
        signal_label  = "🟢🟢 STRONG POSITIVE — Heavy Institutional Buying"
        implication_l = "LONG: ✅ Institutional tailwind KUAT"
        implication_s = "SHORT: ❌ Melawan institutional, sangat berisiko"
    elif "MILD_LONG" in signal:
        signal_label  = "🟢 POSITIVE — Mild Institutional Buying"
        implication_l = "LONG: ✅ Institutional support ada"
        implication_s = "SHORT: ⚠️ Melawan institutional bias"
    elif "STRONG_SHORT" in signal:
        signal_label  = "🔴🔴 STRONG NEGATIVE — Heavy Institutional Selling"
        implication_l = "LONG: ❌ Institutional selling berat, sangat berisiko"
        implication_s = "SHORT: ✅ Institutional tailwind KUAT"
    elif "MILD_SHORT" in signal:
        signal_label  = "🔴 NEGATIVE — Mild Institutional Selling"
        implication_l = "LONG: ⚠️ Institutional pressure ada"
        implication_s = "SHORT: ✅ Institutional support untuk SHORT"
    elif "DIVERGENCE_LONG" in signal:
        signal_label  = "⚠️ REVERSAL UP — Premium recovering dari negatif"
        implication_l = "LONG: 🟡 Early accumulation signal — watch closely"
        implication_s = "SHORT: ⚠️ Institutional selling mereda"
    elif "DIVERGENCE_SHORT" in signal:
        signal_label  = "⚠️ REVERSAL DOWN — Premium dropping dari positif"
        implication_l = "LONG: ⚠️ Institutional buying mereda"
        implication_s = "SHORT: 🟡 Early distribution signal — watch closely"
    else:
        signal_label  = "⚪ NEUTRAL — No clear institutional bias"
        implication_l = "LONG: ⚪ No institutional support/headwind"
        implication_s = "SHORT: ⚪ No institutional support/headwind"

    mom_label = {
        "RISING":  "📈 RISING  — Institutional pressure increasing",
        "FALLING": "📉 FALLING — Institutional pressure decreasing",
        "FLAT":    "➡️ FLAT    — Stable",
        "UNKNOWN": "❓ Unknown",
    }.get(mom, mom)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🏦 *COINBASE PREMIUM INDEX*",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📊 Premium : *{p:+.4f}%*",
        f"💰 CB BTC  : ${cb_px:,.2f}" if cb_px else "",
        f"💰 BN BTC  : ${bn_px:,.2f}" if bn_px else "",
        "",
        f"`{bar_label}`",
        "",
        f"📡 Signal  : *{signal_label}*",
        f"📈 Momentum: {mom_label}",
        "",
        "─────── TRADING IMPLICATION ───────",
        f"  {implication_l}",
        f"  {implication_s}",
    ]

    # Premium history mini chart
    if len(history) >= 3:
        lines.append("")
        lines.append("─────── PREMIUM HISTORY (last 1H) ───────")
        hist_str = "  "
        for h in history[-8:]:
            pv = h.get("premium", 0)
            if pv > 0.05:    hist_str += "▲"
            elif pv < -0.05: hist_str += "▼"
            else:            hist_str += "─"
        avg_recent = sum(h.get("premium", 0) for h in history[-6:]) / min(6, len(history))
        lines.append(hist_str + f"  avg: {avg_recent:+.4f}%")

    # Rule summary
    lines += [
        "",
        "─────── RULE SUMMARY ───────",
        "  Premium > +0.05%  → LONG boost",
        "  Premium > +0.10%  → LONG strong boost",
        "  Premium < -0.05%  → SHORT boost",
        "  Premium < -0.10%  → SHORT strong boost",
        "  |Premium| > +0.15% → Hard block contra-direction",
        "",
        "_Data: Coinbase Pro vs Binance BTC price diff_",
        "⚠️ _Not financial advice. DYOR._",
    ]

    return "\n".join(l for l in lines if l is not None)
