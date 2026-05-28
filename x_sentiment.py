#!/usr/bin/env python3
"""
X (TWITTER) SENTIMENT MODULE
==============================
Scraping tweet data untuk crypto signal — fokus pada:
  1. KOL (Key Opinion Leader) activity tracker
  2. Early narrative vs top signal detection
  3. Mention velocity (trending di X)
  4. DCA signal scoring per coin

Data Source:
  PRIMARY  : Nitter RSS (public instances, gratis, no API key)
  SECONDARY: Official Twitter API v2 (jika TWITTER_BEARER_TOKEN diset)

Setup Official API (optional, lebih reliable):
  1. Daftar di https://developer.twitter.com/en/portal/dashboard
  2. Buat project → pilih "Read" access
  3. Buat app → copy "Bearer Token"
  4. Tambah ke .env: TWITTER_BEARER_TOKEN=your_token_here

Usage:
  from x_sentiment import get_x_coin_analysis, get_dca_signal, format_x_block

  analysis = get_x_coin_analysis("BTC")
  dca      = get_dca_signal("SOL", price=150, price_change_7d=12.5, from_low_30d=18.0)
"""

import os
import re
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET
from collections import defaultdict

log = logging.getLogger("x_sentiment")

TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")

# ── Cache TTLs ─────────────────────────────────
_TWEET_CACHE_TTL  = 20 * 60    # 20 min — X data changes frequently
_DCA_CACHE_TTL    = 30 * 60    # 30 min
_cache: dict = {}

# ── Nitter Instances (fallback chain) ──────────
_NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.1d4.us",
]
_nitter_ok_index = 0   # remember last working instance

# ── Crypto KOL List ────────────────────────────
# Accounts with high conviction + verified track record
# Organized by category for conviction weighting
KOLS = {
    # On-chain / Whale trackers (highest signal quality)
    "on_chain": [
        "lookonchain",      # whale move tracker — very credible
        "WClementeIII",     # BTC on-chain analyst
        "woonomic",         # on-chain metrics
        "glassnode",        # on-chain data
        "ali_charts",       # on-chain technicals
    ],
    # Macro + BTC focused
    "macro": [
        "APompliano",       # BTC macro bull
        "RaoulGMI",         # macro/crypto
        "michael_saylor",   # BTC maximalist (signal: BTC accumulation)
        "PeterLBrandt",     # veteran TA trader
        "CarpeNoctom",      # macro trader
    ],
    # Altcoin / DeFi traders (higher noise, but early narrative detection)
    "alt_traders": [
        "CryptoKaleo",      # popular alt trader
        "CredibleCrypto",   # Elliott wave + TA
        "Pentosh1",         # alt trader
        "HsakaTrades",      # alt trader
        "PostyXBT",         # trader
        "VirtualBacon0x",   # DeFi/alt focused
        "CryptoCaesar3",    # alt picks
        "GiganticRebirth",  # DeFi narratives
        "inversebrah",      # contrarian signals
        "CryptoGodJohn",    # alt analysis
    ],
    # News + Data accounts
    "news": [
        "AltcoinDailyio",   # alt news
        "Cointelegraph",    # news
        "CoinDesk",         # news
        "TheBlock__",       # news/research
        "DeFiPulse",        # DeFi metrics
    ],
}

# Flatten + add weight multipliers
_KOL_WEIGHTS = {}
for _cat, _accts in KOLS.items():
    _w = {"on_chain": 3, "macro": 2, "alt_traders": 1.5, "news": 1}.get(_cat, 1)
    for _a in _accts:
        _KOL_WEIGHTS[_a.lower()] = _w

ALL_KOLS = [a for accts in KOLS.values() for a in accts]

