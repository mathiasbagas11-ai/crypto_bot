#!/usr/bin/env python3
"""
EXCHANGE RESOLVER v1.0
=======================
Freqtrade-inspired multi-exchange symbol resolution + OHLCV fetcher.

Fallback chain (mirip freqtrade --exchange):
  1. Binance Futures (fapi) — OI/funding tersedia ✅
  2. Binance Spot    (api)  — volume besar, no OI
  3. Bybit Futures   (v5)   — banyak koin kecil, OI tersedia
  4. OKX Futures            — coverage luas
  5. Gate.io Futures        — koin-koin microcap & baru

Interval mapping: semua exchange punya format berbeda, di-normalize ke
standard internal: "1m","5m","15m","1h","4h","1d"

Usage:
  from exchange_resolver import resolve_symbol, get_ohlcv, get_ticker

  result = resolve_symbol("LAB")
  # → {"symbol": "LABUSDT", "exchange": "bybit", "type": "futures"}

  candles = get_ohlcv("LABUSDT", "1h", exchange="bybit")
  # → [{"time":..., "open":..., "high":..., "low":..., "close":..., "volume":...}, ...]

  ticker = get_ticker("LABUSDT", exchange="bybit")
  # → {"price": 0.012, "change_24h": 3.5, "volume_24h": 1234567}
"""

import requests
import logging
import time
from typing import Optional

log = logging.getLogger("exchange_resolver")

# ─── Exchange endpoints ───────────────────────────────────────────────────────

BINANCE_FUTURES = "https://fapi.binance.com"
BINANCE_SPOT    = "https://api.binance.com/api/v3"
BYBIT_BASE      = "https://api.bybit.com"
OKX_BASE        = "https://www.okx.com"
GATE_BASE       = "https://api.gateio.ws/api/v4"

# ─── Interval normalization ───────────────────────────────────────────────────

# Standard → exchange-specific interval strings
INTERVAL_MAP = {
    "bybit": {
        "1m": "1",  "3m": "3",  "5m": "5",  "15m": "15",
        "30m": "30", "1h": "60", "2h": "120", "4h": "240",
        "6h": "360", "12h": "720", "1d": "D",
    },
    "okx": {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H",
        "12h": "12H", "1d": "1D",
    },
    "gate": {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "4h": "4h", "8h": "8h", "1d": "1d",
    },
    # Binance uses standard strings: 1m, 5m, 15m, 1h, 4h, 1d
}

# ─── Exchange priority order ──────────────────────────────────────────────────

EXCHANGE_PRIORITY = [
    "binance_futures",
    "binance_spot",
    "bybit",
    "okx",
    "gate",
]

EXCHANGE_LABELS = {
    "binance_futures": "Binance Futures",
    "binance_spot":    "Binance Spot",
    "bybit":           "Bybit",
    "okx":             "OKX",
    "gate":            "Gate.io",
}

# ─── Symbol check per exchange ────────────────────────────────────────────────

def _check_binance_futures(symbol: str) -> bool:
    """Check apakah symbol ada di Binance Futures (USDT-M)."""
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/ticker/price",
            params={"symbol": symbol}, timeout=5
        )
        return r.status_code == 200
    except Exception:
        return False


def _check_binance_spot(symbol: str) -> bool:
    """Check apakah symbol ada di Binance Spot."""
    try:
        r = requests.get(
            f"{BINANCE_SPOT}/ticker/price",
            params={"symbol": symbol}, timeout=5
        )
        return r.status_code == 200
    except Exception:
        return False


def _check_bybit(symbol: str) -> bool:
    """Check apakah symbol ada di Bybit linear futures."""
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/tickers",
            params={"category": "linear", "symbol": symbol}, timeout=5
        )
        if r.status_code != 200:
            return False
        data = r.json()
        return bool(data.get("result", {}).get("list"))
    except Exception:
        return False


def _check_okx(symbol: str) -> bool:
    """Check apakah symbol ada di OKX futures (swap)."""
    # OKX format: BTC-USDT-SWAP
    base = symbol.replace("USDT", "")
    inst_id = f"{base}-USDT-SWAP"
    try:
        r = requests.get(
            f"{OKX_BASE}/api/v5/market/ticker",
            params={"instId": inst_id}, timeout=5
        )
        if r.status_code != 200:
            return False
        data = r.json()
        return bool(data.get("data"))
    except Exception:
        return False


