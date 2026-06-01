#!/usr/bin/env python3
"""
NEWS SENTIMENT MODULE
=====================
Pure module — dipanggil dari crypto_screening_bot_v13.py

Fungsi utama:
  get_coin_sentiment(symbol)     → dict sentiment untuk coin tertentu
  get_macro_sentiment()          → dict macro sentiment global
  get_high_impact_events(symbol) → list high-impact events (Fed, unlock, hack, dll)
  format_sentiment_block(s)      → string siap kirim ke Telegram
  get_news_gate(symbol, direction) → (penalty_pts, blocked, reasons)

High-Impact Event Categories:
  FED_HAWKISH / FED_DOVISH    — Federal Reserve rate decisions
  FOMC_DECISION               — FOMC meeting outcomes
  TOKEN_UNLOCK                — scheduled large token unlocks
  HACK_EXPLOIT                — protocol hacks / exploits
  REGULATORY_NEGATIVE         — SEC, bans, crackdowns
  REGULATORY_POSITIVE         — ETF approvals, positive regulation
  BUYBACK                     — company/protocol token buyback
  PARTNERSHIP                 — major partnership announcements
  LISTING                     — major exchange listing
  DELISTING                   — exchange delisting

Requires .env:
  NEWSAPI_KEY=...
  GEMINI_API_KEY=...
"""

import os, time, logging, requests, re
from datetime import datetime, timezone, timedelta

log = logging.getLogger("news_sentiment")

NEWSAPI_KEY    = os.getenv("NEWSAPI_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-2.0-flash"
GEMINI_BASE    = "https://generativelanguage.googleapis.com/v1beta"
NEWSAPI_BASE   = "https://newsapi.org/v2"

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
    "ORDI":"Ordinals ORDI Bitcoin","WIF":"dogwifhat WIF Solana",
}

SENTIMENT_EMOJI = {
    "BULLISH":"🟢","BEARISH":"🔴","NEUTRAL":"⚪","MIXED":"🟡","UNKNOWN":"❓"
}

# ── High-Impact Keyword Patterns ──────────────
# Each entry: (category, direction, min_score_impact, keywords_list)
# direction: "BEARISH" | "BULLISH" | "NEUTRAL"
_HIGH_IMPACT_PATTERNS = [
    # Federal Reserve
    ("FED_HAWKISH",          "BEARISH", 25,
     ["rate hike", "hawkish", "fed raises", "interest rate increase",
      "tighten", "higher for longer", "powell hawkish"]),
    ("FED_DOVISH",           "BULLISH", 20,
     ["rate cut", "dovish", "fed cuts", "fed lowers", "pivot",
      "pause rate", "easing", "powell dovish", "rate reduction"]),
    ("FOMC_DECISION",        "NEUTRAL", 15,
     ["fomc", "federal open market", "fomc meeting", "fed decision",
      "federal reserve decision"]),

    # Token / Protocol Events
    ("TOKEN_UNLOCK",         "BEARISH", 20,
     ["token unlock", "vesting unlock", "cliff unlock",
      "tokens unlocked", "unlock event", "large unlock"]),
    ("HACK_EXPLOIT",         "BEARISH", 35,
     ["hack", "exploit", "hacked", "exploited", "stolen", "attack",
      "rug pull", "smart contract exploit", "bridge hack", "protocol drained"]),
    ("BUYBACK",              "BULLISH", 20,
     ["buyback", "buy back", "token buyback", "treasury buyback",
      "repurchase program", "burning tokens"]),
    ("PARTNERSHIP",          "BULLISH", 12,
     ["partnership", "collaboration", "integration", "alliance",
      "major deal", "signs agreement"]),
    ("LISTING",              "BULLISH", 18,
     ["listed on", "new listing", "coinbase listing", "binance listing",
      "exchange listing", "listed at"]),
    ("DELISTING",            "BEARISH", 22,
     ["delist", "delisting", "removed from", "withdrawn from listing"]),

    # Regulatory
    ("REGULATORY_NEGATIVE",  "BEARISH", 25,
     ["sec charges", "sec lawsuit", "banned", "crackdown", "illegal",
      "money laundering", "fraud charges", "regulator blocks",
      "china ban", "government ban"]),
    ("REGULATORY_POSITIVE",  "BULLISH", 22,
     ["etf approved", "approved by sec", "etf approval", "regulatory clarity",
      "legal tender", "regulated", "approved crypto", "green light"]),

    # Macro
    ("INFLATION_HIGH",       "BEARISH", 15,
     ["cpi above", "inflation surges", "hot inflation", "inflation jumps",
      "higher inflation", "inflation beats"]),
    ("INFLATION_LOW",        "BULLISH", 12,
     ["cpi below", "inflation cools", "disinflation", "inflation drops",
      "inflation falls", "lower than expected inflation"]),
    ("MARKET_CRASH",         "BEARISH", 30,
     ["market crash", "stock market crash", "financial crisis",
      "black swan", "systemic risk", "liquidity crisis"]),
    ("RISK_ON",              "BULLISH", 10,
     ["risk appetite", "risk on", "stocks rally", "bull market",
      "market optimism", "institutional buying"]),
]

