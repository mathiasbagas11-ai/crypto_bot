#!/usr/bin/env python3
"""
NEWS AGENT — Auto Hourly News + X Sentiment Intelligence
=========================================================
Fetch dan proses berita + X (Twitter) sentiment setiap jam.

Sources:
  1. NewsAPI         — berita, token unlock, macro events (NEWSAPI_KEY)
  2. X via Nitter    — KOL activity, narrative cycle, social sentiment (gratis)
  3. X via API v2    — lebih reliable (opsional, TWITTER_BEARER_TOKEN)

Output untuk:
  - DeepSeek signal review (context otomatis tanpa re-fetch)
  - Learning engine (derive lessons dari events + KOL activity)
  - Sinyal confluence (unlock, euphoria, KOL pump = confluence data)

Flow per jam:
  → NewsAPI: fetch 10 koin + macro → deteksi events
  → X Nitter/API: fetch 10 koin → KOL activity, sentiment, euphoria
  → AI: derive lessons dari gabungan news + X
  → Simpan ke news_intelligence.json

File output:
  news_intelligence.json — cache intel (TTL 1 jam)

Requires .env:
  NEWSAPI_KEY=...            (untuk fetch artikel)
  DEEPSEEK_API_KEY=...       (untuk derive lessons via AI)
  TWITTER_BEARER_TOKEN=...   (opsional — lebih reliable X fetch)
"""

import os
import json
import time
import logging
import requests
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("news_agent")

NEWS_INTELLIGENCE_FILE = "news_intelligence.json"
CACHE_TTL_SECONDS      = 3600   # 1 jam
FETCH_TIMEOUT          = 15

NEWSAPI_KEY      = os.getenv("NEWSAPI_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL   = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
NEWSAPI_BASE     = "https://newsapi.org/v2"

# Koin yang selalu dimonitor
MONITORED_COINS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "AVAX", "DOGE",
    "LINK", "NEAR", "APT", "INJ", "SUI", "ARB", "OP",
    "TIA", "RENDER", "FET", "PENDLE", "ENA", "AAVE",
    "JUP", "HYPE", "ORDI", "WIF",
]

COIN_SEARCH_MAP = {
    "BTC":    "Bitcoin BTC crypto", "ETH":    "Ethereum ETH crypto",
    "SOL":    "Solana SOL crypto",  "XRP":    "XRP Ripple crypto",
    "BNB":    "BNB Binance crypto", "ADA":    "Cardano ADA crypto",
    "AVAX":   "Avalanche AVAX crypto","DOGE":  "Dogecoin DOGE",
    "LINK":   "Chainlink LINK crypto","NEAR":  "NEAR Protocol crypto",
    "APT":    "Aptos APT crypto",   "INJ":    "Injective INJ crypto",
    "SUI":    "Sui SUI crypto",     "ARB":    "Arbitrum ARB crypto",
    "OP":     "Optimism OP crypto", "TIA":    "Celestia TIA crypto",
    "RENDER": "Render RNDR crypto", "FET":    "Fetch.ai FET crypto",
    "PENDLE": "Pendle crypto",      "ENA":    "Ethena ENA crypto",
    "AAVE":   "Aave AAVE crypto",   "JUP":    "Jupiter JUP Solana",
    "HYPE":   "Hyperliquid HYPE",   "ORDI":   "Ordinals ORDI Bitcoin",
    "WIF":    "dogwifhat WIF Solana",
}

MACRO_QUERY = (
    "Federal Reserve interest rate OR FOMC crypto OR "
    "Bitcoin ETF OR crypto regulation OR CPI inflation crypto OR "
    "SEC crypto OR stablecoin regulation"
)

