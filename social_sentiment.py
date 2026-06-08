"""Social sentiment analysis for crypto coins via Reddit and HackerNews.

Integrates last30days-skill library (MIT license, mvanhorn/last30days-skill)
to fetch keyless Reddit RSS and HackerNews Algolia data, then scores
bullish/bearish sentiment for a given coin symbol.
"""

from __future__ import annotations

import datetime
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# ── Path setup so last30days_lib resolves its internal imports ────────────────
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from last30days_lib import reddit_rss, hackernews

# ── Sentiment keyword lists ───────────────────────────────────────────────────
_BULLISH_WORDS = frozenset({
    "bullish", "moon", "pump", "breakout", "buy", "long", "accumulate",
    "accumulation", "support", "bounce", "rally", "surge", "rip", "ath",
    "upside", "uptrend", "recovery", "mooning", "gem", "undervalued",
    "bullrun", "bull", "green", "gains", "profit", "winning", "strong",
    "hold", "hodl", "target", "potential", "opportunity", "momentum",
})

_BEARISH_WORDS = frozenset({
    "bearish", "dump", "crash", "sell", "short", "resistance", "breakdown",
    "bear", "falling", "decline", "drop", "dip", "correction", "loss",
    "losing", "liquidation", "liquidate", "rugpull", "rug", "scam",
    "dead", "rekt", "capitulation", "downtrend", "downside", "overvalued",
    "sell-off", "selloff", "panic", "fear", "overbought",
})

_NOISE_COINS = frozenset({"crypto", "defi", "nft", "blockchain", "altcoin"})


def _score_text(text: str) -> int:
    """Return +1 (bullish), -1 (bearish), or 0 (neutral) for a text snippet."""
    words = set(re.sub(r"[^\w\s]", " ", text.lower()).split())
    bullish = len(words & _BULLISH_WORDS)
    bearish = len(words & _BEARISH_WORDS)
    if bullish > bearish:
        return 1
    if bearish > bullish:
        return -1
    return 0


def _coin_query(symbol: str) -> str:
    """Build a search query from a coin symbol (strips USDT/BUSD suffix)."""
    sym = re.sub(r"(USDT|BUSD|PERP|USD)$", "", symbol.upper()).strip()
    full_names = {
        "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
        "BNB": "BNB", "XRP": "XRP", "DOGE": "Dogecoin", "ADA": "Cardano",
        "AVAX": "Avalanche", "DOT": "Polkadot", "LINK": "Chainlink",
        "MATIC": "Polygon", "LTC": "Litecoin", "UNI": "Uniswap",
        "ATOM": "Cosmos", "NEAR": "NEAR Protocol", "ARB": "Arbitrum",
        "OP": "Optimism", "APT": "Aptos", "SUI": "Sui", "TON": "Toncoin",
        "INJ": "Injective", "SEI": "Sei", "TIA": "Celestia",
        "JUP": "Jupiter", "WIF": "dogwifhat", "PEPE": "Pepe",
    }
    name = full_names.get(sym, sym)
    if sym == name:
        return f"{sym} crypto price"
    return f"{name} {sym} crypto"


def _date_range(days: int = 30) -> tuple[str, str]:
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days)
    return start.isoformat(), today.isoformat()


