#!/usr/bin/env python3
"""
BACKTEST ENGINE v2.0
Freqtrade-inspired backtesting untuk Crypto Screening Bot v13.

Multi-indicator confirmation layer (di luar SMC):
- EMA Trend Filter (9/21/50 EMA)
- MACD Signal Cross
- Stoch RSI Overbought/Oversold
- Bollinger Band Squeeze + Breakout
- ADX Trend Strength (>25 = trending)
- VWAP Bias (price above/below VWAP)
- Volume Profile (relative volume vs avg)
"""

import os, json, time, logging, requests, numpy as np
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("backtest")

BINANCE_BASE    = "https://api.binance.com/api/v3"
BINANCE_FUTURES = "https://fapi.binance.com"
TAKER_FEE       = 0.001
SLIPPAGE        = 0.0005
DEFAULT_DAYS    = 30
DEFAULT_STAKE   = 100.0
BT_RESULTS_FILE = "backtest_results.json"

# ── Multi-exchange support ────────────────────
try:
    from exchange_resolver import (
        resolve_symbol       as _exc_resolve,
        get_ohlcv            as _exc_ohlcv,
        format_not_found_message as _exc_not_found,
    )
    _EXCHANGE_RESOLVER = True
except ImportError:
    _EXCHANGE_RESOLVER = False
    log.warning("exchange_resolver.py tidak ditemukan — backtest hanya support Binance")

STRATEGY_CONFIG = {
    "prepump": {
        "signal_tf": "1h", "trade_tf": "15m", "min_score": 70,
        "tp_pct": 0.05, "sl_pct": 0.025, "max_hold_candles": 48,
        "description": "Pre-Pump Detector (Funding Squeeze + Momentum + OI)"
    },
    "predump": {
        "signal_tf": "1h", "trade_tf": "15m", "min_score": 70,
        "tp_pct": 0.05, "sl_pct": 0.025, "max_hold_candles": 48,
        "description": "Pre-Dump Detector (Long Squeeze + Bearish Momentum + OI)"
    },
    "scalp": {
        "signal_tf": "15m", "trade_tf": "5m", "min_score": 50,
        "tp_pct": 0.015, "sl_pct": 0.008, "max_hold_candles": 24,
        "description": "Scalp Setup (Liquidity Sweep + Rejection + OB/FVG)"
    },
    "swing": {
        "signal_tf": "4h", "trade_tf": "1h", "min_score": 50,
        "tp_pct": 0.055, "sl_pct": 0.025, "max_hold_candles": 24,
        "description": "Intraday Swing (4H HTF + 1H trigger + Liq Sweep)"
    },
    "combined": {
        "signal_tf": "1h", "trade_tf": "15m", "min_score": 50,
        "tp_pct": 0.04, "sl_pct": 0.02, "max_hold_candles": 48,
        "description": "Combined (best signal dari semua strategy)"
    }
}


class LocalTrade:
    """In-memory trade. Mirip Freqtrade LocalTrade."""
    def __init__(self, symbol, direction, entry_price, tp, sl, open_time,
                 stake_usdt=DEFAULT_STAKE, entry_reason="", score=0):
        self.symbol       = symbol
        self.direction    = direction
        self.entry_price  = entry_price
        self.tp, self.sl  = tp, sl
        self.open_time    = open_time
        self.close_time   = None
        self.close_price  = None
        self.exit_reason  = None
        self.stake_usdt   = stake_usdt
        self.entry_reason = entry_reason
        self.score        = score
        self.is_open      = True
        self.pnl_pct = self.pnl_usdt = self.hold_hours = 0.0
        cost = TAKER_FEE + SLIPPAGE
        self.actual_entry = entry_price * (1 + cost) if direction == "LONG" \
                            else entry_price * (1 - cost)

    def close(self, close_price, close_time, reason):
        self.is_open = False
        self.close_time, self.exit_reason = close_time, reason
        cost = TAKER_FEE + SLIPPAGE
        if self.direction == "LONG":
            self.close_price = close_price * (1 - cost)
            self.pnl_pct = (self.close_price - self.actual_entry) / self.actual_entry
        else:
            self.close_price = close_price * (1 + cost)
            self.pnl_pct = (self.actual_entry - self.close_price) / self.actual_entry
        self.pnl_usdt   = self.stake_usdt * self.pnl_pct
        self.hold_hours = (close_time - self.open_time).total_seconds() / 3600

    def to_dict(self):
        d = {
            "symbol": self.symbol, "direction": self.direction,
            "entry": round(self.entry_price, 8),
            "close": round(self.close_price, 8) if self.close_price else None,
            "tp": round(self.tp, 8), "sl": round(self.sl, 8),
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "exit_reason": self.exit_reason,
            "pnl_pct": round(self.pnl_pct * 100, 3),
            "pnl_usdt": round(self.pnl_usdt, 4),
            "hold_hours": round(self.hold_hours, 2),
            "score": self.score, "entry_reason": self.entry_reason,
        }
        if hasattr(self, "multi_ind"):
            d["multi_ind_score"] = self.multi_ind.get("score")
            d["multi_ind_grade"] = self.multi_ind.get("grade")
        return d


