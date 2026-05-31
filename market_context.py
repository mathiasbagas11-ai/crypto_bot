#!/usr/bin/env python3
"""
MARKET CONTEXT MODULE
=====================
Global market regime filters for confirmed signal gate.

Components:
  1. Fear & Greed Index      — alternative.me (free, no key)
  2. BTC Macro Regime        — BTC 4H EMA structure
  3. Market Breadth          — % of 25 scanned coins bullish
  4. Volatility Regime       — ATR contracting vs expanding
  5. BTC Dominance Trend     — CoinGecko /api/v3/global

Usage:
  from market_context import get_market_context, format_market_context_block
  ctx = get_market_context()   # cached, call every scan
"""

import time
import logging
import requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("market_context")

# ── Cache TTLs ────────────────────────────────
_FEAR_GREED_TTL  = 6 * 3600    # 6h — index only updates once/day
_BTC_REGIME_TTL  = 30 * 60     # 30m
_BREADTH_TTL     = 15 * 60     # 15m (per scan cycle)
_DOMINANCE_TTL   = 30 * 60     # 30m

_cache: dict = {}

BINANCE_FUTURES = "https://fapi.binance.com"

# 25 liquid coins used for breadth calculation
BREADTH_COINS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT","LINKUSDT",
    "NEARUSDT","APTUSDT","INJUSDT","SUIUSDT","ARBUSDT",
    "OPUSDT","TIAUSDT","RENDERUSDT","FETUSDT","ENAUSDT",
    "AAVEUSDT","JUPUSDT","ORDIUSDT","WIFUSDT","PENDLEUSDT",
]

# ── Fear & Greed Index ────────────────────────

def get_fear_greed() -> dict:
    """
    Returns:
      value: 0-100
      classification: Extreme Fear / Fear / Neutral / Greed / Extreme Greed
      label: short text
      timestamp: unix
    """
    cache = _cache.get("fear_greed")
    if cache and time.time() - cache["_ts"] < _FEAR_GREED_TTL:
        return cache

    empty = {"value": 50, "classification": "Neutral", "label": "Neutral",
             "timestamp": 0, "_ts": 0, "_error": True}
    try:
        r = requests.get(
            "https://api.alternative.me/fng/",
            params={"limit": 1, "format": "json"},
            timeout=10,
        )
        if r.ok:
            data = r.json().get("data", [{}])[0]
            val  = int(data.get("value", 50))
            cls  = data.get("value_classification", "Neutral")
            ts   = int(data.get("timestamp", 0))
            result = {
                "value":          val,
                "classification": cls,
                "label":          cls,
                "timestamp":      ts,
                "_ts":            time.time(),
                "_error":         False,
            }
            _cache["fear_greed"] = result
            return result
    except Exception as e:
        log.warning(f"Fear & Greed fetch error: {e}")
    empty["_ts"] = time.time()
    _cache["fear_greed"] = empty
    return empty


# ── BTC Macro Regime ──────────────────────────