# ── Sentiment Keywords ─────────────────────────
_BULLISH_KEYWORDS = [
    "moon", "pump", "buy", "buying", "accumulate", "dca", "load",
    "bullish", "breakout", "long", "hold", "hodl", "gem", "early",
    "undervalued", "narrative", "rotation", "season", "launching",
    "launching", "partnership", "milestone", "mainnet", "upgrade",
    "institutional", "adoption", "potential", "upside", "target",
    "flip", "strong", "support", "bounce", "reversal", "bottom",
    "accumulation", "conviction", "fundamental", "alpha",
]
_BEARISH_KEYWORDS = [
    "dump", "sell", "selling", "short", "rekt", "bearish", "crash",
    "rug", "scam", "avoid", "exit", "exit", "overbought", "bubble",
    "top", "distribution", "resistance", "rejection", "failed",
    "hack", "exploit", "fraud", "ponzi", "dead", "exit liquidity",
    "beware", "warning", "careful", "sus", "suspicious",
]
_TOP_SIGNAL_KEYWORDS = [
    "100x", "1000x", "everyone is buying", "cant go wrong",
    "safe bet", "guaranteed", "cant stop", "unstoppable",
    "all in", "max leverage", "mortgage", "life savings",
]

# Coin search terms for X queries
COIN_X_MAP = {
    "BTC":    "Bitcoin OR $BTC OR #BTC",
    "ETH":    "Ethereum OR $ETH OR #ETH",
    "SOL":    "Solana OR $SOL OR #SOL",
    "XRP":    "XRP OR Ripple OR $XRP",
    "BNB":    "$BNB OR Binance Coin",
    "ADA":    "Cardano OR $ADA",
    "AVAX":   "Avalanche OR $AVAX",
    "DOGE":   "Dogecoin OR $DOGE",
    "DOT":    "Polkadot OR $DOT",
    "LINK":   "Chainlink OR $LINK",
    "NEAR":   "NEAR Protocol OR $NEAR",
    "APT":    "Aptos OR $APT",
    "INJ":    "Injective OR $INJ",
    "SUI":    "Sui Network OR $SUI",
    "ARB":    "Arbitrum OR $ARB",
    "OP":     "Optimism OR $OP",
    "TIA":    "Celestia OR $TIA",
    "RENDER": "Render Network OR $RENDER OR $RNDR",
    "FET":    "Fetch.ai OR $FET",
    "PENDLE": "$PENDLE",
    "ENA":    "Ethena OR $ENA",
    "AAVE":   "Aave OR $AAVE",
    "JUP":    "Jupiter OR $JUP",
    "HYPE":   "Hyperliquid OR $HYPE",
    "ORDI":   "Ordinals OR $ORDI",
    "WIF":    "dogwifhat OR $WIF",
    "TON":    "TON OR $TON OR Toncoin",
    "SEI":    "Sei Network OR $SEI",
    "WLD":    "Worldcoin OR $WLD",
    "STRK":   "Starknet OR $STRK",
}

# ── Nitter RSS Scraper ─────────────────────────

def _get_nitter_instance() -> str:
    global _nitter_ok_index
    return _NITTER_INSTANCES[_nitter_ok_index % len(_NITTER_INSTANCES)]


def _fetch_nitter_rss(url: str) -> list:
    """
    Fetch and parse a nitter RSS feed.
    Returns list of tweet dicts: {text, author, date, url}
    """
    global _nitter_ok_index
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RSS reader)"}

    for attempt in range(len(_NITTER_INSTANCES)):
        inst  = _NITTER_INSTANCES[(_nitter_ok_index + attempt) % len(_NITTER_INSTANCES)]
        full_url = url.replace("__NITTER__", inst)
        try:
            r = requests.get(full_url, headers=headers, timeout=12)
            if not r.ok:
                continue
            root = ET.fromstring(r.text)
            items = []
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            # Handle both RSS and Atom formats
            channel = root.find("channel")
            if channel is not None:
                for item in channel.findall("item"):
                    title   = (item.findtext("title") or "").strip()
                    desc    = (item.findtext("description") or "")
                    link    = (item.findtext("link") or "")
                    pub     = (item.findtext("pubDate") or "")
                    creator = (item.findtext("{http://purl.org/dc/elements/1.1/}creator") or "")
                    # Extract plain text from HTML description
                    clean_desc = re.sub(r"<[^>]+>", " ", desc).strip()
                    text = title if len(title) > len(clean_desc) else clean_desc
                    items.append({
                        "text":   text[:500],
                        "author": creator or _extract_author_from_url(link),
                        "date":   pub[:16],
                        "url":    link,
                    })
            _nitter_ok_index = (_nitter_ok_index + attempt) % len(_NITTER_INSTANCES)
            return items[:30]
        except ET.ParseError:
            log.debug(f"Nitter XML parse error at {inst}")
        except Exception as e:
            log.debug(f"Nitter fetch error {inst}: {e}")

    log.warning("All nitter instances failed")
    return []