# High-impact event patterns
_EVENT_PATTERNS = [
    ("TOKEN_UNLOCK",       "BEARISH",  20, ["token unlock", "vesting unlock", "cliff unlock", "tokens unlocked", "unlock event"]),
    ("HACK_EXPLOIT",       "BEARISH",  35, ["hack", "exploit", "hacked", "exploited", "stolen", "attack", "rug pull", "bridge hack"]),
    ("FED_HAWKISH",        "BEARISH",  25, ["rate hike", "hawkish", "fed raises", "tighten", "higher for longer"]),
    ("FED_DOVISH",         "BULLISH",  20, ["rate cut", "dovish", "fed cuts", "pivot", "pause rate", "easing"]),
    ("FOMC_DECISION",      "NEUTRAL",  15, ["fomc", "federal open market", "fomc meeting", "fed decision"]),
    ("REGULATORY_NEGATIVE","BEARISH",  25, ["sec charges", "sec lawsuit", "banned", "crackdown", "regulatory ban"]),
    ("REGULATORY_POSITIVE","BULLISH",  22, ["etf approved", "sec approved", "etf approval", "regulatory clarity"]),
    ("LISTING",            "BULLISH",  18, ["listed on", "new listing", "coinbase listing", "binance listing"]),
    ("DELISTING",          "BEARISH",  22, ["delist", "delisting", "removed from"]),
    ("BUYBACK",            "BULLISH",  18, ["buyback", "buy back", "token buyback", "burning tokens"]),
    ("PARTNERSHIP",        "BULLISH",  10, ["partnership", "collaboration", "integration", "major deal"]),
    ("MARKET_CRASH",       "BEARISH",  30, ["market crash", "financial crisis", "black swan", "liquidity crisis"]),
    ("INFLATION_HIGH",     "BEARISH",  15, ["cpi above", "inflation surges", "hot inflation", "inflation beats"]),
    ("INFLATION_LOW",      "BULLISH",  12, ["cpi below", "inflation cools", "disinflation", "inflation drops"]),
]


# ─────────────────────────────────────────────
# NEWS FETCHING
# ─────────────────────────────────────────────

def _fetch_headlines(query: str, days: int = 1, n: int = 5) -> list[str]:
    """Fetch headlines dari NewsAPI. Return list of title strings."""
    if not NEWSAPI_KEY:
        return []
    try:
        from_dt = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        r = requests.get(
            f"{NEWSAPI_BASE}/everything",
            params={
                "q":        query,
                "from":     from_dt,
                "sortBy":   "relevancy",
                "language": "en",
                "pageSize": n,
                "apiKey":   NEWSAPI_KEY,
            },
            timeout=FETCH_TIMEOUT,
        )
        if r.status_code == 200:
            articles = r.json().get("articles", [])
            return [a["title"] for a in articles if a.get("title")][:n]
    except Exception as e:
        log.debug(f"NewsAPI fetch error: {e}")
    return []


def _scan_events(headlines: list[str], coin: str = "") -> list[dict]:
    """Scan headlines untuk high-impact events."""
    detected = []
    text_combined = " ".join(h.lower() for h in headlines)
    for cat, direction, impact, keywords in _EVENT_PATTERNS:
        for kw in keywords:
            if kw in text_combined:
                # Cek apakah benar-benar relevan untuk koin ini
                coin_relevant = (
                    not coin or
                    coin.lower() in text_combined or
                    any(kw in h.lower() and (coin.lower() in h.lower() or not coin)
                        for h in headlines)
                )
                if coin_relevant:
                    detected.append({
                        "category":  cat,
                        "direction": direction,
                        "impact":    impact,
                        "keyword":   kw,
                    })
                    break  # satu match per kategori cukup
    return detected


def _detect_trading_session() -> str:
    """Return sesi trading aktif berdasarkan UTC."""
    h = datetime.now(timezone.utc).hour
    if   0  <= h <  8: return "ASIA (00–08 UTC)"
    elif 8  <= h < 12: return "EROPA OPEN (08–12 UTC)"
    elif 12 <= h < 17: return "US PRE-MARKET (12–17 UTC)"
    elif 17 <= h < 21: return "US PEAK (17–21 UTC)"
    else:              return "US CLOSE (21–00 UTC)"


# ─────────────────────────────────────────────
# X (TWITTER) SENTIMENT FETCH
# ─────────────────────────────────────────────

# Flag apakah x_sentiment modul tersedia
_X_MODULE_AVAILABLE: bool | None = None

