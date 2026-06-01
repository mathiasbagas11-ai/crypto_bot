#!/usr/bin/env python3
"""
NEWS AGENT — Auto Hourly News Intelligence
==========================================
Fetch dan proses berita crypto setiap jam, simpan sebagai konteks untuk:
1. DeepSeek signal review (news context otomatis tersedia tanpa re-fetch)
2. Learning engine (derive lessons dari events yang terdeteksi)
3. Sinyal otomatis (token unlock, regulatory, dll jadi confluence data)

Flow:
  Setiap jam → fetch news 25 koin + macro
             → deteksi high-impact events
             → derive learning lessons
             → simpan ke news_intelligence.json

File output:
  news_intelligence.json — cache berita + derived lessons (TTL 1 jam)

Requires .env:
  NEWSAPI_KEY=...        (untuk fetch artikel)
  DEEPSEEK_API_KEY=...   (untuk derive lessons via AI)
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


def _ai_summarize_news(coin: str, headlines: list[str], events: list[dict]) -> dict:
    """
    Gunakan DeepSeek untuk derive insight lebih dalam dari berita.
    Return: {"sentiment": str, "score": int, "lesson": str, "urgency": str}
    """
    if not DEEPSEEK_API_KEY or not headlines:
        return {"sentiment": "NEUTRAL", "score": 0, "lesson": "", "urgency": "LOW"}

    events_str = ", ".join(f"{e['category']} ({e['direction']})" for e in events) or "tidak ada"
    headlines_str = "\n".join(f"- {h}" for h in headlines[:5])

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
                            "Kamu adalah analis news crypto. Analisa berita dan berikan output JSON. "
                            "JANGAN pakai markdown. Hanya JSON valid."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Analisa berita untuk {coin}:\n{headlines_str}\n\n"
                            f"Events terdeteksi: {events_str}\n\n"
                            f"Balas JSON persis:\n"
                            f'{{"sentiment":"BULLISH|BEARISH|NEUTRAL|MIXED",'
                            f'"score":-50 hingga 50,'
                            f'"lesson":"satu kalimat insight trading actionable dalam Bahasa Indonesia",'
                            f'"urgency":"LOW|MEDIUM|HIGH"}}'
                        ),
                    },
                ],
                "temperature":   0.2,
                "max_tokens":    150,
                "response_format": {"type": "json_object"},
            },
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()["choices"][0]["message"]["content"]
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

    # ── 2. Per-coin news (batch bergantian untuk tidak exhaust API) ─
    # Batasi ke 10 koin per fetch untuk jaga rate limit NewsAPI
    coins_to_fetch = MONITORED_COINS[:10]
    for coin in coins_to_fetch:
        try:
            query     = COIN_SEARCH_MAP.get(coin, f"{coin} cryptocurrency")
            headlines = _fetch_headlines(query, days=1, n=4)
            events    = _scan_events(headlines, coin)

            # AI hanya untuk koin dengan events atau headlines penting
            ai_result = {}
            if events or (headlines and len(headlines) >= 2):
                ai_result = _ai_summarize_news(coin, headlines, events)
                time.sleep(0.5)   # rate limit buffer

            intelligence["coins"][coin] = {
                "headlines":  headlines[:3],
                "events":     [f"{e['category']} ({e['direction']})" for e in events],
                "sentiment":  ai_result.get("sentiment", "NEUTRAL"),
                "score":      ai_result.get("score", 0),
                "lesson":     ai_result.get("lesson", ""),
                "urgency":    ai_result.get("urgency", "LOW"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }

            coin_lessons = _derive_lessons_from_events(events, coin, headlines)
            all_lessons.extend(coin_lessons)

            if ai_result.get("urgency") == "HIGH":
                high_urgency.append(f"{coin}: {ai_result.get('lesson','')}")

            if events:
                log.info(f"  {coin}: {len(events)} events ({', '.join(e['category'] for e in events)})")

        except Exception as e:
            log.warning(f"News fetch error {coin}: {e}")
        time.sleep(0.3)   # gentle rate limiting

    # ── 3. Store derived lessons ──────────────
    intelligence["derived_lessons"] = [
        {
            "text":       lesson,
            "derived_at": started_at.isoformat(),
            "expires_at": (started_at + timedelta(hours=6)).isoformat(),
        }
        for lesson in all_lessons[:20]  # max 20 lessons
    ]

    # ── 4. Build summary text ─────────────────
    session = intelligence["trading_session"]
    macro_s = intelligence["macro"].get("sentiment", "NEUTRAL")
    macro_l = intelligence["macro"].get("lesson", "")
    n_events = sum(
        len(c.get("events", []))
        for c in intelligence["coins"].values()
    ) + len(intelligence["macro"].get("events", []))

    summary_parts = [f"📰 News fetch selesai | Session: {session} | Macro: {macro_s} | {n_events} events terdeteksi"]
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

        sym = coin.upper().replace("USDT", "")
        coin_data = intel.get("coins", {}).get(sym, {})
        macro_data = intel.get("macro", {})

        return {
            "symbol":            sym,
            "sentiment_label":   coin_data.get("sentiment", macro_data.get("sentiment", "NEUTRAL")),
            "sentiment_score":   coin_data.get("score",     macro_data.get("score", 0)),
            "trading_session":   intel.get("trading_session", ""),
            "high_impact_events": coin_data.get("events", []) + macro_data.get("events", []),
            "upcoming_unlocks":  [e for e in coin_data.get("events", []) if "UNLOCK" in e],
            "headlines":         coin_data.get("headlines", []) + macro_data.get("headlines", [])[:2],
            "macro_risk":        macro_data.get("lesson", ""),
            "coin_lesson":       coin_data.get("lesson", ""),
            "urgency":           coin_data.get("urgency", "LOW"),
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