def get_btc_regime() -> dict:
    """
    BTC 4H structure: EMA9 vs EMA21.

    Returns:
      regime:   BULLISH | BEARISH | NEUTRAL
      ema9:     float
      ema21:    float
      price:    float
      trend_strength: 0-100
    """
    cache = _cache.get("btc_regime")
    if cache and time.time() - cache["_ts"] < _BTC_REGIME_TTL:
        return cache

    empty = {"regime": "NEUTRAL", "ema9": 0, "ema21": 0, "price": 0,
             "trend_strength": 0, "_ts": 0, "_error": True}
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "4h", "limit": 50},
            timeout=10,
        )
        if not r.ok:
            raise ValueError(f"HTTP {r.status_code}")

        raw = r.json()
        closes = [float(c[4]) for c in raw]
        if len(closes) < 22:
            raise ValueError("Not enough candles")

        # EMA9
        k9 = 2 / 10
        e9 = sum(closes[:9]) / 9
        for v in closes[9:]:
            e9 = v * k9 + e9 * (1 - k9)

        # EMA21
        k21 = 2 / 22
        e21 = sum(closes[:21]) / 21
        for v in closes[21:]:
            e21 = v * k21 + e21 * (1 - k21)

        price = closes[-1]
        spread_pct = abs(e9 - e21) / e21 * 100

        if e9 > e21:
            regime = "BULLISH"
        elif e9 < e21:
            regime = "BEARISH"
        else:
            regime = "NEUTRAL"

        # Trend strength: how far price is from EMA21
        dist_pct = abs(price - e21) / e21 * 100
        strength = min(100, int(dist_pct * 10 + spread_pct * 5))

        result = {
            "regime":          regime,
            "ema9":            round(e9, 2),
            "ema21":           round(e21, 2),
            "price":           round(price, 2),
            "spread_pct":      round(spread_pct, 3),
            "trend_strength":  strength,
            "_ts":             time.time(),
            "_error":          False,
        }
        _cache["btc_regime"] = result
        return result
    except Exception as e:
        log.warning(f"BTC regime fetch error: {e}")

    empty["_ts"] = time.time()
    _cache["btc_regime"] = empty
    return empty


# ── Market Breadth ────────────────────────────

def _get_ticker_bulk() -> dict:
    """Fetch 24h ticker for all futures symbols at once (1 API call)."""
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr",
            timeout=10,
        )
        if r.ok:
            return {item["symbol"]: item for item in r.json()}
    except Exception as e:
        log.warning(f"Ticker bulk fetch error: {e}")
    return {}


def get_market_breadth() -> dict:
    """
    % of 25 tracked coins with positive 24h change.

    Returns:
      bullish_pct:   0-100 (% of coins up in last 24h)
      bearish_pct:   0-100
      neutral_pct:   0-100
      bullish_count: int
      bearish_count: int
      regime:        BULLISH (>60%) | BEARISH (<35%) | MIXED
      coins_sampled: int
    """
    cache = _cache.get("breadth")
    if cache and time.time() - cache["_ts"] < _BREADTH_TTL:
        return cache

    empty = {"bullish_pct": 50, "bearish_pct": 50, "neutral_pct": 0,
             "bullish_count": 0, "bearish_count": 0, "regime": "MIXED",
             "coins_sampled": 0, "_ts": 0, "_error": True}
    try:
        tickers = _get_ticker_bulk()
        if not tickers:
            raise ValueError("Empty ticker response")

        bullish = bearish = neutral = 0
        sampled = 0
        for sym in BREADTH_COINS:
            t = tickers.get(sym)
            if not t:
                continue
            chg = float(t.get("priceChangePercent", 0))
            sampled += 1
            if chg > 0.5:
                bullish += 1
            elif chg < -0.5:
                bearish += 1
            else:
                neutral += 1

        if sampled == 0:
            raise ValueError("No coins sampled")

        bull_pct = bullish / sampled * 100
        bear_pct = bearish / sampled * 100
        neut_pct = neutral / sampled * 100

        if bull_pct >= 60:
            regime = "BULLISH"
        elif bear_pct >= 65:
            regime = "BEARISH"
        elif bear_pct >= 50:
            regime = "MIXED_BEAR"
        else:
            regime = "MIXED"

        result = {
            "bullish_pct":   round(bull_pct, 1),
            "bearish_pct":   round(bear_pct, 1),
            "neutral_pct":   round(neut_pct, 1),
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "regime":        regime,
            "coins_sampled": sampled,
            "_ts":           time.time(),
            "_error":        False,
        }
        _cache["breadth"] = result
        return result
    except Exception as e:
        log.warning(f"Market breadth error: {e}")

    empty["_ts"] = time.time()
    _cache["breadth"] = empty
    return empty


# ── Volatility Regime ─────────────────────────