def _x_available() -> bool:
    global _X_MODULE_AVAILABLE
    if _X_MODULE_AVAILABLE is None:
        try:
            import x_sentiment  # noqa
            _X_MODULE_AVAILABLE = True
        except ImportError:
            _X_MODULE_AVAILABLE = False
    return _X_MODULE_AVAILABLE


def _fetch_x_for_coin(coin: str) -> dict:
    """
    Fetch X sentiment untuk satu koin via x_sentiment module.
    Return structured dict yang siap disimpan ke intel.
    """
    if not _x_available():
        return {}
    try:
        from x_sentiment import get_x_coin_analysis
        result = get_x_coin_analysis(coin)
        analysis = result.get("analysis", {})

        sentiment_avg   = analysis.get("sentiment_avg", 0.0)
        kol_count       = analysis.get("kol_count", 0)
        kol_weighted    = analysis.get("kol_weighted", 0.0)
        bull_tweets     = analysis.get("bull_tweets", 0)
        bear_tweets     = analysis.get("bear_tweets", 0)
        euphoria        = analysis.get("euphoria_detected", False)
        top_kols        = analysis.get("kol_tweets", [])
        total_count     = analysis.get("total_count", 0)

        # Sentiment label dari score
        if sentiment_avg >= 0.25:
            x_sentiment_label = "BULLISH"
        elif sentiment_avg <= -0.25:
            x_sentiment_label = "BEARISH"
        elif abs(sentiment_avg) < 0.05:
            x_sentiment_label = "NEUTRAL"
        else:
            x_sentiment_label = "MIXED"

        # Ringkasan KOL mentions (top 3)
        kol_mentions = [
            f"@{t.get('author','?')} ({t.get('category','?')}): {t.get('text','')[:80]}"
            for t in top_kols[:3]
        ]

        return {
            "sentiment_label": x_sentiment_label,
            "sentiment_score": round(sentiment_avg * 100),   # scale ke -100..+100
            "kol_count":       kol_count,
            "kol_weighted":    kol_weighted,
            "bull_tweets":     bull_tweets,
            "bear_tweets":     bear_tweets,
            "total_tweets":    total_count,
            "euphoria":        euphoria,
            "kol_mentions":    kol_mentions,
            "source":          result.get("source", "nitter"),
            "fetched_at":      datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.debug(f"X fetch error {coin}: {e}")
        return {}


def _derive_lessons_from_x(coin: str, x_data: dict) -> list[str]:
    """
    Derive lessons dari X sentiment data.
    Contoh: "BTC euphoria di X terdeteksi — kemungkinan area top"
    """
    if not x_data:
        return []
    lessons = []
    coin_label = coin if coin else "Market"
    euphoria    = x_data.get("euphoria", False)
    kol_count   = x_data.get("kol_count", 0)
    kol_weighted= x_data.get("kol_weighted", 0.0)
    x_sent      = x_data.get("sentiment_label", "NEUTRAL")
    bull        = x_data.get("bull_tweets", 0)
    bear        = x_data.get("bear_tweets", 0)

    if euphoria:
        lessons.append(
            f"{coin_label}: Euphoria terdeteksi di X (banyak 'moon'/'pump' talk) "
            f"— hati-hati, kemungkinan area TOP, hindari LONG baru"
        )
    elif kol_count >= 3 and kol_weighted >= 5 and x_sent == "BULLISH":
        lessons.append(
            f"{coin_label}: {kol_count} KOL aktif bullish di X "
            f"— early narrative, LONG bias valid"
        )
    elif kol_count >= 2 and x_sent == "BEARISH":
        lessons.append(
            f"{coin_label}: KOL bearish di X — SHORT bias meningkat, waspada dump"
        )

    if bear > bull * 2 and bear >= 5:
        lessons.append(
            f"{coin_label}: Bear tweets {bear} >> Bull tweets {bull} di X "
            f"— sentimen sangat negatif"
        )

    return lessons


# ─────────────────────────────────────────────
# AI LESSON DERIVATION
# ─────────────────────────────────────────────

def _derive_lessons_from_events(
    events: list[dict], coin: str, headlines: list[str]
) -> list[str]:
    """
    Derive learning lessons dari events yang terdeteksi.
    Contoh output: "SOL token unlock terdeteksi — hindari LONG sampai event selesai"
    """
    lessons = []
    for ev in events:
        cat = ev["category"]
        direction = ev["direction"]
        coin_label = coin if coin else "Market"

        if cat == "TOKEN_UNLOCK":
            lessons.append(f"{coin_label}: Token unlock terdeteksi — tekanan jual meningkat, hindari LONG")
        elif cat == "HACK_EXPLOIT":
            lessons.append(f"{coin_label}: Exploit/hack terdeteksi — HIGH RISK, jangan trade sampai situasi jelas")
        elif cat == "FED_HAWKISH":
            lessons.append("Makro: Fed hawkish — crypto bearish bias, kurangi exposure LONG")
        elif cat == "FED_DOVISH":
            lessons.append("Makro: Fed dovish/pivot — crypto bullish bias, LONG setup lebih valid")
        elif cat == "FOMC_DECISION":
            lessons.append("Makro: FOMC meeting — volatilitas tinggi, SL lebih lebar atau skip")
        elif cat == "REGULATORY_NEGATIVE":
            lessons.append(f"{coin_label}: Regulatory crackdown — bearish jangka pendek, hindari LONG")
        elif cat == "REGULATORY_POSITIVE":
            lessons.append(f"{coin_label}: Regulatory positive — bullish sentiment, LONG bias valid")
        elif cat == "LISTING":
            lessons.append(f"{coin_label}: Listing baru di exchange besar — bullish pump potential")
        elif cat == "MARKET_CRASH":
            lessons.append("Market: Risk-off event — kurangi semua exposure, tunggu stabilisasi")
        elif cat == "INFLATION_HIGH":
            lessons.append("Makro: Inflasi tinggi — risk asset tertekan, tambah kehati-hatian")
        elif cat == "INFLATION_LOW":
            lessons.append("Makro: Inflasi turun — crypto bullish bias, central bank pivot lebih mungkin")

    return lessons


def _ai_summarize_news(
    coin: str,
    headlines: list[str],
    events: list[dict],
    x_data: dict = None,
) -> dict:
    """
    Gunakan DeepSeek untuk derive insight dari berita + X sentiment.
    Return: {"sentiment": str, "score": int, "lesson": str, "urgency": str}
    """
    if not DEEPSEEK_API_KEY or (not headlines and not x_data):
        return {"sentiment": "NEUTRAL", "score": 0, "lesson": "", "urgency": "LOW"}

    events_str    = ", ".join(f"{e['category']} ({e['direction']})" for e in events) or "tidak ada"
    headlines_str = "\n".join(f"- {h}" for h in headlines[:5]) or "tidak ada"

    # Susun X context
    x_block = ""
    if x_data:
        euphoria  = x_data.get("euphoria", False)
        kol_cnt   = x_data.get("kol_count", 0)
        x_sent    = x_data.get("sentiment_label", "NEUTRAL")
        bull      = x_data.get("bull_tweets", 0)
        bear      = x_data.get("bear_tweets", 0)
        kol_ments = x_data.get("kol_mentions", [])
        x_block = (
            f"X Sentiment: {x_sent} | KOL aktif: {kol_cnt} | "
            f"Bull: {bull} Bear: {bear} | Euphoria: {'YA' if euphoria else 'tidak'}"
        )
        if kol_ments:
            x_block += "\nKOL mentions:\n" + "\n".join(f"  - {m}" for m in kol_ments[:2])

    try:
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Kamu adalah analis news + social sentiment crypto. "
                            "Gabungkan berita dan X (Twitter) untuk insight trading. "
                            "Output JSON valid saja, tanpa markdown."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Analisa untuk {coin}:\n\n"
                            f"BERITA:\n{headlines_str}\n\n"
                            f"EVENTS: {events_str}\n\n"
                            f"X/TWITTER:\n{x_block if x_block else 'tidak ada data'}\n\n"
                            f"Balas JSON:\n"
                            f'{{"sentiment":"BULLISH|BEARISH|NEUTRAL|MIXED",'
                            f'"score":-50 sampai 50,'
                            f'"lesson":"satu kalimat insight trading actionable Bahasa Indonesia",'
                            f'"urgency":"LOW|MEDIUM|HIGH"}}'
                        ),
                    },
                ],
                "temperature":     0.2,
                "max_tokens":      150,
                "response_format": {"type": "json_object"},
            },
            timeout=20,
        )
        if r.status_code == 200:
            data   = r.json()["choices"][0]["message"]["content"]
            result = json.loads(data)
            return {
                "sentiment": str(result.get("sentiment", "NEUTRAL")).upper(),
                "score":     int(result.get("score", 0)),
                "lesson":    str(result.get("lesson", "")),
                "urgency":   str(result.get("urgency", "LOW")).upper(),
            }
    except Exception as e:
        log.debug(f"AI summarize error {coin}: {e}")
    return {"sentiment": "NEUTRAL", "score": 0, "lesson": "", "urgency": "LOW"}


