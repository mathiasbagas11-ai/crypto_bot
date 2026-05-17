#!/usr/bin/env python3
"""
ECOSYSTEM SEASON DETECTOR v1.0
================================
Deteksi ecosystem mana yang lagi "season" saat ini
dan filter/boost coin scoring berdasarkan ecosystem aktif.

Season detection logic:
  - Ambil 7d/30d performance dari representative coins per ecosystem
  - Bandingkan relative performance antar ecosystem
  - Ecosystem dengan outperformance terkuat = ACTIVE SEASON

Ecosystems yang ditrack:
  BTC     → Bitcoin ecosystem (BTC, WBTC, STX, RUNE, ORDI, SATS)
  ETH     → Ethereum L1 (ETH, stETH, LDO, RPL, ENS)
  BASE    → Base L2 ecosystem (AERO, BRETT, DEGEN, TOSHI, BALD, HIGHER)
  SOL     → Solana ecosystem (SOL, JTO, PYTH, WIF, BONK, JUP, POPCAT)
  AI      → AI / DePIN (FET, TAO, RENDER, WLD, OCEAN, AKT, IO, GRASS)
  RWA     → Real World Assets (ONDO, POLYX, CFG, RIO, MKR, PENDLE)
  GAMING  → GameFi (BEAM, IMX, RONIN, GALA, AXS, PRIME)
  DeFi    → DeFi bluechip (UNI, AAVE, CRV, SNX, COMP, GMX, DYDX)
  MEME    → Meme coins (DOGE, SHIB, PEPE, FLOKI, WIF, BONK, NEIRO, MOG)
  BNB     → BNB Chain ecosystem (BNB, CAKE, TWT, BAKE, XVS)
  AVAX    → Avalanche ecosystem (AVAX, JOE, QI, PNG, XAVA)
  TON     → TON / Telegram (TON, DOGS, HMSTR, BLUM, NOT)

Output:
  - active_seasons: list ecosystem yang lagi hot (sorted by score)
  - coin_ecosystem_map: {coin_id: ecosystem}
  - season_boost: {coin_id: boost_score} — untuk inject ke quality_score

Integration:
  1. screen_coins() → filter + boost coins dari active ecosystem
  2. confirmed_signal.compute_master_score() → ecosystem alignment bonus
  3. /season command → kirim Telegram info season terkini
"""

import time
import logging
import requests
import numpy as np
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("ecosystem")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# ─────────────────────────────────────────────
# ECOSYSTEM DEFINITIONS
# Representative coins per ecosystem untuk performance sampling
# ─────────────────────────────────────────────