def _check_gate(symbol: str) -> bool:
    """Check apakah symbol ada di Gate.io futures."""
    # Gate.io format: BTC_USDT
    gate_sym = symbol.replace("USDT", "_USDT")
    try:
        r = requests.get(
            f"{GATE_BASE}/futures/usdt/contracts/{gate_sym}", timeout=5
        )
        return r.status_code == 200
    except Exception:
        return False


# ─── OHLCV fetchers per exchange ─────────────────────────────────────────────

def _klines_binance_futures(symbol: str, interval: str, limit: int = 101) -> Optional[list]:
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10
        )
        if r.status_code != 200:
            return None
        return [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
                for c in r.json()]
    except Exception as e:
        log.warning(f"Binance Futures klines error {symbol}: {e}")
        return None


def _klines_binance_spot(symbol: str, interval: str, limit: int = 101) -> Optional[list]:
    try:
        r = requests.get(
            f"{BINANCE_SPOT}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10
        )
        if r.status_code != 200:
            return None
        return [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
                for c in r.json()]
    except Exception as e:
        log.warning(f"Binance Spot klines error {symbol}: {e}")
        return None


def _klines_bybit(symbol: str, interval: str, limit: int = 101) -> Optional[list]:
    bybit_interval = INTERVAL_MAP["bybit"].get(interval, "60")
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "linear", "symbol": symbol,
                    "interval": bybit_interval, "limit": limit}, timeout=10
        )
        if r.status_code != 200:
            return None
        raw = r.json().get("result", {}).get("list", [])
        if not raw:
            return None
        # Bybit returns newest first — reverse
        candles = []
        for c in reversed(raw):
            candles.append({
                "time":   int(c[0]),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        return candles
    except Exception as e:
        log.warning(f"Bybit klines error {symbol}: {e}")
        return None


def _klines_okx(symbol: str, interval: str, limit: int = 101) -> Optional[list]:
    base     = symbol.replace("USDT", "")
    inst_id  = f"{base}-USDT-SWAP"
    okx_bar  = INTERVAL_MAP["okx"].get(interval, "1H")
    try:
        r = requests.get(
            f"{OKX_BASE}/api/v5/market/candles",
            params={"instId": inst_id, "bar": okx_bar, "limit": limit}, timeout=10
        )
        if r.status_code != 200:
            return None
        raw = r.json().get("data", [])
        if not raw:
            return None
        candles = []
        for c in reversed(raw):  # OKX newest first
            candles.append({
                "time":   int(c[0]),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        return candles
    except Exception as e:
        log.warning(f"OKX klines error {symbol}: {e}")
        return None


def _klines_gate(symbol: str, interval: str, limit: int = 101) -> Optional[list]:
    gate_sym   = symbol.replace("USDT", "_USDT")
    gate_itvl  = INTERVAL_MAP["gate"].get(interval, "1h")
    try:
        r = requests.get(
            f"{GATE_BASE}/futures/usdt/candlesticks",
            params={"contract": gate_sym, "interval": gate_itvl, "limit": limit}, timeout=10
        )
        if r.status_code != 200:
            return None
        raw = r.json()
        if not raw:
            return None
        return [{
            "time":   int(c["t"]) * 1000,
            "open":   float(c["o"]),
            "high":   float(c["h"]),
            "low":    float(c["l"]),
            "close":  float(c["c"]),
            "volume": float(c["v"]),
        } for c in raw]
    except Exception as e:
        log.warning(f"Gate klines error {symbol}: {e}")
        return None


# ─── Ticker fetchers ──────────────────────────────────────────────────────────

def _ticker_binance_futures(symbol: str) -> Optional[dict]:
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr",
                         params={"symbol": symbol}, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        return {"price": float(d["lastPrice"]),
                "change_24h": float(d["priceChangePercent"]),
                "volume_24h": float(d["quoteVolume"]),
                "raw": d}
    except Exception:
        return None


def _ticker_binance_spot(symbol: str) -> Optional[dict]:
    try:
        r = requests.get(f"{BINANCE_SPOT}/ticker/24hr",
                         params={"symbol": symbol}, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        return {"price": float(d["lastPrice"]),
                "change_24h": float(d["priceChangePercent"]),
                "volume_24h": float(d["quoteVolume"]),
                "raw": d}
    except Exception:
        return None


def _ticker_bybit(symbol: str) -> Optional[dict]:
    try:
        r = requests.get(f"{BYBIT_BASE}/v5/market/tickers",
                         params={"category": "linear", "symbol": symbol}, timeout=8)
        if r.status_code != 200:
            return None
        lst = r.json().get("result", {}).get("list", [])
        if not lst:
            return None
        d = lst[0]
        return {"price": float(d["lastPrice"]),
                "change_24h": float(d.get("price24hPcnt", 0)) * 100,
                "volume_24h": float(d.get("turnover24h", 0)),
                "raw": d}
    except Exception:
        return None


def _ticker_okx(symbol: str) -> Optional[dict]:
    base    = symbol.replace("USDT", "")
    inst_id = f"{base}-USDT-SWAP"
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/market/ticker",
                         params={"instId": inst_id}, timeout=8)
        if r.status_code != 200:
            return None
        lst = r.json().get("data", [])
        if not lst:
            return None
        d = lst[0]
        last = float(d["last"])
        open24 = float(d.get("open24h", last))
        chg = ((last - open24) / open24 * 100) if open24 > 0 else 0
        return {"price": last,
                "change_24h": round(chg, 2),
                "volume_24h": float(d.get("volCcy24h", 0)),
                "raw": d}
    except Exception:
        return None


def _ticker_gate(symbol: str) -> Optional[dict]:
    gate_sym = symbol.replace("USDT", "_USDT")
    try:
        r = requests.get(f"{GATE_BASE}/futures/usdt/tickers",
                         params={"contract": gate_sym}, timeout=8)
        if r.status_code != 200:
            return None
        lst = r.json()
        if not lst:
            return None
        d = lst[0]
        last = float(d["last"])
        chg  = float(d.get("change_percentage", 0))
        vol  = float(d.get("volume_24h_quote", 0))
        return {"price": last, "change_24h": chg, "volume_24h": vol, "raw": d}
    except Exception:
        return None


# ─── Exchange dispatch tables ─────────────────────────────────────────────────

_CHECKERS = {
    "binance_futures": _check_binance_futures,
    "binance_spot":    _check_binance_spot,
    "bybit":           _check_bybit,
    "okx":             _check_okx,
    "gate":            _check_gate,
}

_KLINES = {
    "binance_futures": _klines_binance_futures,
    "binance_spot":    _klines_binance_spot,
    "bybit":           _klines_bybit,
    "okx":             _klines_okx,
    "gate":            _klines_gate,
}

_TICKERS = {
    "binance_futures": _ticker_binance_futures,
    "binance_spot":    _ticker_binance_spot,
    "bybit":           _ticker_bybit,
    "okx":             _ticker_okx,
    "gate":            _ticker_gate,
}

# ─── Simple in-memory cache ───────────────────────────────────────────────────
# Hindari re-resolve symbol yang sama tiap request
_resolve_cache: dict = {}   # symbol → {"exchange": ..., "ts": ...}
_CACHE_TTL = 3600           # 1 jam


def _cache_get(symbol: str) -> Optional[str]:
    entry = _resolve_cache.get(symbol)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["exchange"]
    return None


def _cache_set(symbol: str, exchange: str):
    _resolve_cache[symbol] = {"exchange": exchange, "ts": time.time()}


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def resolve_symbol(user_input: str) -> Optional[dict]:
    """
    Resolve user input ke exchange yang valid.

    Returns:
        {
          "symbol":   "LABUSDT",
          "exchange": "bybit",          # key untuk _KLINES/_TICKERS
          "exchange_label": "Bybit",    # human-readable
          "has_futures": True,          # apakah ada OI/funding
        }
    Or None kalau tidak ditemukan di semua exchange.
    """
    inp = user_input.strip().upper().replace("/", "").replace("-", "")

    # Normalize ke USDT pair
    if inp.endswith("USDT"):
        symbol = inp
    elif inp.endswith("PERP") or inp.endswith("USD"):
        symbol = inp.replace("PERP", "USDT").replace("USD", "USDT")
    else:
        symbol = inp + "USDT"

    # Cache hit
    cached = _cache_get(symbol)
    if cached:
        log.debug(f"Cache hit: {symbol} → {cached}")
        has_fut = cached in ("binance_futures", "bybit", "okx", "gate")
        return {"symbol": symbol, "exchange": cached,
                "exchange_label": EXCHANGE_LABELS[cached], "has_futures": has_fut}

    # Walk fallback chain
    log.info(f"Resolving {symbol} across exchanges...")
    for exc in EXCHANGE_PRIORITY:
        try:
            found = _CHECKERS[exc](symbol)
            if found:
                log.info(f"  ✅ {symbol} found on {exc}")
                _cache_set(symbol, exc)
                has_fut = exc in ("binance_futures", "bybit", "okx", "gate")
                return {"symbol": symbol, "exchange": exc,
                        "exchange_label": EXCHANGE_LABELS[exc], "has_futures": has_fut}
            else:
                log.debug(f"  ❌ {symbol} not on {exc}")
        except Exception as e:
            log.debug(f"  ⚠️ {exc} check error: {e}")

    return None


def get_ohlcv(symbol: str, interval: str, exchange: str, limit: int = 101) -> Optional[list]:
    """
    Fetch OHLCV candles dari exchange tertentu.
    Returns list of {"time", "open", "high", "low", "close", "volume"} atau None.
    """
    fn = _KLINES.get(exchange)
    if not fn:
        log.warning(f"No klines fetcher for exchange: {exchange}")
        return None
    return fn(symbol, interval, limit)


def get_ticker(symbol: str, exchange: str) -> Optional[dict]:
    """
    Fetch ticker dari exchange tertentu.
    Returns {"price", "change_24h", "volume_24h", "raw"} atau None.
    """
    fn = _TICKERS.get(exchange)
    if not fn:
        return None
    return fn(symbol)  # semua _ticker_* hanya terima 1 arg (symbol)


def resolve_and_get_ohlcv(user_input: str, interval: str, limit: int = 101) -> Optional[dict]:
    """
    One-shot: resolve symbol + langsung fetch OHLCV.
    Returns {"symbol", "exchange", "exchange_label", "has_futures", "candles"} atau None.
    """
    info = resolve_symbol(user_input)
    if not info:
        return None
    candles = get_ohlcv(info["symbol"], interval, info["exchange"], limit)
    if not candles:
        return None
    return {**info, "candles": candles}


def format_not_found_message(user_input: str) -> str:
    """Pesan error kalau symbol tidak ditemukan di semua exchange."""
    sym = user_input.strip().upper()
    return (
        f"❌ <b>Symbol {sym} tidak ditemukan</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Sudah dicek di:\n"
        f"  • Binance Futures & Spot\n"
        f"  • Bybit\n"
        f"  • OKX\n"
        f"  • Gate.io\n\n"
        f"💡 Tips:\n"
        f"  • Coba pakai nama lengkap: <code>{sym}USDT</code>\n"
        f"  • Pastikan ejaan benar\n"
        f"  • Koin sangat baru mungkin belum ada di semua exchange\n"
        f"  • Beberapa koin hanya ada di DEX (tidak support)\n\n"
        f"Contoh valid: <code>BTC ETH SOL LAB ONDO</code>"
    )


def format_found_on_other_exchange(info: dict) -> str:
    """Notifikasi kalau koin ditemukan di exchange selain Binance Futures."""
    exc   = info["exchange_label"]
    sym   = info["symbol"]
    note  = ""
    if not info.get("has_futures") or info["exchange"] == "binance_spot":
        note = "\n⚠️ <i>OI/Funding data tidak tersedia (spot only)</i>"
    elif info["exchange"] != "binance_futures":
        note = f"\n⚠️ <i>OI/Funding terbatas (exchange: {exc})</i>"
    return f"ℹ️ <b>{sym}</b> ditemukan di <b>{exc}</b>{note}"