# ─────────────────────────────────────────────
# MAIN FETCH CYCLE
# ─────────────────────────────────────────────

def run_news_fetch(send_telegram_fn=None) -> dict:
    """
    Main hourly news fetch cycle.
    Fetch semua koin + macro, derive lessons, simpan ke cache.

    Return: intelligence dict yang disimpan.
    """
    log.info("📰 News Agent: memulai hourly fetch cycle...")
    started_at = datetime.now(timezone.utc)

    intelligence = {
        "last_updated":    started_at.isoformat(),
        "trading_session": _detect_trading_session(),
        "coins":           {},
        "macro":           {},
        "derived_lessons": [],
        "summary_text":    "",
    }

    all_lessons     = []
    high_urgency    = []   # coins dengan urgency HIGH

    # ── 1. Macro news ─────────────────────────
    try:
        macro_headlines = _fetch_headlines(MACRO_QUERY, days=1, n=7)
        macro_events    = _scan_events(macro_headlines)
        macro_ai        = _ai_summarize_news("MACRO", macro_headlines, macro_events) if macro_headlines else {}

        intelligence["macro"] = {
            "headlines":  macro_headlines[:5],
            "events":     [f"{e['category']} ({e['direction']})" for e in macro_events],
            "sentiment":  macro_ai.get("sentiment", "NEUTRAL"),
            "score":      macro_ai.get("score", 0),
            "lesson":     macro_ai.get("lesson", ""),
            "urgency":    macro_ai.get("urgency", "LOW"),
            "fetched_at": started_at.isoformat(),
        }

        macro_lessons = _derive_lessons_from_events(macro_events, "", macro_headlines)
        all_lessons.extend(macro_lessons)

        if macro_ai.get("urgency") == "HIGH":
            high_urgency.append(f"MACRO: {macro_ai.get('lesson','')}")

        log.info(f"  Macro: {len(macro_headlines)} articles, {len(macro_events)} events, sentiment={macro_ai.get('sentiment','?')}")
    except Exception as e:
        log.warning(f"Macro news fetch error: {e}")

    # ── 2. Per-coin: News + X sentiment secara bersamaan ──────────
    # NewsAPI: top 10, X: top 10 (bisa overlap, rate limit masing-masing)
    coins_to_fetch = MONITORED_COINS[:10]
    x_euphoria_coins = []   # koin dengan euphoria X

    for coin in coins_to_fetch:
        try:
            # 2a. NewsAPI
            query     = COIN_SEARCH_MAP.get(coin, f"{coin} cryptocurrency")
            headlines = _fetch_headlines(query, days=1, n=4)
            events    = _scan_events(headlines, coin)

            # 2b. X sentiment (parallel lewat module)
            x_data = _fetch_x_for_coin(coin)
            time.sleep(0.4)   # rate limit buffer antara koin

            # 2c. AI summarize gabungan news + X
            ai_result = {}
            has_content = events or (headlines and len(headlines) >= 2) or x_data
            if has_content:
                ai_result = _ai_summarize_news(coin, headlines, events, x_data)
                time.sleep(0.5)

            intelligence["coins"][coin] = {
                "headlines":  headlines[:3],
                "events":     [f"{e['category']} ({e['direction']})" for e in events],
                "sentiment":  ai_result.get("sentiment", "NEUTRAL"),
                "score":      ai_result.get("score", 0),
                "lesson":     ai_result.get("lesson", ""),
                "urgency":    ai_result.get("urgency", "LOW"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                # X-specific fields
                "x": x_data,
            }

            # Derive lessons dari news events + X
            coin_lessons  = _derive_lessons_from_events(events, coin, headlines)
            x_lessons     = _derive_lessons_from_x(coin, x_data)
            all_lessons.extend(coin_lessons)
            all_lessons.extend(x_lessons)

            if x_data.get("euphoria"):
                x_euphoria_coins.append(coin)

            if ai_result.get("urgency") == "HIGH":
                high_urgency.append(f"{coin}: {ai_result.get('lesson','')}")

            log_parts = []
            if events:
                log_parts.append(f"{len(events)} news events")
            if x_data:
                log_parts.append(
                    f"X: {x_data.get('sentiment_label','?')} "
                    f"KOL={x_data.get('kol_count',0)}"
                    f"{' EUPHORIA' if x_data.get('euphoria') else ''}"
                )
            if log_parts:
                log.info(f"  {coin}: " + " | ".join(log_parts))

        except Exception as e:
            log.warning(f"Coin fetch error {coin}: {e}")
        time.sleep(0.2)

    # ── 3. Store derived lessons ──────────────
    intelligence["derived_lessons"] = [
        {
            "text":       lesson,
            "derived_at": started_at.isoformat(),
            "expires_at": (started_at + timedelta(hours=6)).isoformat(),
        }
        for lesson in all_lessons[:25]
    ]

    # Simpan juga x_euphoria info di level intelligence
    if x_euphoria_coins:
        intelligence["x_euphoria_coins"] = x_euphoria_coins

    # ── 4. Build summary text ─────────────────
    session = intelligence["trading_session"]
    macro_s = intelligence["macro"].get("sentiment", "NEUTRAL")
    macro_l = intelligence["macro"].get("lesson", "")
    n_events = sum(
        len(c.get("events", []))
        for c in intelligence["coins"].values()
    ) + len(intelligence["macro"].get("events", []))
    n_x_active = sum(
        1 for c in intelligence["coins"].values()
        if c.get("x", {}).get("kol_count", 0) >= 1
    )

    summary_parts = [
        f"📰 Fetch selesai | Session: {session} | Macro: {macro_s}",
        f"⚡ {n_events} news events | 🐦 {n_x_active} koin dengan KOL aktif",
    ]
    if x_euphoria_coins:
        summary_parts.append(f"🚨 X Euphoria: {', '.join(x_euphoria_coins[:3])}")
    if high_urgency:
        summary_parts.append("⚠️ HIGH URGENCY: " + " | ".join(high_urgency[:3]))
    if macro_l:
        summary_parts.append(f"🌐 Macro: {macro_l}")

    intelligence["summary_text"] = "\n".join(summary_parts)

    # ── 5. Inject lessons ke learning engine ─
    try:
        from learning_engine import add_manual_lesson
        for lesson in all_lessons[:5]:   # inject top 5 ke learning engine
            try:
                add_manual_lesson(
                    rule=f"[News Agent] {lesson}",
                    tags=["news", "event", "macro"],
                    pinned=False,
                )
            except Exception:
                pass
    except ImportError:
        pass

    # ── 6. Save ke file ───────────────────────
    try:
        with open(NEWS_INTELLIGENCE_FILE, "w") as f:
            json.dump(intelligence, f, indent=2, ensure_ascii=False)
        log.info(f"📰 News intelligence saved ({len(all_lessons)} lessons, {n_events} events)")
    except Exception as e:
        log.warning(f"Failed to save news intelligence: {e}")

    # ── 7. Kirim Telegram alert kalau ada HIGH URGENCY ─
    if high_urgency and send_telegram_fn:
        ts = started_at.strftime("%d %b %H:%M UTC")
        alert = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📰 <b>NEWS ALERT</b> — {ts}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⚠️ <b>High-Impact Event Terdeteksi:</b>\n"
            + "\n".join(f"  • {h}" for h in high_urgency[:5])
            + "\n\n<i>Data otomatis dari News Agent hourly fetch.</i>"
        )
        try:
            send_telegram_fn(alert, parse_mode="HTML")
        except Exception as e:
            log.warning(f"Telegram alert error: {e}")

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    log.info(f"📰 News Agent: selesai dalam {elapsed:.1f}s")
    return intelligence


