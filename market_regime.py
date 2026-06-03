"""
Market Regime & Advanced Candle Pattern Module
===============================================
Provides:
- calculate_adx()        → ADX trend strength
- calculate_bb_squeeze() → Bollinger Band squeeze / expansion
- detect_candle_patterns() → Full candle structure recognition
- detect_market_regime()   → RANGING / BULLISH_TREND / BEARISH_TREND / BB_SQUEEZE / BREAKOUT_UP / BREAKOUT_DOWN / VOLATILE
- detect_volume_coil()     → Volume compression + spike detection (pre-pump spring)
- detect_sudden_breakout() → Sudden range breakout with volume explosion (ALLO-type pump detection)
"""

import numpy as np


# ─────────────────────────────────────────────
# ADX INDICATOR
# ─────────────────────────────────────────────

def calculate_adx(candles: list, period: int = 14) -> dict:
    """
    Wilder's ADX, +DI, -DI.
    ADX > 25 = trending, ADX < 20 = ranging/weak.
    """
    empty = {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    if not candles or len(candles) < period * 2 + 5:
        return empty

    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]
    n = len(candles)

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)

        up_move   = highs[i]   - highs[i-1]
        down_move = lows[i-1]  - lows[i]
        plus_dms.append(up_move  if up_move  > down_move and up_move  > 0 else 0)
        minus_dms.append(down_move if down_move > up_move  and down_move > 0 else 0)

    def wilder_smooth(data, p):
        smoothed = [sum(data[:p])]
        for i in range(p, len(data)):
            smoothed.append(smoothed[-1] - smoothed[-1] / p + data[i])
        return smoothed

    atr_s  = wilder_smooth(trs,       period)
    pdi_s  = wilder_smooth(plus_dms,  period)
    mdi_s  = wilder_smooth(minus_dms, period)

    dxs, plus_dis, minus_dis = [], [], []
    for i in range(len(atr_s)):
        if atr_s[i] == 0:
            continue
        pdi = 100 * pdi_s[i] / atr_s[i]
        mdi = 100 * mdi_s[i] / atr_s[i]
        plus_dis.append(pdi)
        minus_dis.append(mdi)
        dm_sum = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / dm_sum if dm_sum > 0 else 0)

    if len(dxs) < period:
        return empty

    adx      = sum(dxs[-period:]) / period
    curr_pdi = plus_dis[-1]  if plus_dis  else 0
    curr_mdi = minus_dis[-1] if minus_dis else 0

    return {
        "adx":      round(adx, 2),
        "plus_di":  round(curr_pdi, 2),
        "minus_di": round(curr_mdi, 2),
    }


# ─────────────────────────────────────────────
# BOLLINGER BAND SQUEEZE
# ─────────────────────────────────────────────

