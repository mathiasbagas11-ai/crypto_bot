#!/usr/bin/env python3
"""
WHALE TRACKER v3
=================
Behavior baru — tidak spam alert:

1. CONTEXT PROVIDER (passive)
   get_whale_context_for_coin(coin) dipanggil dari v13 setiap sinyal keluar.
   Whale data muncul di bawah sinyal sebagai confluence tambahan.
   Tidak ada Telegram alert sendiri dari sini.

2. ACCUMULATION DETECTOR (event-driven, bukan polling spam)
   Scan posisi top wallet tiap WALLET_SCAN_INTERVAL menit.
   Alert Telegram HANYA kalau:
   - MIN_WALLETS_THRESHOLD atau lebih wallet berbeda open/tambah posisi
     di coin yang sama, arah yang sama, dalam satu scan window.

   Ini mendeteksi koordinasi/konsentrasi whale, bukan noise individual.

DIHAPUS dari v2:
- Large trade monitor (terlalu noisy, spam tiap 60 detik)
- Alert per perubahan posisi individual
"""

import os
import time
import logging
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
HL_INFO_URL             = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD_URL      = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

WALLET_SCAN_INTERVAL    = 10       # menit antara scan
TOP_WALLETS_COUNT       = 30       # jumlah top wallet yang ditrack
MIN_POSITION_USD        = 30_000   # minimum posisi per wallet agar dihitung
MIN_WALLETS_THRESHOLD   = 3        # minimum wallet akumulasi arah sama untuk alert
ACCUM_COOLDOWN_MINUTES  = 60       # cooldown per coin setelah alert dikirim

# ─────────────────────────────────────────────
# INTERNAL STATE
# ─────────────────────────────────────────────
_state = {
    "top_wallets":          [],  # list wallet dari leaderboard
    "wallet_positions":     {},  # address → {coin: {side, notional, ...}}
    "whale_position_bias":  {},  # coin → {long_notional, short_notional, long_count, short_count}
    "accumulation_alerts":  {},  # coin → last_alert datetime (cooldown)
}

_send_telegram_fn = None
_initialized      = False

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def fmt_usd(n: float) -> str:
    if n >= 1_000_000:  return f"${n/1_000_000:.2f}M"
    elif n >= 1_000:    return f"${n/1_000:.1f}K"
    else:               return f"${n:.0f}"

def _fmt_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) >= 10 else addr

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

def _send_msg(msg: str):
    if _send_telegram_fn:
        try:
            _send_telegram_fn(msg)
            return
        except Exception as e:
            log.warning(f"Whale send error: {e}")
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.debug(f"Whale direct send error: {e}")

# ─────────────────────────────────────────────
# HYPERLIQUID API
# ─────────────────────────────────────────────