def download_ohlcv(symbol: str, interval: str, days: int = 30,
                   exchange: str = "binance_futures") -> list:
    """
    Download OHLCV dari exchange yang benar dengan pagination.
    Mirip freqtrade download-data — support Binance Futures, Bybit, OKX, Gate.io.
    """
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000

    # Binance Futures — full pagination support
    if exchange in ("binance_futures", "binance_spot"):
        base = BINANCE_FUTURES + "/fapi/v1/klines" if exchange == "binance_futures" \
               else BINANCE_BASE + "/klines"
        all_candles = []
        cur = start_ms
        log.info(f"Downloading {symbol} {interval} {days}d from {exchange}...")
        while cur < end_ms:
            try:
                r = requests.get(base,
                    params={"symbol": symbol, "interval": interval,
                            "startTime": cur, "endTime": end_ms, "limit": 1000}, timeout=15)
                if r.status_code != 200: break
                batch = r.json()
                if not batch: break
                for c in batch:
                    candle = {"time": int(c[0]), "open": float(c[1]),
                              "high": float(c[2]), "low": float(c[3]),
                              "close": float(c[4]), "volume": float(c[5])}
                    if len(c) > 9 and c[9] not in (None, ""):
                        try:
                            candle["taker_buy_vol"] = float(c[9])
                        except (ValueError, TypeError):
                            pass
                    all_candles.append(candle)
                cur = int(batch[-1][0]) + 1
                if len(batch) < 1000: break
                time.sleep(0.1)
            except Exception as e:
                log.warning(f"DL error {symbol}: {e}"); break
        log.info(f"Downloaded {len(all_candles)} candles {symbol} {interval}")
        return all_candles

    # Other exchanges — fetch in batches via exchange_resolver
    if _EXCHANGE_RESOLVER:
        # Fetch max candles per request, paginate manually
        all_candles = []
        # Most exchanges max 200 candles per request
        # Estimate needed candles based on interval
        tf_mins = {"1m":1,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440}
        mins_per_candle = tf_mins.get(interval, 60)
        total_needed    = (days * 24 * 60) // mins_per_candle
        pages_needed    = max(1, (total_needed + 199) // 200)
        log.info(f"Downloading {symbol} {interval} {days}d from {exchange} (~{total_needed} candles, {pages_needed} pages)...")

        for _ in range(min(pages_needed, 20)):  # max 20 pages
            batch = _exc_ohlcv(symbol, interval, exchange, limit=200)
            if not batch:
                break
            # Deduplicate and merge
            existing_times = {c["time"] for c in all_candles}
            new_candles    = [c for c in batch if c["time"] not in existing_times
                              and c["time"] >= start_ms]
            all_candles.extend(new_candles)
            if not new_candles:
                break
            time.sleep(0.2)

        all_candles.sort(key=lambda x: x["time"])
        log.info(f"Downloaded {len(all_candles)} candles {symbol} {interval} from {exchange}")
        return all_candles

    log.warning(f"Exchange {exchange} not supported and resolver unavailable")
    return []


def resample_to_tf(candles: list, target_tf: str) -> list:
    """Resample ke TF lebih tinggi tanpa re-download."""
    tf_ms = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
    ms = tf_ms.get(target_tf)
    if not ms or not candles: return candles
    res, grp, bkt = [], [], (candles[0]["time"] // ms) * ms
    for c in candles:
        b = (c["time"] // ms) * ms
        if b != bkt:
            if grp:
                res.append({"time": bkt, "open": grp[0]["open"],
                    "high": max(x["high"] for x in grp), "low": min(x["low"] for x in grp),
                    "close": grp[-1]["close"], "volume": sum(x["volume"] for x in grp)})
            grp, bkt = [c], b
        else: grp.append(c)
    if grp:
        res.append({"time": bkt, "open": grp[0]["open"],
            "high": max(x["high"] for x in grp), "low": min(x["low"] for x in grp),
            "close": grp[-1]["close"], "volume": sum(x["volume"] for x in grp)})
    return res


def _build_tf_data(candles: list, interval: str) -> dict:
    """Build tf_data dengan logic IDENTIK dengan live bot. Anti-lookahead."""
    try:
        from crypto_screening_bot_v13 import (
            detect_market_structure, detect_fvg, detect_order_blocks,
            detect_candle_rejection, detect_volume_anomaly,
            calculate_rsi, calculate_atr,
            detect_equal_highs_lows, detect_liquidity_sweep, detect_trendline,
            detect_money_flow,
        )
    except ImportError:
        try:
            from crypto_screening_bot_v11 import (
                detect_market_structure, detect_fvg, detect_order_blocks,
                detect_candle_rejection, detect_volume_anomaly,
                calculate_rsi, calculate_atr,
                detect_equal_highs_lows, detect_liquidity_sweep, detect_trendline,
            )
            detect_money_flow = None
        except ImportError:
            log.error("Cannot import bot functions from v13 or v11")
            return {"error": True, "interval": interval}

    if not candles or len(candles) < 20:
        return {"error": True, "interval": interval}

    structure = detect_market_structure(candles)
    result = {
        "interval": interval, "error": False, "price": candles[-1]["close"],
        "candles": candles, "structure": structure,
        "fvg": detect_fvg(candles), "order_blocks": detect_order_blocks(candles),
        "rejection": detect_candle_rejection(candles),
        "volume_anomaly": detect_volume_anomaly(candles),
        "rsi": calculate_rsi(candles), "atr": calculate_atr(candles),
        "liquidity": detect_equal_highs_lows(candles),
        "sweep": detect_liquidity_sweep(candles, structure),
        "trendline_sup": detect_trendline(candles, "lows"),
        "trendline_res": detect_trendline(candles, "highs"),
    }
    if detect_money_flow:
        result["money_flow"] = detect_money_flow(candles)
    return result


# ─────────────────────────────────────────────
# MULTI-INDICATOR CONFIRMATION LAYER
# Freqtrade community strategies: EMA + MACD + StochRSI + BB + ADX + VWAP
# ─────────────────────────────────────────────

def _calc_ema(closes: list, period: int) -> list:
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    emas = [None] * (period - 1)
    emas.append(sum(closes[:period]) / period)
    for c in closes[period:]:
        emas.append(c * k + emas[-1] * (1 - k))
    return emas


def _calc_macd(closes: list, fast=12, slow=26, signal=9) -> dict:
    if len(closes) < slow + signal:
        return {"macd": None, "signal": None, "hist": None, "cross_bull": False, "cross_bear": False}
    ema_fast  = _calc_ema(closes, fast)
    ema_slow  = _calc_ema(closes, slow)
    macd_line = [(f-s) if f is not None and s is not None else None for f,s in zip(ema_fast,ema_slow)]
    valid     = [x for x in macd_line if x is not None]
    if len(valid) < signal:
        return {"macd": None, "signal": None, "hist": None, "cross_bull": False, "cross_bear": False}
    sig_ema    = _calc_ema(valid, signal)
    m_last, m_prev = valid[-1], valid[-2] if len(valid)>1 else valid[-1]
    s_last = sig_ema[-1] if sig_ema[-1] is not None else 0
    s_prev = sig_ema[-2] if len(sig_ema)>1 and sig_ema[-2] is not None else s_last
    return {"macd": round(m_last,6), "signal": round(s_last,6), "hist": round(m_last-s_last,6),
            "cross_bull": m_prev < s_prev and m_last >= s_last,
            "cross_bear": m_prev > s_prev and m_last <= s_last}


def _calc_stoch_rsi(closes: list, rsi_period=14, stoch_period=14, k=3, d=3) -> dict:
    empty = {"k": None, "d": None, "oversold": False, "overbought": False}
    if len(closes) < rsi_period + stoch_period + k + d:
        return empty
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1,len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1,len(closes))]
    if len(gains) < rsi_period: return empty
    ag = np.mean(gains[:rsi_period]); al = np.mean(losses[:rsi_period])
    rsi_vals = []
    for i in range(rsi_period, len(gains)):
        ag = (ag*(rsi_period-1)+gains[i])/rsi_period
        al = (al*(rsi_period-1)+losses[i])/rsi_period
        rs = ag/al if al>0 else 100
        rsi_vals.append(100 - 100/(1+rs))
    if len(rsi_vals) < stoch_period: return empty
    stoch_k = []
    for i in range(stoch_period-1, len(rsi_vals)):
        w = rsi_vals[i-stoch_period+1:i+1]
        lo,hi = min(w),max(w)
        stoch_k.append(100*(rsi_vals[i]-lo)/(hi-lo) if hi!=lo else 50)
    def _sma(a,n): return [sum(a[i:i+n])/n for i in range(len(a)-n+1)]
    k_s = _sma(stoch_k, k) if len(stoch_k)>=k else stoch_k
    d_s = _sma(k_s, d)     if len(k_s)>=d    else k_s
    kv  = k_s[-1] if k_s else None
    dv  = d_s[-1] if d_s else None
    return {"k": round(kv,2) if kv else None, "d": round(dv,2) if dv else None,
            "oversold": kv is not None and kv<20, "overbought": kv is not None and kv>80}


def _calc_bbands(closes: list, period=20, mult=2.0) -> dict:
    empty = {"upper":None,"mid":None,"lower":None,"squeeze":False,"breakout_up":False,"breakout_down":False,"bw_pct":None}
    if len(closes) < period: return empty
    w   = closes[-period:]
    mid = float(np.mean(w)); std = float(np.std(w))
    upper = mid + mult*std; lower = mid - mult*std
    bw  = (upper-lower)/mid*100
    p   = closes[-1]
    return {"upper": round(upper,8), "mid": round(mid,8), "lower": round(lower,8),
            "bw_pct": round(bw,3), "squeeze": bw<4.0,
            "breakout_up": p>upper, "breakout_down": p<lower}


def _calc_adx(candles: list, period=14) -> dict:
    empty = {"adx": None, "trending": False, "strong_trend": False}
    if len(candles) < period+2: return empty
    hi = [c["high"] for c in candles]; lo = [c["low"] for c in candles]; cl = [c["close"] for c in candles]
    tr_l, pdm_l, ndm_l = [], [], []
    for i in range(1,len(candles)):
        tr   = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
        up   = hi[i]-hi[i-1]; dn = lo[i-1]-lo[i]
        pdm_l.append(max(up,0) if up>dn else 0)
        ndm_l.append(max(dn,0) if dn>up else 0)
        tr_l.append(tr)
    def _smooth(lst,n):
        s=sum(lst[:n]); res=[s]
        for v in lst[n:]: s=s-s/n+v; res.append(s)
        return res
    atr_s=_smooth(tr_l,period); pdm_s=_smooth(pdm_l,period); ndm_s=_smooth(ndm_l,period)
    dx=[]
    for a,p,n in zip(atr_s,pdm_s,ndm_s):
        pdi=100*p/a if a>0 else 0; ndi=100*n/a if a>0 else 0
        dx.append(100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi)>0 else 0)
    if len(dx)<period: return empty
    adx = float(np.mean(dx[-period:]))
    return {"adx": round(adx,2), "trending": adx>25, "strong_trend": adx>40}


def _calc_vwap(candles: list) -> dict:
    if not candles: return {"vwap":None,"above":False,"below":False}
    sess   = candles[-24:] if len(candles)>=24 else candles
    cum_tv = sum(((c["high"]+c["low"]+c["close"])/3)*c["volume"] for c in sess)
    cum_v  = sum(c["volume"] for c in sess)
    vwap   = cum_tv/cum_v if cum_v>0 else candles[-1]["close"]
    price  = candles[-1]["close"]
    return {"vwap": round(vwap,8), "above": price>vwap*1.001, "below": price<vwap*0.999}