# ── In-memory cache for articles (avoid hammering NewsAPI) ──
_article_cache: dict = {}
_ARTICLE_CACHE_TTL = 30 * 60   # 30 minutes

# ── NewsAPI ───────────────────────────────────

def _fetch_articles(query: str, days: int = NEWS_LOOKBACK_DAYS,
                    n: int = MAX_ARTICLES) -> list:
    if not NEWSAPI_KEY:
        return []
    cache_key = f"{query}_{days}_{n}"
    cached = _article_cache.get(cache_key)
    if cached and time.time() - cached["_ts"] < _ARTICLE_CACHE_TTL:
        return cached["articles"]

    from_dt = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(f"{NEWSAPI_BASE}/everything", params={
            "q": query, "from": from_dt, "sortBy": "relevancy",
            "language": "en", "pageSize": n, "apiKey": NEWSAPI_KEY,
        }, timeout=15)
        if r.ok:
            articles = r.json().get("articles", [])
            _article_cache[cache_key] = {"articles": articles, "_ts": time.time()}
            return articles
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

# ── High-Impact Event Detector ────────────────

def _scan_articles_for_high_impact(articles: list, coin_sym: str = "") -> list:
    """
    Scan article titles + descriptions for high-impact patterns.

    Returns list of dicts:
      category:  str (e.g. "FED_HAWKISH")
      direction: "BULLISH" | "BEARISH" | "NEUTRAL"
      impact:    int (0-100, score penalty/bonus magnitude)
      headline:  str (matched article title)
      source:    str
      date:      str
    """
    found = []
    seen_cats: set = set()

    for article in articles:
        title = (article.get("title") or "").lower()
        desc  = (article.get("description") or "").lower()
        full  = f"{title} {desc}"
        src   = article.get("source", {}).get("name", "")
        date  = article.get("publishedAt", "")[:10]
        headline = article.get("title", "")

        for cat, direction, impact, keywords in _HIGH_IMPACT_PATTERNS:
            if cat in seen_cats:
                continue
            for kw in keywords:
                if kw in full:
                    # Coin-specific: check if the article is about this coin
                    # (unless it's a macro event like FED/FOMC)
                    is_macro = cat.startswith(("FED_", "FOMC", "INFLATION", "MARKET_"))
                    if not is_macro and coin_sym:
                        sym_l = coin_sym.lower()
                        coin_name_l = COIN_SEARCH_MAP.get(coin_sym.upper(), "").lower()
                        if sym_l not in full and not any(
                            part in full for part in coin_name_l.split()[:2]
                        ):
                            break  # keyword match but not about this coin

                    found.append({
                        "category":  cat,
                        "direction": direction,
                        "impact":    impact,
                        "headline":  headline[:120],
                        "source":    src,
                        "date":      date,
                        "keyword":   kw,
                    })
                    seen_cats.add(cat)
                    break

    # Sort by impact descending
    found.sort(key=lambda x: x["impact"], reverse=True)
    return found


# ── Public API ────────────────────────────────