def _fetch_reddit(query: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    """Fetch Reddit posts via keyless RSS (no API key required)."""
    crypto_subs = ["CryptoCurrency", "CryptoMarkets", "CryptoMoonShots",
                   "Bitcoin", "ethtrader", "SatoshiStreetBets"]
    try:
        posts = reddit_rss.search_rss(
            query=query,
            depth="default",
            subreddits=crypto_subs,
        )
        return [p for p in posts
                if p.get("date") is None or (from_date <= (p.get("date") or "") <= to_date)]
    except Exception:
        return []


def _fetch_hackernews(query: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    """Fetch HackerNews stories via free Algolia API (no key required)."""
    try:
        response = hackernews.search_hackernews(query, from_date, to_date, depth="default")
        items = hackernews.parse_hackernews_response(response, query)
        return hackernews.enrich_top_stories(items, depth="quick")
    except Exception:
        return []


def get_social_sentiment(symbol: str, days: int = 30) -> dict[str, Any]:
    """Fetch and score social sentiment for a crypto coin.

    Args:
        symbol: Coin symbol e.g. "BTC", "SOLUSDT"
        days: Lookback window in days (default 30)

    Returns:
        Dict with keys: symbol, query, score, label, reddit_posts,
        hn_stories, bullish_count, bearish_count, neutral_count, total_posts
    """
    query = _coin_query(symbol)
    sym_clean = re.sub(r"(USDT|BUSD|PERP|USD)$", "", symbol.upper()).strip()
    from_date, to_date = _date_range(days)

    reddit_posts: list[dict] = []
    hn_stories: list[dict] = []

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_reddit = ex.submit(_fetch_reddit, query, from_date, to_date)
        f_hn = ex.submit(_fetch_hackernews, query, from_date, to_date)
        for future in as_completed([f_reddit, f_hn]):
            if future is f_reddit:
                reddit_posts = future.result()
            else:
                hn_stories = future.result()

    bullish = bearish = neutral = 0
    for post in reddit_posts:
        text = f"{post.get('title', '')} {post.get('selftext', '')}"
        s = _score_text(text)
        if s > 0:
            bullish += 1
        elif s < 0:
            bearish += 1
        else:
            neutral += 1

    for story in hn_stories:
        text = story.get("title", "")
        s = _score_text(text)
        if s > 0:
            bullish += 1
        elif s < 0:
            bearish += 1
        else:
            neutral += 1

    total = bullish + bearish + neutral
    if total > 0:
        net_score = round((bullish - bearish) / total * 100)
    else:
        net_score = 0

    if net_score >= 30:
        label = "🟢 BULLISH"
    elif net_score >= 10:
        label = "🟡 SLIGHTLY BULLISH"
    elif net_score <= -30:
        label = "🔴 BEARISH"
    elif net_score <= -10:
        label = "🟠 SLIGHTLY BEARISH"
    else:
        label = "⚪ NEUTRAL"

    return {
        "symbol": sym_clean,
        "query": query,
        "score": net_score,
        "label": label,
        "reddit_posts": reddit_posts[:5],
        "hn_stories": hn_stories[:5],
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "total_posts": total,
        "days": days,
        "from_date": from_date,
        "to_date": to_date,
    }


def format_social_sentiment_telegram(data: dict[str, Any]) -> str:
    """Format social sentiment data as a Telegram HTML message."""
    sym = data["symbol"]
    label = data["label"]
    score = data["score"]
    bullish = data["bullish_count"]
    bearish = data["bearish_count"]
    neutral = data["neutral_count"]
    total = data["total_posts"]
    days = data["days"]
    reddit_posts = data["reddit_posts"]
    hn_stories = data["hn_stories"]

    score_bar = _score_bar(score)

    lines = [
        f"📡 <b>SOCIAL SENTIMENT — {sym}</b> (Last {days} days)",
        "",
        f"Sentiment: <b>{label}</b>",
        f"Score: <b>{score:+d}/100</b>  {score_bar}",
        f"📊 {bullish} 🟢 bullish  |  {bearish} 🔴 bearish  |  {neutral} ⚪ neutral",
        f"Total posts analyzed: {total}",
        "",
    ]

    if reddit_posts:
        lines.append("📌 <b>Top Reddit Posts:</b>")
        for i, p in enumerate(reddit_posts[:3], 1):
            title = _truncate(p.get("title", ""), 80)
            sub = p.get("subreddit", "")
            score_p = p.get("score", 0)
            comments = p.get("num_comments", 0)
            url = p.get("url", "")
            sub_tag = f" r/{sub}" if sub else ""
            eng = f"↑{score_p} 💬{comments}" if score_p or comments else ""
            lines.append(f"  {i}. <a href=\"{url}\">{title}</a>{sub_tag} {eng}".rstrip())
        lines.append("")

    if hn_stories:
        lines.append("🟠 <b>HackerNews:</b>")
        for i, s in enumerate(hn_stories[:3], 1):
            title = _truncate(s.get("title", ""), 80)
            url = s.get("url") or s.get("hn_url", "")
            pts = s.get("engagement", {}).get("points", 0)
            lines.append(f"  {i}. <a href=\"{url}\">{title}</a> ↑{pts}")
        lines.append("")

    lines.append(f"🔍 Query: <code>{data['query']}</code>")
    lines.append(f"📅 {data['from_date']} → {data['to_date']}")
    lines.append("⚠️ <i>Not financial advice. DYOR.</i>")

    return "\n".join(lines)


def _score_bar(score: int) -> str:
    """Visual bar for score -100 to +100."""
    blocks = 10
    filled = round((score + 100) / 200 * blocks)
    filled = max(0, min(blocks, filled))
    empty = blocks - filled
    return "🟩" * filled + "⬜" * empty


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"