def _extract_author_from_url(url: str) -> str:
    m = re.search(r"nitter\.[^/]+/([^/]+)/status", url)
    return m.group(1) if m else ""


def search_tweets_nitter(query: str, max_results: int = 30) -> list:
    """Search tweets matching query via nitter RSS."""
    import urllib.parse
    enc = urllib.parse.quote(query)
    url = f"__NITTER__/search/rss?q={enc}&f=tweets"
    return _fetch_nitter_rss(url)[:max_results]


def get_kol_tweets_nitter(username: str, max_results: int = 10) -> list:
    """Fetch a specific KOL's recent tweets via nitter RSS."""
    url = f"__NITTER__/{username}/rss"
    tweets = _fetch_nitter_rss(url)[:max_results]
    for t in tweets:
        if not t.get("author"):
            t["author"] = username
    return tweets


# ── Official Twitter API v2 ────────────────────

def _twitter_api_search(query: str, max_results: int = 20) -> list:
    """Use official Twitter API v2 if bearer token is set."""
    if not TWITTER_BEARER_TOKEN:
        return []
    url = "https://api.twitter.com/2/tweets/search/recent"
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    params  = {
        "query":       f"{query} lang:en -is:retweet",
        "max_results": min(max_results, 100),
        "tweet.fields": "created_at,author_id,public_metrics",
        "expansions":  "author_id",
        "user.fields": "username,public_metrics",
    }
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code == 429:
            log.warning("Twitter API rate limit hit")
            return []
        if not r.ok:
            log.warning(f"Twitter API error: {r.status_code}")
            return []
        data  = r.json()
        users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}
        result = []
        for t in data.get("data", []):
            result.append({
                "text":   t.get("text", "")[:500],
                "author": users.get(t.get("author_id", ""), ""),
                "date":   t.get("created_at", "")[:16],
                "url":    f"https://twitter.com/i/web/status/{t['id']}",
                "likes":  t.get("public_metrics", {}).get("like_count", 0),
                "rts":    t.get("public_metrics", {}).get("retweet_count", 0),
            })
        return result
    except Exception as e:
        log.warning(f"Twitter API v2 error: {e}")
        return []


# ── Sentiment Scorer ───────────────────────────

def _score_tweet_sentiment(text: str) -> float:
    """
    Keyword-based sentiment score.
    Returns: -1.0 (very bearish) to +1.0 (very bullish)
    """
    t = text.lower()
    bull = sum(1 for kw in _BULLISH_KEYWORDS if kw in t)
    bear = sum(1 for kw in _BEARISH_KEYWORDS if kw in t)
    top  = sum(1 for kw in _TOP_SIGNAL_KEYWORDS if kw in t)

    if top >= 2:
        return 0.3   # Euphoric = potential top = caution, not full bearish

    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


def _is_kol(author: str) -> tuple:
    """Returns (is_kol: bool, weight: float, category: str)"""
    a = author.lower().lstrip("@")
    for cat, accts in KOLS.items():
        if a in [x.lower() for x in accts]:
            return True, _KOL_WEIGHTS.get(a, 1.0), cat
    return False, 0.0, ""