def calculate_bb_squeeze(candles: list, period: int = 20, mult: float = 2.0) -> dict:
    """
    Bollinger Band squeeze detector.
    squeeze=True  → BB width in bottom 20th percentile → coiling before explosion.
    expanding=True → BB width growing fast → breakout in progress.
    """
    empty = {"squeeze": False, "width_pct": 50.0, "bb_width": 0.0,
             "expanding": False, "squeeze_bars": 0}
    if not candles or len(candles) < period + 15:
        return empty

    closes = [c["close"] for c in candles]

    widths = []
    for i in range(period, len(closes)):
        window = closes[i - period:i]
        ma  = sum(window) / period
        std = (sum((x - ma) ** 2 for x in window) / period) ** 0.5
        w   = (2 * mult * std) / ma * 100 if ma > 0 else 0
        widths.append(w)

    if not widths:
        return empty

    current_width = widths[-1]

    # Degenerate case: zero variance = maximum compression = squeeze
    if current_width == 0.0:
        return {
            "squeeze": True, "width_pct": 0.0, "bb_width": 0.0,
            "expanding": False, "squeeze_bars": min(10, len(widths)),
        }

    lookback = widths[-50:] if len(widths) >= 50 else widths
    sorted_w = sorted(lookback)

    # Percentile rank: what fraction of historical widths is STRICTLY greater?
    # This gives a "lower is tighter" reading — current_width at 0% = tightest ever.
    rank = sum(1 for w in sorted_w if w < current_width)
    width_percentile = (rank / max(len(sorted_w) - 1, 1)) * 100

    # squeeze threshold = 20th percentile
    pct20_val = sorted_w[max(0, len(sorted_w) // 5)]
    squeeze_bars = sum(1 for w in widths[-10:] if w <= pct20_val)

    expanding = len(widths) >= 3 and widths[-1] > widths[-2] > widths[-3]

    return {
        "squeeze":      width_percentile <= 20,
        "width_pct":    round(width_percentile, 1),
        "bb_width":     round(current_width, 2),
        "expanding":    expanding,
        "squeeze_bars": squeeze_bars,
    }


# ─────────────────────────────────────────────
# CANDLE PATTERN RECOGNITION
# ─────────────────────────────────────────────

def detect_candle_patterns(candles: list) -> dict:
    """
    Full candle structure recognition for scalping.

    Patterns detected (in priority order for scalping):
    1. BULLISH_ENGULFING / BEARISH_ENGULFING  — strongest reversal signal
    2. MORNING_STAR / EVENING_STAR            — 3-candle reversal
    3. THREE_WHITE_SOLDIERS / THREE_BLACK_CROWS — momentum continuation
    4. BULLISH_MARUBOZU / BEARISH_MARUBOZU   — pure directional pressure
    5. INSIDE_BAR                             — compression, breakout pending
    6. DOJI                                   — indecision at extreme
    """
    empty = {"pattern": "NONE", "direction": "NEUTRAL", "strength": 0,
             "detail": "", "patterns_found": []}

    if not candles or len(candles) < 3:
        return empty

    c0 = candles[-1]
    c1 = candles[-2]
    c2 = candles[-3] if len(candles) >= 3 else None

    def body(c):
        return abs(c["close"] - c["open"])

    def rng(c):
        return max(c["high"] - c["low"], 1e-10)

    def is_bull(c):
        return c["close"] > c["open"]

    def is_bear(c):
        return c["close"] < c["open"]

    def upper_wick(c):
        return c["high"] - max(c["close"], c["open"])

    def lower_wick(c):
        return min(c["close"], c["open"]) - c["low"]

    patterns = []

    # ── Bullish Engulfing ─────────────────────
    if is_bull(c0) and is_bear(c1):
        if c0["open"] <= c1["close"] and c0["close"] >= c1["open"]:
            ratio = body(c0) / body(c1) if body(c1) > 0 else 1
            patterns.append({
                "name": "BULLISH_ENGULFING",
                "direction": "BULLISH",
                "strength": min(100, int(60 + ratio * 15)),
                "detail": f"Bullish engulfing {ratio:.1f}x — buyers ambil kontrol penuh"
            })

    # ── Bearish Engulfing ─────────────────────
    if is_bear(c0) and is_bull(c1):
        if c0["open"] >= c1["close"] and c0["close"] <= c1["open"]:
            ratio = body(c0) / body(c1) if body(c1) > 0 else 1
            patterns.append({
                "name": "BEARISH_ENGULFING",
                "direction": "BEARISH",
                "strength": min(100, int(60 + ratio * 15)),
                "detail": f"Bearish engulfing {ratio:.1f}x — sellers ambil kontrol penuh"
            })

    # ── Morning Star (3-candle bullish reversal) ──
    if c2 is not None:
        if (is_bear(c2) and body(c1) < body(c2) * 0.5
                and is_bull(c0)
                and c0["close"] > (c2["open"] + c2["close"]) / 2):
            patterns.append({
                "name": "MORNING_STAR",
                "direction": "BULLISH",
                "strength": 80,
                "detail": "Morning Star — 3-candle reversal bullish terkonfirmasi"
            })

        # ── Evening Star (3-candle bearish reversal) ──
        if (is_bull(c2) and body(c1) < body(c2) * 0.5
                and is_bear(c0)
                and c0["close"] < (c2["open"] + c2["close"]) / 2):
            patterns.append({
                "name": "EVENING_STAR",
                "direction": "BEARISH",
                "strength": 80,
                "detail": "Evening Star — 3-candle reversal bearish terkonfirmasi"
            })

        # ── Three White Soldiers ──────────────
        if (is_bull(c0) and is_bull(c1) and is_bull(c2)
                and c0["close"] > c1["close"] > c2["close"]
                and c0["open"] > c1["open"] > c2["open"]
                and upper_wick(c0) / rng(c0) < 0.2):
            patterns.append({
                "name": "THREE_WHITE_SOLDIERS",
                "direction": "BULLISH",
                "strength": 85,
                "detail": "Three White Soldiers — strong bullish momentum, continuation"
            })

        # ── Three Black Crows ─────────────────
        if (is_bear(c0) and is_bear(c1) and is_bear(c2)
                and c0["close"] < c1["close"] < c2["close"]
                and c0["open"] < c1["open"] < c2["open"]
                and lower_wick(c0) / rng(c0) < 0.2):
            patterns.append({
                "name": "THREE_BLACK_CROWS",
                "direction": "BEARISH",
                "strength": 85,
                "detail": "Three Black Crows — strong bearish momentum, continuation"
            })

    # ── Bullish Marubozu ──────────────────────
    br0 = body(c0) / rng(c0)
    if is_bull(c0) and br0 >= 0.80:
        if upper_wick(c0) / rng(c0) < 0.10 and lower_wick(c0) / rng(c0) < 0.10:
            patterns.append({
                "name": "BULLISH_MARUBOZU",
                "direction": "BULLISH",
                "strength": int(br0 * 100),
                "detail": f"Bullish Marubozu ({br0*100:.0f}% body) — pure buyer pressure, no hesitation"
            })

    # ── Bearish Marubozu ──────────────────────
    if is_bear(c0) and br0 >= 0.80:
        if upper_wick(c0) / rng(c0) < 0.10 and lower_wick(c0) / rng(c0) < 0.10:
            patterns.append({
                "name": "BEARISH_MARUBOZU",
                "direction": "BEARISH",
                "strength": int(br0 * 100),
                "detail": f"Bearish Marubozu ({br0*100:.0f}% body) — pure seller pressure, no hesitation"
            })

    # ── Inside Bar ────────────────────────────
    if c0["high"] < c1["high"] and c0["low"] > c1["low"]:
        patterns.append({
            "name": "INSIDE_BAR",
            "direction": "NEUTRAL",
            "strength": 45,
            "detail": "Inside bar — kompresi, breakout imminent, tunggu direction confirm"
        })

    # ── Doji ─────────────────────────────────
    if br0 < 0.10:
        if lower_wick(c0) > upper_wick(c0) * 2:
            d_dir, d_detail = "BULLISH", "Dragonfly Doji — buyers rejected low, potential reversal UP"
        elif upper_wick(c0) > lower_wick(c0) * 2:
            d_dir, d_detail = "BEARISH", "Gravestone Doji — sellers rejected high, potential reversal DOWN"
        else:
            d_dir, d_detail = "NEUTRAL", "Doji — indecision, momentum exhausted, watch for breakout"
        patterns.append({"name": "DOJI", "direction": d_dir, "strength": 40, "detail": d_detail})

    if not patterns:
        return empty

    # Priority order: Engulfing > Star > Soldiers/Crows > Marubozu > Inside > Doji
    priority = {
        "BULLISH_ENGULFING": 10, "BEARISH_ENGULFING": 10,
        "MORNING_STAR": 9,       "EVENING_STAR": 9,
        "THREE_WHITE_SOLDIERS": 8, "THREE_BLACK_CROWS": 8,
        "BULLISH_MARUBOZU": 7,   "BEARISH_MARUBOZU": 7,
        "INSIDE_BAR": 4, "DOJI": 3,
    }
    patterns.sort(key=lambda p: (priority.get(p["name"], 0), p["strength"]), reverse=True)
    best = patterns[0]

    return {
        "pattern":       best["name"],
        "direction":     best["direction"],
        "strength":      best["strength"],
        "detail":        best["detail"],
        "patterns_found": [p["name"] for p in patterns],
    }


# ─────────────────────────────────────────────
# MARKET REGIME CLASSIFIER
# ─────────────────────────────────────────────

def detect_market_regime(candles: list) -> dict:
    """
    Classify market regime from a single timeframe's candles.
    Uses ADX + BB squeeze + ATR ratio.

    Regimes:
    - BULLISH_TREND:  ADX > 25, +DI > -DI, price above EMA21
    - BEARISH_TREND:  ADX > 25, -DI > +DI, price below EMA21
    - RANGING:        ADX < 20, price oscillating, ATR normal
    - BB_SQUEEZE:     BB width in bottom 20% — compression, coiling for explosion
    - BREAKOUT_UP:    BB expanding + price breaks N-bar high (range escape UP)
    - BREAKOUT_DOWN:  BB expanding + price breaks N-bar low  (range escape DOWN)
    - VOLATILE:       ATR >> average, no clear structure
    """
    empty = {
        "regime": "UNKNOWN", "adx": 0.0, "squeeze": False,
        "detail": "", "is_trending": False, "is_ranging": False,
        "breakout_confirmed": False, "breakout_direction": "NONE",
    }
    if not candles or len(candles) < 30:
        return empty

    adx_data = calculate_adx(candles)
    adx      = adx_data["adx"]
    plus_di  = adx_data["plus_di"]
    minus_di = adx_data["minus_di"]

    bb_data  = calculate_bb_squeeze(candles)
    squeeze  = bb_data["squeeze"]
    expanding = bb_data["expanding"]

    closes = [c["close"] for c in candles]
    # ATR ratio: recent 5 vs prior 15
    recent_ranges = [abs(c["high"] - c["low"]) for c in candles[-5:]]
    prior_ranges  = [abs(c["high"] - c["low"]) for c in candles[-20:-5]]
    curr_atr = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0
    base_atr = sum(prior_ranges) / len(prior_ranges) if prior_ranges else 1
    atr_ratio = curr_atr / base_atr if base_atr > 0 else 1.0

    # EMA21 for trend filter — EMA sungguhan (dulu ini SMA meski dinamai EMA,
    # sehingga klasifikasi trend gampang flip di sekitar MA).
    if len(closes) >= 21:
        k = 2 / (21 + 1)
        ema21 = sum(closes[:21]) / 21          # seed dengan SMA awal
        for px in closes[21:]:
            ema21 = px * k + ema21 * (1 - k)
    else:
        ema21 = closes[-1]
    price_above_ema = closes[-1] > ema21

    result = {
        "regime": "UNKNOWN", "adx": adx, "plus_di": plus_di, "minus_di": minus_di,
        "squeeze": squeeze, "bb_width_pct": bb_data["width_pct"],
        "bb_expanding": expanding, "atr_ratio": round(atr_ratio, 2),
        "detail": "", "is_trending": False, "is_ranging": False,
        "breakout_confirmed": False, "breakout_direction": "NONE",
    }

    # ── Priority 1: BB Squeeze (check before ADX) ──
    if squeeze and not expanding:
        result["regime"]     = "BB_SQUEEZE"
        result["is_ranging"] = True
        bars = bb_data.get("squeeze_bars", 0)
        result["detail"] = (f"BB Squeeze aktif ({bars} bar) — harga coiling, "
                            f"breakout imminent. Width percentile {bb_data['width_pct']:.0f}%")
        return result

    # ── Priority 2: Breakout (BB expanding + price breaks range) ──
    if expanding and atr_ratio > 1.3:
        lookback_hi = max(closes[-16:-1]) if len(closes) >= 16 else closes[-1]
        lookback_lo = min(closes[-16:-1]) if len(closes) >= 16 else closes[-1]
        if closes[-1] > lookback_hi:
            result["regime"]               = "BREAKOUT_UP"
            result["breakout_confirmed"]   = True
            result["breakout_direction"]   = "UP"
            result["detail"] = (f"BREAKOUT UP: price menembus high 15-bar, "
                                f"BB expanding, ATR {atr_ratio:.1f}x baseline")
            return result
        elif closes[-1] < lookback_lo:
            result["regime"]               = "BREAKOUT_DOWN"
            result["breakout_confirmed"]   = True
            result["breakout_direction"]   = "DOWN"
            result["detail"] = (f"BREAKOUT DOWN: price menembus low 15-bar, "
                                f"BB expanding, ATR {atr_ratio:.1f}x baseline")
            return result

    # ── Priority 3: Trending (ADX > 25) ──
    if adx >= 25:
        result["is_trending"] = True
        if plus_di > minus_di and price_above_ema:
            result["regime"] = "BULLISH_TREND"
            result["detail"] = (f"Bullish Trend: ADX={adx:.0f}, "
                                f"+DI={plus_di:.0f} > -DI={minus_di:.0f}, price > EMA21")
        elif minus_di > plus_di and not price_above_ema:
            result["regime"] = "BEARISH_TREND"
            result["detail"] = (f"Bearish Trend: ADX={adx:.0f}, "
                                f"-DI={minus_di:.0f} > +DI={plus_di:.0f}, price < EMA21")
        else:
            result["regime"] = "WEAK_TREND"
            result["detail"] = f"Weak trend: ADX={adx:.0f}, DI conflict atau EMA mismatch"
        return result

    # ── Priority 4: Volatile (high ATR, no structure) ──
    if atr_ratio > 2.0:
        result["regime"] = "VOLATILE"
        result["detail"] = f"Volatile: ATR {atr_ratio:.1f}x normal, harga tidak stabil"
        return result

    # ── Default: Ranging ──
    result["regime"]     = "RANGING"
    result["is_ranging"] = True
    result["detail"]     = (f"Ranging: ADX={adx:.0f} (lemah), "
                            f"harga oscillating tanpa trend jelas")
    return result


# ─────────────────────────────────────────────
# VOLUME COIL (SPRING-LOAD DETECTION)
# ─────────────────────────────────────────────

def detect_volume_coil(candles: list, lookback: int = 10) -> dict:
    """
    Deteksi volume coil: declining volume over multiple candles then sudden spike.
    Classic signature sebelum "spring release" — sudden pump setelah akumulasi.
    """
    empty = {"coiling": False, "spike_detected": False,
             "compression_bars": 0, "vol_ratio": 1.0, "detail": ""}

    if not candles or len(candles) < lookback + 5:
        return empty

    vols    = [c["volume"] for c in candles]
    recent  = vols[-(lookback + 1):-1]
    curr_v  = vols[-1]

    if not recent or sum(recent) == 0:
        return empty

    declining = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i-1])
    coiling   = declining >= lookback * 0.55

    avg_vol   = sum(recent) / len(recent)
    vol_ratio = curr_v / avg_vol if avg_vol > 0 else 1.0
    spike     = vol_ratio >= 2.5

    detail = ""
    if coiling and spike:
        detail = (f"Volume coil RELEASED: spike {vol_ratio:.1f}x setelah "
                  f"{declining} bar declining — spring terlepas!")
    elif coiling:
        detail = (f"Volume coiling: {declining}/{lookback} bar declining "
                  f"— spring loading, akumulasi silent")

    return {
        "coiling":           coiling,
        "spike_detected":    spike,
        "compression_bars":  declining,
        "vol_ratio":         round(vol_ratio, 2),
        "detail":            detail,
    }