def _multi_indicator_check(candles_1h: list, candles_15m: list, direction: str) -> dict:
    """
    Multi-indicator confirmation layer (Freqtrade-inspired).
    Score 0-100. Grade: STRONG(>=70) / MODERATE(>=40) / WEAK(<40)

    Weights:
    - EMA trend stack (9/21/50) : 20pts
    - MACD cross/bias           : 20pts
    - RSI 15M OB/OS             : 20pts  (replaced StochRSI — 54% win rate vs 51%)
    - Bollinger Band touch      : 15pts
    - ADX trend strength        : 15pts
    - VWAP bias                 : 10pts
    """
    # calculate_rsi di-import lokal (sama pola dengan _build_tf_data) untuk
    # hindari circular import backtest_engine <-> crypto_screening_bot_v13.
    try:
        from crypto_screening_bot_v13 import calculate_rsi
    except ImportError:
        try:
            from crypto_screening_bot_v11 import calculate_rsi
        except ImportError:
            calculate_rsi = None

    closes_1h  = [c["close"] for c in candles_1h]
    closes_15m = [c["close"] for c in candles_15m]
    score = 0; detail = {}; reasons = []

    # EMA Trend
    e9 = _calc_ema(closes_1h, 9)[-1]
    e21= _calc_ema(closes_1h, 21)[-1]
    e50= _calc_ema(closes_1h, 50)[-1]
    if all(x is not None for x in [e9,e21,e50]):
        if direction=="LONG"  and e9>e21>e50: score+=20; reasons.append("📈 EMA Bull Stack"); detail["ema"]="BULL"
        elif direction=="SHORT" and e9<e21<e50: score+=20; reasons.append("📉 EMA Bear Stack"); detail["ema"]="BEAR"
        elif (direction=="LONG" and e9>e21) or (direction=="SHORT" and e9<e21):
            score+=10; reasons.append("⚠️ EMA partial"); detail["ema"]="PARTIAL"
        else: detail["ema"]="AGAINST"
    else: detail["ema"]="N/A"

    # MACD
    macd = _calc_macd(closes_1h)
    detail["macd"] = {"val": macd["macd"], "sig": macd["signal"], "hist": macd["hist"]}
    if macd["macd"] is not None:
        if direction=="LONG":
            if macd["cross_bull"]: score+=20; reasons.append("✅ MACD Bull Cross")
            elif macd["macd"]>macd["signal"]: score+=10; reasons.append("🟡 MACD > Signal")
        else:
            if macd["cross_bear"]: score+=20; reasons.append("✅ MACD Bear Cross")
            elif macd["macd"]<macd["signal"]: score+=10; reasons.append("🟡 MACD < Signal")

    # RSI 15M (replaced StochRSI — research: RSI 54% win rate > StochRSI 51%, less noise)
    rsi_15m = calculate_rsi(candles_15m) if calculate_rsi else None
    detail["rsi_15m"] = rsi_15m
    if rsi_15m is not None:
        if direction == "LONG":
            if rsi_15m <= 35:   score += 20; reasons.append(f"✅ RSI 15M oversold ({rsi_15m:.0f})")
            elif rsi_15m <= 50: score += 8;  reasons.append(f"🟡 RSI 15M building ({rsi_15m:.0f})")
        else:
            if rsi_15m >= 65:   score += 20; reasons.append(f"✅ RSI 15M overbought ({rsi_15m:.0f})")
            elif rsi_15m >= 50: score += 8;  reasons.append(f"🟡 RSI 15M fading ({rsi_15m:.0f})")

    # Bollinger Bands (1H)
    bb = _calc_bbands(closes_1h)
    detail["bb"] = {"bw": bb["bw_pct"], "squeeze": bb["squeeze"]}
    if bb["mid"] is not None:
        if direction=="LONG":
            if bb["breakout_down"]: score+=15; reasons.append("✅ BB Lower break")
            elif bb["squeeze"]:     score+=8;  reasons.append("🟡 BB Squeeze")
        else:
            if bb["breakout_up"]:  score+=15; reasons.append("✅ BB Upper break")
            elif bb["squeeze"]:    score+=8;  reasons.append("🟡 BB Squeeze")

    # ADX (1H)
    adx_d = _calc_adx(candles_1h)
    detail["adx"] = adx_d["adx"]
    if adx_d["adx"] is not None:
        if adx_d["strong_trend"]: score+=15; reasons.append(f"✅ ADX Strong ({adx_d['adx']:.0f})")
        elif adx_d["trending"]:   score+=8;  reasons.append(f"🟡 ADX Trending ({adx_d['adx']:.0f})")

    # VWAP (1H)
    vwap_d = _calc_vwap(candles_1h)
    detail["vwap"] = vwap_d["vwap"]
    if vwap_d["vwap"] is not None:
        if direction=="LONG"  and vwap_d["above"]: score+=10; reasons.append("✅ Price > VWAP")
        elif direction=="SHORT" and vwap_d["below"]: score+=10; reasons.append("✅ Price < VWAP")

    score = min(score, 100)
    return {"score": score, "reasons": reasons, "detail": detail,
            "grade": "STRONG" if score>=70 else "MODERATE" if score>=40 else "WEAK"}


def _mock_oi(hist_1h: list) -> dict:
    if not hist_1h or len(hist_1h) < 6:
        return {"oi": None, "oi_change_pct": 0, "ls_ratio": 1.0,
                "ls_bias": "BALANCED", "funding_rate": 0.0}
    p_now = hist_1h[-1]["close"]
    p_6h  = hist_1h[-min(6, len(hist_1h))]["close"]
    p_1h  = hist_1h[-min(2, len(hist_1h))]["close"]
    chg_6 = (p_now - p_6h) / p_6h if p_6h > 0 else 0
    chg_1 = (p_now - p_1h) / p_1h if p_1h > 0 else 0
    funding = max(-0.05, min(0.05, chg_6 * 0.1))
    vols   = [c["volume"] for c in hist_1h[-20:]]
    v_avg  = np.mean(vols[:-1]) if len(vols) > 1 else vols[0]
    v_now  = vols[-1]
    oi_p   = ((v_now / v_avg) - 1) * 5 if v_avg > 0 else 0
    if chg_1 > 0.005:   ls_bias, ls_r = "LONG_HEAVY", min(2.5, 1.3 + chg_1 * 10)
    elif chg_1 < -0.005: ls_bias, ls_r = "SHORT_HEAVY", max(0.3, 0.7 + chg_1 * 10)
    else:                ls_bias, ls_r = "BALANCED", 1.0
    return {"oi": None, "oi_change_pct": round(oi_p, 2), "ls_ratio": round(ls_r, 2),
            "ls_bias": ls_bias, "funding_rate": round(funding * 100, 4)}


def _run_signal(strategy: str, symbol: str, h4: list, h1: list, h15: list, oi: dict,
                multi_ind_filter: bool = True) -> Optional[dict]:
    """
    Jalankan satu strategy pada satu bar. Return signal atau None.
    multi_ind_filter=True → hanya return signal jika multi-indicator grade MODERATE/STRONG.
    """
    try:
        from crypto_screening_bot_v13 import (
            detect_prepump, detect_predump, detect_scalp_setup, detect_swing_setup)
    except ImportError:
        try:
            from crypto_screening_bot_v11 import (
                detect_prepump, detect_predump, detect_scalp_setup, detect_swing_setup)
        except ImportError:
            return None

    tf4  = _build_tf_data(h4,  "4h")
    tf1  = _build_tf_data(h1,  "1h")
    tf15 = _build_tf_data(h15, "15m")
    if tf4.get("error") or tf1.get("error"): return None

    cfg = STRATEGY_CONFIG.get(strategy, STRATEGY_CONFIG["scalp"])
    sig = None

    if strategy == "prepump":
        s = detect_prepump(symbol, tf1, tf4, oi)
        if s["total_score"] >= cfg["min_score"]:
            sig = {"score": s["total_score"], "direction": "LONG", "label": s["label"]}
    elif strategy == "predump":
        s = detect_predump(symbol, tf1, tf4, oi)
        if s["total_score"] >= cfg["min_score"]:
            sig = {"score": s["total_score"], "direction": "SHORT", "label": s["label"]}
    elif strategy == "scalp":
        s = detect_scalp_setup(symbol, tf15, tf1, tf4, oi)
        if s["score"] >= cfg["min_score"] and s["direction"] != "NONE":
            sig = {"score": s["score"], "direction": s["direction"], "label": s["label"]}
    elif strategy == "swing":
        eql = tf1.get("liquidity", {})
        s = detect_swing_setup(symbol, tf4, tf1, tf15, oi, eql)
        if s["score"] >= cfg["min_score"] and s["direction"] != "NONE":
            sig = {"score": s["score"], "direction": s["direction"], "label": s["label"]}
    elif strategy == "combined":
        best, best_score = None, 0
        for st in ["prepump", "predump", "scalp", "swing"]:
            r = _run_signal(st, symbol, h4, h1, h15, oi, multi_ind_filter=False)
            if r and r["score"] > best_score:
                best, best_score = r, r["score"]
                best["source"] = st
        sig = best

    if sig is None:
        return None

    # Multi-indicator confirmation layer
    mi = _multi_indicator_check(h1, h15, sig["direction"])
    sig["multi_ind"] = mi

    if multi_ind_filter and mi["grade"] == "WEAK":
        log.debug(f"  ⚠️ {symbol} {strategy} signal filtered: multi-ind WEAK ({mi['score']})")
        return None

    return sig

def _simulate_trade(trade: LocalTrade, future_candles: list, max_hold: int) -> LocalTrade:
    """Simulasi TP/SL/timeout. SL checked first (conservative)."""
    for c in future_candles[:max_hold]:
        t = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        if trade.direction == "LONG":
            if c["low"]  <= trade.sl: trade.close(trade.sl, t, "SL"); return trade
            if c["high"] >= trade.tp: trade.close(trade.tp, t, "TP"); return trade
        else:
            if c["high"] >= trade.sl: trade.close(trade.sl, t, "SL"); return trade
            if c["low"]  <= trade.tp: trade.close(trade.tp, t, "TP"); return trade
    if future_candles:
        idx  = min(max_hold - 1, len(future_candles) - 1)
        last = future_candles[idx]
        t    = datetime.fromtimestamp(last["time"] / 1000, tz=timezone.utc)
        trade.close(last["close"], t, "TIMEOUT")
    return trade