def _analyze_tweets(tweets: list, coin_sym: str = "") -> dict:
    """
    Analyze a list of tweets and return structured data.
    """
    if not tweets:
        return {
            "total_count": 0, "kol_count": 0, "kol_weighted": 0.0,
            "sentiment_avg": 0.0, "bull_tweets": 0, "bear_tweets": 0,
            "top_signal_count": 0, "kol_tweets": [], "top_tweets": [],
            "euphoria_detected": False,
        }

    kol_tweets = []
    kol_weighted = 0.0
    sentiments = []
    bull = bear = top_sig = 0

    for t in tweets:
        text    = t.get("text", "")
        author  = t.get("author", "")
        is_kol_, weight, cat = _is_kol(author)
        score   = _score_tweet_sentiment(text)
        sentiments.append(score)

        if score > 0.2:
            bull += 1
        elif score < -0.2:
            bear += 1

        # Check for euphoria keywords
        t_lower = text.lower()
        if any(kw in t_lower for kw in _TOP_SIGNAL_KEYWORDS):
            top_sig += 1

        if is_kol_:
            kol_weighted += weight * max(score, 0)  # only count positive KOL sentiment
            kol_tweets.append({
                "author":   author,
                "text":     text[:200],
                "score":    round(score, 2),
                "weight":   weight,
                "category": cat,
                "date":     t.get("date", ""),
                "url":      t.get("url", ""),
            })

    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
    euphoria = top_sig >= 3 or (avg_sentiment > 0.6 and len(tweets) > 10)

    return {
        "total_count":      len(tweets),
        "kol_count":        len(kol_tweets),
        "kol_weighted":     round(kol_weighted, 2),
        "sentiment_avg":    round(avg_sentiment, 3),
        "bull_tweets":      bull,
        "bear_tweets":      bear,
        "top_signal_count": top_sig,
        "kol_tweets":       sorted(kol_tweets, key=lambda x: -x["weight"])[:5],
        "top_tweets":       [t for t in tweets if _score_tweet_sentiment(t.get("text","")) > 0.3][:3],
        "euphoria_detected": euphoria,
    }


# ── Cycle Detection: Early Narrative vs Top ────

def _classify_narrative_cycle(
    tweet_data: dict,
    price_change_7d: float = 0.0,
    price_from_low_30d: float = 0.0,
) -> dict:
    """
    Determine whether X activity is an EARLY NARRATIVE or a TOP SIGNAL.

    Early Narrative signals:
      - KOLs starting to mention (kol_count 1-3)
      - Retail volume NOT yet explosive
      - Price has NOT yet moved dramatically
      - Sentiment building, not euphoric

    Late/Top signals:
      - Everyone talking about it (high total_count)
      - Euphoria keywords detected
      - Price already up significantly
      - Retail outnumber KOLs heavily

    Returns:
      cycle:      EARLY_NARRATIVE | BUILDING | PEAK | TOP_CAUTION | UNKNOWN
      conviction: HIGH | MEDIUM | LOW
      hold_horizon: LONG_TERM (4W+) | MEDIUM_TERM (1-4W) | SHORT_TERM | AVOID
      dca_signal: ACCUMULATE | START_DCA | WATCH | CAUTION | AVOID
    """
    kol_count      = tweet_data.get("kol_count", 0)
    kol_weighted   = tweet_data.get("kol_weighted", 0)
    total_count    = tweet_data.get("total_count", 0)
    sentiment      = tweet_data.get("sentiment_avg", 0)
    euphoria       = tweet_data.get("euphoria_detected", False)
    top_signals    = tweet_data.get("top_signal_count", 0)

    # Retail-to-KOL ratio (high = retail FOMO dominating)
    retail_count  = max(0, total_count - kol_count)
    retail_kol_r  = retail_count / max(kol_count, 1)

    # Price context
    price_pumped  = price_change_7d > 40 or price_from_low_30d > 60
    price_mid     = 20 < price_change_7d <= 40 or 30 < price_from_low_30d <= 60
    price_low     = price_change_7d <= 10 and price_from_low_30d <= 25

    # ── Classification logic ──
    if kol_count == 0 and total_count < 3:
        return {
            "cycle":        "UNKNOWN",
            "conviction":   "LOW",
            "hold_horizon": "UNKNOWN",
            "dca_signal":   "WATCH",
            "notes":        "Tidak ada data X yang cukup",
        }

    if euphoria or top_signals >= 3 or (price_pumped and retail_kol_r > 8):
        cycle = "TOP_CAUTION"
        conv  = "LOW"
        horizon = "AVOID"
        dca   = "AVOID"
        note  = f"Euforia terdeteksi di X — price already up {price_change_7d:+.0f}% (7d). Jangan DCA sekarang."

    elif kol_count >= 3 and price_low and retail_kol_r < 5 and not euphoria:
        cycle = "EARLY_NARRATIVE"
        conv  = "HIGH" if kol_weighted >= 6 else "MEDIUM"
        horizon = "LONG_TERM"
        dca   = "ACCUMULATE"
        note  = f"{kol_count} KOL aktif, harga belum bergerak — early narrative! Good DCA zone."

    elif kol_count >= 2 and not price_pumped and sentiment > 0.1:
        cycle = "BUILDING"
        conv  = "MEDIUM" if kol_weighted >= 4 else "LOW"
        horizon = "MEDIUM_TERM"
        dca   = "START_DCA" if price_low else "WATCH"
        note  = f"Narrative building — {kol_count} KOL bullish, price up {price_change_7d:+.0f}% (7d)."

    elif kol_count >= 2 and price_mid:
        cycle = "PEAK"
        conv  = "LOW"
        horizon = "SHORT_TERM"
        dca   = "CAUTION"
        note  = f"KOL aktif tapi harga sudah naik — mungkin sudah PEAK cycle."

    elif kol_count == 1 and sentiment > 0.15:
        cycle = "BUILDING"
        conv  = "LOW"
        horizon = "MEDIUM_TERM"
        dca   = "WATCH"
        note  = "1 KOL bullish — terlalu sedikit untuk konfirmasi."

    else:
        cycle = "UNKNOWN"
        conv  = "LOW"
        horizon = "UNKNOWN"
        dca   = "WATCH"
        note  = "Data X tersedia tapi sinyal tidak cukup kuat."

    return {
        "cycle":         cycle,
        "conviction":    conv,
        "hold_horizon":  horizon,
        "dca_signal":    dca,
        "notes":         note,
        "kol_count":     kol_count,
        "kol_weighted":  kol_weighted,
        "retail_kol_r":  round(retail_kol_r, 1),
    }


