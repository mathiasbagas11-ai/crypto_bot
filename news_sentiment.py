#!/usr/bin/env python3
"""
NEWS SENTIMENT MODULE
=====================
Pure module — dipanggil dari crypto_screening_bot_v9.py
Tidak ada polling loop, tidak ada Telegram handler sendiri.

Fungsi utama:
  get_coin_sentiment(symbol)  → dict sentiment untuk coin tertentu
  get_macro_sentiment()       → dict macro sentiment global
  format_sentiment_block(s)   → string siap kirim ke Telegram

Requires .env:
  NEWSAPI_KEY=...
  GEMINI_API_KEY=...
"""

import os, time, logging, requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("news_sentiment")

NEWSAPI_KEY   = os.getenv("NEWSAPI_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL  = "gemini-2.0-flash"
GEMINI_BASE   = "https://generativelanguage.googleapis.com/v1beta"
NEWSAPI_BASE  = "https://newsapi.org/v2"

NEWS_LOOKBACK_DAYS = 2
MAX_ARTICLES       = 7
SUMMARY_ARTICLES   = 5

COIN_SEARCH_MAP = {
    "BTC":"Bitcoin BTC crypto","ETH":"Ethereum ETH crypto",
    "SOL":"Solana SOL crypto","XRP":"XRP Ripple crypto",
    "BNB":"BNB Binance crypto","ADA":"Cardano ADA crypto",
    "AVAX":"Avalanche AVAX crypto","DOGE":"Dogecoin DOGE",
    "DOT":"Polkadot DOT crypto","LINK":"Chainlink LINK crypto",
    "NEAR":"NEAR Protocol crypto","APT":"Aptos APT crypto",
    "INJ":"Injective INJ crypto","SUI":"Sui SUI crypto",
    "ARB":"Arbitrum ARB crypto","OP":"Optimism OP crypto",
    "TIA":"Celestia TIA crypto","RENDER":"Render RNDR crypto",
    "FET":"Fetch.ai FET crypto","PENDLE":"Pendle crypto",
    "ENA":"Ethena ENA crypto","AAVE":"Aave AAVE crypto",
    "JUP":"Jupiter JUP Solana","HYPE":"Hyperliquid HYPE crypto",
}

SENTIMENT_EMOJI = {
    "BULLISH":"🟢","BEARISH":"🔴","NEUTRAL":"⚪","MIXED":"🟡","UNKNOWN":"❓"
}

# ── NewsAPI ───────────────────────────────────

def _fetch_articles(query: str, days: int = NEWS_LOOKBACK_DAYS,
                    n: int = MAX_ARTICLES) -> list:
    if not NEWSAPI_KEY:
        return []
    from_dt = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(f"{NEWSAPI_BASE}/everything", params={
            "q": query, "from": from_dt, "sortBy": "relevancy",
            "language": "en", "pageSize": n, "apiKey": NEWSAPI_KEY,
        }, timeout=15)
        if r.ok:
            return r.json().get("articles", [])
    except Exception as e:
        log.warning(f"NewsAPI error: {e}")
    return []


def _format_for_prompt(articles: list) -> str:
    lines = []
    for i, a in enumerate(articles[:SUMMARY_ARTICLES], 1):
        title = a.get("title","")
        desc  = (a.get("description") or "")[:180]
        src   = a.get("source",{}).get("name","")
        pub   = a.get("publishedAt","")[:10]
        lines.append(f"{i}. [{pub}] {src}: {title}")
        if desc:
            lines.append(f"   {desc}")
    return "\n".join(lines)

# ── Gemini Sentiment ──────────────────────────

def _gemini_analyze(articles_text: str, context: str) -> dict:
    empty = {"sentiment":"UNKNOWN","score":0,"confidence":"LOW",
             "summary":"","key_events":[],"trading_implication":""}
    if not GEMINI_API_KEY or not articles_text.strip():
        return empty

    prompt = f"""You are a professional crypto trading analyst.

Analyze these news articles about {context} and return ONLY this exact format:

SENTIMENT: [BULLISH/BEARISH/NEUTRAL/MIXED]
SCORE: [integer -100 to +100]
CONFIDENCE: [LOW/MEDIUM/HIGH]
SUMMARY: [2 sentences max]
KEY_EVENTS:
- [event 1 and crypto impact]
- [event 2]
- [event 3 if relevant]
TRADING_IMPLICATION: [1 sentence: risk-on or risk-off, what to watch]

NEWS:
{articles_text}"""

    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500},
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=25)
            if r.status_code == 429:
                time.sleep(10 * (attempt+1)); continue
            if r.ok:
                raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_response(raw)
        except Exception as e:
            log.warning(f"Gemini sentiment error: {e}")
            break
    return empty