def run_backtest(symbol: str, strategy: str = "scalp",
                 days: int = DEFAULT_DAYS, stake_usdt: float = DEFAULT_STAKE,
                 multi_ind_filter: bool = True) -> dict:
    """
    Main backtest engine.
    Walk-forward, anti-lookahead, one trade at a time.
    Mirip Freqtrade Backtesting.start()

    multi_ind_filter=True  → signal hanya jika EMA+MACD+StochRSI+BB+ADX+VWAP ≥ MODERATE
    multi_ind_filter=False → semua signal masuk (SMC-only mode, legacy behavior)

    Auto-resolve exchange: jika symbol tidak ada di Binance Futures,
    otomatis fallback ke Bybit / OKX / Gate.io via exchange_resolver.
    """
    cfg = STRATEGY_CONFIG.get(strategy, STRATEGY_CONFIG["scalp"])

    # ── Resolve symbol ke exchange yang benar ──
    exchange = "binance_futures"  # default
    exc_label = "Binance Futures"
    resolved_symbol = symbol

    if _EXCHANGE_RESOLVER:
        exc_info = _exc_resolve(symbol.replace("USDT", ""))
        if exc_info:
            resolved_symbol = exc_info["symbol"]
            exchange        = exc_info["exchange"]
            exc_label       = exc_info["exchange_label"]
            if resolved_symbol != symbol:
                log.info(f"Symbol resolved: {symbol} → {resolved_symbol} on {exchange}")
        else:
            return {"error": f"Symbol {symbol} tidak ditemukan di Binance, Bybit, OKX, maupun Gate.io."}

    log.info(f"Backtest START: {resolved_symbol} | {strategy} | {days}d | {exchange}")

    # ── Download candles ──
    trade_candles = download_ohlcv(resolved_symbol, cfg["trade_tf"], days=days, exchange=exchange)
    if len(trade_candles) < 60:
        return {"error": f"Data kurang: {len(trade_candles)} candles dari {exc_label}. Minimal 60."}

    c4h  = resample_to_tf(trade_candles, "4h")
    c1h  = resample_to_tf(trade_candles, "1h")
    c15m = resample_to_tf(trade_candles, "15m")

    sig_tf = cfg["signal_tf"]
    sig_c  = c4h if sig_tf == "4h" else (c1h if sig_tf == "1h" else c15m)
    # Durasi 1 bar sinyal (ms) — dipakai untuk mulai simulasi SETELAH bar
    # sinyal benar-benar close (anti-lookahead pada timing entry).
    _TF_MS    = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
    sig_tf_ms = _TF_MS.get(sig_tf, 0)

    log.info(f"  4H:{len(c4h)} 1H:{len(c1h)} 15M:{len(c15m)} trade_tf:{len(trade_candles)}")

    # Walk-forward
    trades: list[LocalTrade] = []
    open_trade = None
    warmup = 50

    for i in range(warmup, len(sig_c)):
        cur = sig_c[i]
        bar_t = datetime.fromtimestamp(cur["time"] / 1000, tz=timezone.utc)

        if open_trade and open_trade.is_open:
            continue

        cut = cur["time"]
        h4  = [c for c in c4h  if c["time"] <= cut][-100:]
        h1  = [c for c in c1h  if c["time"] <= cut][-100:]
        h15 = [c for c in c15m if c["time"] <= cut][-100:]

        oi     = _mock_oi(h1)
        signal = _run_signal(strategy, resolved_symbol, h4, h1, h15, oi, multi_ind_filter=multi_ind_filter)
        if signal is None:
            continue

        entry = cur["close"]
        direc = signal["direction"]
        tp    = round(entry * (1 + cfg["tp_pct"]) if direc == "LONG" else entry * (1 - cfg["tp_pct"]), 8)
        sl    = round(entry * (1 - cfg["sl_pct"]) if direc == "LONG" else entry * (1 + cfg["sl_pct"]), 8)

        label = signal.get("label", "")
        mi    = signal.get("multi_ind", {})
        if mi:
            label += f" | MI:{mi.get('grade','?')}({mi.get('score',0)})"

        open_trade = LocalTrade(
            symbol=resolved_symbol, direction=direc, entry_price=entry,
            tp=tp, sl=sl, open_time=bar_t, stake_usdt=stake_usdt,
            entry_reason=label, score=signal.get("score", 0)
        )
        open_trade.multi_ind = mi

        # Entry difill di close bar sinyal (cur["close"]). Simulasi SL/TP mulai
        # dari trade candle pertama SETELAH bar sinyal close — bukan dari awal
        # bar yang harganya (close) belum diketahui saat itu (lookahead bias).
        tidx = next((j for j, tc in enumerate(trade_candles)
                     if tc["time"] >= cur["time"] + sig_tf_ms), None)
        if tidx is not None:
            open_trade = _simulate_trade(open_trade, trade_candles[tidx:], cfg["max_hold_candles"])

        trades.append(open_trade)
        open_trade = None

    stats = _calc_stats(trades, days, stake_usdt, resolved_symbol, strategy, cfg)
    stats["exchange"]       = exchange
    stats["exchange_label"] = exc_label
    stats["trades_detail"] = [t.to_dict() for t in trades[-30:]]
    _save_result({k: v for k, v in stats.items() if k != "trades_detail"})

    log.info(f"Backtest DONE: {len(trades)} trades WR:{stats['win_rate']:.1f}% PnL:{stats['total_pnl_pct']:.2f}%")
    return stats


def _calc_stats(trades: list, days: int, stake: float, symbol: str, strategy: str, cfg: dict) -> dict:
    """Hitung semua statistik. Mirip Freqtrade optimize_reports."""
    base = {"symbol": symbol, "strategy": strategy, "days": days,
            "strategy_desc": cfg.get("description", ""), "stake_usdt": stake,
            "run_time": datetime.now(timezone.utc).isoformat()}
    empty = {**base, "total_trades": 0, "win_rate": 0, "total_pnl_pct": 0,
             "total_pnl_usdt": 0, "profit_factor": 0, "expectancy": 0,
             "avg_win_pct": 0, "avg_loss_pct": 0, "best_trade_pct": 0, "worst_trade_pct": 0,
             "max_drawdown_pct": 0, "max_drawdown_usdt": 0, "sharpe": 0, "sortino": 0,
             "calmar": 0, "annualized_ret": 0, "avg_hold_hours": 0, "trades_per_day": 0,
             "tp_count": 0, "sl_count": 0, "timeout_count": 0,
             "note": "Tidak ada trade. Threshold mungkin terlalu tinggi — coba turunkan atau extend days."}
    if not trades: return empty

    pnl_p = [t.pnl_pct for t in trades]
    pnl_u = [t.pnl_usdt for t in trades]
    holds = [t.hold_hours for t in trades]
    wins  = [p for p in pnl_p if p > 0]
    loss  = [p for p in pnl_p if p <= 0]

    wr  = len(wins) / len(trades) * 100
    gp  = sum(p for p in pnl_u if p > 0)
    gl  = abs(sum(p for p in pnl_u if p < 0))
    pf  = gp / gl if gl > 0 else 999.0

    # Equity & drawdown
    equity = [stake]
    for p in pnl_u: equity.append(equity[-1] + p)
    peak = equity[0]; max_dd_p = max_dd_u = 0
    for e in equity:
        peak = max(peak, e)
        ddp  = (peak - e) / peak * 100 if peak > 0 else 0
        if ddp > max_dd_p: max_dd_p, max_dd_u = ddp, peak - e

    # Sharpe/Sortino — annualize atas hari KALENDER (termasuk hari tanpa trade).
    # Sebelumnya hanya hari aktif yang dihitung → mean/std bias dan Sharpe
    # ter-overstate untuk strategi yang jarang entry. Crypto 24/7 → faktor 365.
    daily = {}
    for t in trades:
        d = t.close_time.date().isoformat() if t.close_time else "x"
        daily[d] = daily.get(d, 0) + t.pnl_pct
    active = list(daily.values())
    n_days = max(days, len(active))
    series = active + [0.0] * max(0, n_days - len(active))   # pad hari nol
    # Butuh cukup hari aktif supaya rasio bukan sekadar noise dari sampel kecil.
    if len(active) >= 5 and len(series) >= 2:
        mu  = np.mean(series); std = np.std(series, ddof=1)
        sharpe = mu / std * np.sqrt(365) if std > 0 else 0
        down   = [r for r in series if r < 0]
        dstd   = np.std(down, ddof=1) if len(down) > 1 else (abs(down[0]) if down else 0)
        sortino = mu / dstd * np.sqrt(365) if dstd > 0 else 0
    else:
        sharpe = sortino = 0

    # Annualized return: compounding geometris dari equity curve (bukan
    # ekstrapolasi linear sum(pnl)×365 yang mengabaikan compounding & jumlah trade).
    if days > 0 and equity[0] > 0 and equity[-1] > 0:
        total_mult = equity[-1] / equity[0]
        ann_ret    = (total_mult ** (365.0 / days) - 1) * 100
    elif days > 0 and equity[-1] <= 0:
        ann_ret = -100.0
    else:
        ann_ret = 0
    calmar  = ann_ret / max_dd_p if max_dd_p > 0 else 0
    exp     = (wr / 100 * (np.mean(wins) * 100 if wins else 0)) + \
              ((1 - wr / 100) * (np.mean(loss) * 100 if loss else 0))

    # Multi-indicator breakdown stats
    mi_trades = [t for t in trades if hasattr(t, "multi_ind") and t.multi_ind]
    mi_strong  = [t for t in mi_trades if t.multi_ind.get("grade") == "STRONG"]
    mi_moderate= [t for t in mi_trades if t.multi_ind.get("grade") == "MODERATE"]
    mi_weak    = [t for t in mi_trades if t.multi_ind.get("grade") == "WEAK"]

    mi_stats = {}
    for label, group in [("STRONG", mi_strong), ("MODERATE", mi_moderate), ("WEAK", mi_weak)]:
        if group:
            g_wins = [t.pnl_pct for t in group if t.pnl_pct > 0]
            mi_stats[label] = {
                "count": len(group),
                "win_rate": round(len(g_wins)/len(group)*100, 1),
                "avg_pnl_pct": round(np.mean([t.pnl_pct*100 for t in group]), 2),
            }

    return {**base,
        "total_trades": len(trades), "win_rate": round(wr, 2),
        "total_pnl_pct": round(sum(pnl_p) * 100, 3),
        "total_pnl_usdt": round(sum(pnl_u), 4),
        "profit_factor": round(min(pf, 999), 3),
        "expectancy": round(exp, 3),
        "avg_win_pct": round(np.mean(wins) * 100, 3) if wins else 0,
        "avg_loss_pct": round(np.mean(loss) * 100, 3) if loss else 0,
        "best_trade_pct": round(max(pnl_p) * 100, 3),
        "worst_trade_pct": round(min(pnl_p) * 100, 3),
        "max_drawdown_pct": round(max_dd_p, 3),
        "max_drawdown_usdt": round(max_dd_u, 4),
        "sharpe": round(sharpe, 3), "sortino": round(sortino, 3),
        "calmar": round(calmar, 3), "annualized_ret": round(ann_ret, 2),
        "avg_hold_hours": round(np.mean(holds), 2),
        "trades_per_day": round(len(trades) / days, 2) if days > 0 else 0,
        "tp_count": sum(1 for t in trades if t.exit_reason == "TP"),
        "sl_count": sum(1 for t in trades if t.exit_reason == "SL"),
        "timeout_count": sum(1 for t in trades if t.exit_reason == "TIMEOUT"),
        "multi_ind_breakdown": mi_stats,
    }