# ─────────────────────────────────────────────
# SUDDEN BREAKOUT DETECTOR (ALLO-TYPE PUMP)
# ─────────────────────────────────────────────

def detect_sudden_breakout(candles: list, vol_threshold: float = 3.0,
                           range_lookback: int = 12) -> dict:
    """
    Deteksi sudden breakout dari consolidation — menangkap pump tiba-tiba seperti ALLO.

    Criteria:
    1. Harga sebelumnya konsolidasi dalam range sempit (ATR rendah vs baseline)
    2. Candle terbaru volume meledak >= vol_threshold x baseline
    3. Price close di atas high dari X candle sebelumnya (range breakout)

    Returns sudden_breakout=True kalau ketiga kriteria terpenuhi.
    """
    empty = {
        "sudden_breakout": False, "direction": "NONE",
        "vol_spike": 1.0, "range_break_pct": 0.0, "detail": "",
        "was_consolidating": False,
    }

    if not candles or len(candles) < range_lookback + 5:
        return empty

    c_curr = candles[-1]
    prev   = candles[-(range_lookback + 1):-1]

    if not prev:
        return empty

    vols_prev  = [c["volume"] for c in prev]
    avg_vol    = sum(vols_prev) / len(vols_prev) if vols_prev else 1
    curr_vol   = c_curr["volume"]
    vol_spike  = curr_vol / avg_vol if avg_vol > 0 else 1.0

    prev_high  = max(c["high"]  for c in prev)
    prev_low   = min(c["low"]   for c in prev)
    prev_range = (prev_high - prev_low) / ((prev_high + prev_low) / 2) * 100

    # ATR compression: was range narrow vs its own average?
    prev_atrs       = [abs(c["high"] - c["low"]) for c in prev]
    older_atrs      = [abs(c["high"] - c["low"]) for c in candles[-(range_lookback*2):-range_lookback]]
    avg_prev_atr    = sum(prev_atrs)  / len(prev_atrs)  if prev_atrs  else 1
    avg_older_atr   = sum(older_atrs) / len(older_atrs) if older_atrs else 1
    was_consolidating = avg_prev_atr < avg_older_atr * 0.75  # recent range 25%+ narrower

    breakout_up   = c_curr["close"] > prev_high
    breakout_down = c_curr["close"] < prev_low

    if vol_spike >= vol_threshold and (breakout_up or breakout_down):
        direction     = "UP" if breakout_up else "DOWN"
        break_pct     = abs(c_curr["close"] - (prev_high if breakout_up else prev_low))
        break_pct_rel = break_pct / prev_high * 100 if prev_high > 0 else 0
        detail = (
            f"SUDDEN BREAKOUT {'UP' if breakout_up else 'DOWN'}! "
            f"Vol spike {vol_spike:.1f}x, range break +{break_pct_rel:.1f}% "
            f"{'(was consolidating)' if was_consolidating else ''}"
        )
        return {
            "sudden_breakout":   True,
            "direction":         direction,
            "vol_spike":         round(vol_spike, 2),
            "range_break_pct":   round(break_pct_rel, 2),
            "detail":            detail,
            "was_consolidating": was_consolidating,
        }

    partial_detail = ""
    if vol_spike >= vol_threshold * 0.7 and was_consolidating:
        partial_detail = f"Volume building {vol_spike:.1f}x, consolidating — watch for breakout"

    return {
        "sudden_breakout":   False,
        "direction":         "NONE",
        "vol_spike":         round(vol_spike, 2),
        "range_break_pct":   0.0,
        "detail":            partial_detail,
        "was_consolidating": was_consolidating,
    }