# ── Main Analysis Functions ────────────────────

def get_x_coin_analysis(symbol: str) -> dict:
    """
    Fetch and analyze X (Twitter) sentiment for a specific coin.
    Uses cache to avoid hammering nitter.

    Returns full analysis dict.
    """
    sym = symbol.upper().replace("USDT", "")
    cache_key = f"x_analysis_{sym}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["_ts"] < _TWEET_CACHE_TTL:
        return cached

    query = COIN_X_MAP.get(sym, f"${sym} OR #{sym} cryptocurrency")

    # Fetch tweets
    if TWITTER_BEARER_TOKEN:
        tweets = _twitter_api_search(query, max_results=50)
        source = "twitter_api"
    else:
        tweets = search_tweets_nitter(query, max_results=30)
        source = "nitter"

    # Also fetch from specific KOLs about this coin
    kol_specific = []
    if not TWITTER_BEARER_TOKEN:
        # Fetch from top KOLs — limit to 5 to avoid being too slow
        priority_kols = KOLS["on_chain"][:3] + KOLS["alt_traders"][:2]
        for kol in priority_kols:
            kol_tweets = get_kol_tweets_nitter(kol, max_results=5)
            # Filter: only include if they mention this coin
            sym_variants = [sym.lower(), f"${sym.lower()}", f"#{sym.lower()}"]
            coin_terms   = [t.lower() for t in COIN_X_MAP.get(sym, sym).split(" OR ")]
            for t in kol_tweets:
                text_l = t.get("text", "").lower()
                if any(v in text_l for v in sym_variants + coin_terms[:3]):
                    t["author"] = t.get("author") or kol
                    kol_specific.append(t)
            time.sleep(0.5)  # rate limit guard

    # Merge + deduplicate
    seen_urls = set()
    all_tweets = []
    for t in tweets + kol_specific:
        u = t.get("url", "")
        if u and u in seen_urls:
            continue
        seen_urls.add(u)
        all_tweets.append(t)

    analysis  = _analyze_tweets(all_tweets, sym)
    timestamp = datetime.now(timezone.utc).isoformat()

    result = {
        "symbol":    sym,
        "source":    source,
        "analysis":  analysis,
        "tweets":    all_tweets[:5],
        "timestamp": timestamp,
        "_ts":       time.time(),
        "_error":    len(all_tweets) == 0,
    }
    _cache[cache_key] = result
    return result