def _save_result(stats: dict):
    try:
        existing = []
        if os.path.exists(BT_RESULTS_FILE):
            with open(BT_RESULTS_FILE) as f: existing = json.load(f)
        if len(existing) >= 100: existing = existing[-99:]
        existing.append(stats)
        with open(BT_RESULTS_FILE, "w") as f: json.dump(existing, f, indent=2)
    except Exception as e: log.warning(f"Save BT error: {e}")


def load_backtest_results() -> list:
    try:
        if os.path.exists(BT_RESULTS_FILE):
            with open(BT_RESULTS_FILE) as f: return json.load(f)
    except: pass
    return []


def get_last_backtest() -> Optional[dict]:
    r = load_backtest_results()
    return r[-1] if r else None


def compare_strategies(symbol: str, days: int = 14) -> list:
    """Jalankan semua 4 strategies dan compare. Mirip freqtrade --strategy-list."""
    results = []
    for s in ["scalp", "swing", "prepump", "predump"]:
        log.info(f"  Comparing: {s}...")
        try:
            r = run_backtest(symbol, s, days=days)
            if "error" not in r: results.append(r)
        except Exception as e: log.warning(f"Compare {s}: {e}")
        time.sleep(0.3)
    results.sort(key=lambda x: x.get("profit_factor", 0), reverse=True)
    return results


# ─────────────────────────────────────────────
# BATCH BACKTEST — semua koin sekaligus
# ─────────────────────────────────────────────

BTALL_RESULTS_FILE = "btall_results.json"

def run_batch_backtest(symbols: list, strategy: str = "combined", days: int = 30,
                       stake_usdt: float = DEFAULT_STAKE) -> list:
    """
    Backtest multiple symbols sekaligus. Returns list sorted by grade + WR.
    Hasil disimpan ke btall_results.json untuk caching.
    """
    results = []
    total   = len(symbols)

    for i, symbol in enumerate(symbols, 1):
        sym = symbol.upper()
        if not sym.endswith("USDT"):
            sym = sym + "USDT"
        log.info(f"  btall [{i}/{total}]: {sym}")
        try:
            r = run_backtest(sym, strategy, days, stake_usdt)
            n  = r.get("total_trades", 0)
            wr = r.get("win_rate", 0)
            pf = r.get("profit_factor", 0)
            if r.get("error"):
                r["_grade"] = "ERROR"
            elif n < 3:
                r["_grade"] = "INSUFFICIENT"
            elif wr >= 55 and pf >= 1.5:
                r["_grade"] = "STRONG"
            elif wr >= 45 and pf >= 1.0:
                r["_grade"] = "MODERATE"
            else:
                r["_grade"] = "WEAK"
            results.append(r)
        except Exception as e:
            log.warning(f"btall {sym}: {e}")
            results.append({
                "symbol": sym, "strategy": strategy, "error": str(e),
                "_grade": "ERROR", "total_trades": 0, "win_rate": 0, "profit_factor": 0,
            })

    _grade_order = {"STRONG": 0, "MODERATE": 1, "WEAK": 2, "INSUFFICIENT": 3, "ERROR": 4}
    results.sort(key=lambda r: (
        _grade_order.get(r.get("_grade", "ERROR"), 4),
        -(r.get("win_rate", 0) * min(r.get("profit_factor", 0), 3))
    ))

    try:
        with open(BTALL_RESULTS_FILE, "w") as f:
            json.dump({
                "run_time": datetime.now(timezone.utc).isoformat(),
                "strategy": strategy, "days": days,
                "results": results,
            }, f, indent=2)
    except Exception as e:
        log.warning(f"Save btall error: {e}")

    return results


def get_coin_bt_grade(symbol: str, max_age_hours: int = 168) -> Optional[dict]:
    """
    Lookup cached batch backtest result untuk 1 coin (max 7 hari).
    Return dict dengan grade/WR/PF, atau None kalau belum ada / kadaluarsa.
    """
    try:
        if not os.path.exists(BTALL_RESULTS_FILE):
            return None
        with open(BTALL_RESULTS_FILE) as f:
            data = json.load(f)
        run_time = datetime.fromisoformat(data.get("run_time", "2000-01-01T00:00:00"))
        if run_time.tzinfo is None:
            run_time = run_time.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - run_time).total_seconds() / 3600 > max_age_hours:
            return None
        sym = symbol.upper()
        if not sym.endswith("USDT"):
            sym = sym + "USDT"
        for r in data.get("results", []):
            if r.get("symbol", "").upper() == sym:
                return r
    except Exception as e:
        log.debug(f"get_coin_bt_grade: {e}")
    return None


def format_batch_result(results: list, strategy: str, days: int) -> str:
    """Format batch backtest results untuk Telegram."""
    if not results:
        return "❌ Batch backtest gagal — tidak ada hasil."

    strong  = [r for r in results if r.get("_grade") == "STRONG"]
    moderate= [r for r in results if r.get("_grade") == "MODERATE"]
    weak    = [r for r in results if r.get("_grade") == "WEAK"]
    insuff  = [r for r in results if r.get("_grade") == "INSUFFICIENT"]
    errors  = [r for r in results if r.get("_grade") == "ERROR"]

    valid   = strong + moderate + weak
    ts      = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🧪 <b>BATCH BACKTEST — {len(results)} COINS</b>",
        f"📅 Strategy: <b>{strategy.upper()}</b> | {days} hari",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if strong:
        lines.append("✅ <b>LAYAK TRADE (WR ≥ 55%, PF ≥ 1.5)</b>")
        for i, r in enumerate(strong, 1):
            sym = r.get("symbol", "?").replace("USDT", "")
            wr  = r.get("win_rate", 0)
            pf  = min(r.get("profit_factor", 0), 99)
            n   = r.get("total_trades", 0)
            pnl = r.get("total_pnl_pct", 0)
            lines.append(f"  {i}. <b>{sym}</b> — WR:{wr:.0f}% | PF:{pf:.2f} | {n}T | PnL:{pnl:+.1f}%")
        lines.append("")

    if moderate:
        lines.append("⚠️ <b>BORDERLINE (WR ≥ 45%, PF ≥ 1.0)</b>")
        for i, r in enumerate(moderate, 1):
            sym = r.get("symbol", "?").replace("USDT", "")
            wr  = r.get("win_rate", 0)
            pf  = min(r.get("profit_factor", 0), 99)
            n   = r.get("total_trades", 0)
            lines.append(f"  {i}. <b>{sym}</b> — WR:{wr:.0f}% | PF:{pf:.2f} | {n}T")
        lines.append("")

    if weak:
        lines.append("🔴 <b>HINDARI (WR &lt; 45% atau PF &lt; 1.0)</b>")
        for r in weak:
            sym = r.get("symbol", "?").replace("USDT", "")
            wr  = r.get("win_rate", 0)
            pf  = min(r.get("profit_factor", 0), 99)
            n   = r.get("total_trades", 0)
            lines.append(f"  ⛔ <b>{sym}</b> — WR:{wr:.0f}% | PF:{pf:.2f} | {n}T")
        lines.append("")

    if insuff:
        syms = ", ".join(r.get("symbol", "?").replace("USDT", "") for r in insuff)
        lines.append(f"⚫ Data kurang (&lt;3 trades): {syms}")
        lines.append("")

    if errors:
        syms = ", ".join(r.get("symbol", "?").replace("USDT", "") for r in errors)
        lines.append(f"❌ Error fetch: {syms}")
        lines.append("")

    if valid:
        avg_wr = sum(r.get("win_rate", 0) for r in valid) / len(valid)
        lines.append(f"📊 Avg WR ({len(valid)} valid coins): <b>{avg_wr:.1f}%</b>")
        lines.append(f"💎 {len(strong)} layak | ⚠️ {len(moderate)} borderline | 🚫 {len(weak)} hindari")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# SIGNAL-BASED BACKTEST
# Freqtrade-inspired "export:signals" approach:
# ukur akurasi SINYAL, bukan profitabilitas TRADE
# ─────────────────────────────────────────────