def get_volatility_regime(symbol: str = "BTCUSDT") -> dict:
    """
    Compare current ATR to 30-candle rolling average on 4H.

    Returns:
      regime:       CONTRACTING | NORMAL | EXPANDING
      atr_current:  float
      atr_avg:      float
      ratio:        current/avg
    """
    cache_key = f"vol_regime_{symbol}"
    cache = _cache.get(cache_key)
    if cache and time.time() - cache["_ts"] < _BTC_REGIME_TTL:
        return cache

    empty = {"regime": "NORMAL", "atr_current": 0, "atr_avg": 0,
             "ratio": 1.0, "_ts": 0, "_error": True}
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "4h", "limit": 35},
            timeout=10,
        )
        if not r.ok:
            raise ValueError(f"HTTP {r.status_code}")

        raw = r.json()
        if len(raw) < 31:
            raise ValueError("Not enough candles")

        # True Range for each candle
        trs = []
        for i in range(1, len(raw)):
            h = float(raw[i][2])
            l = float(raw[i][3])
            c_prev = float(raw[i-1][4])
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            trs.append(tr)

        # Smooth ATR (Wilder's)
        period = 14
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period

        # 30-candle average of simple TR
        avg_tr = sum(trs[-30:]) / 30
        ratio  = atr / avg_tr if avg_tr > 0 else 1.0

        if ratio < 0.8:
            regime = "CONTRACTING"
        elif ratio > 1.3:
            regime = "EXPANDING"
        else:
            regime = "NORMAL"

        result = {
            "regime":      regime,
            "atr_current": round(atr, 4),
            "atr_avg":     round(avg_tr, 4),
            "ratio":       round(ratio, 3),
            "_ts":         time.time(),
            "_error":      False,
        }
        _cache[cache_key] = result
        return result
    except Exception as e:
        log.warning(f"Volatility regime error {symbol}: {e}")

    empty["_ts"] = time.time()
    _cache[cache_key] = empty
    return empty


# ── BTC Dominance Trend ───────────────────────

def get_btc_dominance() -> dict:
    """
    CoinGecko /api/v3/global — BTC dominance %.
    Tracks 3 readings to determine trend.

    Returns:
      dominance_pct: float
      trend:         RISING | FALLING | STABLE
      readings:      list of last 3 dominance values
    """
    cache = _cache.get("btc_dominance")
    if cache and time.time() - cache["_ts"] < _DOMINANCE_TTL:
        return cache

    empty = {"dominance_pct": 50.0, "trend": "STABLE",
             "readings": [], "_ts": 0, "_error": True}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10,
        )
        if not r.ok:
            raise ValueError(f"HTTP {r.status_code}")

        data = r.json().get("data", {})
        dom  = data.get("market_cap_percentage", {}).get("btc", 50.0)

        # Build rolling readings (max 3)
        prev = _cache.get("btc_dominance", {})
        prev_readings = prev.get("readings", []) if prev else []
        readings = (prev_readings + [round(dom, 2)])[-3:]

        if len(readings) >= 2:
            delta = readings[-1] - readings[0]
            if delta > 0.3:
                trend = "RISING"
            elif delta < -0.3:
                trend = "FALLING"
            else:
                trend = "STABLE"
        else:
            trend = "STABLE"

        result = {
            "dominance_pct": round(dom, 2),
            "trend":         trend,
            "readings":      readings,
            "_ts":           time.time(),
            "_error":        False,
        }
        _cache["btc_dominance"] = result
        return result
    except Exception as e:
        log.warning(f"BTC dominance fetch error: {e}")

    empty["_ts"] = time.time()
    _cache["btc_dominance"] = empty
    return empty


# ── USDT Dominance Trend ──────────────────────