def get_dca_signal(
    symbol: str,
    price: float = 0,
    price_change_7d: float = 0.0,
    price_from_low_30d: float = 0.0,
) -> dict:
    """
    Generate a DCA recommendation for a coin based on:
      - X (Twitter) KOL activity + sentiment
      - Price context (7d change, distance from 30d low)
      - Narrative cycle phase

    Returns:
      dca_signal:    ACCUMULATE | START_DCA | WATCH | CAUTION | AVOID
      conviction:    HIGH | MEDIUM | LOW
      hold_horizon:  LONG_TERM | MEDIUM_TERM | SHORT_TERM | AVOID | UNKNOWN
      cycle:         EARLY_NARRATIVE | BUILDING | PEAK | TOP_CAUTION | UNKNOWN
      score:         0-100 (DCA attractiveness score)
      reasons:       list[str]
      kol_activity:  list of active KOL tweets
    """
    sym = symbol.upper().replace("USDT", "")
    cache_key = f"dca_{sym}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["_ts"] < _DCA_CACHE_TTL:
        return cached

    x_data = get_x_coin_analysis(sym)
    analysis = x_data.get("analysis", {})
    cycle_data = _classify_narrative_cycle(
        analysis,
        price_change_7d=price_change_7d,
        price_from_low_30d=price_from_low_30d,
    )

    dca_sig  = cycle_data.get("dca_signal", "WATCH")
    conv     = cycle_data.get("conviction", "LOW")
    horizon  = cycle_data.get("hold_horizon", "UNKNOWN")
    cycle    = cycle_data.get("cycle", "UNKNOWN")
    kol_cnt  = analysis.get("kol_count", 0)
    kol_w    = analysis.get("kol_weighted", 0)
    sentiment = analysis.get("sentiment_avg", 0)

    reasons = [cycle_data.get("notes", "")]

    # ── DCA Score (0-100) ──
    score = 0

    # Base score from cycle
    cycle_scores = {
        "EARLY_NARRATIVE": 70,
        "BUILDING":        50,
        "PEAK":            25,
        "TOP_CAUTION":     10,
        "UNKNOWN":         30,
    }
    score += cycle_scores.get(cycle, 30)

    # KOL conviction bonus
    if kol_cnt >= 5:
        score += 20
        reasons.append(f"🔥 {kol_cnt} KOL aktif di X — very high conviction")
    elif kol_cnt >= 3:
        score += 12
        reasons.append(f"👥 {kol_cnt} KOL aktif di X — good conviction")
    elif kol_cnt >= 1:
        score += 5
        reasons.append(f"👤 {kol_cnt} KOL menyebut {sym}")

    # High-weight KOL bonus (on-chain / macro)
    if kol_w >= 8:
        score += 10
        reasons.append(f"⚡ High-weight KOL active (on-chain/macro analyst) — very credible signal")
    elif kol_w >= 4:
        score += 5

    # Sentiment bonus
    if sentiment > 0.3:
        score += 8
        reasons.append(f"📊 Sentiment X positif ({sentiment:+.2f})")
    elif sentiment < -0.2:
        score -= 10
        reasons.append(f"📊 Sentiment X negatif ({sentiment:+.2f}) — banyak FUD")

    # Price context bonus/penalty
    if price_from_low_30d <= 15:
        score += 8
        reasons.append(f"💰 Price masih dekat 30d low (+{price_from_low_30d:.0f}%) — good entry")
    elif price_from_low_30d <= 30:
        score += 3
    elif price_from_low_30d >= 60:
        score -= 15
        reasons.append(f"⚠️ Price sudah jauh dari 30d low (+{price_from_low_30d:.0f}%) — late entry risk")

    # Cap
    score = max(0, min(100, score))

    # Force AVOID if euphoria
    if analysis.get("euphoria_detected"):
        score = min(score, 20)
        dca_sig = "AVOID"
        reasons.append("🚨 Euphoria di X terdeteksi — jangan DCA, kemungkinan sudah TOP")

    # Assemble KOL activity snippets
    kol_activity = [
        {
            "author":   t["author"],
            "text":     t["text"][:150],
            "weight":   t["weight"],
            "category": t["category"],
            "date":     t["date"],
        }
        for t in analysis.get("kol_tweets", [])[:4]
    ]

    result = {
        "symbol":       sym,
        "dca_signal":   dca_sig,
        "conviction":   conv,
        "hold_horizon": horizon,
        "cycle":        cycle,
        "score":        score,
        "reasons":      [r for r in reasons if r],
        "kol_activity": kol_activity,
        "kol_count":    kol_cnt,
        "sentiment":    round(sentiment, 3),
        "price_change_7d": price_change_7d,
        "price_from_low_30d": price_from_low_30d,
        "x_source":     x_data.get("source", "?"),
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "_ts":          time.time(),
        "_error":       x_data.get("_error", False),
    }
    _cache[cache_key] = result
    return result