def _parse_response(text: str) -> dict:
    result = {"sentiment":"NEUTRAL","score":0,"confidence":"MEDIUM",
              "summary":"","key_events":[],"trading_implication":""}
    section, events, summary_buf, impl_buf = None, [], [], []
    for line in text.split("\n"):
        l = line.strip()
        if not l: continue
        if l.startswith("SENTIMENT:"):
            v = l.split(":",1)[1].strip()
            if v in ["BULLISH","BEARISH","NEUTRAL","MIXED"]:
                result["sentiment"] = v
        elif l.startswith("SCORE:"):
            try: result["score"] = int(l.split(":",1)[1].strip())
            except: pass
        elif l.startswith("CONFIDENCE:"):
            v = l.split(":",1)[1].strip()
            if v in ["LOW","MEDIUM","HIGH"]: result["confidence"] = v
        elif l.startswith("SUMMARY:"): section = "summary"
        elif l.startswith("KEY_EVENTS:"): section = "events"
        elif l.startswith("TRADING_IMPLICATION:"): section = "impl"
        else:
            if section == "summary": summary_buf.append(l)
            elif section == "events" and l.startswith("-"): events.append(l[1:].strip())
            elif section == "impl": impl_buf.append(l)
    result["summary"]             = " ".join(summary_buf)
    result["key_events"]          = events[:3]
    result["trading_implication"] = " ".join(impl_buf)
    return result

# ── Public API ────────────────────────────────

def get_coin_sentiment(symbol: str) -> dict:
    """Fetch + analyze sentiment untuk satu coin. Returns dict."""
    sym   = symbol.upper().replace("USDT","")
    query = COIN_SEARCH_MAP.get(sym, f"{sym} cryptocurrency")
    # Mix coin news + macro
    coin_arts  = _fetch_articles(query, days=2, n=5)
    macro_arts = _fetch_articles(
        "Federal Reserve interest rate crypto market", days=2, n=3)
    # Merge deduplicated
    seen, merged = set(), []
    for a in coin_arts + macro_arts:
        t = a.get("title","")
        if t and t not in seen:
            seen.add(t); merged.append(a)
    txt = _format_for_prompt(merged[:MAX_ARTICLES])
    result = _gemini_analyze(txt, f"{sym} crypto and macro market")
    result["symbol"]   = sym
    result["articles"] = merged[:5]
    return result


def get_macro_sentiment() -> dict:
    """Fetch + analyze macro sentiment (Fed, CPI, FOMC, dll)."""
    query = ("Federal Reserve interest rate OR CPI inflation OR FOMC "
             "OR crypto regulation OR Bitcoin ETF OR market risk")
    arts  = _fetch_articles(query, days=2, n=10)
    txt   = _format_for_prompt(arts)
    result = _gemini_analyze(
        txt, "global macro economics, Federal Reserve, crypto regulation and ETF")
    result["articles"] = arts[:5]
    return result


def format_sentiment_block(s: dict, mode: str = "full") -> str:
    """
    Format sentiment dict jadi string Telegram-ready.
    mode='full'  → lengkap dengan artikel
    mode='short' → 3 baris ringkas (untuk embed di coin analysis block)
    """
    emoji = SENTIMENT_EMOJI.get(s.get("sentiment","UNKNOWN"), "❓")
    score = s.get("score", 0)
    bar   = _score_bar(score)
    conf  = s.get("confidence","?")
    sym   = s.get("symbol","MACRO")

    if mode == "short":
        lines = [
            f"📰 *News Sentiment {sym}:* {emoji} {s.get('sentiment','?')} "
            f"({score:+d}) {bar}",
        ]
        if s.get("trading_implication"):
            lines.append(f"   _{s['trading_implication']}_")
        return "\n".join(lines)

    # Full mode
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📰 *NEWS SENTIMENT — {sym}*",
        f"🕐 {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"\n{emoji} *{s.get('sentiment','?')}* | Score: *{score:+d}/100* {bar}",
        f"Confidence: {conf}",
    ]
    if s.get("summary"):
        lines += ["\n📋 *Summary:*", f"_{s['summary']}_"]
    if s.get("key_events"):
        lines.append("\n⚡ *Key Events:*")
        for ev in s["key_events"]:
            lines.append(f"  • {ev}")
    if s.get("trading_implication"):
        lines += ["\n💡 *Trading Implication:*", f"_{s['trading_implication']}_"]
    if s.get("articles"):
        lines.append(f"\n📄 *Artikel ({len(s['articles'])}):")
        for a in s["articles"]:
            title = a.get("title","")[:75]
            src   = a.get("source",{}).get("name","")
            pub   = a.get("publishedAt","")[:10]
            url   = a.get("url","")
            if title and url:
                lines.append(f"  • [{pub}] {src}: [{title}]({url})")
    lines.append("\n⚠️ _Not financial advice. DYOR._")
    return "\n".join(lines)


def _score_bar(score: int, w: int = 10) -> str:
    filled = max(0, min(w, int((score+100)/200*w)))
    return f"`[{'█'*filled}{'░'*(w-filled)}]`"