def _run_all_detectors_on_bar(symbol: str, h4: list, h1: list, h15: list, oi: dict) -> Optional[dict]:
    """Jalankan semua detector pada satu bar historis. Return dict atau None."""
    try:
        from crypto_screening_bot_v13 import (
            detect_prepump, detect_predump, detect_scalp_setup, detect_swing_setup,
            calculate_confluence_v4,
        )
    except ImportError:
        return None

    tf4  = _build_tf_data(h4,  "4h")
    tf1  = _build_tf_data(h1,  "1h")
    tf15 = _build_tf_data(h15, "15m")

    if tf4.get("error") or tf1.get("error"):
        return None

    try:
        eql = tf1.get("liquidity", {})
        return {
            "prepump":    detect_prepump(symbol, tf1, tf4, oi),
            "predump":    detect_predump(symbol, tf1, tf4, oi),
            "scalp":      detect_scalp_setup(symbol, tf15, tf1, tf4, oi),
            "swing":      detect_swing_setup(symbol, tf4, tf1, tf15, oi, eql),
            "confluence": calculate_confluence_v4(tf4, tf1, tf15, oi),
        }
    except Exception as e:
        log.debug(f"Detector bar error: {e}")
        return None


def run_signal_backtest(symbol: str, days: int = 30,
                        score_threshold: int = 75) -> dict:
    """
    Backtest berbasis SINYAL — apakah confirmed signal score >= threshold
    memprediksi arah price dengan benar setelah N jam?

    Berbeda dari run_backtest() yang ukur TP/SL hit:
      run_signal_backtest → ukur: "apakah ARAH sinyal benar setelah 4h/12h/24h?"

    Menjawab: "dari semua confirmed signal yang dikirim, berapa % yang benar?"
    """
    exchange = "binance_futures"
    resolved = symbol
    if _EXCHANGE_RESOLVER:
        exc_info = _exc_resolve(symbol.replace("USDT", ""))
        if exc_info:
            resolved = exc_info["symbol"]
            exchange  = exc_info["exchange"]
        else:
            return {"error": f"Symbol {symbol} tidak ditemukan di exchange manapun"}

    log.info(f"Signal BT START: {resolved} | {days}d | threshold={score_threshold}")
    c1h  = download_ohlcv(resolved, "1h",  days=days, exchange=exchange)
    c15m = download_ohlcv(resolved, "15m", days=days, exchange=exchange)
    if len(c1h) < 80:
        return {"error": f"Data kurang: {len(c1h)} candles. Minimal 80."}
    if len(c15m) < 80:
        # Fallback: resample dari 1H jika 15M tidak tersedia
        c15m = resample_to_tf(c1h, "15m")
        log.warning(f"Signal BT: 15M tidak tersedia untuk {resolved}, fallback resample dari 1H")

    c4h = resample_to_tf(c1h, "4h")

    try:
        from confirmed_signal import compute_master_score
    except ImportError:
        return {"error": "confirmed_signal.py tidak tersedia — pastikan ada di folder yang sama"}

    signal_events  = []
    warmup         = 60
    last_sig_bar   = -(score_threshold)   # initial cooldown sentinel
    horizons       = [4, 12, 24]

    for i in range(warmup, len(c1h)):
        if i - last_sig_bar < 4:   # 4h cooldown antar sinyal
            continue

        cur = c1h[i]
        h1  = c1h[max(0, i - 100):i + 1]
        h4  = [c for c in c4h if c["time"] <= cur["time"]][-50:]
        h15 = [c for c in c15m if c["time"] <= cur["time"]][-100:]

        if len(h4) < 20 or len(h1) < 20:
            continue

        oi  = _mock_oi(h1)
        det = _run_all_detectors_on_bar(resolved, h4, h1, h15, oi)
        if det is None:
            continue

        master = compute_master_score(
            resolved,
            det["confluence"], det["prepump"], det["predump"],
            det["scalp"], det["swing"], oi,
        )

        direction = master["direction"]
        score     = master["master_score"]

        if direction == "NONE" or score < score_threshold:
            continue

        last_sig_bar = i
        entry_price  = cur["close"]
        bar_time     = datetime.fromtimestamp(cur["time"] / 1000, tz=timezone.utc)

        event: dict = {
            "time":      bar_time.isoformat(),
            "direction": direction,
            "score":     score,
            "entry":     entry_price,
        }

        for h in horizons:
            fut_idx   = min(i + h, len(c1h) - 1)
            fut_price = c1h[fut_idx]["close"]
            pct_chg   = (fut_price - entry_price) / entry_price * 100

            move    = pct_chg  if direction == "LONG" else -pct_chg
            correct = move > 0.5   # min 0.5% move in predicted direction

            event[f"h{h}"] = {
                "ok":   correct,
                "chg":  round(pct_chg, 3),
                "move": round(move, 3),
            }

        signal_events.append(event)

    n = len(signal_events)
    if n == 0:
        return {
            "symbol": resolved, "days": days, "threshold": score_threshold,
            "signals_fired": 0,
            "error": (
                f"Tidak ada sinyal dengan score >= {score_threshold} dalam {days} hari. "
                f"Coba turunkan threshold atau extend days."
            ),
        }

    # ── Aggregate stats per horizon ──
    h_stats: dict = {}
    for h in horizons:
        ok_ev    = [e for e in signal_events if e.get(f"h{h}", {}).get("ok")]
        wrong_ev = [e for e in signal_events if not e.get(f"h{h}", {}).get("ok", True)]
        acc      = len(ok_ev) / n * 100
        moves_ok    = [e[f"h{h}"]["move"] for e in ok_ev    if f"h{h}" in e]
        moves_wrong = [abs(e[f"h{h}"]["move"]) for e in wrong_ev if f"h{h}" in e]
        h_stats[f"accuracy_{h}h"]          = round(acc, 1)
        h_stats[f"avg_move_correct_{h}h"]  = round(float(np.mean(moves_ok)),    2) if moves_ok    else 0.0
        h_stats[f"avg_move_wrong_{h}h"]    = round(float(np.mean(moves_wrong)), 2) if moves_wrong else 0.0

    long_n  = sum(1 for e in signal_events if e["direction"] == "LONG")
    short_n = n - long_n

    return {
        "symbol":          resolved,
        "days":            days,
        "threshold":       score_threshold,
        "signals_fired":   n,
        "long_signals":    long_n,
        "short_signals":   short_n,
        "avg_score":       round(float(np.mean([e["score"] for e in signal_events])), 1),
        "signals_per_day": round(n / days, 2),
        **h_stats,
        "signal_events":   signal_events[-20:],
        "run_time":        datetime.now(timezone.utc).isoformat(),
    }


def format_signal_backtest_result(stats: dict) -> str:
    """Format hasil signal backtest untuk Telegram."""
    if "error" in stats and stats.get("signals_fired", 0) == 0:
        return f"❌ Signal Backtest Error\n{stats['error']}"

    n      = stats["signals_fired"]
    sym    = stats["symbol"].replace("USDT", "")
    days   = stats["days"]
    thresh = stats["threshold"]
    acc4   = stats.get("accuracy_4h",  0)
    acc12  = stats.get("accuracy_12h", 0)
    acc24  = stats.get("accuracy_24h", 0)
    ok4    = stats.get("avg_move_correct_4h", 0)
    bad4   = stats.get("avg_move_wrong_4h",   0)
    ts     = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    def _grade(acc: float) -> str:
        if acc >= 70: return "🔥 Excellent"
        if acc >= 60: return "✅ Good"
        if acc >= 50: return "🟡 Fair"
        return "🔴 Poor"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📡 *SIGNAL ACCURACY BACKTEST*",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💎 *{sym}* | {days} hari | Threshold: *{thresh}/100*",
        f"📊 Sinyal fired: *{n}* ({stats.get('signals_per_day', 0):.1f}/hari)",
        f"🟢 LONG: {stats.get('long_signals', 0)} | 🔴 SHORT: {stats.get('short_signals', 0)}",
        f"🎯 Avg master score: *{stats.get('avg_score', 0):.1f}/100*",
        "",
        "─────── AKURASI ARAH SINYAL ───────",
        f"📊 @4H  : *{acc4:.1f}%*  {_grade(acc4)}",
        f"   ↳ Avg move saat benar : +{ok4:.2f}%",
        f"   ↳ Avg move saat salah : -{bad4:.2f}%",
        f"📊 @12H : *{acc12:.1f}%*",
        f"📊 @24H : *{acc24:.1f}%*",
        "",
        "─────── INTERPRETASI ───────",
    ]

    tips = []
    if n < 5:
        tips.append("⚠️ Sample kecil (<5 sinyal). Extend period ke 60+ hari untuk hasil signifikan.")
    if acc4 >= 65:
        tips.append(f"✅ Akurasi {acc4:.0f}% di 4H — sinyal memiliki predictive value yang solid")
    elif acc4 >= 55:
        tips.append(f"🟡 Akurasi {acc4:.0f}% di 4H — decent, tapi tune threshold atau detector weight")
    else:
        tips.append(f"⚠️ Akurasi hanya {acc4:.0f}% di 4H — perlu improvement di detector / weight")
    if ok4 > 0 and bad4 > 0:
        edge = ok4 / bad4
        if edge >= 1.5:
            tips.append(f"✅ Edge ratio {edge:.1f}x — move saat benar >> saat salah (asimetri bagus)")
        elif edge < 0.8:
            tips.append(f"⚠️ Edge ratio {edge:.1f}x — move saat salah > saat benar (asimetri buruk)")
    if acc4 < acc24 - 5:
        tips.append("💡 Akurasi naik di 24H — sinyal butuh waktu terbukti (hold lebih lama = lebih akurat)")
    if not tips:
        tips.append("📊 Statistik dalam range normal")

    lines.extend(tips)
    lines += ["", "─────── 5 SINYAL TERAKHIR ───────"]

    for ev in reversed(stats.get("signal_events", [])[-5:]):
        t     = ev["time"][:16].replace("T", " ")
        d_e   = "🟢" if ev["direction"] == "LONG" else "🔴"
        ok4e  = "✅" if ev.get("h4",  {}).get("ok") else "❌"
        ok24e = "✅" if ev.get("h24", {}).get("ok") else "❌"
        chg4e = ev.get("h4", {}).get("chg", 0)
        lines.append(
            f"  {d_e} {t} | Score {ev['score']} | "
            f"4H {ok4e}({chg4e:+.1f}%) | 24H {ok24e}"
        )

    lines += [
        "",
        "💡 _Signal accuracy ≠ trade profitability. Gunakan /backtest untuk PF/WR._",
        "⚠️ _15M data diproxy dari 1H — scalp signal accuracy mungkin lebih rendah dari live._",
    ]
    return "\n".join(lines)