def get_usdt_dominance() -> dict:
    """
    CoinGecko /api/v3/global — USDT dominance %.
    Tracks 3 readings to determine trend.
    USDT.D rising = risk-off (crypto bearish), falling = bullish.

    Returns:
      usdt_dom_pct: float
      trend:        RISING | FALLING | STABLE
      readings:     list of last 3 dominance values
    """
    cache = _cache.get("usdt_dominance")
    if cache and time.time() - cache["_ts"] < _DOMINANCE_TTL:
        return cache

    empty = {"usdt_dom_pct": 5.0, "trend": "STABLE",
             "readings": [], "_ts": 0, "_error": True}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10,
        )
        if not r.ok:
            raise ValueError(f"HTTP {r.status_code}")

        data = r.json().get("data", {})
        dom  = data.get("market_cap_percentage", {}).get("usdt", 5.0)

        # Build rolling readings (max 3)
        prev = _cache.get("usdt_dominance", {})
        prev_readings = prev.get("readings", []) if prev else []
        readings = (prev_readings + [round(dom, 2)])[-3:]

        if len(readings) >= 2:
            delta = readings[-1] - readings[0]
            if delta > 0.2:
                trend = "RISING"
            elif delta < -0.2:
                trend = "FALLING"
            else:
                trend = "STABLE"
        else:
            trend = "STABLE"

        result = {
            "usdt_dom_pct": round(dom, 2),
            "trend":        trend,
            "readings":     readings,
            "_ts":          time.time(),
            "_error":       False,
        }
        _cache["usdt_dominance"] = result
        return result
    except Exception as e:
        log.warning(f"USDT dominance fetch error: {e}")

    empty["_ts"] = time.time()
    _cache["usdt_dominance"] = empty
    return empty


# ── Aggregated Market Context ─────────────────

