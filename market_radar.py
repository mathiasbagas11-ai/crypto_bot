#!/usr/bin/env python3
"""
MARKET RADAR — Hot Sector / Narrative Detection
================================================
Modul PURE LOGIC (seperti session_report.py): semua data fetching ada di
orchestration functions, semua formatting pure/testable.

Tidak ada import dari bot utama — fully standalone.
Cukup: requests, math, time, logging, datetime.

CoinGecko API:
  GET /api/v3/coins/categories   → list kategori + vol/chg 24h
  GET /api/v3/coins/markets      → top coins per kategori

Binance API:
  GET /api/v3/ticker/24hr?symbol=XXXUSDT → real-time 24h ticker
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("market_radar")

# Cache file untuk hot sectors — dipakai AI debate sebagai konteks 24h.
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hot_sectors_cache.json")

# ── Module-level constants ──────────────────────────────────────────────────

_WIB = timezone(timedelta(hours=7))

_EXCLUDE_CATS = frozenset({
    "cryptocurrency",
    "stablecoins",
    "wrapped-tokens",
    "exchange-based-tokens",
    "liquid-staking-tokens",
    "bridged-usdc",
    "wrapped-usdc",
    "tokenized-gold",
    "fan-token",
})

# CoinGecko symbol (lowercase) yang tidak bisa dimap ke "<UPPER>USDT"
_SYMBOL_OVERRIDE: dict[str, str] = {
    "icp":  "ICPUSDT",
    "gmt":  "GMTUSDT",
    "ton":  "TONUSDT",
    "wld":  "WLDUSDT",
}

# Rank icons untuk top-5
_RANK_ICONS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


# ── Data helpers (side effects — not unit-tested directly) ──────────────────

def _cg_get(path: str, params: dict | None = None) -> list | dict | None:
    """CoinGecko GET — returns parsed JSON or None on 429/error.

    URL: https://api.coingecko.com/api/v3{path}
    Headers: Accept: application/json
    timeout=15, log warning on 429 or exception.
    """
    url = f"https://api.coingecko.com/api/v3{path}"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 429:
            log.warning(f"CoinGecko 429 rate-limit on {path}")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"CoinGecko error {path}: {e}")
        return None


def _binance_tickers(symbols: list[str]) -> dict[str, dict]:
    """Fetch Binance 24hr ticker for each symbol.

    URL: https://api.binance.com/api/v3/ticker/24hr?symbol=X
    timeout=5 per call, best-effort (skip failures).
    Returns {symbol: {"pct": float, "vol": float, "price": float}}
    """
    result: dict[str, dict] = {}
    for sym in symbols:
        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/ticker/24hr",
                params={"symbol": sym},
                timeout=5,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            result[sym] = {
                "pct":   float(data.get("priceChangePercent", 0)),
                "vol":   float(data.get("quoteVolume", 0)),
                "price": float(data.get("lastPrice", 0)),
            }
        except Exception as e:
            log.warning(f"Binance ticker error {sym}: {e}")
    return result


def _to_binance_symbol(cg_symbol: str) -> str:
    """CoinGecko symbol (lowercase) → Binance USDT pair.

    e.g. "sol" → "SOLUSDT", with override dict for common edge cases.
    """
    sym = (cg_symbol or "").strip().lower()
    if sym in _SYMBOL_OVERRIDE:
        return _SYMBOL_OVERRIDE[sym]
    return sym.upper() + "USDT"


# ── Pure logic (unit-tested) ────────────────────────────────────────────────

def sector_score(chg24h: float, vol24h: float) -> float:
    """Score = chg24h * log10(max(vol24h, 1)).

    Returns 0.0 if vol24h <= 0.
    """
    if vol24h <= 0:
        return 0.0
    return chg24h * math.log10(max(vol24h, 1))


def filter_and_rank_sectors(
    raw_categories: list[dict],
    top_n: int = 5,
    min_vol_usd: float = 50_000_000,
) -> list[dict]:
    """Filter and rank CoinGecko categories by hot-sector score.

    Excludes generic/noise categories (frozenset _EXCLUDE_CATS).
    Filters categories with vol24h < min_vol_usd.
    Sorts by sector_score() descending.
    Returns top_n dicts with: id, name, chg24h, vol24h, score.
    """
    ranked = []
    for cat in (raw_categories or []):
        cat_id  = cat.get("id") or ""
        name    = cat.get("name") or cat_id
        chg24h  = cat.get("market_cap_change_24h") or 0.0
        # CoinGecko may expose volume in different keys
        vol24h  = (
            cat.get("volume_24h")
            or cat.get("total_volume")
            or 0.0
        )

        # Exclude noise categories
        if cat_id.lower() in _EXCLUDE_CATS:
            continue

        # Filter low volume
        if vol24h < min_vol_usd:
            continue

        score = sector_score(float(chg24h), float(vol24h))
        ranked.append({
            "id":     cat_id,
            "name":   name,
            "chg24h": float(chg24h),
            "vol24h": float(vol24h),
            "score":  score,
        })

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:top_n]


def _fmt_vol(vol: float) -> str:
    """Format volume: "$X.XB" if >= 1B, "$XXXM" if >= 1M, else "$XXXK"."""
    if vol >= 1_000_000_000:
        return f"${vol / 1_000_000_000:.1f}B"
    if vol >= 1_000_000:
        return f"${vol / 1_000_000:.0f}M"
    return f"${vol / 1_000:.0f}K"


def build_radar_message(sectors: list[dict], now_utc: datetime | None = None) -> str:
    """Format Telegram HTML message for Market Radar.

    Header: "📡 MARKET RADAR — Sektor Paling Rame" + WIB time + separator
    If empty sectors: "⚠️ Data sektor tidak tersedia saat ini."
    For each sector (rank icons 🥇🥈🥉4️⃣5️⃣):
      "{rank} <b>{name}</b>  {heat_icon} {chg:+.1f}%  Vol {vol_str}"
      heat_icon = 🔥 if chg >= 5, 🟢 if chg > 0, 🔴 if chg <= 0
    Coins line: "   🟢 ETH +3.2%  ·  🟢 AAVE +2.1%  ·  ⚪ UNI +0.1%"
    Trading tesis from top sector.
    Footer: "⚠️ <i>Not financial advice. DYOR.</i>"
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    wib_time = now_utc.astimezone(_WIB).strftime("%d %b %Y %H:%M") + " WIB"

    header = (
        f"📡 <b>MARKET RADAR — Sektor Paling Rame</b>\n"
        f"🕐 {wib_time}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    parts = [header]

    if not sectors:
        parts.append("⚠️ Data sektor tidak tersedia saat ini.")
        parts.append("⚠️ <i>Not financial advice. DYOR.</i>")
        return "\n\n".join(parts)

    for i, sec in enumerate(sectors):
        rank_icon = _RANK_ICONS[i] if i < len(_RANK_ICONS) else f"{i+1}."
        chg       = sec.get("chg24h", 0.0)
        vol       = sec.get("vol24h", 0.0)
        name      = sec.get("name", sec.get("id", "?"))
        vol_str   = _fmt_vol(vol)

        if chg >= 5:
            heat_icon = "🔥"
        elif chg > 0:
            heat_icon = "🟢"
        else:
            heat_icon = "🔴"

        sector_line = f"{rank_icon} <b>{name}</b>  {heat_icon} {chg:+.1f}%  Vol {vol_str}"

        coins = sec.get("coins", [])
        coin_parts = []
        for coin in coins[:3]:
            c_name = coin.get("name") or coin.get("symbol", "?")
            c_pct  = coin.get("pct", 0.0)
            if c_pct >= 1:
                c_icon = "🟢"
            elif c_pct > 0:
                c_icon = "🟢"
            elif c_pct == 0:
                c_icon = "⚪"
            else:
                c_icon = "🔴"
            # Use ⚪ for near-zero
            if abs(c_pct) < 0.5:
                c_icon = "⚪"
            coin_parts.append(f"{c_icon} {c_name} {c_pct:+.1f}%")

        block = sector_line
        if coin_parts:
            block += "\n   " + "  ·  ".join(coin_parts)

        parts.append(block)

    # Trading tesis from top sector
    top = sectors[0]
    top_name = top.get("name", top.get("id", "?"))
    top_chg  = top.get("chg24h", 0.0)
    tesis = (
        f"🎯 <b>Tesis:</b> <b>{top_name}</b> memimpin ({top_chg:+.1f}%) — "
        f"pertimbangkan coins di sektor ini sebagai alts target saat BTC sideways."
    )
    parts.append(tesis)

    parts.append("⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n\n".join(parts)


# ── Orchestration ───────────────────────────────────────────────────────────

def fetch_hot_sectors(top_n: int = 5) -> list[dict]:
    """Full pipeline: CoinGecko categories → rank → enrich with top coins.

    1. _cg_get("/coins/categories") → raw list
    2. filter_and_rank_sectors(raw, top_n)
    3. For each sector: fetch top 5 coins by volume via /coins/markets
       (sleep 0.35s between calls to respect rate limits)
    4. Map each coin symbol to Binance pair via _to_binance_symbol()
    5. _binance_tickers() for real-time prices
    6. Build coins list: prefer Binance data, fallback CoinGecko chg%/price
    7. Sort coins by pct desc, take top 3
    8. Return enriched sectors with "coins" field
    """
    raw = _cg_get("/coins/categories")
    if not raw:
        return []

    sectors = filter_and_rank_sectors(raw, top_n=top_n)
    if not sectors:
        return []

    enriched = []
    for sec in sectors:
        markets_params = {
            "vs_currency":              "usd",
            "category":                 sec["id"],
            "order":                    "volume_desc",
            "per_page":                 5,
            "page":                     1,
            "price_change_percentage":  "24h",
            "sparkline":                False,
        }
        market_data = _cg_get("/coins/markets", markets_params)
        time.sleep(0.35)

        coins_list = []
        if market_data:
            bin_symbols = [_to_binance_symbol(c.get("symbol", "")) for c in market_data]
            bin_data = _binance_tickers(bin_symbols)

            for coin, bsym in zip(market_data, bin_symbols):
                cg_name  = coin.get("name") or coin.get("symbol", "?")
                cg_pct   = coin.get("price_change_percentage_24h") or 0.0
                cg_price = coin.get("current_price") or 0.0

                if bsym in bin_data:
                    pct   = bin_data[bsym]["pct"]
                    price = bin_data[bsym]["price"]
                else:
                    pct   = cg_pct
                    price = cg_price

                coins_list.append({
                    "name":   cg_name,
                    "symbol": coin.get("symbol", ""),
                    "pct":    pct,
                    "price":  price,
                })

            coins_list.sort(key=lambda x: x["pct"], reverse=True)
            coins_list = coins_list[:3]

        enriched.append({**sec, "coins": coins_list})

    return enriched


# ── Sector cache (shared dengan ai_debate.py) ───────────────────────────────

def save_sectors_cache(sectors: list[dict], now_utc: datetime | None = None) -> None:
    """Persist hot sectors ke JSON (atomic write) untuk dipakai AI debate.

    Disimpan dengan timestamp UTC supaya konsumen bisa cek umur data.
    """
    if not sectors:
        return
    now_utc = now_utc or datetime.now(timezone.utc)
    payload = {
        "updated_utc": now_utc.isoformat(),
        "sectors": sectors,
    }
    try:
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_FILE)
    except Exception as e:
        log.warning(f"save_sectors_cache error: {e}")


def load_sectors_cache(max_age_hours: float = 48.0) -> dict | None:
    """Load cached hot sectors. Return None kalau tidak ada / kedaluwarsa.

    Return dict: {"updated_utc": str, "age_hours": float, "sectors": list}
    """
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception as e:
        log.warning(f"load_sectors_cache error: {e}")
        return None

    updated = payload.get("updated_utc", "")
    age_hours = 999.0
    try:
        dt = datetime.fromisoformat(updated)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        pass

    if age_hours > max_age_hours:
        return None
    return {
        "updated_utc": updated,
        "age_hours": age_hours,
        "sectors": payload.get("sectors", []),
    }


def build_sector_brief_for_ai(max_sectors: int = 4, max_age_hours: float = 48.0) -> str:
    """Ringkasan teks hot sectors 24h untuk konteks AI debate.

    Return "" kalau cache kosong/kedaluwarsa.
    Contoh:
      "AI Agents 🔥 +8.2% (top: TAO +12%, FET +9%) · DePIN 🟢 +3.1% ..."
    """
    cache = load_sectors_cache(max_age_hours=max_age_hours)
    if not cache or not cache.get("sectors"):
        return ""

    lines = []
    for sec in cache["sectors"][:max_sectors]:
        name = sec.get("name") or sec.get("id", "?")
        chg  = sec.get("chg24h", 0.0)
        heat = "🔥" if chg >= 5 else ("🟢" if chg > 0 else "🔴")
        coins = sec.get("coins", [])[:3]
        coin_str = ", ".join(
            f"{c.get('symbol','?').upper()} {c.get('pct',0):+.0f}%" for c in coins
        )
        line = f"{name} {heat} {chg:+.1f}%"
        if coin_str:
            line += f" (top: {coin_str})"
        lines.append(line)

    age = cache.get("age_hours", 0)
    header = f"Sektor terpanas 24h (data {age:.0f}j lalu):"
    return header + "\n" + "\n".join(f"  • {l}" for l in lines)
