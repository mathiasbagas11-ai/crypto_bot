"""Social sentiment analysis for crypto coins via Reddit and HackerNews.

Integrates last30days-skill library (MIT license, mvanhorn/last30days-skill)
to fetch keyless Reddit RSS and HackerNews Algolia data, then scores
bullish/bearish sentiment for a given coin symbol.

Modes:
  1. On-demand  : get_social_sentiment(symbol) + format_social_sentiment_telegram()
  2. Auto-scan  : run_social_scan(coins, send_telegram_fn) — scheduled every 30m
  3. Gate check : get_social_gate(symbol, direction) — integrated into confirmed_signal.py
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# ── Path setup so last30days_lib resolves its internal imports ────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from last30days_lib import reddit_rss, hackernews

log = logging.getLogger("social_sentiment")

# ── Cache file ────────────────────────────────────────────────────────────────
SOCIAL_CACHE_FILE = os.path.join(_HERE, "social_intelligence.json")
CACHE_MAX_AGE_HOURS = 1        # cache stale threshold for gate checks
SPIKE_ALERT_COOLDOWN_MIN = 90  # min minutes between repeated spike alerts per coin
SPIKE_MIN_POSTS = 12           # minimum posts to consider a spike real
SPIKE_MIN_SCORE = 35           # minimum |net_score| to trigger spike alert

# ── Sentiment keywords ────────────────────────────────────────────────────────
_BULLISH_WORDS = frozenset({
    "bullish", "moon", "pump", "breakout", "buy", "long", "accumulate",
    "accumulation", "support", "bounce", "rally", "surge", "rip", "ath",
    "upside", "uptrend", "recovery", "mooning", "gem", "undervalued",
    "bullrun", "bull", "green", "gains", "profit", "winning", "strong",
    "hold", "hodl", "target", "potential", "opportunity", "momentum",
    "macd", "golden", "oversold", "reversal", "rebound", "institutional",
})

_BEARISH_WORDS = frozenset({
    "bearish", "dump", "crash", "sell", "short", "resistance", "breakdown",
    "bear", "falling", "decline", "drop", "dip", "correction", "loss",
    "losing", "liquidation", "liquidate", "rugpull", "rug", "scam",
    "dead", "rekt", "capitulation", "downtrend", "downside", "overvalued",
    "sell-off", "selloff", "panic", "fear", "overbought", "rejection",
    "hack", "exploit", "banned", "delisted", "lawsuit", "sec", "fraud",
})

# Known coin name mappings (symbol → full names for better search coverage)
_COIN_NAMES: dict[str, str] = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
    "BNB": "BNB", "XRP": "XRP Ripple", "DOGE": "Dogecoin",
    "ADA": "Cardano", "AVAX": "Avalanche", "DOT": "Polkadot",
    "LINK": "Chainlink", "MATIC": "Polygon", "LTC": "Litecoin",
    "UNI": "Uniswap", "ATOM": "Cosmos", "NEAR": "NEAR Protocol",
    "ARB": "Arbitrum", "OP": "Optimism", "APT": "Aptos",
    "SUI": "Sui", "TON": "Toncoin", "INJ": "Injective",
    "SEI": "Sei", "TIA": "Celestia", "JUP": "Jupiter",
    "WIF": "dogwifhat", "PEPE": "Pepe", "FET": "Fetch.ai",
}

# Default coins scanned in auto-scan (matches bot's prepump scan list)
DEFAULT_SCAN_COINS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX",
    "LINK", "DOT", "MATIC", "NEAR", "ARB", "OP", "APT", "SUI",
    "INJ", "TIA", "WIF", "PEPE",
]


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache() -> dict[str, Any]:
    try:
        with open(SOCIAL_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict[str, Any]) -> None:
    try:
        with open(SOCIAL_CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to save social cache: {e}")


def get_cached_social(symbol: str) -> dict[str, Any] | None:
    """Return cached sentiment entry if fresh (< CACHE_MAX_AGE_HOURS old)."""
    cache = _load_cache()
    sym = _clean_symbol(symbol)
    entry = cache.get(sym)
    if not entry:
        return None
    updated = entry.get("last_updated")
    if updated:
        try:
            age = datetime.datetime.now() - datetime.datetime.fromisoformat(updated)
            if age.total_seconds() > CACHE_MAX_AGE_HOURS * 3600:
                return None
        except Exception:
            pass
    return entry


# ── Core helpers ──────────────────────────────────────────────────────────────

def _clean_symbol(symbol: str) -> str:
    return re.sub(r"(USDT|BUSD|PERP|USD)$", "", symbol.upper()).strip()


def _coin_query(symbol: str) -> str:
    sym = _clean_symbol(symbol)
    name = _COIN_NAMES.get(sym, sym)
    if sym == name:
        return f"{sym} crypto price"
    return f"{name} {sym} crypto"


def _date_range(days: int) -> tuple[str, str]:
    today = datetime.date.today()
    return (today - datetime.timedelta(days=days)).isoformat(), today.isoformat()


def _score_text(text: str) -> int:
    words = set(re.sub(r"[^\w\s]", " ", text.lower()).split())
    bullish = len(words & _BULLISH_WORDS)
    bearish = len(words & _BEARISH_WORDS)
    if bullish > bearish:
        return 1
    if bearish > bullish:
        return -1
    return 0


def _score_bar(score: int) -> str:
    blocks = 10
    filled = round((score + 100) / 200 * blocks)
    filled = max(0, min(blocks, filled))
    return "🟩" * filled + "⬜" * (blocks - filled)


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _fetch_reddit(query: str, from_date: str, to_date: str) -> list[dict]:
    crypto_subs = [
        "CryptoCurrency", "CryptoMarkets", "CryptoMoonShots",
        "Bitcoin", "ethtrader", "SatoshiStreetBets", "altcoin",
    ]
    try:
        posts = reddit_rss.search_rss(query=query, depth="default", subreddits=crypto_subs)
        return [p for p in posts
                if p.get("date") is None or (from_date <= (p.get("date") or "") <= to_date)]
    except Exception as e:
        log.debug(f"Reddit fetch error: {e}")
        return []


def _fetch_hackernews(query: str, from_date: str, to_date: str) -> list[dict]:
    try:
        response = hackernews.search_hackernews(query, from_date, to_date, depth="default")
        items = hackernews.parse_hackernews_response(response, query)
        return hackernews.enrich_top_stories(items, depth="quick")
    except Exception as e:
        log.debug(f"HN fetch error: {e}")
        return []


# ── On-demand sentiment ───────────────────────────────────────────────────────

def get_social_sentiment(symbol: str, days: int = 30) -> dict[str, Any]:
    """Fetch and score social sentiment for a crypto coin (on-demand).

    Args:
        symbol: Coin symbol e.g. "BTC", "SOLUSDT"
        days:   Lookback window in days (default 30)

    Returns:
        Dict with score, label, reddit_posts, hn_stories, counts.
    """
    query = _coin_query(symbol)
    sym_clean = _clean_symbol(symbol)
    from_date, to_date = _date_range(days)

    reddit_posts: list[dict] = []
    hn_stories: list[dict] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_r = ex.submit(_fetch_reddit, query, from_date, to_date)
        f_h = ex.submit(_fetch_hackernews, query, from_date, to_date)
        for fut in as_completed([f_r, f_h]):
            if fut is f_r:
                reddit_posts = fut.result()
            else:
                hn_stories = fut.result()

    bullish = bearish = neutral = 0
    for post in reddit_posts:
        s = _score_text(f"{post.get('title','')} {post.get('selftext','')}")
        if s > 0:
            bullish += 1
        elif s < 0:
            bearish += 1
        else:
            neutral += 1
    for story in hn_stories:
        s = _score_text(story.get("title", ""))
        if s > 0:
            bullish += 1
        elif s < 0:
            bearish += 1
        else:
            neutral += 1

    total = bullish + bearish + neutral
    net_score = round((bullish - bearish) / total * 100) if total > 0 else 0

    if net_score >= 35:
        label = "🟢 BULLISH"
    elif net_score >= 15:
        label = "🟡 SLIGHTLY BULLISH"
    elif net_score <= -35:
        label = "🔴 BEARISH"
    elif net_score <= -15:
        label = "🟠 SLIGHTLY BEARISH"
    else:
        label = "⚪ NEUTRAL"

    return {
        "symbol": sym_clean, "query": query, "score": net_score, "label": label,
        "reddit_posts": reddit_posts[:5], "hn_stories": hn_stories[:5],
        "bullish_count": bullish, "bearish_count": bearish,
        "neutral_count": neutral, "total_posts": total,
        "days": days, "from_date": from_date, "to_date": to_date,
    }


def format_social_sentiment_telegram(data: dict[str, Any]) -> str:
    """Format on-demand social sentiment as Telegram HTML message."""
    sym = data["symbol"]
    score = data["score"]
    label = data["label"]
    bullish = data["bullish_count"]
    bearish = data["bearish_count"]
    neutral = data["neutral_count"]
    total = data["total_posts"]
    reddit_posts = data["reddit_posts"]
    hn_stories = data["hn_stories"]

    lines = [
        f"📡 <b>SOCIAL SENTIMENT — {sym}</b> (Last {data['days']} days)",
        "",
        f"Sentiment: <b>{label}</b>",
        f"Score: <b>{score:+d}/100</b>  {_score_bar(score)}",
        f"📊 {bullish} 🟢  {bearish} 🔴  {neutral} ⚪  (total: {total} posts)",
        "",
    ]

    if reddit_posts:
        lines.append("📌 <b>Top Reddit Posts:</b>")
        for i, p in enumerate(reddit_posts[:3], 1):
            title = _truncate(p.get("title", ""), 80)
            sub = p.get("subreddit", "")
            upvotes = p.get("score", 0)
            comments = p.get("num_comments", 0)
            url = p.get("url", "")
            sub_tag = f" r/{sub}" if sub else ""
            eng = f" ↑{upvotes} 💬{comments}" if (upvotes or comments) else ""
            lines.append(f'  {i}. <a href="{url}">{title}</a>{sub_tag}{eng}')
        lines.append("")

    if hn_stories:
        lines.append("🟠 <b>HackerNews:</b>")
        for i, s in enumerate(hn_stories[:3], 1):
            title = _truncate(s.get("title", ""), 80)
            url = s.get("url") or s.get("hn_url", "")
            pts = s.get("engagement", {}).get("points", 0)
            lines.append(f'  {i}. <a href="{url}">{title}</a> ↑{pts}')
        lines.append("")

    lines.append(f"🔍 Query: <code>{data['query']}</code>")
    lines.append(f"📅 {data['from_date']} → {data['to_date']}")
    lines.append("⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)


# ── Gate integration ──────────────────────────────────────────────────────────

def get_social_gate(symbol: str, direction: str) -> tuple[int, bool, list[str]]:
    """Signal gate: return (score_adj, blocked, reasons) from cached social data.

    Integrates into confirmed_signal.py master score pipeline.

    Returns:
        score_adj: points added to master score (negative = penalty)
        blocked:   True if sentiment hard-contradicts direction (very strong signal)
        reasons:   Human-readable explanation strings
    """
    entry = get_cached_social(symbol)
    if not entry:
        return 0, False, []

    score = entry.get("score", 0)
    total = entry.get("total_posts", 0)
    label = entry.get("label", "NEUTRAL")

    if total < 5:
        return 0, False, []

    adj = 0
    blocked = False
    reasons: list[str] = []
    dir_up = direction.upper() in ("LONG", "BUY")

    # Strong social alignment → boost; strong contradiction → penalty/block
    if dir_up:
        if score >= 50:
            adj = +10
            reasons.append(f"📡 Social sangat bullish ({score:+d}) — Reddit+HN boost")
        elif score >= 30:
            adj = +5
            reasons.append(f"📡 Social bullish ({score:+d})")
        elif score <= -50 and total >= SPIKE_MIN_POSTS:
            adj = -10
            blocked = True
            reasons.append(f"📡 Social sangat bearish ({score:+d}) — LONG diblokir")
        elif score <= -30:
            adj = -5
            reasons.append(f"📡 Social bearish ({score:+d}) — counter LONG")
    else:  # SHORT
        if score <= -50:
            adj = +10
            reasons.append(f"📡 Social sangat bearish ({score:+d}) — SHORT boost")
        elif score <= -30:
            adj = +5
            reasons.append(f"📡 Social bearish ({score:+d})")
        elif score >= 50 and total >= SPIKE_MIN_POSTS:
            adj = -10
            blocked = True
            reasons.append(f"📡 Social sangat bullish ({score:+d}) — SHORT diblokir")
        elif score >= 30:
            adj = -5
            reasons.append(f"📡 Social bullish ({score:+d}) — counter SHORT")

    return adj, blocked, reasons


# ── Auto-scan ─────────────────────────────────────────────────────────────────

def run_social_scan(
    coins: list[str] | None = None,
    send_telegram_fn=None,
    days: int = 7,
) -> dict[str, Any]:
    """Periodic auto-scan: fetch social sentiment for all coins, update cache.

    Detects "spikes" (high engagement + strong sentiment) and fires Telegram
    alerts. Designed to run every 30 minutes via APScheduler.

    Args:
        coins:            List of clean symbols (e.g. ["BTC","ETH"]). Defaults
                          to DEFAULT_SCAN_COINS.
        send_telegram_fn: Callable(msg, parse_mode=) for alert delivery.
        days:             Lookback window (default 7 for recency sensitivity).

    Returns:
        Dict mapping symbol → entry dict (also saved to social_intelligence.json).
    """
    if coins is None:
        coins = DEFAULT_SCAN_COINS

    cache = _load_cache()
    results: dict[str, Any] = {}
    spike_alerts: list[dict] = []

    log.info(f"[SocialScan] Starting scan for {len(coins)} coins (last {days}d)")

    # Sequential to avoid hammering Reddit/HN with parallel requests
    for sym in coins:
        try:
            data = get_social_sentiment(sym, days=days)
            now_str = datetime.datetime.now().isoformat(timespec="seconds")

            entry: dict[str, Any] = {
                "score":        data["score"],
                "label":        data["label"],
                "total_posts":  data["total_posts"],
                "bullish":      data["bullish_count"],
                "bearish":      data["bearish_count"],
                "neutral":      data["neutral_count"],
                "top_titles":   [p.get("title", "")[:80]
                                 for p in data["reddit_posts"][:3]],
                "hn_titles":    [s.get("title", "")[:80]
                                 for s in data["hn_stories"][:3]],
                "last_updated": now_str,
            }

            # Preserve alert_sent timestamp from previous cache entry
            prev = cache.get(sym, {})
            if "alert_sent" in prev:
                entry["alert_sent"] = prev["alert_sent"]

            spike = _check_spike(sym, entry, prev)
            if spike:
                spike["symbol"] = sym
                spike_alerts.append(spike)
                entry["alert_sent"] = now_str

            cache[sym] = entry
            results[sym] = entry
            log.info(
                f"[SocialScan] {sym}: score={data['score']:+d} "
                f"label={data['label']} posts={data['total_posts']}"
            )
        except Exception as e:
            log.warning(f"[SocialScan] {sym} error: {e}")

    _save_cache(cache)

    if spike_alerts and send_telegram_fn:
        _dispatch_spike_alerts(spike_alerts, send_telegram_fn)

    log.info(f"[SocialScan] Done. {len(results)} coins updated, {len(spike_alerts)} spikes.")
    return results


def _check_spike(sym: str, entry: dict, prev: dict) -> dict | None:
    """Return spike data if this entry qualifies as a big news event."""
    score = entry["score"]
    total = entry["total_posts"]

    if total < SPIKE_MIN_POSTS:
        return None
    if abs(score) < SPIKE_MIN_SCORE:
        return None

    # Cooldown check — don't re-alert the same coin within cooldown window
    last_alert = prev.get("alert_sent")
    if last_alert:
        try:
            elapsed = (
                datetime.datetime.now()
                - datetime.datetime.fromisoformat(last_alert)
            )
            if elapsed.total_seconds() < SPIKE_ALERT_COOLDOWN_MIN * 60:
                return None
        except Exception:
            pass

    # Score must have worsened or improved significantly vs previous scan
    prev_score = prev.get("score", 0)
    delta = abs(score) - abs(prev_score)
    if abs(prev_score) >= SPIKE_MIN_SCORE and delta < 10:
        # Already knew about this trend, not a new spike
        return None

    return {
        "score":      score,
        "label":      entry["label"],
        "total":      total,
        "top_titles": entry.get("top_titles", []),
        "hn_titles":  entry.get("hn_titles", []),
        "prev_score": prev_score,
        "delta":      delta,
    }


def _dispatch_spike_alerts(alerts: list[dict], send_telegram_fn) -> None:
    """Send Telegram alerts for all detected social spikes."""
    if len(alerts) > 1:
        # Bundle multiple spikes into one message
        lines = ["📡 <b>SOCIAL SENTIMENT SPIKE ALERT</b>\n"]
        for a in alerts[:6]:
            sym = a["symbol"]
            score = a["score"]
            label = a["label"]
            total = a["total"]
            direction = "📈" if score > 0 else "📉"
            lines.append(
                f"{direction} <b>{sym}</b>: {label} ({score:+d}/100) — {total} posts"
            )
        lines.append("")
        lines.append("Gunakan <code>/social SYMBOL</code> untuk detail.")
        msg = "\n".join(lines)
        try:
            send_telegram_fn(msg, parse_mode="HTML")
        except Exception as e:
            log.warning(f"Failed to send bundle spike alert: {e}")
    else:
        a = alerts[0]
        sym = a["symbol"]
        score = a["score"]
        label = a["label"]
        total = a["total"]
        prev = a.get("prev_score", 0)
        direction = "🚀" if score > 0 else "⚠️"

        lines = [
            f"{direction} <b>SOCIAL SPIKE — {sym}</b>",
            "",
            f"Sentiment berubah: <b>{label}</b>",
            f"Score: <b>{score:+d}/100</b>  {_score_bar(score)}",
            f"Posts analyzed: {total}  |  Prev score: {prev:+d}",
        ]
        titles = a.get("top_titles", []) + a.get("hn_titles", [])
        if titles:
            lines.append("")
            lines.append("<b>Yang dibicarakan:</b>")
            for t in titles[:4]:
                lines.append(f"  • {_truncate(t, 80)}")
        lines.append("")
        lines.append(f"⚡ <code>/social {sym}</code> untuk detail lengkap")
        lines.append("⚠️ <i>Not financial advice. DYOR.</i>")

        try:
            send_telegram_fn("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            log.warning(f"Failed to send spike alert for {sym}: {e}")