def handle_signal_bt_command(user_input: str, chat_id: str, send_tg):
    """
    /signalbt <COIN> [DAYS] [THRESHOLD]
    Ukur akurasi SINYAL confirmed (bukan profitabilitas trade).
    Contoh: /signalbt BTC 30
            /signalbt ETH 14 70
    """
    parts = user_input.strip().split()
    if not parts:
        send_tg(
            "❓ *Format:* `/signalbt <COIN> [DAYS] [THRESHOLD]`\n\n"
            "Contoh:\n"
            "• `/signalbt BTC 30` — backtest sinyal BTC 30 hari, score >= 75\n"
            "• `/signalbt ETH 14 70` — threshold 70\n\n"
            "📡 *Berbeda dari /backtest:*\n"
            "  /backtest  → ukur profit factor TP/SL trade\n"
            "  /signalbt  → ukur akurasi ARAH sinyal (benar ke mana?)\n\n"
            "_Ini menjawab: 'sinyal confirmed yang dikirim bot, berapa % arahnya benar?'_",
            chat_id,
        )
        return

    coin_raw = parts[0].upper().replace("USDT", "").replace("/", "")
    try:   days   = max(7,  min(int(parts[1]), 90)) if len(parts) > 1 else 30
    except ValueError: days = 30
    try:   thresh = max(50, min(int(parts[2]), 95)) if len(parts) > 2 else 75
    except ValueError: thresh = 75

    symbol = coin_raw + "USDT"
    send_tg(
        f"📡 *Signal Accuracy Backtest*\n"
        f"💎 Coin    : *{coin_raw}*\n"
        f"📅 Period  : *{days} hari*\n"
        f"🎯 Threshold: *{thresh}/100* (master score min)\n\n"
        f"⏳ _Replay semua confirmed signal historis... harap tunggu_",
        chat_id,
    )
    try:
        stats = run_signal_backtest(symbol, days=days, score_threshold=thresh)
        # Cache result untuk confirmed_signal.py (dipakai saat validasi live)
        if "error" not in stats or stats.get("signals_fired", 0) > 0:
            try:
                from confirmed_signal import save_signal_bt_cache
                save_signal_bt_cache(symbol, stats)
            except Exception:
                pass
        send_tg(format_signal_backtest_result(stats), chat_id)
    except Exception as e:
        log.error(f"Signal BT command error: {e}", exc_info=True)
        send_tg(f"❌ Error saat signal backtest: `{str(e)[:300]}`", chat_id)


# ─── Formatters ──────────────────────────────

def _fp(v: float) -> str:
    return f"🟢 +{v:.2f}%" if v > 0 else (f"🔴 {v:.2f}%" if v < 0 else f"⚪ {v:.2f}%")

def _fu(v: float) -> str:
    return f"${v:+,.2f}" if abs(v) >= 1 else f"${v:+.4f}"


def format_backtest_result(stats: dict) -> str:
    if "error" in stats:
        return f"❌ Backtest Error: {stats['error']}"
    n, wr, pnl = stats["total_trades"], stats["win_rate"], stats["total_pnl_pct"]
    pf, dd, sh  = stats["profit_factor"], stats["max_drawdown_pct"], stats["sharpe"]
    so          = stats["sortino"]
    if   pf >= 2.0 and wr >= 55: grade = "🔥 A+ (Excellent)"
    elif pf >= 1.5 and wr >= 50: grade = "✅ B+ (Good)"
    elif pf >= 1.2 and wr >= 45: grade = "🟡 C  (Fair)"
    elif pf >= 1.0:              grade = "🟠 D  (Break Even)"
    else:                        grade = "🔴 F  (Loss)"
    tp_p = stats["tp_count"] / n * 100 if n > 0 else 0
    sl_p = stats["sl_count"] / n * 100 if n > 0 else 0
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 *BACKTEST RESULT*",
        f"🕐 {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💎 *{stats['symbol']}* | Strategy: *{stats['strategy'].upper()}*",
        f"📅 Period: {stats['days']} hari | Stake: ${stats.get('stake_usdt', 100):.0f}/trade",
        f"🏦 Exchange: {stats.get('exchange_label', 'Binance Futures')}",
        f"_{stats.get('strategy_desc', '')}_",
        "",
        f"🏆 *Grade: {grade}*",
        "",
        "─────── RINGKASAN ───────",
        f"📊 Total Trades  : *{n}*",
        f"🎯 Win Rate      : *{wr:.1f}%*",
        f"💰 Total PnL     : *{_fp(pnl)}* ({_fu(stats['total_pnl_usdt'])})",
        f"📈 Profit Factor : *{pf:.2f}x*",
        f"💡 Expectancy    : {_fp(stats['expectancy'])} per trade",
        "",
        "─────── RISK ───────",
        f"📉 Max Drawdown  : *{dd:.2f}%* ({_fu(-stats['max_drawdown_usdt'])})",
        f"📐 Sharpe Ratio  : *{sh:.2f}*",
        f"📐 Sortino Ratio : *{so:.2f}*",
        f"📐 Calmar Ratio  : *{stats.get('calmar', 0):.2f}*",
        f"📅 Return/Year   : *{stats.get('annualized_ret', 0):.1f}%* (proyeksi)",
        "",
        "─────── TRADE BREAKDOWN ───────",
        f"✅ TP Hit   : {stats['tp_count']} ({tp_p:.0f}%)",
        f"❌ SL Hit   : {stats['sl_count']} ({sl_p:.0f}%)",
        f"⏱️ Timeout  : {stats['timeout_count']}",
        f"📈 Avg Win  : *+{stats['avg_win_pct']:.2f}%*",
        f"📉 Avg Loss : *{stats['avg_loss_pct']:.2f}%*",
        f"🏆 Best     : *+{stats['best_trade_pct']:.2f}%*",
        f"💀 Worst    : *{stats['worst_trade_pct']:.2f}%*",
        f"⏰ Avg Hold : *{stats['avg_hold_hours']:.1f}h*",
        f"🔄 Freq     : {stats['trades_per_day']:.1f} trades/hari",
        "",
    ]
    # Multi-indicator breakdown
    mi_bd = stats.get("multi_ind_breakdown", {})
    if mi_bd:
        lines.append("─────── MULTI-IND FILTER ───────")
        lines.append("_(EMA+MACD+StochRSI+BB+ADX+VWAP)_")
        for grade in ["STRONG", "MODERATE", "WEAK"]:
            gd = mi_bd.get(grade)
            if gd:
                wr_g = gd["win_rate"]; pnl_g = gd["avg_pnl_pct"]
                emoji = "🔥" if grade=="STRONG" else ("🟡" if grade=="MODERATE" else "🔴")
                lines.append(f"{emoji} {grade}: {gd['count']} trades | WR {wr_g:.0f}% | Avg {pnl_g:+.2f}%")
        lines.append("")
    lines += [
        "─────── INTERPRETASI ───────",
    ]
    tips = []
    if n == 0:   tips.append("😴 0 trades → threshold terlalu tinggi. Extend days atau turunkan min_score")
    if n < 10:   tips.append("⚠️ Sample kecil (<10 trades) → belum statistik signifikan. Extend ke 60+ hari")
    if pf < 1.0: tips.append("⚠️ PF < 1 → strategy lose di periode ini. Coba timeframe berbeda")
    if dd > 25:  tips.append("⚠️ Max drawdown > 25% → risiko tinggi. Pertimbangkan kurangi stake")
    if sh < 0:   tips.append("⚠️ Sharpe negatif → risk-adjusted return buruk")
    if sh >= 1.5 and pf >= 1.5: tips.append("✅ Sharpe & PF bagus → worth divalidasi di paper trading")
    if stats.get("timeout_count", 0) > n * 0.4 > 0: tips.append("⚠️ >40% timeout → TP terlalu jauh untuk TF ini")
    if not tips: tips.append("📊 Semua metrik dalam range normal")
    lines.extend(tips)
    lines += ["", "⚠️ _Backtest ≠ jaminan profit masa depan. DYOR._"]
    return "\n".join(lines)