def _hl_post(payload: dict) -> dict | None:
    try:
        r = requests.post(HL_INFO_URL, json=payload, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def _get_leaderboard() -> list:
    try:
        r = requests.get(HL_LEADERBOARD_URL, timeout=15)
        return r.json().get("leaderboardRows", []) if r.status_code == 200 else []
    except Exception:
        return []

def _get_wallet_positions(address: str) -> dict:
    data = _hl_post({"type": "clearinghouseState", "user": address})
    if not data:
        return {}
    positions = {}
    for pos in data.get("assetPositions", []):
        p        = pos.get("position", {})
        coin     = p.get("coin", "")
        szi      = float(p.get("szi", 0))
        entry_px = float(p.get("entryPx", 0) or 0)
        notional = abs(szi) * entry_px
        if szi == 0 or notional < MIN_POSITION_USD:
            continue
        positions[coin] = {
            "side":     "LONG" if szi > 0 else "SHORT",
            "size":     abs(szi),
            "entry_px": entry_px,
            "notional": notional,
            "pnl":      float(p.get("unrealizedPnl", 0) or 0),
        }
    return positions

# ─────────────────────────────────────────────
# ACCUMULATION DETECTOR
# ─────────────────────────────────────────────

def _in_cooldown(coin: str) -> bool:
    last = _state["accumulation_alerts"].get(coin)
    if not last:
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() / 60 < ACCUM_COOLDOWN_MINUTES

def _set_cooldown(coin: str):
    _state["accumulation_alerts"][coin] = datetime.now(timezone.utc)

def _detect_accumulation(old_pos: dict, new_pos: dict) -> list:
    """
    Cari coin yang di-open atau di-tambah oleh >= MIN_WALLETS_THRESHOLD wallet
    dalam satu scan window, arah yang sama.

    Returns list of events sorted by total notional desc.
    """
    coin_map: dict[str, dict[str, list]] = {}  # coin → {LONG: [...], SHORT: [...]}

    for addr, new_wallet in new_pos.items():
        old_wallet = old_pos.get(addr, {})
        for coin, pos in new_wallet.items():
            old_coin = old_wallet.get(coin)
            is_new   = old_coin is None
            is_add   = old_coin is not None and pos["notional"] > old_coin["notional"] * 1.10

            if not (is_new or is_add):
                continue

            if coin not in coin_map:
                coin_map[coin] = {"LONG": [], "SHORT": []}
            coin_map[coin][pos["side"]].append({
                "addr":     addr,
                "notional": pos["notional"],
                "entry":    pos["entry_px"],
                "is_new":   is_new,
            })

    events = []
    for coin, sides in coin_map.items():
        for side, wallets in sides.items():
            if len(wallets) >= MIN_WALLETS_THRESHOLD:
                events.append({
                    "coin":            coin,
                    "side":            side,
                    "wallet_count":    len(wallets),
                    "total_notional":  sum(w["notional"] for w in wallets),
                    "wallets":         wallets,
                })

    return sorted(events, key=lambda x: x["total_notional"], reverse=True)

def _build_accum_msg(event: dict) -> str:
    coin    = event["coin"]
    side    = event["side"]
    count   = event["wallet_count"]
    total   = event["total_notional"]
    wallets = event["wallets"]

    s_emoji  = "🟢" if side == "LONG" else "🔴"
    s_label  = "AKUMULASI / LONG" if side == "LONG" else "DISTRIBUSI / SHORT"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🐳 <b>WHALE ACCUMULATION</b>",
        f"🕐 {_ts()}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"{s_emoji} <b>{coin}</b> — {s_label}",
        f"👥 <b>{count} top wallet</b> open/tambah posisi bersamaan",
        f"💰 Total: <b>{fmt_usd(total)}</b>",
        "",
        "─── Wallet Detail ───",
    ]
    for i, w in enumerate(wallets[:5], 1):
        tag = "🆕" if w["is_new"] else "➕"
        lines.append(
            f"  {i}. {tag} <code>{_fmt_addr(w['addr'])}</code>  "
            f"{fmt_usd(w['notional'])} @ {fmt_usd(w['entry'])}"
        )
    lines += ["", "<i>⚠️ Not financial advice. DYOR.</i>"]
    return "\n".join(lines)

# ─────────────────────────────────────────────
# MAIN SCAN — background thread
# ─────────────────────────────────────────────

def _scan():
    rows = _get_leaderboard()
    if not rows:
        log.warning("Whale: leaderboard kosong")
        return

    # Parse + sort by all-time PnL
    wallets = []
    for row in rows:
        addr = row.get("ethAddress") or row.get("address", "")
        if not addr:
            continue
        pnl_raw = row.get("pnl", {})
        pnl_val = float(pnl_raw.get("allTime", 0)) if isinstance(pnl_raw, dict) else float(pnl_raw or 0)
        wallets.append({"address": addr, "pnl_alltime": pnl_val})

    wallets.sort(key=lambda x: x["pnl_alltime"], reverse=True)
    wallets = wallets[:TOP_WALLETS_COUNT]
    _state["top_wallets"] = wallets

    log.info(f"🐳 Whale scan: {len(wallets)} wallets")

    # Snapshot lama
    old_pos = {a: dict(p) for a, p in _state["wallet_positions"].items()}

    # Fetch posisi baru
    new_pos = {}
    for w in wallets:
        try:
            p = _get_wallet_positions(w["address"])
            if p:
                new_pos[w["address"]] = p
        except Exception as e:
            log.debug(f"Wallet fetch error: {e}")
        time.sleep(0.15)

    _state["wallet_positions"] = new_pos

    # Update coin bias (untuk get_whale_context_for_coin)
    bias: dict[str, dict] = {}
    for addr, positions in new_pos.items():
        for coin, pos in positions.items():
            if coin not in bias:
                bias[coin] = {"long_notional": 0, "short_notional": 0,
                              "long_count": 0, "short_count": 0}
            if pos["side"] == "LONG":
                bias[coin]["long_notional"] += pos["notional"]
                bias[coin]["long_count"]    += 1
            else:
                bias[coin]["short_notional"] += pos["notional"]
                bias[coin]["short_count"]    += 1
    _state["whale_position_bias"] = bias

    # Skip accumulation detection pada scan pertama (belum ada baseline)
    if not old_pos:
        log.info("Whale: baseline captured, mulai deteksi di scan berikutnya")
        return

    events = _detect_accumulation(old_pos, new_pos)
    if not events:
        log.info("Whale: tidak ada akumulasi terkoordinasi")
        return

    for ev in events:
        coin = ev["coin"]
        if _in_cooldown(coin):
            log.info(f"Whale: {coin} cooldown, skip")
            continue
        _send_msg(_build_accum_msg(ev))
        _set_cooldown(coin)
        log.info(f"🐳 Accum alert: {coin} {ev['side']} x{ev['wallet_count']} wallets {fmt_usd(ev['total_notional'])}")
        time.sleep(0.5)