def get_high_impact_events(symbol: str) -> list:
    """
    Fetch and scan news for high-impact events for a specific coin.

    Returns list of event dicts (may be empty if no high-impact news found).
    """
    sym = symbol.upper().replace("USDT", "")
    query = COIN_SEARCH_MAP.get(sym, f"{sym} cryptocurrency")

    # Include macro news too
    macro_q  = "Federal Reserve FOMC interest rate OR token unlock OR hack exploit crypto"
    coin_arts  = _fetch_articles(query, days=2, n=7)
    macro_arts = _fetch_articles(macro_q, days=2, n=5)

    # Deduplicate
    seen, merged = set(), []
    for a in coin_arts + macro_arts:
        t = a.get("title", "")
        if t and t not in seen:
            seen.add(t); merged.append(a)

    return _scan_articles_for_high_impact(merged, sym)


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
    txt    = _format_for_prompt(merged[:MAX_ARTICLES])
    result = _gemini_analyze(txt, f"{sym} crypto and macro market")
    result["symbol"]        = sym
    result["articles"]      = merged[:5]
    result["high_impact"]   = _scan_articles_for_high_impact(merged, sym)
    return result


def get_macro_sentiment() -> dict:
    """Fetch + analyze macro sentiment (Fed, CPI, FOMC, dll)."""
    query = ("Federal Reserve interest rate OR CPI inflation OR FOMC "
             "OR crypto regulation OR Bitcoin ETF OR market risk")
    arts  = _fetch_articles(query, days=2, n=10)
    txt   = _format_for_prompt(arts)
    result = _gemini_analyze(
        txt, "global macro economics, Federal Reserve, crypto regulation and ETF")
    result["articles"]    = arts[:5]
    result["high_impact"] = _scan_articles_for_high_impact(arts, "")
    return result