def get_market_context() -> dict:
    """
    Fetch all market context components and aggregate into a single dict.
    Cached individually — safe to call every scan cycle.

    Returns:
      fear_greed:   {value, classification}
      btc_regime:   {regime, ema9, ema21}
      breadth:      {bullish_pct, bearish_pct, regime}
      volatility:   {regime, ratio}
      btc_dom:      {dominance_pct, trend}

      overall_bias:  RISK_ON | RISK_OFF | NEUTRAL
      long_penalty:  int (pts to subtract from LONG master score)
      short_penalty: int (pts to subtract from SHORT master score)
      long_blocked:  bool
      short_blocked: bool
      reasons:       list of str
    """
    fg   = get_fear_greed()
    btcr = get_btc_regime()
    brd  = get_market_breadth()
    vol  = get_volatility_regime()
    dom  = get_btc_dominance()
    usdt_dom = get_usdt_dominance()

    reasons   = []
    long_pen  = 0
    short_pen = 0
    long_blk  = False
    short_blk = False

    # ── Fear & Greed ──
    fg_val = fg["value"]
    if fg_val <= 20:
        # Extreme Fear → market panicking → LONG risky, SHORT has tailwind
        long_pen += 10
        reasons.append(f"😱 Fear & Greed: {fg_val} (Extreme Fear) — risk-off, LONG penalty -10pt")
    elif fg_val <= 35:
        long_pen += 5
        reasons.append(f"😨 Fear & Greed: {fg_val} (Fear) — cautious, LONG -5pt")
    elif fg_val >= 85:
        # Extreme Greed → overextended → SHORT has potential, LONG risky
        short_pen += 5
        reasons.append(f"🤑 Fear & Greed: {fg_val} (Extreme Greed) — overextended, SHORT -5pt")
    elif fg_val >= 70:
        short_pen += 3
        reasons.append(f"😀 Fear & Greed: {fg_val} (Greed) — extended, SHORT -3pt")

    # ── BTC Regime ──
    btc_regime = btcr["regime"]
    if btc_regime == "BEARISH":
        long_pen += 15
        reasons.append(f"📉 BTC 4H Regime: BEARISH (EMA9 < EMA21) — LONG penalty -15pt")
    elif btc_regime == "BULLISH":
        short_pen += 10
        reasons.append(f"📈 BTC 4H Regime: BULLISH (EMA9 > EMA21) — SHORT penalty -10pt")

    # ── Market Breadth ──
    brd_regime = brd["regime"]
    bull_pct   = brd["bullish_pct"]
    bear_pct   = brd["bearish_pct"]

    if bear_pct >= 70:
        # 70%+ coins bearish → market-wide sell-off → LONG suppressed
        long_pen += 15
        reasons.append(f"📊 Breadth: {bear_pct:.0f}% bearish — broad sell-off, LONG -15pt")
    elif bear_pct >= 55:
        long_pen += 8
        reasons.append(f"📊 Breadth: {bear_pct:.0f}% bearish — mixed-bearish, LONG -8pt")
    elif bull_pct >= 70:
        short_pen += 8
        reasons.append(f"📊 Breadth: {bull_pct:.0f}% bullish — broad rally, SHORT -8pt")

    # ── Volatility Regime ──
    vol_regime = vol["regime"]
    if vol_regime == "EXPANDING":
        # Expanding volatility = momentum environment — both directions valid but risky
        # Actually expanding vol = good for breakout trades, slightly prefer direction
        reasons.append(f"⚡ Volatility: EXPANDING (ATR ratio {vol['ratio']:.2f}x) — momentum environment")
    elif vol_regime == "CONTRACTING":
        # Contracting = range-bound → entries before expansion are low quality
        long_pen  += 5
        short_pen += 5
        reasons.append(f"😴 Volatility: CONTRACTING (ATR ratio {vol['ratio']:.2f}x) — pre-expansion, both sides -5pt")

    # ── BTC Dominance ──
    dom_trend = dom["trend"]
    dom_pct   = dom["dominance_pct"]
    if dom_trend == "RISING":
        # Rising BTC dominance = alts losing relative to BTC = risk-off for alts
        long_pen += 5
        reasons.append(f"₿ BTC Dominance: {dom_pct:.1f}% RISING — alts risk-off, LONG -5pt")
    elif dom_trend == "FALLING":
        # Falling dominance = altseason → alts gaining vs BTC
        short_pen += 3
        reasons.append(f"₿ BTC Dominance: {dom_pct:.1f}% FALLING — altseason, SHORT -3pt")

    # ── USDT Dominance ──
    usdt_trend = usdt_dom["trend"]
    usdt_pct   = usdt_dom["usdt_dom_pct"]
    if len(usdt_dom.get("readings", [])) >= 2:
        usdt_delta = abs(usdt_dom["readings"][-1] - usdt_dom["readings"][0])
    else:
        usdt_delta = 0.0
    if usdt_trend == "RISING" and usdt_delta > 0.5:
        # USDT.D rising sharply = crypto capital fleeing to stablecoins = bearish for all crypto
        long_pen  += 8
        short_pen -= 5  # boost SHORT (cap at 0 below)
        short_pen  = max(0, short_pen)
        reasons.append(f"💵 USDT.D: {usdt_pct:.2f}% RISING (+{usdt_delta:.2f}%) — capital flight to stable, LONG -8pt / SHORT +5pt")
    elif usdt_trend == "RISING":
        long_pen += 4
        reasons.append(f"💵 USDT.D: {usdt_pct:.2f}% RISING — mild risk-off, LONG -4pt")
    elif usdt_trend == "FALLING" and usdt_delta > 0.5:
        # USDT.D falling = stable coins rotating into crypto = bullish
        short_pen += 5
        long_pen  -= 4  # mild LONG boost, cap at 0
        long_pen   = max(0, long_pen)
        reasons.append(f"💵 USDT.D: {usdt_pct:.2f}% FALLING (-{usdt_delta:.2f}%) — stablecoin rotation into crypto, SHORT +5pt / LONG boost")
    elif usdt_trend == "FALLING":
        short_pen += 3
        reasons.append(f"💵 USDT.D: {usdt_pct:.2f}% FALLING — mild risk-on, SHORT +3pt")

    # ── Overall Bias ──
    if long_pen >= 25 or long_blk:
        overall = "RISK_OFF"
    elif short_pen >= 20 or short_blk:
        overall = "RISK_ON"
    elif long_pen > short_pen + 10:
        overall = "RISK_OFF"
    elif short_pen > long_pen + 8:
        overall = "RISK_ON"
    else:
        overall = "NEUTRAL"

    return {
        "fear_greed":     fg,
        "btc_regime":     btcr,
        "breadth":        brd,
        "volatility":     vol,
        "btc_dom":        dom,
        "usdt_dominance": usdt_dom,
        "overall_bias":   overall,
        "long_penalty":   long_pen,
        "short_penalty":  short_pen,
        "long_blocked":   long_blk,
        "short_blocked":  short_blk,
        "reasons":        reasons,
        "_ts":            time.time(),
    }


