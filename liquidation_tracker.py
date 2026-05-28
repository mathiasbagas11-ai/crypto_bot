#!/usr/bin/env python3
"""
LIQUIDATION CASCADE TRACKER
=============================
Streams Binance futures forced liquidation orders via WebSocket.
Aggregates into rolling 15-minute windows per symbol.

Usage:
    from liquidation_tracker import start_liq_tracker, get_liq_data

    start_liq_tracker()   # call once at bot startup (non-blocking thread)
    data = get_liq_data("BTCUSDT")  # get current 15m liquidation stats
"""

import time
import json
import logging
import threading
from collections import defaultdict, deque

log = logging.getLogger("liq_tracker")

# Rolling window of liquidation events: symbol → deque of (timestamp, side, usd_value)
_liq_events: dict = defaultdict(lambda: deque(maxlen=2000))
_lock = threading.Lock()

# Thresholds (USD) for "surge" classification
# BTC gets higher thresholds; everything else uses altcoin threshold
SURGE_LONG_BTC    = 50_000_000   # $50M long liquidations in 15m = squeeze potential
SURGE_SHORT_BTC   = 50_000_000   # $50M short liquidations in 15m = dump potential
SURGE_LONG_ALT    = 5_000_000    # $5M for altcoins
SURGE_SHORT_ALT   = 5_000_000

WINDOW_SECONDS = 15 * 60   # 15-minute rolling window
_RECONNECT_DELAY = 5       # seconds between reconnect attempts

_tracker_started = False
_tracker_thread = None


def _record_event(symbol: str, side: str, qty: float, price: float):
    """Record a liquidation event with current timestamp."""
    usd_val = qty * price
    with _lock:
        _liq_events[symbol].append((time.time(), side, usd_val))


def _prune_window(symbol: str) -> list:
    """Return events within the rolling window (does NOT modify the deque)."""
    cutoff = time.time() - WINDOW_SECONDS
    with _lock:
        events = list(_liq_events[symbol])
    return [(t, s, v) for t, s, v in events if t >= cutoff]


def get_liq_data(symbol: str) -> dict:
    """
    Return aggregated liquidation stats for the last 15 minutes.

    Returns:
        long_liq_usd:   total USD value of long liquidations (= forced sells)
        short_liq_usd:  total USD value of short liquidations (= forced buys)
        liq_surge_long: True if long liq exceeded threshold (squeeze fuel)
        liq_surge_short: True if short liq exceeded threshold (dump fuel)
        net_liq_bias:   "LONG_SQUEEZE" | "SHORT_SQUEEZE" | "BALANCED"
        event_count:    total events in window
    """
    events = _prune_window(symbol)
    long_usd  = sum(v for _, s, v in events if s == "SELL")   # long positions → forced sell
    short_usd = sum(v for _, s, v in events if s == "BUY")    # short positions → forced buy

    is_btc = symbol.upper().startswith("BTC")
    surge_long_thr  = SURGE_LONG_BTC  if is_btc else SURGE_LONG_ALT
    surge_short_thr = SURGE_SHORT_BTC if is_btc else SURGE_SHORT_ALT

    surge_long  = long_usd  >= surge_long_thr
    surge_short = short_usd >= surge_short_thr

    if surge_long and not surge_short:
        bias = "LONG_SQUEEZE"    # longs getting liquidated → potential bounce
    elif surge_short and not surge_long:
        bias = "SHORT_SQUEEZE"   # shorts getting liquidated → potential dump
    elif surge_long and surge_short:
        bias = "BOTH_SURGE"
    else:
        bias = "BALANCED"

    return {
        "long_liq_usd":   round(long_usd,  0),
        "short_liq_usd":  round(short_usd, 0),
        "liq_surge_long":  surge_long,
        "liq_surge_short": surge_short,
        "net_liq_bias":   bias,
        "event_count":    len(events),
    }


def get_all_surge_symbols() -> list:
    """Return list of symbols currently experiencing any liquidation surge."""
    result = []
    with _lock:
        symbols = list(_liq_events.keys())
    for sym in symbols:
        d = get_liq_data(sym)
        if d["liq_surge_long"] or d["liq_surge_short"]:
            result.append((sym, d))
    return result


def _ws_loop():
    """WebSocket main loop with auto-reconnect."""
    try:
        import websocket
    except ImportError:
        log.error("websocket-client not installed — liquidation tracker disabled. Run: pip install websocket-client")
        return

    url = "wss://fstream.binance.com/ws/!forceOrder@arr"

    def on_message(ws, raw):
        try:
            msg = json.loads(raw)
            # Binance force order stream wraps event in {"stream":..., "data":{...}}
            # or sends directly for single-stream subscriptions
            if "data" in msg:
                evt = msg["data"]
            else:
                evt = msg
            order = evt.get("o", {})
            sym   = order.get("s", "")
            side  = order.get("S", "")   # "BUY" or "SELL"
            qty   = float(order.get("q", 0))
            price = float(order.get("ap", 0))  # average fill price
            if sym and side and qty > 0 and price > 0:
                _record_event(sym, side, qty, price)
        except Exception as e:
            log.debug(f"Liq parse error: {e}")

    def on_error(ws, error):
        log.warning(f"Liquidation WS error: {error}")

    def on_close(ws, code, msg):
        log.info(f"Liquidation WS closed ({code})")

    def on_open(ws):
        log.info("Liquidation cascade tracker connected")

    while True:
        try:
            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log.warning(f"Liquidation WS exception: {e}")
        time.sleep(_RECONNECT_DELAY)


def start_liq_tracker():
    """Start the liquidation WebSocket tracker in a daemon thread (call once)."""
    global _tracker_started, _tracker_thread
    if _tracker_started:
        return
    _tracker_started = True
    _tracker_thread = threading.Thread(target=_ws_loop, daemon=True, name="liq-tracker")
    _tracker_thread.start()
    log.info("Liquidation cascade tracker started")