# ── Telegram Formatter ─────────────────────────

_DCA_EMOJI = {
    "ACCUMULATE": "💎",
    "START_DCA":  "🟢",
    "WATCH":      "👀",
    "CAUTION":    "🟡",
    "AVOID":      "🔴",
}
_CYCLE_LABEL = {
    "EARLY_NARRATIVE": "🌱 EARLY NARRATIVE",
    "BUILDING":        "📈 BUILDING",
    "PEAK":            "⚡ PEAK",
    "TOP_CAUTION":     "🚨 TOP CAUTION",
    "UNKNOWN":         "❓ Unknown",
}
_HORIZON_LABEL = {
    "LONG_TERM":   "📅 Long term (4 minggu+) — cocok untuk DCA bertahap",
    "MEDIUM_TERM": "📅 Medium term (1-4 minggu) — masuk bertahap",
    "SHORT_TERM":  "⚡ Short term saja — jangan DCA besar",
    "AVOID":       "🚫 Hindari posisi sekarang",
    "UNKNOWN":     "❓ Tidak cukup data",
}


def format_x_block(x_data: dict, mode: str = "full") -> str:
    """Format X analysis for Telegram."""
    analysis = x_data.get("analysis", {})
    sym      = x_data.get("symbol", "")
    kol_cnt  = analysis.get("kol_count", 0)
    sent     = analysis.get("sentiment_avg", 0)
    s_emoji  = "🟢" if sent > 0.15 else "🔴" if sent < -0.15 else "⚪"
    src      = x_data.get("source", "?")

    if mode == "short":
        return (
            f"🐦 *X Sentiment {sym}:* {s_emoji} {sent:+.2f} | "
            f"KOLs: {kol_cnt} aktif | source: {src}"
        )

    lines = [
        f"🐦 *X (Twitter) Sentiment — {sym}*",
        f"Source: {src} | {datetime.now(timezone.utc).strftime('%d %b %H:%M UTC')}",
        "",
        f"{s_emoji} Sentiment: *{sent:+.2f}* | 🟢 {analysis.get('bull_tweets',0)} | 🔴 {analysis.get('bear_tweets',0)}",
        f"👥 KOL Activity: *{kol_cnt}* KOL aktif",
    ]
    if analysis.get("euphoria_detected"):
        lines.append("🚨 *EUPHORIA TERDETEKSI* — hati-hati, kemungkinan sudah TOP")

    kol_tweets = analysis.get("kol_tweets", [])
    if kol_tweets:
        lines.append("\n*Top KOL Mentions:*")
        for t in kol_tweets[:3]:
            w_label = "⚡" if t["weight"] >= 3 else "✅" if t["weight"] >= 2 else "  "
            lines.append(f"  {w_label} @{t['author']} ({t['category']}): _{t['text'][:100]}_")

    return "\n".join(lines)