def get_news_gate(symbol: str, direction: str) -> tuple:
    """
    Evaluate news for a signal gate decision.

    Returns: (penalty_pts, blocked, reasons)
      penalty_pts: int — subtract from master score
      blocked:     bool — hard block the signal
      reasons:     list[str]

    Logic:
      - Any HACK_EXPLOIT → immediate block if direction == LONG
      - FED_HAWKISH + direction LONG → -15pt
      - TOKEN_UNLOCK (large) + direction LONG → -10pt
      - REGULATORY_NEGATIVE → -12pt LONG
      - REGULATORY_POSITIVE → -8pt SHORT (counter-signal)
      - Multiple HIGH_IMPACT bearish events → escalating penalty
    """
    events = get_high_impact_events(symbol)
    if not events:
        return 0, False, []

    penalty  = 0
    blocked  = False
    reasons  = []
    is_long  = direction in ("LONG", "PUMP")

    for ev in events:
        cat    = ev["category"]
        ev_dir = ev["direction"]
        impact = ev["impact"]
        hl     = ev["headline"][:80]

        if is_long:
            if cat == "HACK_EXPLOIT":
                blocked = True
                reasons.append(f"🚨 HACK/EXPLOIT detected — {hl} | LONG BLOCKED")
                break

            if ev_dir == "BEARISH":
                if cat == "FED_HAWKISH":
                    pts = 15
                elif cat in ("REGULATORY_NEGATIVE",):
                    pts = 12
                elif cat == "TOKEN_UNLOCK":
                    pts = 10
                elif cat == "MARKET_CRASH":
                    pts = 20
                else:
                    pts = min(impact // 2, 10)
                penalty += pts
                reasons.append(f"📰 {cat} ({ev['date']}): -{pts}pt | {hl}")

        else:  # SHORT
            if ev_dir == "BULLISH":
                if cat == "FED_DOVISH":
                    pts = 12
                elif cat in ("REGULATORY_POSITIVE", "LISTING"):
                    pts = 10
                elif cat == "BUYBACK":
                    pts = 8
                elif cat == "RISK_ON":
                    pts = 6
                else:
                    pts = min(impact // 2, 8)
                penalty += pts
                reasons.append(f"📰 {cat} ({ev['date']}): -{pts}pt | {hl}")

            if cat == "HACK_EXPLOIT":
                # Hack is SHORT tailwind — bonus not penalty
                penalty -= 5  # reduce penalty
                reasons.append(f"⚠️ HACK/EXPLOIT → SHORT aligned: {hl}")

    # Cap penalty at 30pt
    penalty = min(penalty, 30)
    return penalty, blocked, reasons


def format_high_impact_block(events: list) -> str:
    """Format high-impact events for Telegram display."""
    if not events:
        return ""
    lines = ["⚡ *HIGH-IMPACT EVENTS:*"]
    for ev in events[:4]:
        d_emoji = "🔴" if ev["direction"] == "BEARISH" else \
                  "🟢" if ev["direction"] == "BULLISH" else "⚪"
        lines.append(
            f"  {d_emoji} [{ev['date']}] *{ev['category']}* (impact {ev['impact']})"
        )
        lines.append(f"     _{ev['headline']}_")
    return "\n".join(lines)


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
        # High-impact events in short mode
        hi = s.get("high_impact", [])
        if hi:
            top = hi[0]
            d_e = "🔴" if top["direction"] == "BEARISH" else "🟢"
            lines.append(f"   {d_e} ⚡ {top['category']}: {top['headline'][:70]}")
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

    # High-impact events section
    hi = s.get("high_impact", [])
    if hi:
        lines.append("")
        lines.append(format_high_impact_block(hi))

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


# ── Trading Session Detector ──────────────────
def _get_trading_session() -> str:
    """Return sesi trading aktif berdasarkan waktu UTC."""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    if 0 <= hour < 8:
        return "ASIA (00:00–08:00 UTC) — likuiditas sedang"
    elif 8 <= hour < 12:
        return "EROPA OPEN (08:00–12:00 UTC) — volatilitas tinggi"
    elif 12 <= hour < 17:
        return "US PRE-MARKET (12:00–17:00 UTC) — momentum kuat"
    elif 17 <= hour < 21:
        return "US PEAK (17:00–21:00 UTC) — likuiditas tertinggi"
    else:
        return "US CLOSE / LATE (21:00–00:00 UTC) — volume menurun"


# ── AI-Ready Structured News Context ─────────
def get_structured_news_for_ai(symbol: str) -> dict:
    """
    Susun konteks news untuk DeepSeek.
    Prioritas: news_agent cache (fresh, hourly) → fallback NewsAPI live fetch.

    Return dict:
      sentiment_label    : str
      sentiment_score    : int
      trading_session    : str
      high_impact_events : list[str]
      upcoming_unlocks   : list[str]
      headlines          : list[str]
      macro_risk         : str
    """
    sym = symbol.upper().replace("USDT", "")

    # ── Cek news_agent cache dulu ───────────
    try:
        from news_agent import get_cached_news
        cached = get_cached_news(sym, max_age_seconds=3900)   # fresh kalau ≤ 65 menit
        if cached:
            return cached
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"news_agent cache read error: {e}")

    # ── Fallback: build minimal context ──────
    result = {
        "symbol":             sym,
        "sentiment_label":    "NEUTRAL",
        "sentiment_score":    0,
        "trading_session":    _get_trading_session(),
        "high_impact_events": [],
        "upcoming_unlocks":   [],
        "headlines":          [],
        "macro_risk":         "",
    }

    if not NEWSAPI_KEY:
        return result

    try:
        # Fetch coin-specific articles
        query = COIN_SEARCH_MAP.get(sym, f"{sym} cryptocurrency")
        coin_arts  = _fetch_articles(query, days=2, n=5)

        # Fetch macro articles
        macro_arts = _fetch_articles(
            "Federal Reserve crypto Bitcoin market risk inflation", days=2, n=3)

        # Merge & deduplicate
        seen, merged = set(), []
        for a in coin_arts + macro_arts:
            t = a.get("title", "")
            if t and t not in seen:
                seen.add(t)
                merged.append(a)

        # Headlines (plain text, tanpa source)
        headlines = [
            a.get("title", "")[:100]
            for a in merged[:5]
            if a.get("title")
        ]
        result["headlines"] = headlines

        # High-impact events detection
        hi_events = _scan_articles_for_high_impact(merged, sym)
        event_labels = []
        unlock_labels = []
        for ev in hi_events:
            cat = ev.get("category", "")
            imp = ev.get("direction", "NEUTRAL")
            label = f"{cat} ({imp})"
            if "TOKEN_UNLOCK" in cat:
                unlock_labels.append(label)
            else:
                event_labels.append(label)

        result["high_impact_events"] = event_labels
        result["upcoming_unlocks"]   = unlock_labels

        # Sentiment analysis via Gemini (jika tersedia)
        if GEMINI_API_KEY and merged:
            txt = _format_for_prompt(merged[:MAX_ARTICLES])
            senti = _gemini_analyze(txt, f"{sym} crypto market")
            result["sentiment_label"] = senti.get("sentiment",    "NEUTRAL")
            result["sentiment_score"] = senti.get("score",        0)
            result["macro_risk"]      = senti.get("summary",      "")[:200]

    except Exception as e:
        log.warning(f"get_structured_news_for_ai({sym}) error: {e}")

    return result