ECOSYSTEMS = {
    "BTC": {
        "label":       "₿ Bitcoin Season",
        "emoji":       "🟠",
        "coins":       ["bitcoin", "stacks", "thorchain", "ordinals"],
        "description": "Bitcoin & Bitcoin L2/meta-protocols pumping",
        "dump_desc":   "Bitcoin dominance dropping, altcoin season incoming",
    },
    "ETH": {
        "label":       "◆ Ethereum Season",
        "emoji":       "🔵",
        "coins":       ["ethereum", "lido-dao", "rocket-pool", "ethereum-name-service"],
        "description": "Ethereum L1 + liquid staking narrative hot",
        "dump_desc":   "ETH selling off, rotation to other L1s",
    },
    "BASE": {
        "label":       "🔵 Base Season",
        "emoji":       "🔵",
        "coins":       ["aerodrome-finance", "brett", "toshi"],
        "description": "Base L2 ecosystem (Coinbase chain) narratives hot",
        "dump_desc":   "Base ecosystem cooling down",
    },
    "SOL": {
        "label":       "◎ Solana Season",
        "emoji":       "🟣",
        "coins":       ["solana", "jito-governance-token", "pyth-network", "dogwifcoin", "bonk"],
        "description": "Solana ecosystem + meme coins on SOL pumping",
        "dump_desc":   "SOL ecosystem rotating out",
    },
    "AI": {
        "label":       "🤖 AI/DePIN Season",
        "emoji":       "🤖",
        "coins":       ["fetch-ai", "bittensor", "render-token", "worldcoin-wld", "akash-network"],
        "description": "AI & DePIN narrative driving strong moves",
        "dump_desc":   "AI narrative fading, tokens correcting",
    },
    "RWA": {
        "label":       "🏦 RWA Season",
        "emoji":       "🏦",
        "coins":       ["ondo-finance", "polymesh", "maker", "pendle"],
        "description": "Real World Assets tokenization narrative hot",
        "dump_desc":   "RWA narrative cooling",
    },
    "GAMING": {
        "label":       "🎮 Gaming Season",
        "emoji":       "🎮",
        "coins":       ["beam-2", "immutable-x", "ronin", "gala"],
        "description": "GameFi & NFT gaming narratives pumping",
        "dump_desc":   "Gaming tokens selling off",
    },
    "DEFI": {
        "label":       "💱 DeFi Season",
        "emoji":       "💱",
        "coins":       ["uniswap", "aave", "curve-dao-token", "dydx"],
        "description": "DeFi bluechips outperforming, yield farming narrative",
        "dump_desc":   "DeFi TVL declining, tokens underperforming",
    },
    "MEME": {
        "label":       "🐸 Meme Season",
        "emoji":       "🐸",
        "coins":       ["dogecoin", "shiba-inu", "pepe", "floki", "dogwifcoin"],
        "description": "Meme coins pumping hard, risk-on mode",
        "dump_desc":   "Meme coins dumping, risk-off rotation",
    },
    "BNB": {
        "label":       "🟡 BNB Chain Season",
        "emoji":       "🟡",
        "coins":       ["binancecoin", "pancakeswap-token"],
        "description": "BNB Chain ecosystem narrative active",
        "dump_desc":   "BNB ecosystem cooling",
    },
    "AVAX": {
        "label":       "🔺 Avalanche Season",
        "emoji":       "🔺",
        "coins":       ["avalanche-2", "joe"],
        "description": "Avalanche ecosystem outperforming",
        "dump_desc":   "AVAX ecosystem rotation out",
    },
    "TON": {
        "label":       "💎 TON Season",
        "emoji":       "💎",
        "coins":       ["the-open-network", "dogs-2", "notcoin"],
        "description": "TON/Telegram mini-app ecosystem hot",
        "dump_desc":   "TON ecosystem selling off",
    },
}

# ─────────────────────────────────────────────
# COIN → ECOSYSTEM MAPPING
# Untuk inject ecosystem info ke screen_coins()
# ─────────────────────────────────────────────