def _scan_loop():
    interval = WALLET_SCAN_INTERVAL * 60
    log.info(
        f"🐳 Whale Tracker v3 aktif | "
        f"scan tiap {WALLET_SCAN_INTERVAL}m | "
        f"threshold {MIN_WALLETS_THRESHOLD} wallets | "
        f"cooldown {ACCUM_COOLDOWN_MINUTES}m"
    )
    time.sleep(20)  # biar v13 selesai init dulu
    while True:
        try:
            _scan()
        except Exception as e:
            log.warning(f"Whale loop error: {e}")
        time.sleep(interval)

# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

def get_whale_context_for_coin(coin: str) -> dict:
    """
    Dipanggil dari _build_gated_signal_message di v13 saat sinyal keluar.
    Return bias whale untuk coin tersebut berdasarkan posisi top wallet saat ini.

    coin: ticker tanpa USDT, e.g. "BTC", "SOL", "HBAR"
    """
    empty = {
        "wallet_long_notional":  0,
        "wallet_short_notional": 0,
        "wallet_long_count":     0,
        "wallet_short_count":    0,
        "whale_bias":            "NEUTRAL",
        "whale_summary":         "",
    }

    bias = _state["whale_position_bias"].get(coin)
    if not bias:
        return empty

    ln = bias.get("long_notional", 0)
    sn = bias.get("short_notional", 0)
    lc = bias.get("long_count", 0)
    sc = bias.get("short_count", 0)

    total    = ln + sn
    bias_str = "NEUTRAL"
    if total > 0:
        long_pct = ln / total
        if long_pct > 0.65:
            bias_str = "BULLISH"
        elif long_pct < 0.35:
            bias_str = "BEARISH"

    summary = f"{lc}L / {sc}S  ({fmt_usd(ln)} vs {fmt_usd(sn)})" if (lc or sc) else ""

    return {
        "wallet_long_notional":  ln,
        "wallet_short_notional": sn,
        "wallet_long_count":     lc,
        "wallet_short_count":    sc,
        "whale_bias":            bias_str,
        "whale_summary":         summary,
    }

def get_top_wallets(n: int = 5) -> list:
    return _state["top_wallets"][:n]

# ─────────────────────────────────────────────
# INIT (dipanggil dari v13 __main__ block)
# ─────────────────────────────────────────────

def init(telegram_fn=None):
    global _send_telegram_fn, _initialized
    if _initialized:
        return
    if telegram_fn:
        _send_telegram_fn = telegram_fn
    t = threading.Thread(target=_scan_loop, daemon=True, name="WhaleTracker-v3")
    t.start()
    _initialized = True
    log.info("🐳 Whale Tracker v3: OK")
    return t

# Backward-compat
def start(state=None, telegram_fn=None):
    return init(telegram_fn=telegram_fn)

# ─────────────────────────────────────────────
# STANDALONE
# ─────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S"
    )
    log.info("🐳 Whale Tracker v3 — Standalone Mode")
    log.info(f"   Scan interval : {WALLET_SCAN_INTERVAL} menit")
    log.info(f"   Top wallets   : {TOP_WALLETS_COUNT}")
    log.info(f"   Alert trigger : >= {MIN_WALLETS_THRESHOLD} wallet, arah sama")
    log.info(f"   Cooldown      : {ACCUM_COOLDOWN_MINUTES} menit per coin")
    init()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("🛑 Stopped")