# ─────────────────────────────────────────────
# CACHE READ (untuk deepseek_ai.py dan news_sentiment.py)
# ─────────────────────────────────────────────

def get_cached_news(coin: str = None, max_age_seconds: int = CACHE_TTL_SECONDS) -> Optional[dict]:
    """
    Baca news intelligence dari cache.
    Return dict untuk coin tertentu, atau full intelligence kalau coin=None.
    Return None kalau cache expired atau tidak ada.
    """
    try:
        if not os.path.exists(NEWS_INTELLIGENCE_FILE):
            return None
        with open(NEWS_INTELLIGENCE_FILE) as f:
            intel = json.load(f)

        last_updated = intel.get("last_updated", "")
        if last_updated:
            lu_dt = datetime.fromisoformat(last_updated)
            if lu_dt.tzinfo is None:
                lu_dt = lu_dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - lu_dt).total_seconds()
            if age > max_age_seconds:
                return None   # cache expired

        if coin is None:
            return intel

        sym        = coin.upper().replace("USDT", "")
        coin_data  = intel.get("coins", {}).get(sym, {})
        macro_data = intel.get("macro", {})
        x_data     = coin_data.get("x", {})

        # X sentiment fields
        x_sentiment_label = x_data.get("sentiment_label", "NEUTRAL") if x_data else "NEUTRAL"
        x_kol_count       = x_data.get("kol_count",       0)          if x_data else 0
        x_euphoria        = x_data.get("euphoria",         False)      if x_data else False
        x_kol_mentions    = x_data.get("kol_mentions",     [])         if x_data else []

        return {
            "symbol":             sym,
            "sentiment_label":    coin_data.get("sentiment", macro_data.get("sentiment", "NEUTRAL")),
            "sentiment_score":    coin_data.get("score",     macro_data.get("score", 0)),
            "trading_session":    intel.get("trading_session", ""),
            "high_impact_events": coin_data.get("events", []) + macro_data.get("events", []),
            "upcoming_unlocks":   [e for e in coin_data.get("events", []) if "UNLOCK" in e],
            "headlines":          coin_data.get("headlines", []) + macro_data.get("headlines", [])[:2],
            "macro_risk":         macro_data.get("lesson", ""),
            "coin_lesson":        coin_data.get("lesson", ""),
            "urgency":            coin_data.get("urgency", "LOW"),
            # X sentiment
            "x_sentiment":        x_sentiment_label,
            "x_kol_count":        x_kol_count,
            "x_euphoria":         x_euphoria,
            "x_kol_mentions":     x_kol_mentions[:3],
            "x_source":           x_data.get("source", "nitter") if x_data else "",
        }
    except Exception as e:
        log.debug(f"Cache read error: {e}")
        return None


def get_active_lessons_from_news() -> list[str]:
    """
    Ambil lessons yang masih aktif (belum expired) dari cache.
    Untuk diinjeksikan ke AI prompt.
    """
    try:
        if not os.path.exists(NEWS_INTELLIGENCE_FILE):
            return []
        with open(NEWS_INTELLIGENCE_FILE) as f:
            intel = json.load(f)

        now = datetime.now(timezone.utc)
        active = []
        for lesson in intel.get("derived_lessons", []):
            expires_str = lesson.get("expires_at", "")
            if expires_str:
                exp = datetime.fromisoformat(expires_str)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp > now:
                    active.append(lesson["text"])
            else:
                active.append(lesson["text"])
        return active[:10]
    except Exception:
        return []


def is_news_cache_fresh(max_age_minutes: int = 65) -> bool:
    """Check apakah cache masih segar."""
    return get_cached_news(max_age_seconds=max_age_minutes * 60) is not None