def apply_market_context_to_score(
    direction: str,
    master_score: int,
    ctx: dict,
) -> tuple:
    """
    Apply market context penalties to a master score.

    Returns: (adjusted_score, blocked, context_reasons)
    """
    if direction == "LONG":
        penalty  = ctx.get("long_penalty", 0)
        blocked  = ctx.get("long_blocked", False)
    else:
        penalty  = ctx.get("short_penalty", 0)
        blocked  = ctx.get("short_blocked", False)

    adj_score = max(0, master_score - penalty)
    return adj_score, blocked, ctx.get("reasons", [])


# ── Formatter ─────────────────────────────────

def format_market_context_block(ctx: dict, compact: bool = False) -> str:
    """Format market context for Telegram display."""
    fg      = ctx.get("fear_greed", {})
    btcr    = ctx.get("btc_regime", {})
    brd     = ctx.get("breadth", {})
    vol     = ctx.get("volatility", {})
    dom     = ctx.get("btc_dom", {})
    usdt_d  = ctx.get("usdt_dominance", {})
    bias    = ctx.get("overall_bias", "NEUTRAL")

    bias_emoji = {"RISK_ON": "🟢", "RISK_OFF": "🔴", "NEUTRAL": "⚪"}.get(bias, "⚪")
    fg_emoji   = "😱" if fg.get("value", 50) <= 20 else \
                 "😨" if fg.get("value", 50) <= 35 else \
                 "😐" if fg.get("value", 50) <= 55 else \
                 "😀" if fg.get("value", 50) <= 75 else "🤑"
    btc_emoji  = "📈" if btcr.get("regime") == "BULLISH" else \
                 "📉" if btcr.get("regime") == "BEARISH" else "↔️"
    brd_emoji  = "🟢" if brd.get("regime") == "BULLISH" else \
                 "🔴" if brd.get("regime") in ("BEARISH","MIXED_BEAR") else "🟡"

    if compact:
        return (
            f"🌐 *Market:* {bias_emoji} {bias}  |  "
            f"{fg_emoji} F&G {fg.get('value', '?')}  |  "
            f"{btc_emoji} BTC {btcr.get('regime','?')}  |  "
            f"{brd_emoji} Breadth {brd.get('bullish_pct','?'):.0f}%↑"
        )

    usdt_trend_emoji = "📈" if usdt_d.get("trend") == "RISING" else \
                       "📉" if usdt_d.get("trend") == "FALLING" else "➡️"

    lines = [
        "─────── MARKET CONTEXT ───────",
        f"{bias_emoji} Overall Bias   : *{bias}*",
        f"{fg_emoji} Fear & Greed   : {fg.get('value','?')} — {fg.get('classification','?')}",
        f"{btc_emoji} BTC 4H Regime  : {btcr.get('regime','?')} "
        f"(EMA9={btcr.get('ema9',0):,.0f} / EMA21={btcr.get('ema21',0):,.0f})",
        f"{brd_emoji} Market Breadth : {brd.get('bullish_pct','?'):.0f}% bullish / "
        f"{brd.get('bearish_pct','?'):.0f}% bearish ({brd.get('coins_sampled','?')} coins)",
        f"⚡ Volatility     : {vol.get('regime','?')} (ATR ratio {vol.get('ratio',1):.2f}x)",
        f"₿ BTC Dominance  : {dom.get('dominance_pct','?'):.1f}% ({dom.get('trend','?')})",
        f"{usdt_trend_emoji} USDT Dominance : {usdt_d.get('usdt_dom_pct','?'):.2f}% ({usdt_d.get('trend','?')})",
    ]
    if ctx.get("reasons"):
        lines.append("")
        for r in ctx["reasons"][:4]:
            lines.append(f"  {r}")
    return "\n".join(lines)