COIN_ECOSYSTEM_MAP = {
    # BTC
    "bitcoin": "BTC", "stacks": "BTC", "thorchain": "BTC",
    "wrapped-bitcoin": "BTC", "bitcoin-cash": "BTC",

    # ETH
    "ethereum": "ETH", "lido-dao": "ETH", "rocket-pool": "ETH",
    "ethereum-name-service": "ETH", "frax-share": "ETH",
    "wrapped-steth": "ETH", "reth": "ETH",

    # BASE
    "aerodrome-finance": "BASE", "brett": "BASE", "toshi": "BASE",
    "degen-base": "BASE", "bald": "BASE", "higher": "BASE",

    # SOL
    "solana": "SOL", "jito-governance-token": "SOL", "pyth-network": "SOL",
    "dogwifcoin": "SOL", "bonk": "SOL", "jupiter-exchange-solana": "SOL",
    "popcat": "SOL", "fartcoin": "SOL", "ai16z": "SOL",
    "raydium": "SOL", "helium": "SOL",

    # AI / DePIN
    "fetch-ai": "AI", "bittensor": "AI", "render-token": "AI",
    "worldcoin-wld": "AI", "akash-network": "AI", "ocean-protocol": "AI",
    "grass": "AI", "io-net": "AI", "near": "AI",
    "artificial-superintelligence-alliance": "AI",

    # RWA
    "ondo-finance": "RWA", "polymesh": "RWA", "maker": "RWA",
    "pendle": "RWA", "centrifuge": "RWA", "realio-network": "RWA",
    "maple": "RWA",

    # GAMING
    "beam-2": "GAMING", "immutable-x": "GAMING", "ronin": "GAMING",
    "gala": "GAMING", "axie-infinity": "GAMING", "prime": "GAMING",
    "pixels": "GAMING", "illuvium": "GAMING",

    # DeFi
    "uniswap": "DEFI", "aave": "DEFI", "curve-dao-token": "DEFI",
    "dydx": "DEFI", "synthetix": "DEFI", "compound-governance-token": "DEFI",
    "gmx": "DEFI", "hyperliquid": "DEFI", "jupiter-exchange-solana": "DEFI",
    "1inch": "DEFI", "balancer": "DEFI",

    # MEME
    "dogecoin": "MEME", "shiba-inu": "MEME", "pepe": "MEME",
    "floki": "MEME", "bonk": "MEME", "dogwifcoin": "MEME",
    "neiro-on-eth": "MEME", "mog-coin": "MEME", "brett": "MEME",
    "book-of-meme": "MEME", "mother-iggy": "MEME",

    # BNB
    "binancecoin": "BNB", "pancakeswap-token": "BNB",
    "trust-wallet-token": "BNB", "bakerytoken": "BNB",

    # AVAX
    "avalanche-2": "AVAX", "joe": "AVAX", "benqi": "AVAX",
    "pangolin": "AVAX",

    # TON
    "the-open-network": "TON", "dogs-2": "TON",
    "notcoin": "TON", "hamster-kombat": "TON",
}

# ─────────────────────────────────────────────
# SEASON CACHE
# ─────────────────────────────────────────────

_season_cache = {
    "last_update":    None,
    "active_seasons": [],
    "scores":         {},
    "dominant":       None,
    "btc_dominance":  0.0,
    "market_phase":   "UNKNOWN",
}
CACHE_TTL_MINUTES = 30  # refresh tiap 30 menit


def _cache_is_fresh() -> bool:
    last = _season_cache["last_update"]
    if not last:
        return False
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return elapsed < CACHE_TTL_MINUTES


# ─────────────────────────────────────────────
# FETCH PERFORMANCE DATA
# ─────────────────────────────────────────────

def _fetch_coins_performance(coin_ids: list) -> dict:
    """
    Fetch 7d/24h performance dari CoinGecko untuk list coins.
    Returns: {coin_id: {change_24h, change_7d, volume, market_cap}}
    """
    result = {}
    if not coin_ids:
        return result

    # Batch request (CoinGecko supports comma-separated ids)
    ids_str = ",".join(coin_ids[:50])
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids_str,
                "order": "market_cap_desc",
                "sparkline": False,
                "price_change_percentage": "24h,7d",
                "per_page": 50,
            },
            timeout=15
        )
        if r.status_code != 200:
            log.warning(f"CoinGecko performance fetch error: {r.status_code}")
            return result

        for c in r.json():
            cid = c.get("id", "")
            result[cid] = {
                "change_24h": c.get("price_change_percentage_24h") or 0,
                "change_7d":  c.get("price_change_percentage_7d_in_currency") or 0,
                "volume":     c.get("total_volume") or 0,
                "market_cap": c.get("market_cap") or 0,
                "name":       c.get("name", ""),
                "symbol":     c.get("symbol", "").upper(),
            }
    except Exception as e:
        log.warning(f"Performance fetch error: {e}")

    return result