def format_backtest_compare(results: list, symbol: str, days: int) -> str:
    if not results: return "❌ Tidak ada hasil backtest."
    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "🔬 *STRATEGY COMPARISON*",
             f"🕐 {ts}", f"💎 *{symbol}* | {days} hari",
             "━━━━━━━━━━━━━━━━━━━━━━━━", "", "Ranked by Profit Factor:", ""]
    medals = ["🥇", "🥈", "🥉", "4️⃣"]
    for i, r in enumerate(results):
        medal = medals[i] if i < len(medals) else f"#{i+1}"
        pnl_s = f"+{r['total_pnl_pct']:.2f}%" if r["total_pnl_pct"] > 0 else f"{r['total_pnl_pct']:.2f}%"
        lines += [
            f"{medal} *{r['strategy'].upper()}*",
            f"  PnL: {pnl_s} | WR: {r['win_rate']:.0f}% | PF: {r['profit_factor']:.2f}x",
            f"  DD: -{r['max_drawdown_pct']:.1f}% | Sharpe: {r['sharpe']:.2f} | Trades: {r['total_trades']}",
            "─────────────────────"
        ]
    if results:
        b = results[0]
        lines += [f"\n💡 *Best: {b['strategy'].upper()}*",
                  "✅ Worth paper-trading" if b["profit_factor"] >= 1.5 
                  else "⚠️ Semua PF < 1.5 — coba di kondisi market berbeda"]
    lines += ["", "⚠️ _DYOR. Backtest ≠ jaminan profit._"]
    return "\n".join(lines)


def format_btstats_summary() -> str:
    results = load_backtest_results()
    if not results:
        return "📭 Belum ada backtest history.\nMulai dengan: `/backtest BTC scalp 30`"
    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "📚 *BACKTEST HISTORY*",
             f"🕐 {ts}", f"📊 Total sessions: *{len(results)}*",
             "━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    by_s = {}
    for r in results: by_s.setdefault(r.get("strategy", "?"), []).append(r)
    lines.append("─── BY STRATEGY ───")
    for strat, runs in sorted(by_s.items()):
        pf_list = [r["profit_factor"] for r in runs if r["profit_factor"] < 999]
        lines += [
            f"📊 *{strat.upper()}* ({len(runs)} runs)",
            f"  Avg WR: {np.mean([r['win_rate'] for r in runs]):.0f}% | "
            f"Avg PnL: {np.mean([r['total_pnl_pct'] for r in runs]):+.1f}%",
            f"  Avg PF: {np.mean(pf_list):.2f} | Best PF: {max(pf_list):.2f}" if pf_list else "  PF: N/A",
            ""
        ]
    lines.append("─── LAST 5 RUNS ───")
    for r in reversed(results[-5:]):
        pnl = r["total_pnl_pct"]
        lines.append(f"• {r['symbol']} | {r['strategy'].upper()} | "
                     f"{'+' if pnl >= 0 else ''}{pnl:.2f}% | WR:{r['win_rate']:.0f}% | "
                     f"{r['days']}d | {r.get('run_time', '?')[:10]}")
    return "\n".join(lines)


# ─── Telegram Handlers ────────────────────────

def handle_backtest_command(user_input: str, chat_id: str, send_tg):
    """
    /backtest <COIN> [STRATEGY] [DAYS]
    Contoh: /backtest BTC scalp 30
            /backtest LAB swing 14     ← auto resolve ke Bybit jika tidak di Binance
    """
    parts    = user_input.strip().split()
    if not parts:
        send_tg(
            "❓ *Format:* `/backtest <COIN> [STRATEGY] [DAYS]`\n\n"
            "Contoh:\n• `/backtest BTC scalp 30`\n• `/backtest ETH prepump 14`\n"
            "• `/backtest SOL swing 60`\n• `/backtest LAB combined 14`\n\n"
            f"_Strategies: {', '.join(STRATEGY_CONFIG.keys())}_\n"
            "_Auto-resolve: Binance Futures → Bybit → OKX → Gate.io_", chat_id)
        return

    coin_raw = parts[0].upper().replace("USDT", "").replace("/", "")
    strategy = parts[1].lower() if len(parts) > 1 else "scalp"
    days_raw = parts[2]         if len(parts) > 2 else "30"

    if strategy not in STRATEGY_CONFIG:
        send_tg(f"❓ Strategy `{strategy}` tidak dikenal.\nAvailable: `{', '.join(STRATEGY_CONFIG.keys())}`", chat_id)
        return
    try: days = max(7, min(int(days_raw), 180))
    except ValueError: days = 30

    # Resolve exchange dulu sebelum mulai download
    symbol    = coin_raw + "USDT"
    exc_label = "Binance Futures"
    if _EXCHANGE_RESOLVER:
        exc_info = _exc_resolve(coin_raw)
        if not exc_info:
            send_tg(_exc_not_found(coin_raw), chat_id)
            return
        symbol    = exc_info["symbol"]
        exc_label = exc_info["exchange_label"]

    cfg = STRATEGY_CONFIG[strategy]
    exc_note = f"\n🏦 Exchange : *{exc_label}*" if exc_label != "Binance Futures" else ""
    send_tg(
        f"🔄 *Backtesting {symbol}*\n"
        f"📋 Strategy : *{strategy.upper()}*\n"
        f"📅 Period   : *{days} hari*\n"
        f"📐 Trade TF : {cfg['trade_tf']} | Signal TF: {cfg['signal_tf']}\n"
        f"🎯 TP: +{cfg['tp_pct']*100:.1f}% | SL: -{cfg['sl_pct']*100:.1f}%{exc_note}\n\n"
        f"⏳ _Sedang download data & replay sinyal..._", chat_id)
    try:
        stats = run_backtest(symbol, strategy, days=days)
        send_tg(format_backtest_result(stats), chat_id)
    except Exception as e:
        log.error(f"BT error: {e}", exc_info=True)
        send_tg(f"❌ Error saat backtest: `{str(e)[:300]}`", chat_id)


def handle_btresult_command(chat_id: str, send_tg):
    last = get_last_backtest()
    if not last:
        send_tg("📭 Belum ada backtest.\nJalankan: `/backtest BTC scalp 30`", chat_id)
        return
    send_tg(format_backtest_result(last), chat_id)


def handle_btcompare_command(user_input: str, chat_id: str, send_tg):
    """
    /btcompare <COIN> [DAYS]
    """
    parts = user_input.strip().split()
    if not parts:
        send_tg("❓ Format: `/btcompare BTC 14`", chat_id); return
    coin_raw = parts[0].upper().replace("USDT", "").replace("/", "")
    try: days = max(7, min(int(parts[1]), 60)) if len(parts) > 1 else 14
    except ValueError: days = 14
    symbol = coin_raw + "USDT"
    send_tg(
        f"🔬 *Comparing ALL strategies untuk {symbol}*\n"
        f"📅 Period: {days} hari\n\n"
        f"⏳ _Ini butuh 3-5 menit (4 strategies)..._", chat_id)
    try:
        results = compare_strategies(symbol, days=days)
        send_tg(format_backtest_compare(results, symbol, days), chat_id)
    except Exception as e:
        log.error(f"BT compare error: {e}", exc_info=True)
        send_tg(f"❌ Error: `{str(e)[:200]}`", chat_id)


def handle_btstats_command(chat_id: str, send_tg):
    send_tg(format_btstats_summary(), chat_id)


# ─── INTEGRATION GUIDE ────────────────────────────────────────────────────────
#
# STEP 1 — Tambah di crypto_screening_bot_v11.py bagian imports (setelah trade_journal):
#
#   try:
#       from backtest_engine import (
#           handle_backtest_command, handle_btresult_command,
#           handle_btcompare_command, handle_btstats_command,
#       )
#       BACKTEST_MODULE = True
#   except ImportError:
#       BACKTEST_MODULE = False
#       logging.getLogger("v11").warning("backtest_engine.py tidak ditemukan")
#
#
# STEP 2 — Di command dispatcher (cari bagian elif cmd == "/..."), tambahkan:
#
#   elif cmd == "/backtest":
#       if BACKTEST_MODULE:
#           threading.Thread(
#               target=handle_backtest_command,
#               args=(args, chat_id, send_telegram), daemon=True
#           ).start()
#       else:
#           send_telegram("❌ Backtest module tidak tersedia.", chat_id)
#
#   elif cmd == "/btresult":
#       if BACKTEST_MODULE:
#           handle_btresult_command(chat_id, send_telegram)
#
#   elif cmd == "/btcompare":
#       if BACKTEST_MODULE:
#           threading.Thread(
#               target=handle_btcompare_command,
#               args=(args, chat_id, send_telegram), daemon=True
#           ).start()
#
#   elif cmd == "/btstats":
#       if BACKTEST_MODULE:
#           handle_btstats_command(chat_id, send_telegram)
#
#
# STEP 3 — Di handle_help_command(), tambahkan section baru:
#
#   lines.append("\n📊 <b>BACKTEST</b>")
#   lines.append("  /backtest &lt;COIN&gt; &lt;STRATEGY&gt; &lt;DAYS&gt;")
#   lines.append("    strategies: scalp, swing, prepump, predump, combined")
#   lines.append("  /btresult — hasil backtest terakhir")
#   lines.append("  /btcompare &lt;COIN&gt; [DAYS] — compare semua strategy")
#   lines.append("  /btstats — semua backtest history")