def format_dca_block(dca: dict) -> str:
    """Format DCA signal for Telegram message."""
    sym       = dca.get("symbol", "")
    signal    = dca.get("dca_signal", "WATCH")
    score     = dca.get("score", 0)
    cycle     = dca.get("cycle", "UNKNOWN")
    horizon   = dca.get("hold_horizon", "UNKNOWN")
    conv      = dca.get("conviction", "LOW")
    kol_cnt   = dca.get("kol_count", 0)
    sent      = dca.get("sentiment", 0)
    p7d       = dca.get("price_change_7d", 0)
    p_low     = dca.get("price_from_low_30d", 0)
    reasons   = dca.get("reasons", [])
    kol_acts  = dca.get("kol_activity", [])
    src       = dca.get("x_source", "?")

    sig_emoji = _DCA_EMOJI.get(signal, "❓")
    cyc_label = _CYCLE_LABEL.get(cycle, cycle)
    hor_label = _HORIZON_LABEL.get(horizon, horizon)

    # Score bar
    filled = max(0, min(10, score // 10))
    bar    = f"`[{'█'*filled}{'░'*(10-filled)}]`"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💎 *DCA SIGNAL — {sym}*",
        f"🕐 {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"{sig_emoji} *Signal: {signal}*  |  Score: *{score}/100* {bar}",
        f"📊 Conviction: *{conv}*",
        f"🔄 Narrative Cycle: {cyc_label}",
        f"{hor_label}",
        "",
        "─────── X ANALYSIS ───────",
        f"🐦 KOLs aktif: *{kol_cnt}* | Sentiment: {'🟢' if sent>0.1 else '🔴' if sent<-0.1 else '⚪'} {sent:+.2f}",
        f"📈 Price 7d: *{p7d:+.1f}%* | Dari 30d low: *+{p_low:.1f}%*",
        f"Data source: {src}",
    ]

    if kol_acts:
        lines.append("\n*KOL Activity:*")
        for t in kol_acts[:3]:
            cat_e = "⚡" if t.get("category") in ("on_chain", "macro") else "👤"
            lines.append(f"  {cat_e} @{t['author']} [{t.get('date','')[:10]}]:")
            lines.append(f"    _{t['text'][:120]}_")

    if reasons:
        lines.append("\n*Analisis:*")
        for r in reasons[:4]:
            if r:
                lines.append(f"  • {r}")

    lines += [
        "",
        "─────── STRATEGI DCA ───────",
    ]

    if signal == "ACCUMULATE":
        lines += [
            "💎 Masuk bertahap — split 3-4x entry:",
            "   • Entry 1: 25% posisi sekarang",
            "   • Entry 2: 25% kalau turun -5% dari sekarang",
            "   • Entry 3: 25% kalau turun -10%",
            "   • Entry 4: 25% cadangan untuk dip besar",
            "   SL: -15% dari avg entry | TP: tidak ada, hold sampai narrative matang",
        ]
    elif signal == "START_DCA":
        lines += [
            "🟢 Mulai DCA kecil — jangan all-in:",
            "   • Entry 1: 30% posisi sekarang",
            "   • Entry 2: 30% kalau ada konfirmasi lanjutan",
            "   • Sisa 40%: tunggu dip atau konfirmasi KOL tambahan",
        ]
    elif signal == "CAUTION":
        lines += [
            "🟡 CAUTION — jangan DCA sekarang:",
            "   • Price sudah naik, tunggu pullback ke support",
            "   • Monitor KOL activity — kalau semakin ramai = potential top",
        ]
    elif signal == "AVOID":
        lines += [
            "🔴 JANGAN DCA — kondisi tidak bagus:",
            "   • Tunggu harga pullback signifikan (>20%)",
            "   • Tunggu narrative cycle reset",
        ]
    else:
        lines += [
            "👀 WATCH — belum ada signal kuat:",
            "   • Monitor dulu, tunggu lebih banyak KOL confirm",
            "   • Set alert di harga support terdekat",
        ]

    lines += [
        "",
        "⚠️ _DCA signal berdasarkan X sentiment + price context._",
        "⚠️ _Bukan financial advice. Selalu DYOR dan manage risk._",
    ]
    return "\n".join(lines)


def get_x_source_status() -> str:
    """Check which data source is active."""
    if TWITTER_BEARER_TOKEN:
        return "✅ Official Twitter API v2 (TWITTER_BEARER_TOKEN set)"
    return "🔄 Nitter RSS scraping (tidak butuh API key)"