def _fetch_btc_dominance() -> float:
    """Fetch BTC dominance dari CoinGecko global endpoint."""
    try:
        r = requests.get(f"{COINGECKO_BASE}/global", timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", {})
            return data.get("market_cap_percentage", {}).get("btc", 0)
    except Exception as e:
        log.debug(f"BTC dominance fetch error: {e}")
    return 0.0


# ─────────────────────────────────────────────
# SEASON SCORING
# ─────────────────────────────────────────────

def _score_ecosystem(eco_id: str, coin_performances: dict) -> dict:
    """
    Score satu ecosystem dari performance coins-nya.
    Returns: {score, avg_24h, avg_7d, best_coin, performers}
    """
    eco      = ECOSYSTEMS[eco_id]
    coins    = eco["coins"]
    perf_24h = []
    perf_7d  = []
    performers = []

    for cid in coins:
        p = coin_performances.get(cid)
        if not p:
            continue
        c24 = p["change_24h"]
        c7d = p["change_7d"]
        perf_24h.append(c24)
        perf_7d.append(c7d)
        performers.append({
            "id": cid, "symbol": p["symbol"],
            "change_24h": c24, "change_7d": c7d
        })

    if not perf_24h:
        return {"score": 0, "avg_24h": 0, "avg_7d": 0,
                "best_coin": "", "performers": [], "direction": "NEUTRAL"}

    avg_24h = np.mean(perf_24h)
    avg_7d  = np.mean(perf_7d)

    # Score formula:
    # - 7d performance: leading indicator (lebih penting untuk season detection)
    # - 24h performance: momentum saat ini
    # - Consistency: berapa banyak coins yang positif (ecosystem width)

    positive_7d  = sum(1 for p in perf_7d  if p > 0)
    positive_24h = sum(1 for p in perf_24h if p > 0)
    consistency  = (positive_7d + positive_24h) / (len(perf_7d) + len(perf_24h))

    # Raw score dari performance
    score = (avg_7d * 0.5) + (avg_24h * 0.3) + (consistency * 20)

    # Normalize ke 0-100
    # avg_7d +20% dan consistency 100% → approx 100
    score = max(-100, min(100, score))

    # Sort performers by 24h desc
    performers.sort(key=lambda x: x["change_24h"], reverse=True)
    best_coin = performers[0]["symbol"] if performers else ""

    direction = "BULLISH" if score > 5 else "BEARISH" if score < -5 else "NEUTRAL"

    return {
        "score":       round(score, 2),
        "avg_24h":     round(avg_24h, 2),
        "avg_7d":      round(avg_7d, 2),
        "best_coin":   best_coin,
        "performers":  performers[:3],
        "direction":   direction,
        "consistency": round(consistency * 100),
    }


# ─────────────────────────────────────────────
# MAIN: DETECT SEASON
# ─────────────────────────────────────────────

def detect_season(force_refresh: bool = False) -> dict:
    """
    Deteksi season yang aktif saat ini.
    Hasil di-cache 30 menit untuk hemat API call.

    Returns:
      active_seasons: [(eco_id, score_dict), ...] sorted by score desc
      dominant: eco_id yang paling kuat
      btc_dominance: float
      market_phase: ALTSEASON | BTC_SEASON | MIXED | BEAR
      all_scores: {eco_id: score_dict}
    """
    if not force_refresh and _cache_is_fresh():
        return _season_cache

    log.info("🔍 Detecting ecosystem seasons...")

    # 1. Fetch semua representative coins sekaligus
    all_coin_ids = list({cid for eco in ECOSYSTEMS.values() for cid in eco["coins"]})
    performances = {}

    # Batch per 50
    for i in range(0, len(all_coin_ids), 50):
        batch = all_coin_ids[i:i+50]
        performances.update(_fetch_coins_performance(batch))
        if i + 50 < len(all_coin_ids):
            time.sleep(1.2)  # CoinGecko rate limit

    # 2. Score each ecosystem
    all_scores = {}
    for eco_id in ECOSYSTEMS:
        all_scores[eco_id] = _score_ecosystem(eco_id, performances)

    # 3. Sort by score
    active_seasons = sorted(
        [(eco_id, s) for eco_id, s in all_scores.items()],
        key=lambda x: x[1]["score"],
        reverse=True
    )

    # 4. Dominant ecosystem (top scorer yang jelas outperform)
    dominant = None
    if active_seasons:
        top_id, top_score = active_seasons[0]
        # Dominant kalau score > 10 dan lead > 5 dari #2
        if top_score["score"] > 10:
            second_score = active_seasons[1][1]["score"] if len(active_seasons) > 1 else 0
            if top_score["score"] - second_score > 5:
                dominant = top_id

    # 5. BTC dominance
    btc_dom = _fetch_btc_dominance()

    # 6. Market phase
    btc_score  = all_scores.get("BTC", {}).get("score", 0)
    avg_alt    = np.mean([s["score"] for eco_id, s in all_scores.items() if eco_id != "BTC"])
    eth_score  = all_scores.get("ETH", {}).get("score", 0)
    meme_score = all_scores.get("MEME", {}).get("score", 0)

    if btc_dom > 58 and btc_score > avg_alt + 10:
        market_phase = "BTC_SEASON"
    elif btc_dom < 45 and avg_alt > 10:
        market_phase = "ALTSEASON"
    elif avg_alt < -10:
        market_phase = "BEAR"
    elif meme_score > 15:
        market_phase = "MEME_SEASON"
    else:
        market_phase = "MIXED"

    # Update cache
    _season_cache.update({
        "last_update":    datetime.now(timezone.utc),
        "active_seasons": active_seasons,
        "all_scores":     all_scores,
        "scores":         {eid: s["score"] for eid, s in all_scores.items()},
        "dominant":       dominant,
        "btc_dominance":  btc_dom,
        "market_phase":   market_phase,
    })

    hot_list = [f"{ECOSYSTEMS[e]['emoji']}{e}({s['score']:+.0f})"
                for e, s in active_seasons[:5]]
    log.info(f"✅ Season detected: phase={market_phase} | top={', '.join(hot_list)}")
    return _season_cache


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

def get_active_seasons(top_n: int = 3) -> list:
    """Return top N active ecosystem IDs."""
    data = detect_season()
    return [eco_id for eco_id, s in data["active_seasons"][:top_n]
            if s["score"] > 0]


def get_coin_ecosystem(coin_id: str) -> Optional[str]:
    """Return ecosystem ID untuk satu coin, atau None."""
    return COIN_ECOSYSTEM_MAP.get(coin_id.lower())


def get_ecosystem_boost(coin_id: str) -> float:
    """
    Return boost score (0-3.0) untuk coin berdasarkan ecosystem season.
    Dipakai di calculate_quality_score() untuk boost coins dari active ecosystem.

    Boost:
      Dominant ecosystem coin → +3.0
      Top-3 active ecosystem  → +2.0
      Top-5 active ecosystem  → +1.0
      Active (score > 0)      → +0.5
      Neutral / inactive      → 0
      Bearish ecosystem       → -1.0
    """
    eco = get_coin_ecosystem(coin_id)
    if not eco:
        return 0.0

    if not _cache_is_fresh():
        try:
            detect_season()
        except Exception:
            return 0.0

    scores    = _season_cache.get("scores", {})
    dominant  = _season_cache.get("dominant")
    seasons   = _season_cache.get("active_seasons", [])
    eco_score = scores.get(eco, 0)

    top_3 = [e for e, _ in seasons[:3]]
    top_5 = [e for e, _ in seasons[:5]]

    if dominant and eco == dominant:
        return 3.0
    elif eco in top_3:
        return 2.0
    elif eco in top_5:
        return 1.0
    elif eco_score > 0:
        return 0.5
    elif eco_score < -10:
        return -1.0
    return 0.0


def get_dump_ecosystem_penalty(coin_id: str) -> float:
    """
    Return penalty untuk SHORT signal alignment.
    Kalau ecosystem coin lagi bearish → SHORT signal lebih valid (bonus).
    Kalau ecosystem lagi bullish season → SHORT kurang reliable (penalty).
    """
    eco = get_coin_ecosystem(coin_id)
    if not eco:
        return 0.0
    if not _cache_is_fresh():
        return 0.0
    scores    = _season_cache.get("scores", {})
    eco_score = scores.get(eco, 0)
    if eco_score < -15:
        return 2.0    # ecosystem lagi bearish → SHORT lebih valid, bonus
    elif eco_score < -5:
        return 1.0
    elif eco_score > 15:
        return -1.5   # ecosystem bullish tapi mau SHORT → contra-trend, penalty
    return 0.0


def get_market_phase() -> str:
    if not _cache_is_fresh():
        try:
            detect_season()
        except Exception:
            return "UNKNOWN"
    return _season_cache.get("market_phase", "UNKNOWN")


# ─────────────────────────────────────────────
# SEASON-AWARE COIN FILTER
# Untuk dipakai di screen_coins()
# ─────────────────────────────────────────────

def filter_coins_by_season(coins: list) -> list:
    """
    Filter dan re-rank coins dari screen_coins() berdasarkan ecosystem season.

    1. Coins dari active ecosystem: dapat quality_score boost
    2. Coins dari bearish ecosystem: diberi penalty (mungkin masih masuk untuk SHORT)
    3. Coins yang ecosystemnya tidak dikenal: tidak berubah

    Input/output: list of coin dicts (format screen_coins)
    """
    if not _cache_is_fresh():
        try:
            detect_season()
        except Exception as e:
            log.warning(f"Season detection failed, skip filter: {e}")
            return coins

    active = get_active_seasons(top_n=5)
    market_phase = get_market_phase()

    for coin in coins:
        cid = coin.get("id", "")
        eco = get_coin_ecosystem(cid)
        boost = get_ecosystem_boost(cid)

        coin["ecosystem"]       = eco or "UNKNOWN"
        coin["ecosystem_boost"] = boost
        coin["season_aligned"]  = eco in active if eco else False

        # Apply boost ke quality_score
        if boost != 0:
            old_score = coin.get("quality_score", 0)
            coin["quality_score"] = round(min(10.0, max(0.0, old_score + boost * 0.5)), 2)

        # Tag market phase context
        coin["market_phase"] = market_phase

    # Re-sort setelah boost
    coins.sort(key=lambda x: x["quality_score"], reverse=True)
    return coins


# ─────────────────────────────────────────────
# TELEGRAM FORMATTERS
# ─────────────────────────────────────────────

def format_season_report() -> str:
    """Format season report untuk /season command."""
    data = detect_season(force_refresh=True)

    ts           = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    phase        = data.get("market_phase", "UNKNOWN")
    dominant     = data.get("dominant")
    btc_dom      = data.get("btc_dominance", 0)
    active       = data.get("active_seasons", [])

    phase_labels = {
        "ALTSEASON":  "🚀 ALTSEASON — Altcoins outperforming BTC",
        "BTC_SEASON": "🟠 BTC SEASON — Bitcoin dominance rising",
        "MEME_SEASON":"🐸 MEME SEASON — Risk-on, meme coins pumping",
        "MIXED":      "🔀 MIXED — No clear sector rotation",
        "BEAR":       "🐻 BEAR MARKET — Most ecosystems declining",
        "UNKNOWN":    "❓ UNKNOWN — Detecting...",
    }

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🌍 *ECOSYSTEM SEASON REPORT*",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📊 *Market Phase:* {phase_labels.get(phase, phase)}",
        f"₿  BTC Dominance: *{btc_dom:.1f}%*",
        "",
        "─────── TOP ECOSYSTEMS ───────",
    ]

    for i, (eco_id, score_data) in enumerate(active[:8], 1):
        eco    = ECOSYSTEMS[eco_id]
        score  = score_data["score"]
        avg24  = score_data["avg_24h"]
        avg7d  = score_data["avg_7d"]
        best   = score_data["best_coin"]
        dir_   = score_data["direction"]

        if score > 15:      heat = "🔥🔥"
        elif score > 5:     heat = "🔥"
        elif score > 0:     heat = "📈"
        elif score > -5:    heat = "➖"
        else:               heat = "📉"

        dom_tag = " ← *DOMINANT*" if eco_id == dominant else ""
        lines.append(
            f"{heat} *{eco['label']}*{dom_tag}\n"
            f"  Score: {score:+.0f} | 24h: {avg24:+.1f}% | 7d: {avg7d:+.1f}%\n"
            f"  Lead: {best}"
        )

    lines.append("")
    lines.append("─────── BEARISH ECOSYSTEMS ───────")
    bear_ecos = [(e, s) for e, s in active if s["score"] < -5][-3:]
    if bear_ecos:
        for eco_id, score_data in reversed(bear_ecos):
            eco   = ECOSYSTEMS[eco_id]
            score = score_data["score"]
            lines.append(f"📉 *{eco['label']}*: Score {score:+.0f} | {eco['dump_desc']}")
    else:
        lines.append("_Tidak ada ecosystem yang signifikan bearish_")

    lines.append("")
    lines.append("─────── TRADING IMPLICATION ───────")
    if phase == "BTC_SEASON":
        lines.append("💡 BTC season: fokus ke BTC, STX, RUNE. Altcoin underperform vs BTC.")
        lines.append("   SHORT bias lebih aman di non-BTC altcoins.")
    elif phase == "ALTSEASON":
        lines.append("💡 Altseason: semua altcoin bisa pump. Fokus ke ecosystem terkuat.")
        lines.append(f"   Prioritas: {', '.join([e for e, _ in active[:3]])}")
    elif phase == "MEME_SEASON":
        lines.append("💡 Meme season: high risk/high reward. DOGE, PEPE, WIF leading.")
        lines.append("   Scalp only, tidak hold lama.")
    elif phase == "BEAR":
        lines.append("💡 Bear market: SHORT bias. Hati-hati dengan semua LONG setup.")
        lines.append("   Focus pada bear continuation setups dan shorting relief rallies.")
    else:
        lines.append("💡 Mixed market: trade hanya coin dari ecosystem hot.")
        if active:
            top3 = [ECOSYSTEMS[e]["label"] for e, _ in active[:3]]
            lines.append(f"   Focus: {' | '.join(top3)}")

    lines.append("\n⚠️ _Season data dari CoinGecko. Bukan financial advice. DYOR._")
    return "\n".join(lines)


def format_coin_season_tag(coin_id: str) -> str:
    """
    Return short tag untuk ditambahkan ke coin analysis block.
    Contoh: "🔥 SOL Season" atau "📉 Bear (ETH)" atau ""
    """
    eco = get_coin_ecosystem(coin_id)
    if not eco:
        return ""
    if not _cache_is_fresh():
        return ""

    scores    = _season_cache.get("scores", {})
    eco_score = scores.get(eco, 0)
    dominant  = _season_cache.get("dominant")
    eco_info  = ECOSYSTEMS.get(eco, {})
    emoji     = eco_info.get("emoji", "")
    label     = eco_info.get("label", eco)

    if eco == dominant:
        return f"🔥🔥 *{label}* ← DOMINANT SEASON"
    elif eco_score > 15:
        return f"🔥 *{label}* (hot)"
    elif eco_score > 5:
        return f"📈 {label}"
    elif eco_score < -10:
        return f"📉 {label} (bearish eco)"
    elif eco_score < -5:
        return f"⚠️ {label} (cooling)"
    return f"{emoji} {label}"
