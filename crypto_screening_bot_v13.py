#!/usr/bin/env python3
"""
CRYPTO SCREENING BOT v13
=========================
Upgrade dari v12:

1. ANTI-LOOKAHEAD (Freqtrade-inspired)
   → analyze_timeframe pakai closed_candles[:-1] untuk semua kalkulasi
   → current_price dari candles[-1] untuk display saja
   → Fix repainting / lookahead bias di semua signal

2. SYMBOL MEMORY (Meridian-inspired)
   → Per-symbol win rate, SL rate, avg hold, avg score
   → Auto-derived lessons per coin dari trade history
   → Auto-blacklist coin jika SL rate > 75% (10 trades terakhir)
   → Inject ke /analyze prompt sebagai historical context
   → Commands: /symbolmemory /symbolstats /blacklist /unblacklist

3. ENTRY CONFIRMATION MODE
   → calculate_trade_plan: entry_mode MOMENTUM_NOW | RETEST_WAIT
   → MOMENTUM_NOW: breakout/breakdown + volume spike + OI surge → entry market
   → RETEST_WAIT: ada OB/FVG valid → tunggu retest + konfirmasi candle 15M
   → build_prepump/predump/scalp_message tampilkan mode secara eksplisit

AI call breakdown (tidak berubah):
  Auto scan         → ❌ no AI (rule-based only)
  /analyze <COIN>   → ✅ Gemini + symbol memory context
  /ask <question>   → ✅ Gemini (manual trigger)
  /chart            → ✅ Gemini Vision (image input)
  /news, /macro     → ✅ Gemini + Google Search grounding
  /scalp/prepump/predump → ❌ no AI (scoring only)
  /weeksummary      → ✅ Gemini (manual trigger)
  /backtest*        → ❌ no AI (pure rule-based replay)
  Confirmed Signal  → ❌ no AI (pure multi-detector fusion)
"""

import os
import re
import base64
import time
import logging
import threading
import requests
import numpy as np
from datetime import datetime, timezone
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler

load_dotenv()

# ── Security module ───────────────────────────
try:
    from security import is_allowed, secure_load, secure_save, get_security_status
    SECURITY_MODULE = True
except ImportError:
    SECURITY_MODULE = False
    logging.getLogger("security").warning("security.py tidak ditemukan — whitelist & enkripsi dinonaktifkan")
    # Fallback stubs
    def is_allowed(chat_id): return True
    def secure_load(filepath, default=None): 
        import json
        try:
            with open(filepath) as f: return json.load(f)
        except: return default or {}
    def secure_save(filepath, data):
        import json
        try:
            with open(filepath, "w") as f: json.dump(data, f, indent=2)
            return True
        except: return False
    def get_security_status(): return "⚠️ Security module tidak aktif"

# ── v9: Module imports ────────────────────────
try:
    from news_sentiment    import get_coin_sentiment, get_macro_sentiment, format_sentiment_block
    NEWS_MODULE = True
except ImportError:
    NEWS_MODULE = False
    log_tmp = logging.getLogger("v9")
    log_tmp.warning("news_sentiment.py tidak ditemukan — fitur news dinonaktifkan")

try:
    from risk_manager      import (calc_position_size, format_risk_block,
                                   format_risk_status, set_capital, set_risk_pct,
                                   set_daily_loss_limit, record_trade_result,
                                   get_risk_summary, reset_daily as risk_reset_daily)
    RISK_MODULE = True
except ImportError:
    RISK_MODULE = False

PORTFOLIO_MODULE = False  # dinonaktifkan

# ── v11: Learning Engine ──────────────────────
try:
    from learning_engine import (
        log_decision, get_recent_decisions, build_ai_context_block,
        handle_logoutcome_command, handle_lessons_command,
        handle_decisions_command, handle_evolve_command,
        handle_addlesson_command, get_performance_stats_text,
    )
    LEARNING_MODULE = True
except ImportError:
    LEARNING_MODULE = False
    logging.getLogger("v11").warning("learning_engine.py tidak ditemukan — fitur learning dinonaktifkan")

# ── v11: Trade Journal ────────────────────────
try:
    from trade_journal import (
        wizard_start, wizard_process, is_in_wizard, is_wizard_expecting_image,
        parse_oneliner, format_trade_logged, log_trade,
        get_recent_trades, format_recent_trades,
        format_weekly_summary, set_initial_balance,
        get_current_balance,
    )
    JOURNAL_MODULE = True
except ImportError:
    JOURNAL_MODULE = False
    logging.getLogger("v11").warning("trade_journal.py tidak ditemukan — fitur journal dinonaktifkan")

# ── v12: Backtest Engine ──────────────────────
try:
    from backtest_engine import (
        handle_backtest_command  as _bt_backtest,
        handle_btresult_command  as _bt_result,
        handle_btcompare_command as _bt_compare,
        handle_btstats_command   as _bt_stats,
        STRATEGY_CONFIG          as BT_STRATEGY_CONFIG,
    )
    BACKTEST_MODULE = True
except ImportError:
    BACKTEST_MODULE = False
    logging.getLogger("v12").warning("backtest_engine.py tidak ditemukan — fitur /backtest dinonaktifkan")

# ── v12: Signal Outcome Tracker ───────────────
try:
    from signal_tracker import (
        on_scan_start, on_signal_sent,
        format_tracker_summary,
    )
    TRACKER_MODULE = True
except ImportError:
    TRACKER_MODULE = False
    logging.getLogger("v12").warning("signal_tracker.py tidak ditemukan — auto signal tracking dinonaktifkan")

# ── v12: Confirmed Entry Signal ───────────────
try:
    from confirmed_signal import (
        run_confirmed_signal_scan,
        format_confirmed_signal_message,
    )
    CONFIRMED_MODULE = True
except ImportError:
    CONFIRMED_MODULE = False
    logging.getLogger("v12").warning("confirmed_signal.py tidak ditemukan — confirmed entry signal dinonaktifkan")

# ── v13: Symbol Memory ────────────────────────
try:
    from symbol_memory import (
        record_symbol_outcome, get_symbol_context,
        build_symbol_context_block, get_all_stats_summary,
        get_symbol_detail, is_blacklisted,
        manual_blacklist, manual_unblacklist,
    )
    SYMBOL_MEMORY_MODULE = True
except ImportError:
    SYMBOL_MEMORY_MODULE = False
    logging.getLogger("v13").warning("symbol_memory.py tidak ditemukan — fitur symbol memory dinonaktifkan")

# ── v13: Multi-Exchange Resolver ──────────────
try:
    from exchange_resolver import (
        resolve_symbol       as _exc_resolve,
        get_ohlcv            as _exc_ohlcv,
        get_ticker           as _exc_ticker,
        format_not_found_message,
        format_found_on_other_exchange,
    )
    EXCHANGE_RESOLVER = True
except ImportError:
    EXCHANGE_RESOLVER = False
    logging.getLogger("v13").warning("exchange_resolver.py tidak ditemukan — fallback ke Binance saja")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY")   # v10: Claude API

# v9: New module env vars
NEWSAPI_KEY           = os.getenv("NEWSAPI_KEY", "")

# v11: Gemini dipakai untuk semua AI calls (free tier)

SCAN_INTERVAL_MINUTES   = 10
TOP_COINS_COUNT         = 5
PREPUMP_SCAN_INTERVAL   = 5    # menit — scan cepat, alert HANYA kalau HOT
PREPUMP_ALERT_THRESHOLD = 70   # score >= 70 → kirim alert
PREDUMP_ALERT_THRESHOLD = 70   # score >= 70 → kirim alert

# CoinGecko filters
MIN_MARKET_CAP       = 50_000_000
MIN_VOLUME           = 20_000_000
MAX_VOLUME_INCREASE  = 500

# SMC thresholds
FVG_MIN_GAP_PCT       = 0.3
REJECTION_WICK_RATIO  = 0.6
OB_LOOKBACK           = 10
ZSCORE_ANOMALY_THRESH = 2.0

# Pre-pump thresholds
FUNDING_SQUEEZE_THRESH  = -0.01   # funding < -1% → squeeze potential
FUNDING_EXTREME_THRESH  = -0.03   # funding < -3% → extreme squeeze
OI_SURGE_THRESH         = 5.0     # OI naik >5% dalam 1h
ATR_COIL_RATIO          = 0.015   # price range < 1.5% ATR → coiling
MOMENTUM_RSI_THRESH     = 55      # RSI > 55 → momentum building
VOLUME_SURGE_MULT       = 2.0     # volume spike 2x normal

# Pre-dump thresholds (opposite of pre-pump)
FUNDING_DUMP_THRESH     = 0.01    # funding > +1% → long squeeze potential
FUNDING_DUMP_EXTREME    = 0.03    # funding > +3% → extreme long squeeze
RSI_OVERBOUGHT_THRESH   = 65      # RSI > 65 → overbought / exhaustion zone
LS_LONG_HEAVY_THRESH    = 1.5     # L/S > 1.5 → longs too crowded → dump fuel

# v8 — Liquidity & Scalp/Swing thresholds
EQUAL_HL_TOLERANCE      = 0.003   # 0.3% tolerance buat equal highs/lows
SWEEP_WICK_MIN_PCT      = 0.004   # spike minimal 0.4% melewati swing H/L
TRENDLINE_MIN_POINTS    = 3       # minimal 3 swing points untuk trendline valid
SCALP_TP_PCT            = 0.015   # scalp TP: 1.5%
SCALP_SL_PCT            = 0.008   # scalp SL: 0.8%
SWING_TP_PCT            = 0.055   # swing TP: 5.5%
SWING_SL_PCT            = 0.025   # swing SL: 2.5%
SCALP_MIN_SCORE         = 50      # minimal score buat scalp signal
SCALP_ALERT_THRESHOLD   = 65      # score >= 65 → auto alert (GOOD SCALP atau lebih)

# ── v13: SIGNAL GATE — hanya kirim kalau semua criteria green ──────────────
GATE_MASTER_SCORE_MIN   = 65      # master score minimum (dari confirmed_signal engine)
GATE_MONEYFLOW_TF_MIN   = 2       # min berapa TF yang harus inflow/outflow
GATE_BT_PF_MIN          = 1.0     # backtest profit factor minimum
GATE_REQUIRE_ENTRY_MODE = True    # entry mode HARUS jelas (MOMENTUM_NOW atau RETEST_WAIT)
GATE_COOLDOWN_HOURS     = 4       # cooldown per symbol setelah signal dikirim
HEARTBEAT_INTERVAL_HRS  = 4       # interval "no signal" update
WATCHLIST_THRESHOLD     = 60      # master score ambang batas masuk watchlist

# Gemini
GEMINI_API_URL   = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
BINANCE_BASE     = "https://api.binance.com/api/v3"
BINANCE_FUTURES  = "https://fapi.binance.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# SYMBOL MAP
# ─────────────────────────────────────────────
SYMBOL_MAP = {
    "bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "binancecoin": "BNBUSDT",
    "solana": "SOLUSDT", "ripple": "XRPUSDT", "cardano": "ADAUSDT",
    "avalanche-2": "AVAXUSDT", "dogecoin": "DOGEUSDT", "polkadot": "DOTUSDT",
    "chainlink": "LINKUSDT", "litecoin": "LTCUSDT", "uniswap": "UNIUSDT",
    "near": "NEARUSDT", "aptos": "APTUSDT", "arbitrum": "ARBUSDT",
    "optimism": "OPUSDT", "celestia": "TIAUSDT", "injective-protocol": "INJUSDT",
    "sui": "SUIUSDT", "sei-network": "SEIUSDT", "render-token": "RENDERUSDT",
    "fetch-ai": "FETUSDT", "bittensor": "TAOUSDT", "worldcoin-wld": "WLDUSDT",
    "pyth-network": "PYTHUSDT", "jito-governance-token": "JITOUSDT",
    "jupiter-exchange-solana": "JUPUSDT", "bonk": "BONKUSDT",
    "pepe": "PEPEUSDT", "floki": "FLOKIUSDT", "shiba-inu": "SHIBUSDT",
    "ondo-finance": "ONDOUSDT", "ethena": "ENAUSDT", "pendle": "PENDLEUSDT",
    "aave": "AAVEUSDT", "maker": "MKRUSDT", "curve-dao-token": "CRVUSDT",
    "bio-protocol": "BIOUSDT", "virtuals-protocol": "VIRTUALUSDT",
    "ai16z": "AI16ZUSDT", "arc": "ARCUSDT",
    "hyperliquid": "HYPEUSDT", "movement": "MOVEUSDT",
    "trump": "TRUMPUSDT", "memefi": "MEMEFIUSDT",
}

TICKER_TO_BINANCE = {}
for _, bsym in SYMBOL_MAP.items():
    ticker = bsym.replace("USDT", "").replace("UST", "")
    TICKER_TO_BINANCE[ticker] = bsym

# ─────────────────────────────────────────────
# GEMINI AI
# ─────────────────────────────────────────────

def _gemini_request(payload: dict, timeout: int = 25, max_retries: int = 4) -> str:
    if not GEMINI_API_KEY:
        return ""

    time.sleep(2)  # rate limit guard — max ~30 RPM, aman di free tier

    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
                json=payload,
                timeout=timeout
            )
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif r.status_code == 429:
                # Exponential backoff: 15s, 30s, 60s, 120s
                wait = 15 * (2 ** attempt)
                log.warning(f"Gemini 429 rate limit (attempt {attempt+1}/{max_retries}), retry in {wait}s...")
                time.sleep(wait)
                continue
            elif r.status_code == 503:
                wait = 10 * (attempt + 1)
                log.warning(f"Gemini 503 overloaded, retry in {wait}s...")
                time.sleep(wait)
                continue
            else:
                log.warning(f"Gemini API error {r.status_code}: {r.text[:200]}")
                return ""
        except Exception as e:
            log.warning(f"Gemini exception (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(10)

    log.warning("Gemini: max retries reached, skipping.")
    return ""


def _build_ob_mitigation_context(ob4: dict, ob1: dict, price: float, candles_4h: list, candles_1h: list) -> str:
    """
    Cek apakah Order Block sudah 'mitigated' (disentuh & ditembus price)
    atau masih 'fresh' (belum pernah dites).
    OB dianggap mitigated kalau price pernah masuk ke dalam zone OB-nya.
    """
    lines = []

    def check_mitigated(ob: dict, candles: list, label: str) -> str:
        if not ob or not candles:
            return ""
        ob_top    = ob.get("top", 0)
        ob_bottom = ob.get("bottom", 0)
        ob_mid    = ob.get("mid", 0)

        # Cek apakah ada candle yang pernah masuk ke dalam zone OB
        touches = 0
        for c in candles[-30:]:  # 30 candle terakhir
            if c["low"] <= ob_top and c["high"] >= ob_bottom:
                touches += 1

        if touches == 0:
            status = "🟢 FRESH (belum pernah disentuh)"
        elif touches == 1:
            status = "🟡 TESTED ONCE (1x disentuh, masih valid)"
        elif touches <= 3:
            status = f"🟠 PARTIALLY MITIGATED ({touches}x disentuh, mulai lemah)"
        else:
            status = f"🔴 MITIGATED ({touches}x disentuh, kemungkinan besar invalid)"

        dist = ob.get("distance_pct", 0)
        return f"{label} OB @ ${ob_mid:.4f} (dist: {dist:.1f}%) → {status}"

    if ob4.get("bullish_ob"):
        r = check_mitigated(ob4["bullish_ob"], candles_4h, "4H Bullish")
        if r: lines.append(r)
    if ob4.get("bearish_ob"):
        r = check_mitigated(ob4["bearish_ob"], candles_4h, "4H Bearish")
        if r: lines.append(r)
    if ob1.get("bullish_ob"):
        r = check_mitigated(ob1["bullish_ob"], candles_1h, "1H Bullish")
        if r: lines.append(r)
    if ob1.get("bearish_ob"):
        r = check_mitigated(ob1["bearish_ob"], candles_1h, "1H Bearish")
        if r: lines.append(r)

    return "\n".join(lines) if lines else "Tidak ada OB aktif terdeteksi"


def gemini_sentiment_overlay(symbol: str) -> str:
    """
    Gunakan Gemini dengan Google Search grounding untuk cek:
    1. Berita/event high-impact hari ini (CPI, FOMC, dll)
    2. Sentimen market crypto saat ini
    3. Apakah ada risk event yang mempengaruhi setup TA
    """
    if not GEMINI_API_KEY:
        return ""

    # Pakai model dengan search grounding (gemini-1.5-flash support built-in search)
    coin_name = symbol.replace("USDT", "").replace("UST", "")
    from datetime import date
    today = date.today().strftime("%d %B %Y")

    prompt = f"""Tanggal hari ini: {today}

Tugas kamu:
1. Cek apakah ada economic event high-impact HARI INI atau BESOK yang bisa mempengaruhi crypto market (contoh: rilis data CPI, FOMC meeting, NFP, GDP, dll). Sebutkan nama event dan jam-nya jika ada.
2. Cek sentimen market crypto global saat ini secara umum (fear & greed, dominasi BTC, dll).
3. Cek apakah ada berita signifikan terbaru (max 24 jam) khusus tentang {coin_name} yang bisa mempengaruhi harga.

Jawab dalam Bahasa Indonesia, SINGKAT dan PADAT (max 4 kalimat total). Format:
- [EVENT] ada/tidak ada event high-impact: ...
- [SENTIMEN] kondisi market: ...
- [{coin_name}] berita terbaru: ...

Jika tidak ada informasi terbaru yang relevan, katakan "tidak ada event/berita signifikan hari ini"."""

    # Gunakan model dengan search grounding
    search_payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300}
    }

    # Try dengan search grounding dulu (model 2.0)
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
            json=search_payload,
            timeout=30
        )
        if r.status_code == 200:
            candidates = r.json().get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return text
    except Exception as e:
        log.warning(f"Gemini search grounding error: {e}")

    # Fallback: tanpa search grounding
    return _gemini_request({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300}
    })


def claude_analyze_coin(symbol: str, confluence: dict, tf_4h: dict, tf_1h: dict,
                         tf_15m: dict, oi_data: dict, price: float,
                         prepump: dict = None, predump: dict = None,
                         scalp: dict = None, swing: dict = None) -> str:
    """
    Analisa coin via Claude API (manual trigger only — tidak auto tiap scan).
    Menggantikan gemini_analyze_coin di v10.
    """
    if not ANTHROPIC_API_KEY:
        return "⚠️ ANTHROPIC_API_KEY belum diset di .env"

    s4  = tf_4h.get("structure", {})
    s1  = tf_1h.get("structure", {})
    rej = tf_15m.get("rejection", {})
    fvg = tf_15m.get("fvg", {})
    ob4 = tf_4h.get("order_blocks", {})
    ob1 = tf_1h.get("order_blocks", {})
    va4 = tf_4h.get("volume_anomaly", {})
    signals_summary = "\n".join(confluence.get("reasons", [])[:8])

    candles_4h = tf_4h.get("candles", [])
    candles_1h = tf_1h.get("candles", [])
    ob_ctx     = _build_ob_mitigation_context(ob4, ob1, price, candles_4h, candles_1h)

    # Liquidity & sweep context
    liq_1h    = tf_1h.get("liquidity", {})
    sweep_1h  = tf_1h.get("sweep", {})
    sweep_15m = tf_15m.get("sweep", {})
    tl_sup    = tf_4h.get("trendline_sup", {})
    tl_res    = tf_4h.get("trendline_res", {})

    liq_ctx = ""
    if liq_1h.get("nearest_eqh"):
        eqh = liq_1h["nearest_eqh"]
        liq_ctx += f"- Equal Highs (EQH): {eqh['distance_pct']:.1f}% di atas ({eqh['count']} touches) = liquidity target\n"
    if liq_1h.get("nearest_eql"):
        eql = liq_1h["nearest_eql"]
        liq_ctx += f"- Equal Lows (EQL): {eql['distance_pct']:.1f}% di bawah ({eql['count']} touches) = liquidity target\n"
    if sweep_1h.get("swept"):
        liq_ctx += f"- 1H Liquidity Sweep: {sweep_1h['sweep_type']} (recovery {sweep_1h['recovery_strength']:.0f}%)\n"
    if sweep_15m.get("swept"):
        liq_ctx += f"- 15M Liquidity Sweep: {sweep_15m['sweep_type']} (recovery {sweep_15m['recovery_strength']:.0f}%)\n"
    if tl_sup.get("valid"):
        liq_ctx += f"- Trendline Support: {tl_sup['distance_pct']:+.1f}% dari harga ({tl_sup['touches']} touches, {tl_sup['direction']})\n"
    if tl_res.get("valid"):
        liq_ctx += f"- Trendline Resist : {tl_res['distance_pct']:+.1f}% dari harga ({tl_res['touches']} touches, {tl_res['direction']})\n"

    prepump_ctx = ""
    if prepump and prepump.get("total_score", 0) >= 35:
        prepump_ctx = (f"\nPre-Pump Score: {prepump['total_score']}/100 ({prepump['label']})\n"
                       f"- Funding: {prepump['funding_score']}/30 | Momentum: {prepump['momentum_score']}/35 | OI+PA: {prepump['oi_pa_score']}/35")

    predump_ctx = ""
    if predump and predump.get("total_score", 0) >= 35:
        predump_ctx = (f"\nPre-Dump Score: {predump['total_score']}/100 ({predump['label']})\n"
                       f"- Funding: {predump['funding_score']}/30 | Momentum: {predump['momentum_score']}/35 | OI+PA: {predump['oi_pa_score']}/35")

    scalp_ctx = ""
    if scalp and scalp.get("score", 0) >= 50:
        scalp_ctx = f"\nScalp Setup: {scalp['label']} (score {scalp['score']}/100, arah {scalp['direction']})"

    swing_ctx = ""
    if swing and swing.get("score", 0) >= 50:
        swing_ctx = f"\nSwing Setup: {swing['label']} (score {swing['score']}/100, est hold {swing.get('hold_estimate','')})"

    prompt = f"""Kamu adalah analis crypto profesional ahli Smart Money Concepts (SMC), liquidity theory, dan price action.

Analisa lengkap untuk {symbol} — data real-time multi-timeframe:

═══ MARKET DATA ═══
Harga: ${price}
Signal: {confluence['direction']} | Score: {confluence['score']}/100 | Level: {confluence['level']}

Market Structure:
- 4H: {s4.get('trend','?')} | CHoCH: {s4.get('choch',False)} | BoS: {s4.get('bos',False)}
- 1H: {s1.get('trend','?')} | CHoCH: {s1.get('choch',False)} | BoS: {s1.get('bos',False)}

Entry Context:
- 15M Rejection: {rej.get('type','NONE')} (strength: {rej.get('strength',0)})
- 15M FVG: {fvg.get('fvg_type','NONE')}
- OI Change: {oi_data.get('oi_change_pct','N/A')}% | L/S Ratio: {oi_data.get('ls_ratio','N/A')} ({oi_data.get('ls_bias','N/A')})
- Funding Rate: {oi_data.get('funding_rate','N/A')}%
- Volume Anomaly 4H: {va4.get('is_anomaly',False)} ({va4.get('multiplier',1):.1f}x)

═══ LIQUIDITY & STRUCTURE ═══
{liq_ctx if liq_ctx else "- Tidak ada data liquidity zone signifikan"}

═══ ORDER BLOCK VALIDATION ═══
{ob_ctx}

═══ SETUP DETECTORS ═══
{prepump_ctx}{predump_ctx}{scalp_ctx}{swing_ctx}

═══ CONFLUENCE SIGNALS ═══
{signals_summary}

Berikan analisa SINGKAT dalam Bahasa Indonesia (max 6 kalimat total):
1. Kondisi market {symbol} sekarang — bias dan struktur dominan
2. Liquidity zone yang paling relevan sebagai target atau trap
3. Order Block yang masih FRESH dan layak sebagai area entry
4. Apakah ada setup scalp atau swing yang valid? Kenapa iya/tidak?
5. Risk utama yang perlu diwaspadai

Gaya: profesional, langsung ke poin, mudah dipahami trader Indonesia. Tanpa bullet point — paragraf mengalir."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key"        : ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type"     : "application/json",
            },
            json={
                "model"      : CLAUDE_MODEL,
                "max_tokens" : 500,
                "messages"   : [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if r.ok:
            return r.json()["content"][0]["text"].strip()
        else:
            log.warning(f"Claude API error {r.status_code}: {r.text[:200]}")
            return "⚠️ Claude tidak merespons saat ini."
    except Exception as e:
        log.error(f"claude_analyze_coin error: {e}")
        return "⚠️ Claude error."


def claude_free_ask(question: str) -> str:
    """
    Jawab pertanyaan crypto via Claude API.
    Menggantikan gemini_free_ask di v10.
    """
    if not ANTHROPIC_API_KEY:
        return "⚠️ ANTHROPIC_API_KEY belum diset di .env"

    prompt = f"""Kamu adalah asisten crypto trading yang ahli SMC, technical analysis, dan fundamental crypto.
Jawab dalam Bahasa Indonesia, singkat dan padat (max 5 kalimat):

{question}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key"        : ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type"     : "application/json",
            },
            json={
                "model"      : CLAUDE_MODEL,
                "max_tokens" : 400,
                "messages"   : [{"role": "user", "content": prompt}],
            },
            timeout=25,
        )
        if r.ok:
            return r.json()["content"][0]["text"].strip()
        else:
            return f"⚠️ Claude error {r.status_code}"
    except Exception as e:
        log.error(f"claude_free_ask error: {e}")
        return "⚠️ Claude tidak merespons."


def gemini_analyze_coin(symbol: str, confluence: dict, tf_4h: dict, tf_1h: dict,
                         tf_15m: dict, oi_data: dict, price: float,
                         prepump: dict = None, predump: dict = None,
                         scalp: dict = None, swing: dict = None) -> str:
    """Analisa coin via Gemini — dipanggil hanya dari /analyze (manual)."""
    if not GEMINI_API_KEY:
        return "⚠️ GEMINI_API_KEY belum diset di .env"

    s4  = tf_4h.get("structure", {})
    s1  = tf_1h.get("structure", {})
    rej = tf_15m.get("rejection", {})
    fvg = tf_15m.get("fvg", {})
    ob4 = tf_4h.get("order_blocks", {})
    ob1 = tf_1h.get("order_blocks", {})
    va4 = tf_4h.get("volume_anomaly", {})
    signals_summary = "\n".join(confluence.get("reasons", [])[:8])

    candles_4h = tf_4h.get("candles", [])
    candles_1h = tf_1h.get("candles", [])
    ob_ctx     = _build_ob_mitigation_context(ob4, ob1, price, candles_4h, candles_1h)

    # Liquidity context
    liq_1h   = tf_1h.get("liquidity", {})
    sweep_1h = tf_1h.get("sweep", {})
    sweep_15m = tf_15m.get("sweep", {})
    tl_sup   = tf_4h.get("trendline_sup", {})
    tl_res   = tf_4h.get("trendline_res", {})

    liq_ctx = ""
    if liq_1h.get("nearest_eqh"):
        eqh = liq_1h["nearest_eqh"]
        liq_ctx += f"- EQH: {eqh['distance_pct']:.1f}% di atas ({eqh['count']} touches)\n"
    if liq_1h.get("nearest_eql"):
        eql = liq_1h["nearest_eql"]
        liq_ctx += f"- EQL: {eql['distance_pct']:.1f}% di bawah ({eql['count']} touches)\n"
    if sweep_1h.get("swept"):
        liq_ctx += f"- 1H Sweep: {sweep_1h['sweep_type']} (recovery {sweep_1h['recovery_strength']:.0f}%)\n"
    if sweep_15m.get("swept"):
        liq_ctx += f"- 15M Sweep: {sweep_15m['sweep_type']} (recovery {sweep_15m['recovery_strength']:.0f}%)\n"
    if tl_sup.get("valid"):
        liq_ctx += f"- Trendline Support: {tl_sup['distance_pct']:+.1f}% ({tl_sup['touches']} touches)\n"
    if tl_res.get("valid"):
        liq_ctx += f"- Trendline Resist : {tl_res['distance_pct']:+.1f}% ({tl_res['touches']} touches)\n"

    prepump_ctx = ""
    if prepump and prepump.get("total_score", 0) >= 35:
        prepump_ctx = (f"\nPre-Pump: {prepump['total_score']}/100 ({prepump['label']})"
                       f" — F:{prepump['funding_score']} M:{prepump['momentum_score']} OI:{prepump['oi_pa_score']}")
    predump_ctx = ""
    if predump and predump.get("total_score", 0) >= 35:
        predump_ctx = (f"\nPre-Dump: {predump['total_score']}/100 ({predump['label']})"
                       f" — F:{predump['funding_score']} M:{predump['momentum_score']} OI:{predump['oi_pa_score']}")
    scalp_ctx = ""
    if scalp and scalp.get("score", 0) >= 50:
        scalp_ctx = f"\nScalp: {scalp['label']} (score {scalp['score']}/100, {scalp['direction']})"
    swing_ctx = ""
    if swing and swing.get("score", 0) >= 50:
        swing_ctx = f"\nSwing: {swing['label']} (score {swing['score']}/100, hold {swing.get('hold_estimate','')})"

    # v13: inject symbol memory context
    sym_memory_ctx = ""
    if SYMBOL_MEMORY_MODULE:
        try:
            sym_memory_ctx = build_symbol_context_block(symbol)
        except Exception:
            sym_memory_ctx = ""

    prompt = f"""Kamu adalah analis crypto profesional ahli Smart Money Concepts (SMC) dan liquidity theory.
{sym_memory_ctx + chr(10) if sym_memory_ctx else ""}
Analisa {symbol} — data real-time multi-timeframe:

MARKET DATA:
- Harga: ${price}
- Signal: {confluence['direction']} | Score: {confluence['score']}/100 | Level: {confluence['level']}
- 4H: {s4.get('trend','?')} | CHoCH:{s4.get('choch',False)} BoS:{s4.get('bos',False)}
- 1H: {s1.get('trend','?')} | CHoCH:{s1.get('choch',False)} BoS:{s1.get('bos',False)}
- 15M Rejection: {rej.get('type','NONE')} | FVG: {fvg.get('fvg_type','NONE')}
- OI Change: {oi_data.get('oi_change_pct','N/A')}% | L/S: {oi_data.get('ls_ratio','N/A')} ({oi_data.get('ls_bias','N/A')})
- Funding: {oi_data.get('funding_rate','N/A')}% | Vol Anomaly 4H: {va4.get('multiplier',1):.1f}x
- Money Flow 4H: {tf_4h.get('money_flow',{}).get('bias','N/A')} {tf_4h.get('money_flow',{}).get('strength','')} (CVD {tf_4h.get('money_flow',{}).get('cvd_pct',0):+.1f}% | MFI {tf_4h.get('money_flow',{}).get('mfi',50):.0f} | VWAP {tf_4h.get('money_flow',{}).get('vwap_bias','?')})
- Money Flow 1H: {tf_1h.get('money_flow',{}).get('bias','N/A')} {tf_1h.get('money_flow',{}).get('strength','')} (CVD {tf_1h.get('money_flow',{}).get('cvd_pct',0):+.1f}% | MFI {tf_1h.get('money_flow',{}).get('mfi',50):.0f})
- Money Flow 15M: {tf_15m.get('money_flow',{}).get('bias','N/A')} (CVD {tf_15m.get('money_flow',{}).get('cvd_pct',0):+.1f}% | MFI {tf_15m.get('money_flow',{}).get('mfi',50):.0f})

LIQUIDITY & STRUCTURE:
{liq_ctx if liq_ctx else "Tidak ada data liquidity zone signifikan"}

ORDER BLOCK VALIDATION:
{ob_ctx}

SETUP DETECTORS:{prepump_ctx}{predump_ctx}{scalp_ctx}{swing_ctx}

CONFLUENCE SIGNALS:
{signals_summary}

Berikan analisa SINGKAT Bahasa Indonesia (max 6 kalimat):
1. Kondisi & bias market {symbol} sekarang
2. Liquidity zone paling relevan sebagai target/trap
3. OB yang masih FRESH dan layak sebagai entry area
4. Setup scalp/swing valid? Kenapa iya/tidak?
5. Risk utama yang perlu diwaspadai

Format: paragraf mengalir, tanpa bullet. Profesional, mudah dipahami trader Indonesia."""

    return _gemini_request({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 900}
    }) or "⚠️ Gemini tidak merespons saat ini, coba lagi sebentar."


def gemini_free_ask(question: str) -> str:
    """Jawab pertanyaan crypto via Gemini."""
    if not GEMINI_API_KEY:
        return "⚠️ GEMINI_API_KEY belum diset di .env"

    prompt = f"""Kamu adalah asisten crypto trading ahli SMC, technical analysis, dan fundamental crypto.
Jawab dalam Bahasa Indonesia, singkat dan padat (max 5 kalimat):

{question}"""

    return _gemini_request({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 400}
    }) or "⚠️ Gemini tidak merespons saat ini."


def gemini_analyze_chart_image(image_base64: str, mime_type: str = "image/jpeg") -> str:
    """Analisa chart image via Gemini Vision."""
    if not GEMINI_API_KEY:
        return "⚠️ GEMINI_API_KEY belum di-set."

    prompt = """Kamu adalah analis crypto profesional SMC (Smart Money Concepts). Tugasmu bukan untuk mengapresiasi atau memuji — tugasmu adalah membedah chart ini secara KRITIS dan memberikan verdict yang actionable.

Analisa WAJIB mencakup:

1. 📊 **MARKET STRUCTURE** — Bullish/Bearish/Ranging? Ada CHoCH atau BoS? Di mana last BoS-nya?
2. 🧱 **ORDER BLOCKS & FVG** — Identifikasi OB yang FRESH (belum mitigated). Ada FVG yang belum diisi? Di range harga berapa?
3. 💧 **LIQUIDITY** — Di mana liquidity pool terkumpul? Equal highs/lows? Sweep sudah terjadi atau belum?
4. ⚠️ **RISIKO & INVALIDASI** — Apa yang bisa bikin setup ini GAGAL? Level mana yang jadi invalidasi?
5. 🎯 **TRADE PLAN** (jika ada setup valid):
   - Entry zone: [range harga]
   - TP1: [harga] | TP2: [harga]
   - SL: [harga]
   - R:R ratio
6. 🏁 **VERDICT** — Pilih SATU: **TRADE** / **SKIP** / **WAIT**
   - TRADE: setup sudah valid, entry sekarang
   - WAIT: setup forming tapi belum konfirmasi
   - SKIP: tidak ada setup valid, terlalu berisiko

PENTING:
- Jika setup LEMAH atau TIDAK JELAS → bilang SKIP dengan alasan spesifik, JANGAN kasih trade plan
- Jika ada kesalahan umum yang sering trader retail buat di chart ini → sebutkan
- Jangan basa-basi. Jawab to the point dalam Bahasa Indonesia."""

    return _gemini_request({
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": image_base64}}
            ]
        }],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 800}
    })


    # ─────────────────────────────────────────────
    # BINANCE API
# ─────────────────────────────────────────────

def get_binance_klines(symbol: str, interval: str, limit: int = 100) -> list | None:
    """
    Fetch klines — priority: Futures (fapi) → Spot (api) fallback.
    fapi lebih reliable dari server luar (Railway/VPS).
    """
    def _parse(raw):
        return [{
            "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]),  "close": float(c[4]),
            "volume": float(c[5]), "time": c[0]
        } for c in raw]

    # 1. Coba Futures endpoint dulu
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        if r.status_code == 200:
            return _parse(r.json())
    except Exception as e:
        log.debug(f"Futures klines error {symbol} {interval}: {e}")

    # 2. Fallback ke Spot
    try:
        r = requests.get(
            f"{BINANCE_BASE}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        if r.status_code == 200:
            return _parse(r.json())
    except Exception as e:
        log.warning(f"Spot klines error {symbol} {interval}: {e}")

    return None


def get_binance_ticker(symbol: str) -> dict | None:
    """Fetch 24hr ticker — Futures → Spot fallback."""
    # 1. Futures
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr",
            params={"symbol": symbol}, timeout=8
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass

    # 2. Spot fallback
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/24hr", params={"symbol": symbol}, timeout=8)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.warning(f"Binance ticker error {symbol}: {e}")
        return None

# ─────────────────────────────────────────────
# OPEN INTEREST & FUNDING
# ─────────────────────────────────────────────

def get_open_interest(symbol: str) -> dict:
    result = {
        "oi": None, "oi_change_pct": None,
        "ls_ratio": None, "ls_bias": "UNKNOWN",
        "funding_rate": None,
    }

    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/openInterest",
                         params={"symbol": symbol}, timeout=8)
        if r.status_code == 200:
            result["oi"] = float(r.json().get("openInterest", 0))
    except Exception:
        pass

    try:
        r = requests.get(f"{BINANCE_FUTURES}/futures/data/globalLongShortAccountRatio",
                         params={"symbol": symbol, "period": "1h", "limit": 2}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                ls = float(data[0].get("longShortRatio", 1.0))
                result["ls_ratio"] = ls
                result["ls_bias"] = ("LONG_HEAVY" if ls > 1.3 else
                                     "SHORT_HEAVY" if ls < 0.7 else "BALANCED")
    except Exception:
        pass

    try:
        r = requests.get(f"{BINANCE_FUTURES}/futures/data/openInterestHist",
                         params={"symbol": symbol, "period": "1h", "limit": 5}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) >= 2:
                oldest = float(data[0].get("sumOpenInterest", 0))
                newest = float(data[-1].get("sumOpenInterest", 0))
                if oldest > 0:
                    result["oi_change_pct"] = round(((newest - oldest) / oldest) * 100, 2)
    except Exception:
        pass

    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/fundingRate",
                         params={"symbol": symbol, "limit": 1}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                result["funding_rate"] = float(data[0].get("fundingRate", 0)) * 100  # konversi ke %
    except Exception:
        pass

    return result


def get_funding_rate_batch(symbols: list) -> dict:
    """Fetch funding rate untuk banyak symbol sekaligus."""
    rates = {}
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/premiumIndex", timeout=10)
        if r.status_code == 200:
            for item in r.json():
                sym = item.get("symbol", "")
                if sym in symbols:
                    rates[sym] = float(item.get("lastFundingRate", 0)) * 100
    except Exception as e:
        log.warning(f"Funding rate batch error: {e}")
    return rates

# ─────────────────────────────────────────────
# SMC ANALYSIS ENGINE
# ─────────────────────────────────────────────

def detect_market_structure(candles: list) -> dict:
    if not candles or len(candles) < 10:
        return {"trend": "UNKNOWN", "choch": False, "bos": False}

    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]

    def find_swing_highs(h, lb=5):
        return [(i, h[i]) for i in range(lb, len(h)-lb)
                if h[i] == max(h[i-lb:i+lb+1])]

    def find_swing_lows(l, lb=5):
        return [(i, l[i]) for i in range(lb, len(l)-lb)
                if l[i] == min(l[i-lb:i+lb+1])]

    sh = find_swing_highs(highs)
    sl = find_swing_lows(lows)
    trend = "NEUTRAL"
    choch = bos = False

    if len(sh) >= 2 and len(sl) >= 2:
        last_hh = sh[-1][1] > sh[-2][1]
        last_hl = sl[-1][1] > sl[-2][1]
        last_lh = sh[-1][1] < sh[-2][1]
        last_ll = sl[-1][1] < sl[-2][1]

        if last_hh and last_hl:   trend = "BULLISH"
        elif last_lh and last_ll: trend = "BEARISH"
        else:                     trend = "RANGING"

        if trend == "BULLISH" and closes[-1] > sh[-1][1]: bos = True
        if trend == "BEARISH" and closes[-1] < sl[-1][1]: bos = True
        if trend == "BULLISH" and len(sl) >= 2 and closes[-1] < sl[-1][1]: choch = True
        if trend == "BEARISH" and len(sh) >= 2 and closes[-1] > sh[-1][1]: choch = True

    return {
        "trend": trend, "choch": choch, "bos": bos,
        "last_high": highs[-1], "last_low": lows[-1],
        "swing_highs": sh[-3:] if sh else [],
        "swing_lows": sl[-3:] if sl else [],
    }


def detect_fvg(candles: list) -> dict:
    result = {"bullish_fvg": None, "bearish_fvg": None, "fvg_type": "NONE"}
    if not candles or len(candles) < 3:
        return result

    for i in range(len(candles)-1, 1, -1):
        c_prev2 = candles[i-2]
        c_curr  = candles[i]
        current_price = candles[-1]["close"]

        gap_bull = c_curr["low"] - c_prev2["high"]
        if gap_bull > 0:
            gap_pct = (gap_bull / c_prev2["high"]) * 100
            if gap_pct >= FVG_MIN_GAP_PCT:
                fvg_mid = (c_curr["low"] + c_prev2["high"]) / 2
                result["bullish_fvg"] = {
                    "top": c_curr["low"], "bottom": c_prev2["high"],
                    "mid": round(fvg_mid, 6), "gap_pct": round(gap_pct, 2),
                    "distance_pct": round(((fvg_mid - current_price) / current_price) * 100, 2)
                }
                if result["fvg_type"] == "NONE": result["fvg_type"] = "BULLISH"
                break

        gap_bear = c_prev2["low"] - c_curr["high"]
        if gap_bear > 0:
            gap_pct = (gap_bear / c_curr["high"]) * 100
            if gap_pct >= FVG_MIN_GAP_PCT:
                fvg_mid = (c_prev2["low"] + c_curr["high"]) / 2
                result["bearish_fvg"] = {
                    "top": c_prev2["low"], "bottom": c_curr["high"],
                    "mid": round(fvg_mid, 6), "gap_pct": round(gap_pct, 2),
                    "distance_pct": round(((current_price - fvg_mid) / current_price) * 100, 2)
                }
                if result["fvg_type"] == "NONE": result["fvg_type"] = "BEARISH"
                break

    return result


def detect_order_blocks(candles: list) -> dict:
    result = {"bullish_ob": None, "bearish_ob": None}
    if not candles or len(candles) < OB_LOOKBACK + 2:
        return result

    current_price = candles[-1]["close"]

    for i in range(len(candles)-2, OB_LOOKBACK, -1):
        c = candles[i]
        next_c = candles[i+1:i+4]
        if len(next_c) < 2:
            continue

        move_up   = all(nc["close"] > c["high"] for nc in next_c[:2])
        move_down = all(nc["close"] < c["low"]  for nc in next_c[:2])

        if move_up and c["close"] < c["open"] and current_price > c["high"]:
            if result["bullish_ob"] is None:
                result["bullish_ob"] = {
                    "top": round(c["open"], 6), "bottom": round(c["close"], 6),
                    "mid": round((c["open"]+c["close"])/2, 6),
                    "distance_pct": round(((current_price-c["close"])/current_price)*100, 2)
                }

        if move_down and c["close"] > c["open"] and current_price < c["low"]:
            if result["bearish_ob"] is None:
                result["bearish_ob"] = {
                    "top": round(c["close"], 6), "bottom": round(c["open"], 6),
                    "mid": round((c["open"]+c["close"])/2, 6),
                    "distance_pct": round(((c["open"]-current_price)/current_price)*100, 2)
                }

        if result["bullish_ob"] and result["bearish_ob"]:
            break

    return result


def detect_candle_rejection(candles: list) -> dict:
    result = {"type": "NONE", "strength": 0, "detail": ""}
    if not candles or len(candles) < 3:
        return result

    for c in reversed(candles[-3:]):
        body   = abs(c["close"] - c["open"])
        range_ = c["high"] - c["low"]
        if range_ == 0:
            continue

        upper_wick  = c["high"] - max(c["close"], c["open"])
        lower_wick  = min(c["close"], c["open"]) - c["low"]
        upper_ratio = upper_wick / range_
        lower_ratio = lower_wick / range_

        if upper_ratio >= REJECTION_WICK_RATIO and body/range_ < 0.3:
            return {
                "type": "BEARISH_REJECTION",
                "strength": int(upper_ratio*100),
                "detail": f"Upper wick {upper_ratio*100:.0f}% of range — sellers rejected price"
            }
        if lower_ratio >= REJECTION_WICK_RATIO and body/range_ < 0.3:
            return {
                "type": "BULLISH_REJECTION",
                "strength": int(lower_ratio*100),
                "detail": f"Lower wick {lower_ratio*100:.0f}% of range — buyers absorbed selling"
            }

    return result


def detect_volume_anomaly(candles: list) -> dict:
    result = {"is_anomaly": False, "zscore": 0.0, "multiplier": 1.0}
    if not candles or len(candles) < 20:
        return result

    vols = [c["volume"] for c in candles[:-1]]
    mean = np.mean(vols)
    std  = np.std(vols)
    if std == 0:
        return result

    current_vol = candles[-1]["volume"]
    zscore = (current_vol - mean) / std
    mult   = current_vol / mean if mean > 0 else 1.0

    return {
        "is_anomaly": zscore >= ZSCORE_ANOMALY_THRESH,
        "zscore": round(zscore, 2),
        "multiplier": round(mult, 2)
    }


def calculate_rsi(candles: list, period: int = 14) -> float:
    """Hitung RSI dari candles."""
    if len(candles) < period + 1:
        return 50.0

    closes = [c["close"] for c in candles]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [abs(min(d, 0)) for d in deltas[-period:]]

    avg_gain = np.mean(gains) if gains else 0
    avg_loss = np.mean(losses) if losses else 0

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_atr(candles: list, period: int = 14) -> float:
    """Hitung ATR (Average True Range)."""
    if len(candles) < period + 1:
        return 0.0

    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))

    return np.mean(trs[-period:]) if trs else 0.0



# ─────────────────────────────────────────────
# v13: MONEY FLOW DETECTOR
# ─────────────────────────────────────────────

def detect_money_flow(candles: list, period: int = 20) -> dict:
    """
    Deteksi arah money flow dari OHLCV candles — zero extra API call.

    Menggabungkan 3 metrik:

    1. CVD (Cumulative Volume Delta)
       Estimasi buy vs sell volume per candle:
       - close dekat high → mayoritas buyer (buy pressure)
       - close dekat low  → mayoritas seller (sell pressure)
       buy_vol  = volume * ((close - low) / (high - low))
       sell_vol = volume * ((high - close) / (high - low))
       CVD = cumsum(buy_vol - sell_vol) — arah = slope CVD terakhir

    2. MFI (Money Flow Index) — RSI berbasis volume
       Typical price = (high + low + close) / 3
       Positive flow = typical_price > prev_typical_price
       MFI = 100 - (100 / (1 + pos_money_flow / neg_money_flow))
       Overbought > 80, Oversold < 20

    3. VWAP Pressure (price vs VWAP rolling)
       Kalau close konsisten di atas VWAP → buy pressure
       Kalau close konsisten di bawah VWAP → sell pressure

    Return:
      bias:         INFLOW | OUTFLOW | NEUTRAL
      strength:     STRONG | MODERATE | WEAK
      cvd_slope:    positif = buy dominant, negatif = sell dominant
      cvd_pct:      CVD net % dari total volume (window terakhir)
      mfi:          0-100
      mfi_signal:   OVERBOUGHT | OVERSOLD | NEUTRAL
      vwap_bias:    ABOVE | BELOW | AT
      vwap_pct:     % distance price dari VWAP
      ltf_score:    0-100 (combined score, >50 = inflow, <50 = outflow)
      reasons:      list string penjelasan
    """
    result = {
        "bias":       "NEUTRAL",
        "strength":   "WEAK",
        "cvd_slope":  0.0,
        "cvd_pct":    0.0,
        "mfi":        50.0,
        "mfi_signal": "NEUTRAL",
        "vwap_bias":  "AT",
        "vwap_pct":   0.0,
        "ltf_score":  50,
        "reasons":    [],
    }

    if not candles or len(candles) < max(period + 1, 15):
        return result

    window = candles[-period:]
    reasons = []
    score_pts = 0   # >0 → inflow, <0 → outflow

    # ── 1. CVD (Cumulative Volume Delta) ──────────
    buy_vols  = []
    sell_vols = []
    for c in window:
        hl = c["high"] - c["low"]
        if hl == 0:
            buy_vols.append(c["volume"] * 0.5)
            sell_vols.append(c["volume"] * 0.5)
        else:
            buy_ratio  = (c["close"] - c["low"]) / hl
            sell_ratio = (c["high"] - c["close"]) / hl
            buy_vols.append(c["volume"] * buy_ratio)
            sell_vols.append(c["volume"] * sell_ratio)

    total_vol  = sum(c["volume"] for c in window)
    net_buy    = sum(buy_vols)
    net_sell   = sum(sell_vols)
    cvd_net    = net_buy - net_sell
    cvd_pct    = (cvd_net / total_vol * 100) if total_vol > 0 else 0

    # CVD slope: cek apakah delta positif atau negatif di 5 candle terakhir
    last5_buy  = sum(buy_vols[-5:])
    last5_sell = sum(sell_vols[-5:])
    cvd_slope  = last5_buy - last5_sell

    result["cvd_slope"] = round(cvd_slope, 2)
    result["cvd_pct"]   = round(cvd_pct, 2)

    if cvd_pct > 3:
        score_pts += 2
        reasons.append(f"💚 CVD: buy vol dominan {cvd_pct:+.1f}% net")
    elif cvd_pct > 1:
        score_pts += 1
        reasons.append(f"🟢 CVD: slight inflow {cvd_pct:+.1f}%")
    elif cvd_pct < -3:
        score_pts -= 2
        reasons.append(f"🔴 CVD: sell vol dominan {cvd_pct:+.1f}% net")
    elif cvd_pct < -1:
        score_pts -= 1
        reasons.append(f"🟡 CVD: slight outflow {cvd_pct:+.1f}%")

    # CVD slope (recent momentum)
    if cvd_slope > 0 and last5_buy / max(last5_sell, 0.001) > 1.3:
        score_pts += 1
        reasons.append(f"📈 CVD momentum: buyers aktif di 5 candle terakhir")
    elif cvd_slope < 0 and last5_sell / max(last5_buy, 0.001) > 1.3:
        score_pts -= 1
        reasons.append(f"📉 CVD momentum: sellers aktif di 5 candle terakhir")

    # ── 2. MFI (Money Flow Index) ─────────────────
    if len(candles) >= period + 1:
        typical_prices = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles[-(period+1):]]
        raw_flows      = [typical_prices[i] * candles[-(period+1)+i]["volume"]
                          for i in range(len(typical_prices))]

        pos_flow = sum(raw_flows[i] for i in range(1, len(raw_flows))
                       if typical_prices[i] > typical_prices[i-1])
        neg_flow = sum(raw_flows[i] for i in range(1, len(raw_flows))
                       if typical_prices[i] < typical_prices[i-1])

        if neg_flow == 0:
            mfi = 100.0
        elif pos_flow == 0:
            mfi = 0.0
        else:
            mfi = 100 - (100 / (1 + pos_flow / neg_flow))

        result["mfi"] = round(mfi, 1)

        if mfi >= 80:
            result["mfi_signal"] = "OVERBOUGHT"
            score_pts -= 1  # overbought = potential outflow / reversal
            reasons.append(f"⚠️ MFI: {mfi:.0f} — overbought, watch for outflow")
        elif mfi <= 20:
            result["mfi_signal"] = "OVERSOLD"
            score_pts += 1  # oversold = potential inflow / reversal
            reasons.append(f"💡 MFI: {mfi:.0f} — oversold, watch for inflow")
        elif mfi >= 60:
            result["mfi_signal"] = "BULLISH"
            score_pts += 1
            reasons.append(f"🟢 MFI: {mfi:.0f} — positive money flow dominan")
        elif mfi <= 40:
            result["mfi_signal"] = "BEARISH"
            score_pts -= 1
            reasons.append(f"🔴 MFI: {mfi:.0f} — negative money flow dominan")
        else:
            result["mfi_signal"] = "NEUTRAL"

    # ── 3. VWAP Pressure ──────────────────────────
    cum_vol     = 0.0
    cum_tpv     = 0.0
    above_count = 0
    below_count = 0

    for c in window:
        tp       = (c["high"] + c["low"] + c["close"]) / 3
        cum_vol  += c["volume"]
        cum_tpv  += tp * c["volume"]
        vwap_now  = cum_tpv / cum_vol if cum_vol > 0 else tp
        if c["close"] > vwap_now:
            above_count += 1
        elif c["close"] < vwap_now:
            below_count += 1

    # Final VWAP using full window
    cum_v = sum(c["volume"] for c in window)
    cum_tp = sum(((c["high"]+c["low"]+c["close"])/3) * c["volume"] for c in window)
    vwap = cum_tp / cum_v if cum_v > 0 else window[-1]["close"]

    current_price = window[-1]["close"]
    vwap_pct = ((current_price - vwap) / vwap * 100) if vwap > 0 else 0
    result["vwap_pct"] = round(vwap_pct, 2)

    above_ratio = above_count / len(window)
    below_ratio = below_count / len(window)

    if above_ratio >= 0.65:
        result["vwap_bias"] = "ABOVE"
        score_pts += 1
        reasons.append(f"🏔️ VWAP: harga di atas VWAP {vwap_pct:+.1f}% ({above_ratio*100:.0f}% candles)")
    elif below_ratio >= 0.65:
        result["vwap_bias"] = "BELOW"
        score_pts -= 1
        reasons.append(f"⬇️ VWAP: harga di bawah VWAP {vwap_pct:+.1f}% ({below_ratio*100:.0f}% candles)")
    else:
        result["vwap_bias"] = "AT"

    # ── Final scoring ────────────────────────────
    # score_pts: max ~+5 (strong inflow) to min ~-5 (strong outflow)
    ltf_score = min(100, max(0, 50 + (score_pts * 10)))
    result["ltf_score"] = ltf_score
    result["reasons"]   = reasons

    if ltf_score >= 70:
        result["bias"]     = "INFLOW"
        result["strength"] = "STRONG"
    elif ltf_score >= 57:
        result["bias"]     = "INFLOW"
        result["strength"] = "MODERATE"
    elif ltf_score >= 50:
        result["bias"]     = "INFLOW"
        result["strength"] = "WEAK"
    elif ltf_score <= 30:
        result["bias"]     = "OUTFLOW"
        result["strength"] = "STRONG"
    elif ltf_score <= 43:
        result["bias"]     = "OUTFLOW"
        result["strength"] = "MODERATE"
    elif ltf_score <= 50:
        result["bias"]     = "OUTFLOW"
        result["strength"] = "WEAK"
    else:
        result["bias"]     = "NEUTRAL"
        result["strength"] = "WEAK"

    return result

# ─────────────────────────────────────────────
# v8: LIQUIDITY ZONE DETECTION
# ─────────────────────────────────────────────

def detect_equal_highs_lows(candles: list) -> dict:
    """
    Deteksi Equal Highs (EQH) dan Equal Lows (EQL).
    EQH = 2+ swing highs dalam toleransi harga → area liquidity di atas
    EQL = 2+ swing lows dalam toleransi harga → area liquidity di bawah
    Smart money akan sweep area ini sebelum reversal.
    """
    result = {"eqh": [], "eql": [], "nearest_eqh": None, "nearest_eql": None}
    if not candles or len(candles) < 20:
        return result

    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    current_price = candles[-1]["close"]

    # Cari swing highs
    swing_highs = []
    for i in range(3, len(highs) - 3):
        if highs[i] == max(highs[i-3:i+4]):
            swing_highs.append((i, highs[i]))

    # Cari swing lows
    swing_lows = []
    for i in range(3, len(lows) - 3):
        if lows[i] == min(lows[i-3:i+4]):
            swing_lows.append((i, lows[i]))

    # Cari Equal Highs: swing high yang levelnya mirip (dalam toleransi)
    eqh_groups = []
    used = set()
    for i, (idx_i, val_i) in enumerate(swing_highs):
        if i in used:
            continue
        group = [(idx_i, val_i)]
        for j, (idx_j, val_j) in enumerate(swing_highs[i+1:], i+1):
            if j in used:
                continue
            if abs(val_i - val_j) / val_i <= EQUAL_HL_TOLERANCE:
                group.append((idx_j, val_j))
                used.add(j)
        if len(group) >= 2:
            used.add(i)
            avg_level = sum(v for _, v in group) / len(group)
            eqh_groups.append({
                "level": round(avg_level, 8),
                "count": len(group),
                "distance_pct": round(((avg_level - current_price) / current_price) * 100, 2)
            })

    # Cari Equal Lows
    eql_groups = []
    used = set()
    for i, (idx_i, val_i) in enumerate(swing_lows):
        if i in used:
            continue
        group = [(idx_i, val_i)]
        for j, (idx_j, val_j) in enumerate(swing_lows[i+1:], i+1):
            if j in used:
                continue
            if abs(val_i - val_j) / val_i <= EQUAL_HL_TOLERANCE:
                group.append((idx_j, val_j))
                used.add(j)
        if len(group) >= 2:
            used.add(i)
            avg_level = sum(v for _, v in group) / len(group)
            eql_groups.append({
                "level": round(avg_level, 8),
                "count": len(group),
                "distance_pct": round(((current_price - avg_level) / current_price) * 100, 2)
            })

    result["eqh"] = sorted(eqh_groups, key=lambda x: abs(x["distance_pct"]))
    result["eql"] = sorted(eql_groups, key=lambda x: abs(x["distance_pct"]))

    # Nearest EQH (di atas harga) dan EQL (di bawah harga)
    above = [e for e in result["eqh"] if e["distance_pct"] > 0]
    below = [e for e in result["eql"] if e["distance_pct"] > 0]
    result["nearest_eqh"] = above[0] if above else None
    result["nearest_eql"] = below[0] if below else None

    return result


def detect_liquidity_sweep(candles: list, structure: dict) -> dict:
    """
    Deteksi apakah harga baru saja melakukan liquidity sweep:
    - Spike melewati swing high/low (minimal SWEEP_WICK_MIN_PCT)
    - Lalu close kembali di dalam range sebelumnya (reversal)
    Ini adalah salah satu entry trigger terkuat di SMC.
    """
    result = {
        "swept": False,
        "sweep_type": "NONE",   # BULLISH_SWEEP (ambil sell-side liq) atau BEARISH_SWEEP
        "sweep_level": None,
        "recovery_strength": 0,
        "candles_ago": None,
    }

    if not candles or len(candles) < 10:
        return result

    swing_highs = structure.get("swing_highs", [])
    swing_lows  = structure.get("swing_lows", [])

    # Cek 5 candle terakhir untuk sweep
    for i in range(len(candles) - 1, max(len(candles) - 6, 0), -1):
        c = candles[i]
        body_top    = max(c["open"], c["close"])
        body_bottom = min(c["open"], c["close"])
        candles_ago = len(candles) - 1 - i

        # Bullish sweep: spike ke bawah swing low lalu recover → ambil sell-side liquidity
        if swing_lows:
            nearest_low = min(swing_lows, key=lambda x: abs(x[1] - c["low"]))
            sl_level = nearest_low[1]
            swept_below = c["low"] < sl_level
            recovered   = c["close"] > sl_level

            if swept_below and recovered:
                sweep_pct = (sl_level - c["low"]) / sl_level
                if sweep_pct >= SWEEP_WICK_MIN_PCT:
                    recovery = (c["close"] - c["low"]) / (c["high"] - c["low"]) if (c["high"] - c["low"]) > 0 else 0
                    result.update({
                        "swept": True,
                        "sweep_type": "BULLISH_SWEEP",
                        "sweep_level": round(sl_level, 8),
                        "recovery_strength": round(recovery * 100, 1),
                        "candles_ago": candles_ago,
                    })
                    return result

        # Bearish sweep: spike ke atas swing high lalu drop → ambil buy-side liquidity
        if swing_highs:
            nearest_high = min(swing_highs, key=lambda x: abs(x[1] - c["high"]))
            sh_level = nearest_high[1]
            swept_above = c["high"] > sh_level
            recovered   = c["close"] < sh_level

            if swept_above and recovered:
                sweep_pct = (c["high"] - sh_level) / sh_level
                if sweep_pct >= SWEEP_WICK_MIN_PCT:
                    recovery = (c["high"] - c["close"]) / (c["high"] - c["low"]) if (c["high"] - c["low"]) > 0 else 0
                    result.update({
                        "swept": True,
                        "sweep_type": "BEARISH_SWEEP",
                        "sweep_level": round(sh_level, 8),
                        "recovery_strength": round(recovery * 100, 1),
                        "candles_ago": candles_ago,
                    })
                    return result

    return result


def detect_trendline(candles: list, swing_type: str = "lows") -> dict:
    """
    Fit trendline diagonal ke swing highs atau swing lows.
    Minimal 3 swing points. Return slope, intercept, dan apakah valid.
    swing_type: 'lows' → ascending trendline support
                'highs' → descending trendline resistance
    """
    result = {
        "valid": False, "direction": "NONE",
        "slope_pct": 0.0, "current_level": None,
        "distance_pct": None, "touches": 0,
    }

    if not candles or len(candles) < 20:
        return result

    prices = [c["low"] if swing_type == "lows" else c["high"] for c in candles]
    lb = 4

    swing_points = []
    for i in range(lb, len(prices) - lb):
        if swing_type == "lows" and prices[i] == min(prices[i-lb:i+lb+1]):
            swing_points.append((i, prices[i]))
        elif swing_type == "highs" and prices[i] == max(prices[i-lb:i+lb+1]):
            swing_points.append((i, prices[i]))

    if len(swing_points) < TRENDLINE_MIN_POINTS:
        return result

    # Ambil 3 swing point terbaru yang paling fit
    recent = swing_points[-5:]  # max 5 point terakhir
    if len(recent) < 3:
        return result

    # Linear regression sederhana
    xs = np.array([p[0] for p in recent], dtype=float)
    ys = np.array([p[1] for p in recent], dtype=float)
    n  = len(xs)
    slope     = (n * np.sum(xs*ys) - np.sum(xs)*np.sum(ys)) / (n*np.sum(xs**2) - np.sum(xs)**2)
    intercept = (np.sum(ys) - slope*np.sum(xs)) / n

    # Level trendline saat ini (di candle terakhir)
    current_idx   = len(candles) - 1
    trendline_now = slope * current_idx + intercept
    current_price = candles[-1]["close"]
    dist_pct      = ((current_price - trendline_now) / trendline_now) * 100

    # Hitung slope dalam % per candle
    slope_pct = (slope / ys.mean()) * 100 if ys.mean() != 0 else 0

    direction = "ASCENDING" if slope > 0 else "DESCENDING"

    # Hitung touches (candle yang menyentuh dekat trendline)
    touches = 0
    for i, c in enumerate(candles):
        tl_at_i = slope * i + intercept
        price_at_i = c["low"] if swing_type == "lows" else c["high"]
        if abs(price_at_i - tl_at_i) / tl_at_i < 0.005:  # dalam 0.5%
            touches += 1

    result.update({
        "valid": touches >= 3,
        "direction": direction,
        "slope_pct": round(slope_pct, 4),
        "current_level": round(trendline_now, 8),
        "distance_pct": round(dist_pct, 2),
        "touches": touches,
    })
    return result


def calculate_entry_zone(ob: dict, fvg: dict, sweep: dict, price: float, direction: str) -> dict:
    """
    Hitung entry zone sebagai RANGE (bukan single price).
    Combine OB + FVG + sweep level untuk dapat zona entry yang lebih akurat.
    """
    zone_top = zone_bottom = None

    if direction == "LONG":
        # Entry zone = area demand: OB bullish + bullish FVG
        candidates = []
        if ob and ob.get("bullish_ob"):
            candidates.append(ob["bullish_ob"]["top"])
            candidates.append(ob["bullish_ob"]["bottom"])
        if fvg and fvg.get("bullish_fvg"):
            candidates.append(fvg["bullish_fvg"]["top"])
            candidates.append(fvg["bullish_fvg"]["bottom"])
        if sweep.get("swept") and sweep["sweep_type"] == "BULLISH_SWEEP":
            candidates.append(sweep["sweep_level"])

        if candidates:
            zone_bottom = min(candidates)
            zone_top    = max(candidates)

    elif direction == "SHORT":
        candidates = []
        if ob and ob.get("bearish_ob"):
            candidates.append(ob["bearish_ob"]["top"])
            candidates.append(ob["bearish_ob"]["bottom"])
        if fvg and fvg.get("bearish_fvg"):
            candidates.append(fvg["bearish_fvg"]["top"])
            candidates.append(fvg["bearish_fvg"]["bottom"])
        if sweep.get("swept") and sweep["sweep_type"] == "BEARISH_SWEEP":
            candidates.append(sweep["sweep_level"])

        if candidates:
            zone_bottom = min(candidates)
            zone_top    = max(candidates)

    if zone_top and zone_bottom:
        # Pastikan zone masuk akal (tidak terlalu lebar > 5%)
        width_pct = ((zone_top - zone_bottom) / zone_bottom) * 100
        if width_pct > 5:
            # Persempit ke ±2% dari harga
            mid = (zone_top + zone_bottom) / 2
            zone_top    = mid * 1.02
            zone_bottom = mid * 0.98

        return {
            "top": round(zone_top, 8),
            "bottom": round(zone_bottom, 8),
            "mid": round((zone_top + zone_bottom) / 2, 8),
            "width_pct": round(((zone_top - zone_bottom) / zone_bottom) * 100, 2),
        }

    return None


# ─────────────────────────────────────────────
# v8: SCALPING SETUP DETECTOR (15M/1H)
# ─────────────────────────────────────────────

def detect_scalp_setup(symbol: str, tf_15m: dict, tf_1h: dict, tf_4h: dict, oi_data: dict) -> dict:
    """
    Deteksi scalping setup berkualitas tinggi berdasarkan:
    - Liquidity sweep (trigger utama)
    - Rejection candle di zona (entry konfirmasi)
    - OB fresh di 15M/1H
    - FVG nearby
    - HTF bias alignment (4H/1H)
    Score 0-100. Setup valid jika >= SCALP_MIN_SCORE.
    """
    result = {
        "symbol": symbol,
        "score": 0,
        "direction": "NONE",
        "reasons": [],
        "entry_zone": None,
        "scalp_tp": None,
        "scalp_sl": None,
        "sweep": {},
        "label": "NO SETUP",
    }

    struct_4h = tf_4h.get("structure", {})
    struct_1h = tf_1h.get("structure", {})
    struct_15m = tf_15m.get("structure", {})
    t4  = struct_4h.get("trend", "UNKNOWN")
    t1  = struct_1h.get("trend", "UNKNOWN")
    price = tf_15m.get("price", tf_1h.get("price", 0))

    score = 0
    direction = "NONE"

    # ── 1. HTF BIAS (4H) — filter utama ──────────
    if t4 == "BULLISH":
        score += 20
        direction = "LONG"
        result["reasons"].append("✅ 4H Bullish bias — long side preferred")
    elif t4 == "BEARISH":
        score += 20
        direction = "SHORT"
        result["reasons"].append("🔴 4H Bearish bias — short side preferred")
    else:
        score += 5
        result["reasons"].append("⚪ 4H Neutral — caution, skip scalp if unsure")

    # ── 2. 1H ALIGNMENT ──────────────────────────
    if direction == "LONG" and t1 == "BULLISH":
        score += 15
        result["reasons"].append("✅ 1H aligned Bullish — high confluence")
    elif direction == "SHORT" and t1 == "BEARISH":
        score += 15
        result["reasons"].append("🔴 1H aligned Bearish — high confluence")
    elif direction == "LONG" and t1 == "RANGING":
        score += 7
        result["reasons"].append("  1H Ranging — partial alignment")
    elif direction == "SHORT" and t1 == "RANGING":
        score += 7
        result["reasons"].append("  1H Ranging — partial alignment")

    # ── 3. LIQUIDITY SWEEP (trigger terkuat) ─────
    candles_15m = tf_15m.get("candles", [])
    candles_1h  = tf_1h.get("candles", [])

    sweep_15m = detect_liquidity_sweep(candles_15m, struct_15m)
    sweep_1h  = detect_liquidity_sweep(candles_1h, struct_1h)

    best_sweep = None
    if sweep_15m.get("swept"):
        best_sweep = sweep_15m
        sweep_src  = "15M"
    elif sweep_1h.get("swept"):
        best_sweep = sweep_1h
        sweep_src  = "1H"

    if best_sweep:
        st = best_sweep["sweep_type"]
        recovery = best_sweep["recovery_strength"]
        ago = best_sweep["candles_ago"]

        # Sweep harus searah bias
        sweep_bullish = st == "BULLISH_SWEEP"
        sweep_bearish = st == "BEARISH_SWEEP"

        if (direction == "LONG" and sweep_bullish) or (direction == "SHORT" and sweep_bearish):
            pts = 25 if recovery >= 70 else 18 if recovery >= 50 else 10
            score += pts
            result["reasons"].append(
                f"🎯 {sweep_src} Liquidity Sweep ({st}) — "
                f"recovery {recovery:.0f}%, {ago} candle ago"
            )
            result["sweep"] = best_sweep
        elif (direction == "LONG" and sweep_bearish) or (direction == "SHORT" and sweep_bullish):
            # Sweep berlawanan arah → kurangi score
            score -= 10
            result["reasons"].append(f"⚠️ {sweep_src} Sweep berlawanan arah — konflik signal")

    # ── 4. REJECTION CANDLE (entry trigger) ──────
    rej_15m = tf_15m.get("rejection", {})
    rej_1h  = tf_1h.get("rejection", {})

    rej_type_15m = rej_15m.get("type", "NONE")
    rej_type_1h  = rej_1h.get("type", "NONE")

    if direction == "LONG":
        if rej_type_15m == "BULLISH_REJECTION":
            score += 15
            result["reasons"].append(f"🕯️ 15M Bullish rejection candle (str:{rej_15m['strength']}) — entry trigger")
        elif rej_type_1h == "BULLISH_REJECTION":
            score += 10
            result["reasons"].append(f"🕯️ 1H Bullish rejection — entry trigger")
    elif direction == "SHORT":
        if rej_type_15m == "BEARISH_REJECTION":
            score += 15
            result["reasons"].append(f"🕯️ 15M Bearish rejection candle (str:{rej_15m['strength']}) — entry trigger")
        elif rej_type_1h == "BEARISH_REJECTION":
            score += 10
            result["reasons"].append(f"🕯️ 1H Bearish rejection — entry trigger")

    # ── 5. OB FRESH DI DEKAT HARGA ───────────────
    ob_15m = tf_15m.get("order_blocks", {})
    ob_1h  = tf_1h.get("order_blocks", {})
    fvg_15m = tf_15m.get("fvg", {})

    if direction == "LONG":
        ob = ob_15m.get("bullish_ob") or ob_1h.get("bullish_ob")
        if ob and ob.get("distance_pct", 999) < 2:
            score += 12
            result["reasons"].append(f"🧱 Bullish OB nearby ({ob['distance_pct']:.1f}% away) — demand zone")
        if fvg_15m.get("fvg_type") == "BULLISH":
            d = fvg_15m.get("bullish_fvg", {}).get("distance_pct", 999)
            if d < 1.5:
                score += 10
                result["reasons"].append(f"🧲 Bullish FVG 15M ({d:.1f}% away) — price magnet")
    elif direction == "SHORT":
        ob = ob_15m.get("bearish_ob") or ob_1h.get("bearish_ob")
        if ob and ob.get("distance_pct", 999) < 2:
            score += 12
            result["reasons"].append(f"🧱 Bearish OB nearby ({ob['distance_pct']:.1f}% away) — supply zone")
        if fvg_15m.get("fvg_type") == "BEARISH":
            d = fvg_15m.get("bearish_fvg", {}).get("distance_pct", 999)
            if d < 1.5:
                score += 10
                result["reasons"].append(f"🧲 Bearish FVG 15M ({d:.1f}% away) — price magnet")

    # ── 6. RSI TIMING ────────────────────────────
    rsi_15m = tf_15m.get("rsi", 50)
    if direction == "LONG" and 35 <= rsi_15m <= 55:
        score += 8
        result["reasons"].append(f"📊 RSI 15M: {rsi_15m:.0f} — oversold area, bounce potential")
    elif direction == "SHORT" and 55 <= rsi_15m <= 75:
        score += 8
        result["reasons"].append(f"📊 RSI 15M: {rsi_15m:.0f} — overbought area, reject potential")
    elif direction == "LONG" and rsi_15m < 35:
        score += 5
        result["reasons"].append(f"📊 RSI 15M: {rsi_15m:.0f} — oversold (extreme)")
    elif direction == "SHORT" and rsi_15m > 75:
        score += 5
        result["reasons"].append(f"📊 RSI 15M: {rsi_15m:.0f} — overbought (extreme)")

    # ── ENTRY ZONE & TRADE PLAN ───────────────────
    ob_for_zone = ob_15m if direction == "LONG" else ob_15m
    entry_zone  = calculate_entry_zone(ob_for_zone, fvg_15m, result["sweep"], price, direction)
    result["entry_zone"] = entry_zone

    if price > 0:
        if direction == "LONG":
            tp = round(price * (1 + SCALP_TP_PCT), 8)
            sl = round(price * (1 - SCALP_SL_PCT), 8)
        elif direction == "SHORT":
            tp = round(price * (1 - SCALP_TP_PCT), 8)
            sl = round(price * (1 + SCALP_SL_PCT), 8)
        else:
            tp = sl = None

        result["scalp_tp"] = tp
        result["scalp_sl"] = sl
        result["price"]    = price

    # ── LABEL ────────────────────────────────────
    result["score"] = max(0, score)
    if score >= 75:
        result["label"] = "🔥 A+ SCALP"
    elif score >= 60:
        result["label"] = "✅ GOOD SCALP"
    elif score >= SCALP_MIN_SCORE:
        result["label"] = "🟡 FAIR SCALP"
    else:
        result["label"] = "❌ NO SETUP"

    result["direction"] = direction
    return result


# ─────────────────────────────────────────────
# v8: INTRADAY SWING SETUP DETECTOR (4H/1H)
# ─────────────────────────────────────────────

def detect_swing_setup(symbol: str, tf_4h: dict, tf_1h: dict, tf_15m: dict,
                        oi_data: dict, eqh_eql: dict = None) -> dict:
    """
    Deteksi intraday swing setup (hold max 1 hari):
    - HTF 4H bias + 1H trigger
    - Liquidity sweep di 1H sebagai konfirmasi
    - Equal H/L sebagai target liquidity
    - OB + FVG confluence
    - TP 3-6%, SL 2-2.5%, hold < 1 hari
    Score 0-100.
    """
    result = {
        "symbol": symbol,
        "score": 0,
        "direction": "NONE",
        "reasons": [],
        "entry_zone": None,
        "swing_tp": None,
        "swing_sl": None,
        "sweep": {},
        "label": "NO SETUP",
        "hold_estimate": "",
    }

    struct_4h = tf_4h.get("structure", {})
    struct_1h = tf_1h.get("structure", {})
    t4  = struct_4h.get("trend", "UNKNOWN")
    t1  = struct_1h.get("trend", "UNKNOWN")
    price = tf_1h.get("price", 0)

    score = 0
    direction = "NONE"

    # ── 1. 4H STRUCTURE (HTF bias) ───────────────
    if t4 == "BULLISH":
        score += 25
        direction = "LONG"
        result["reasons"].append("✅ 4H Bullish structure (HH+HL) — HTF bias LONG")
    elif t4 == "BEARISH":
        score += 25
        direction = "SHORT"
        result["reasons"].append("🔴 4H Bearish structure (LH+LL) — HTF bias SHORT")

    if struct_4h.get("bos"):
        if t4 == "BULLISH":
            score += 10
            result["reasons"].append("💥 4H BoS UP — momentum konfirmasi")
        elif t4 == "BEARISH":
            score += 10
            result["reasons"].append("💥 4H BoS DOWN — momentum konfirmasi")

    # ── 2. 1H TRIGGER ────────────────────────────
    candles_1h = tf_1h.get("candles", [])

    if direction == "LONG":
        if t1 == "BULLISH":
            score += 15
            result["reasons"].append("✅ 1H Bullish — trend aligned, entry optimal")
        elif t1 == "RANGING":
            score += 8
            result["reasons"].append("  1H Ranging — tunggu breakout konfirmasi")
        elif t1 == "BEARISH":
            score -= 5
            result["reasons"].append("⚠️ 1H Bearish — kontra HTF, hindari entry dulu")
    elif direction == "SHORT":
        if t1 == "BEARISH":
            score += 15
            result["reasons"].append("🔴 1H Bearish — trend aligned, entry optimal")
        elif t1 == "RANGING":
            score += 8
            result["reasons"].append("  1H Ranging — tunggu breakdown konfirmasi")
        elif t1 == "BULLISH":
            score -= 5
            result["reasons"].append("⚠️ 1H Bullish — kontra HTF, hindari entry dulu")

    # ── 3. LIQUIDITY SWEEP 1H (konfirmasi entry) ─
    sweep_1h = detect_liquidity_sweep(candles_1h, struct_1h)
    if sweep_1h.get("swept"):
        st = sweep_1h["sweep_type"]
        recovery = sweep_1h["recovery_strength"]
        ago = sweep_1h["candles_ago"]

        if (direction == "LONG" and st == "BULLISH_SWEEP") or \
           (direction == "SHORT" and st == "BEARISH_SWEEP"):
            pts = 20 if recovery >= 65 else 12
            score += pts
            result["reasons"].append(
                f"🎯 1H Liquidity Sweep ({st}) — "
                f"recovery {recovery:.0f}%, {ago} candle ago — STRONG ENTRY SIGNAL"
            )
            result["sweep"] = sweep_1h
        else:
            result["reasons"].append(f"  1H Sweep arah berlawanan ({st}) — belum sesuai bias")

    # ── 4. EQUAL HIGHS/LOWS sebagai TARGET ───────
    if eqh_eql:
        if direction == "LONG" and eqh_eql.get("nearest_eqh"):
            eqh = eqh_eql["nearest_eqh"]
            if 1 < eqh["distance_pct"] < 8:
                score += 10
                result["reasons"].append(
                    f"🎯 EQH Target: {eqh['distance_pct']:.1f}% di atas "
                    f"({eqh['count']} equal highs = liquidity pool)"
                )
        elif direction == "SHORT" and eqh_eql.get("nearest_eql"):
            eql = eqh_eql["nearest_eql"]
            if 1 < eql["distance_pct"] < 8:
                score += 10
                result["reasons"].append(
                    f"🎯 EQL Target: {eql['distance_pct']:.1f}% di bawah "
                    f"({eql['count']} equal lows = liquidity pool)"
                )

    # ── 5. OB + FVG 1H ───────────────────────────
    ob_1h  = tf_1h.get("order_blocks", {})
    ob_4h  = tf_4h.get("order_blocks", {})
    fvg_1h = tf_1h.get("fvg", {})

    if direction == "LONG":
        ob = ob_1h.get("bullish_ob") or ob_4h.get("bullish_ob")
        if ob and ob.get("distance_pct", 999) < 4:
            score += 10
            result["reasons"].append(f"🧱 Bullish OB 1H/4H ({ob['distance_pct']:.1f}% away)")
        if fvg_1h.get("fvg_type") == "BULLISH":
            d = fvg_1h.get("bullish_fvg", {}).get("distance_pct", 999)
            if d < 3:
                score += 8
                result["reasons"].append(f"🧲 Bullish FVG 1H ({d:.1f}% away)")
    elif direction == "SHORT":
        ob = ob_1h.get("bearish_ob") or ob_4h.get("bearish_ob")
        if ob and ob.get("distance_pct", 999) < 4:
            score += 10
            result["reasons"].append(f"🧱 Bearish OB 1H/4H ({ob['distance_pct']:.1f}% away)")
        if fvg_1h.get("fvg_type") == "BEARISH":
            d = fvg_1h.get("bearish_fvg", {}).get("distance_pct", 999)
            if d < 3:
                score += 8
                result["reasons"].append(f"🧲 Bearish FVG 1H ({d:.1f}% away)")

    # ── 6. OI CONFLUENCE ─────────────────────────
    ls_bias = oi_data.get("ls_bias", "UNKNOWN")
    oi_chg  = oi_data.get("oi_change_pct")
    if direction == "LONG" and ls_bias == "SHORT_HEAVY":
        score += 8
        result["reasons"].append(f"⚖️ L/S Short-heavy → squeeze fuel untuk swing long")
    elif direction == "SHORT" and ls_bias == "LONG_HEAVY":
        score += 8
        result["reasons"].append(f"⚖️ L/S Long-heavy → liquidation fuel untuk swing short")
    if oi_chg and abs(oi_chg) > 5:
        score += 5
        result["reasons"].append(f"📊 OI Change: {oi_chg:+.1f}% — conviction tinggi")

    # ── ENTRY ZONE & TRADE PLAN ───────────────────
    fvg_for_zone = fvg_1h
    entry_zone   = calculate_entry_zone(ob_1h, fvg_for_zone, result["sweep"], price, direction)
    result["entry_zone"] = entry_zone

    if price > 0:
        if direction == "LONG":
            tp = round(price * (1 + SWING_TP_PCT), 8)
            sl = round(price * (1 - SWING_SL_PCT), 8)
        elif direction == "SHORT":
            tp = round(price * (1 - SWING_TP_PCT), 8)
            sl = round(price * (1 + SWING_SL_PCT), 8)
        else:
            tp = sl = None

        rr = 0
        if tp and sl and price != sl:
            rr = round(abs(tp - price) / abs(sl - price), 2)

        result["swing_tp"] = tp
        result["swing_sl"] = sl
        result["rr"]       = rr
        result["price"]    = price

    # Estimasi hold time berdasarkan ATR dan target
    atr_1h = tf_1h.get("atr", 0)
    if atr_1h > 0 and price > 0:
        target_move = price * SWING_TP_PCT
        est_candles = int(target_move / atr_1h)
        est_hours   = est_candles  # 1h candles
        if est_hours <= 8:
            result["hold_estimate"] = f"~{est_hours}h"
        elif est_hours <= 24:
            result["hold_estimate"] = f"~{est_hours}h (intraday)"
        else:
            result["hold_estimate"] = ">24h (over target)"

    # ── LABEL ────────────────────────────────────
    result["score"] = max(0, score)
    if score >= 80:
        result["label"] = "🔥 A+ SWING"
    elif score >= 65:
        result["label"] = "✅ GOOD SWING"
    elif score >= 50:
        result["label"] = "🟡 FAIR SWING"
    else:
        result["label"] = "❌ NO SETUP"

    result["direction"] = direction
    return result


def scan_scalp_candidates(symbols: list = None) -> list:
    """Scan symbols untuk cari scalping setups terbaik."""
    if symbols is None:
        symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
            "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
            "NEARUSDT", "APTUSDT", "INJUSDT", "SUIUSDT", "ARBUSDT",
            "OPUSDT", "TIAUSDT", "RENDERUSDT", "FETUSDT", "PENDLEUSDT",
            "ENAUSDT", "AAVEUSDT", "ONDOUSDT", "JUPUSDT", "HYPEUSDT",
        ]

    candidates = []
    log.info(f"⚡ Scalp scan: {len(symbols)} symbols...")

    for sym in symbols:
        try:
            tf_15m = analyze_timeframe(sym, "15m")
            tf_1h  = analyze_timeframe(sym, "1h")
            tf_4h  = analyze_timeframe(sym, "4h")
            oi     = get_open_interest(sym)

            if tf_15m.get("error") or tf_1h.get("error") or tf_4h.get("error"):
                continue

            # v13: blacklist check
            if SYMBOL_MEMORY_MODULE:
                is_bl, bl_r = is_blacklisted(sym)
                if is_bl:
                    log.info(f"⛔ {sym} blacklisted, skip scalp scan: {bl_r}")
                    continue

            scalp = detect_scalp_setup(sym, tf_15m, tf_1h, tf_4h, oi)
            if scalp["score"] >= SCALP_MIN_SCORE:
                # v13: tambah trade plan dengan entry_mode ke scalp
                price_s = tf_15m.get("price", 0)
                atr_1h  = tf_1h.get("atr", 0)
                direc_s = scalp.get("direction", "NONE")
                if direc_s in ("LONG", "SHORT"):
                    scalp["trade"] = calculate_trade_plan(
                        price_s,
                        "PUMP" if direc_s == "LONG" else "DUMP",
                        atr_1h, tf_4h, tf_1h, tf_15m, oi
                    )
                candidates.append(scalp)

            time.sleep(0.3)
        except Exception as e:
            log.warning(f"Scalp scan error {sym}: {e}")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:8]


def analyze_timeframe(symbol: str, interval: str) -> dict:
    """
    v13: ANTI-LOOKAHEAD enforcement (Freqtrade-inspired).
    closed_candles = candles[:-1] → hanya candle yang sudah CLOSE.
    current_price  = candles[-1]  → harga live, BUKAN untuk kalkulasi struktur.
    Semua detector menerima closed_candles → tidak ada repainting.
    """
    candles = get_binance_klines(symbol, interval, limit=101)  # +1 untuk current candle
    if not candles or len(candles) < 10:
        return {"error": True, "interval": interval}

    # ── ANTI-LOOKAHEAD: pisah current (belum close) dari closed candles ──
    current_candle = candles[-1]    # candle aktif — mungkin belum close
    closed_candles = candles[:-1]   # hanya candle yang sudah CLOSE sepenuhnya

    if len(closed_candles) < 10:
        return {"error": True, "interval": interval}

    # Semua kalkulasi pakai closed_candles — zero lookahead
    structure = detect_market_structure(closed_candles)
    return {
        "interval":       interval,
        "error":          False,
        "price":          current_candle["close"],   # live price
        "candles":        closed_candles,            # closed candles untuk reference
        "current_candle": current_candle,            # candle aktif (display saja)
        "structure":      structure,
        "fvg":            detect_fvg(closed_candles),
        "order_blocks":   detect_order_blocks(closed_candles),
        "rejection":      detect_candle_rejection(closed_candles),
        "volume_anomaly": detect_volume_anomaly(closed_candles),
        "rsi":            calculate_rsi(closed_candles),
        "atr":            calculate_atr(closed_candles),
        "liquidity":      detect_equal_highs_lows(closed_candles),
        "sweep":          detect_liquidity_sweep(closed_candles, structure),
        "trendline_sup":  detect_trendline(closed_candles, "lows"),
        "trendline_res":  detect_trendline(closed_candles, "highs"),
        "money_flow":     detect_money_flow(closed_candles),     # v13: CVD+MFI+VWAP
        "_anti_lookahead": True,
        "_closed_count":   len(closed_candles),
    }

# ─────────────────────────────────────────────
# PRE-PUMP DETECTOR
# ─────────────────────────────────────────────

def detect_prepump(symbol: str, tf_1h: dict, tf_4h: dict, oi_data: dict) -> dict:
    """
    Deteksi pre-pump setup berdasarkan 3 indikator utama:
    1. Funding Squeeze  (max 30 poin)
    2. Momentum Runner  (max 35 poin)
    3. OI + PA + ATR    (max 35 poin)
    Total max = 100 poin
    """
    result = {
        "symbol": symbol,
        "funding_score": 0,
        "momentum_score": 0,
        "oi_pa_score": 0,
        "total_score": 0,
        "label": "WEAK",
        "funding_rate": None,
        "rsi": None,
        "reasons": [],
    }

    # ── 1. FUNDING SQUEEZE ──────────────────────
    funding_rate = oi_data.get("funding_rate")
    result["funding_rate"] = funding_rate

    if funding_rate is not None:
        if funding_rate < FUNDING_EXTREME_THRESH:
            # Funding sangat negatif → extreme short squeeze potential
            result["funding_score"] = 30
            result["reasons"].append(
                f"🔥 FUNDING EXTREME: {funding_rate:.3f}% — short squeeze imminent"
            )
        elif funding_rate < FUNDING_SQUEEZE_THRESH:
            result["funding_score"] = 18
            result["reasons"].append(
                f"⚡ Funding negatif: {funding_rate:.3f}% — short squeeze potential"
            )
        elif funding_rate > 0.05:
            # Funding sangat positif → longs berat, potential dump
            result["funding_score"] = 0
            result["reasons"].append(
                f"⚠️ Funding positif tinggi: {funding_rate:.3f}% — longs heavy, hati-hati"
            )
        else:
            result["funding_score"] = 5
            result["reasons"].append(f"  Funding netral: {funding_rate:.3f}%")

    # ── 2. MOMENTUM RUNNER ──────────────────────
    rsi_1h = tf_1h.get("rsi", 50)
    rsi_4h = tf_4h.get("rsi", 50)
    result["rsi"] = rsi_1h

    struct_1h = tf_1h.get("structure", {})
    struct_4h = tf_4h.get("structure", {})
    vol_1h = tf_1h.get("volume_anomaly", {})
    vol_4h = tf_4h.get("volume_anomaly", {})

    mom_score = 0

    # RSI momentum
    if rsi_1h >= 60 and rsi_4h >= 55:
        mom_score += 12
        result["reasons"].append(f"📈 RSI Momentum: 1H={rsi_1h:.0f} | 4H={rsi_4h:.0f} — bullish momentum")
    elif rsi_1h >= MOMENTUM_RSI_THRESH:
        mom_score += 7
        result["reasons"].append(f"  RSI 1H building: {rsi_1h:.0f}")

    # Trend alignment (4H+1H sama arah)
    t4 = struct_4h.get("trend", "UNKNOWN")
    t1 = struct_1h.get("trend", "UNKNOWN")
    if t4 == "BULLISH" and t1 == "BULLISH":
        mom_score += 13
        result["reasons"].append("✅ Trend aligned: 4H=BULLISH + 1H=BULLISH — strong momentum")
    elif t4 == "BULLISH" and t1 == "RANGING":
        mom_score += 6
        result["reasons"].append("  4H Bullish + 1H Ranging — potential momentum build")

    # BoS momentum
    if struct_1h.get("bos") and t1 == "BULLISH":
        mom_score += 10
        result["reasons"].append("💥 BoS confirmed 1H BULLISH — momentum breakout")

    # Volume surge
    if vol_1h.get("is_anomaly") or (vol_1h.get("multiplier", 1) >= VOLUME_SURGE_MULT):
        mom_score += 10
        mult = vol_1h.get("multiplier", 1)
        result["reasons"].append(f"🐳 Volume surge 1H: {mult:.1f}x normal — smart money in")
    elif vol_4h.get("is_anomaly"):
        mom_score += 5
        mult = vol_4h.get("multiplier", 1)
        result["reasons"].append(f"  Volume spike 4H: {mult:.1f}x normal")

    result["momentum_score"] = min(mom_score, 35)

    # ── 3. OI + PRICE ACTION + ATR ──────────────
    oi_pa_score = 0

    # OI naik = conviction building
    oi_change = oi_data.get("oi_change_pct")
    if oi_change is not None:
        if oi_change >= OI_SURGE_THRESH * 2:
            oi_pa_score += 15
            result["reasons"].append(f"🚀 OI Surge: +{oi_change:.1f}% — strong conviction build")
        elif oi_change >= OI_SURGE_THRESH:
            oi_pa_score += 10
            result["reasons"].append(f"📊 OI Rising: +{oi_change:.1f}% — position accumulation")
        elif oi_change < -5:
            result["reasons"].append(f"  OI falling: {oi_change:.1f}% — deleverage caution")

    # L/S bias: short-heavy → squeeze potential
    ls_bias = oi_data.get("ls_bias", "UNKNOWN")
    ls_ratio = oi_data.get("ls_ratio")
    if ls_bias == "SHORT_HEAVY" and ls_ratio:
        oi_pa_score += 12
        result["reasons"].append(f"🎯 L/S Short-heavy: {ls_ratio:.2f} → short squeeze fuel")
    elif ls_bias == "BALANCED":
        oi_pa_score += 4

    # ATR coiling: price dalam range sempit = energy terakumulasi
    atr_1h = tf_1h.get("atr", 0)
    price_1h = tf_1h.get("price", 0)
    if atr_1h > 0 and price_1h > 0:
        candles_1h = tf_1h.get("candles", [])
        if candles_1h and len(candles_1h) >= 10:
            recent_10 = candles_1h[-10:]
            price_range = max(c["high"] for c in recent_10) - min(c["low"] for c in recent_10)
            range_atr_ratio = price_range / (atr_1h * 10) if atr_1h > 0 else 1

            if range_atr_ratio < ATR_COIL_RATIO:
                oi_pa_score += 8
                result["reasons"].append(
                    f"🌀 ATR Coiling: price range tight ({range_atr_ratio*100:.1f}% ATR) — energy compressed"
                )

    # CHoCH = potential reversal / flip
    if struct_1h.get("choch"):
        oi_pa_score += 5
        result["reasons"].append("⚡ CHoCH 1H — trend flip signal detected")

    result["oi_pa_score"] = min(oi_pa_score, 35)

    # ── TOTAL SCORE & LABEL ──────────────────────
    total = result["funding_score"] + result["momentum_score"] + result["oi_pa_score"]
    result["total_score"] = total

    if total >= 75:
        result["label"] = "🔥 HOT PRE-PUMP"
    elif total >= 55:
        result["label"] = "⚡ WARMING UP"
    elif total >= 35:
        result["label"] = "👀 WATCH LIST"
    else:
        result["label"] = "❄️ WEAK"

    return result


def scan_prepump_candidates(symbols: list = None) -> list:
    """
    Scan sejumlah symbol untuk cari pre-pump candidates.
    Simpan tf data untuk trade plan generation.
    Returns sorted list by total_score DESC.
    """
    if symbols is None:
        symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
            "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
            "NEARUSDT", "APTUSDT", "INJUSDT", "SUIUSDT", "ARBUSDT",
            "OPUSDT", "TIAUSDT", "RENDERUSDT", "FETUSDT", "PENDLEUSDT",
            "ENAUSDT", "AAVEUSDT", "ONDOUSDT", "JUPUSDT", "HYPEUSDT",
        ]

    candidates = []
    log.info(f"🔍 Pre-pump scan: {len(symbols)} symbols...")

    for sym in symbols:
        try:
            tf_1h = analyze_timeframe(sym, "1h")
            tf_4h = analyze_timeframe(sym, "4h")
            tf_15m = analyze_timeframe(sym, "15m")
            oi    = get_open_interest(sym)

            if tf_1h.get("error") or tf_4h.get("error"):
                continue

            # v13: blacklist check
            if SYMBOL_MEMORY_MODULE:
                is_bl, bl_r = is_blacklisted(sym)
                if is_bl:
                    log.info(f"⛔ {sym} blacklisted, skip prepump scan: {bl_r}")
                    continue

            pp = detect_prepump(sym, tf_1h, tf_4h, oi)
            if pp["total_score"] >= 35:
                price = tf_1h.get("price", 0)
                pp["price"] = price
                atr_1h = tf_1h.get("atr", 0)
                pp["trade"] = calculate_trade_plan(price, "PUMP", atr_1h, tf_4h, tf_1h, tf_15m, oi)
                candidates.append(pp)

            time.sleep(0.3)
        except Exception as e:
            log.warning(f"Pre-pump scan error {sym}: {e}")

    candidates.sort(key=lambda x: x["total_score"], reverse=True)
    return candidates[:10]

# ─────────────────────────────────────────────
# PRE-DUMP DETECTOR
# ─────────────────────────────────────────────

def detect_predump(symbol: str, tf_1h: dict, tf_4h: dict, oi_data: dict) -> dict:
    """
    Deteksi pre-dump setup — KEBALIKAN dari pre-pump.
    3 indikator utama:
    1. Funding Squeeze Bearish   (max 30 poin) — funding positif tinggi → long squeeze
    2. Bearish Momentum Runner   (max 35 poin) — RSI overbought + bearish BoS + volume spike
    3. OI + PA + ATR Bearish     (max 35 poin) — OI naik + long-heavy + ATR expand bearish
    Total max = 100 poin
    """
    result = {
        "symbol": symbol,
        "funding_score": 0,
        "momentum_score": 0,
        "oi_pa_score": 0,
        "total_score": 0,
        "label": "WEAK",
        "funding_rate": None,
        "rsi": None,
        "reasons": [],
    }

    # ── 1. FUNDING SQUEEZE BEARISH ──────────────
    funding_rate = oi_data.get("funding_rate")
    result["funding_rate"] = funding_rate

    if funding_rate is not None:
        if funding_rate > FUNDING_DUMP_EXTREME:
            # Funding sangat positif → longs terlalu crowded → long squeeze imminent
            result["funding_score"] = 30
            result["reasons"].append(
                f"🔥 FUNDING EXTREME LONG: +{funding_rate:.3f}% — long squeeze imminent"
            )
        elif funding_rate > FUNDING_DUMP_THRESH:
            result["funding_score"] = 18
            result["reasons"].append(
                f"⚠️ Funding positif tinggi: +{funding_rate:.3f}% — longs crowded, dump fuel"
            )
        elif funding_rate < -0.01:
            # Funding negatif → kontra dump (bisa jadi bounce/pump)
            result["funding_score"] = 0
            result["reasons"].append(
                f"🔵 Funding negatif: {funding_rate:.3f}% — short-heavy, kontra dump signal"
            )
        else:
            result["funding_score"] = 5
            result["reasons"].append(f"  Funding netral: {funding_rate:.3f}%")

    # ── 2. BEARISH MOMENTUM RUNNER ──────────────
    rsi_1h = tf_1h.get("rsi", 50)
    rsi_4h = tf_4h.get("rsi", 50)
    result["rsi"] = rsi_1h

    struct_1h = tf_1h.get("structure", {})
    struct_4h = tf_4h.get("structure", {})
    vol_1h    = tf_1h.get("volume_anomaly", {})
    vol_4h    = tf_4h.get("volume_anomaly", {})

    mom_score = 0

    # RSI overbought — exhaustion zone
    if rsi_1h >= 75 and rsi_4h >= 70:
        mom_score += 12
        result["reasons"].append(
            f"📉 RSI Overbought Extreme: 1H={rsi_1h:.0f} | 4H={rsi_4h:.0f} — exhaustion zone"
        )
    elif rsi_1h >= RSI_OVERBOUGHT_THRESH:
        mom_score += 7
        result["reasons"].append(f"  RSI 1H overbought: {rsi_1h:.0f} — momentum fading risk")

    # Trend alignment bearish (4H+1H sama-sama bearish)
    t4 = struct_4h.get("trend", "UNKNOWN")
    t1 = struct_1h.get("trend", "UNKNOWN")
    if t4 == "BEARISH" and t1 == "BEARISH":
        mom_score += 13
        result["reasons"].append("🔴 Trend aligned: 4H=BEARISH + 1H=BEARISH — strong downtrend")
    elif t4 == "BEARISH" and t1 == "RANGING":
        mom_score += 6
        result["reasons"].append("  4H Bearish + 1H Ranging — potential breakdown forming")
    elif t4 == "BULLISH" and t1 == "BEARISH":
        # 4H masih bullish tapi 1H udah flip bearish → reversal early signal
        mom_score += 9
        result["reasons"].append("⚡ 4H Bullish tapi 1H flip BEARISH — potential trend reversal")

    # Bearish BoS — momentum breakdown
    if struct_1h.get("bos") and t1 == "BEARISH":
        mom_score += 10
        result["reasons"].append("💥 Bearish BoS confirmed 1H — momentum breakdown")

    # CHoCH bearish — flip signal
    if struct_1h.get("choch") and t1 == "BEARISH":
        mom_score += 8
        result["reasons"].append("⚡ CHoCH 1H BEARISH — trend flip ke downside")

    # Volume surge saat harga turun — distribusi / smart money keluar
    if vol_1h.get("is_anomaly") or (vol_1h.get("multiplier", 1) >= VOLUME_SURGE_MULT):
        mult = vol_1h.get("multiplier", 1)
        # Volume spike + bearish candle → distribusi
        candles_1h = tf_1h.get("candles", [])
        if candles_1h:
            last_c = candles_1h[-1]
            if last_c["close"] < last_c["open"]:  # bearish candle
                mom_score += 10
                result["reasons"].append(
                    f"🐻 Volume spike {mult:.1f}x + bearish candle — distribusi/smart money keluar"
                )
            else:
                mom_score += 5
                result["reasons"].append(f"  Volume spike {mult:.1f}x (arah perlu dikonfirmasi)")
    elif vol_4h.get("is_anomaly"):
        mult = vol_4h.get("multiplier", 1)
        result["reasons"].append(f"  Volume spike 4H: {mult:.1f}x — perlu konfirmasi arah")

    result["momentum_score"] = min(mom_score, 35)

    # ── 3. OI + PRICE ACTION + ATR BEARISH ──────
    oi_pa_score = 0

    oi_change = oi_data.get("oi_change_pct")
    if oi_change is not None:
        if oi_change >= OI_SURGE_THRESH and t1 == "BEARISH":
            # OI naik tapi harga bearish → long-side accumulation yang akan di-liquidate
            oi_pa_score += 15
            result["reasons"].append(
                f"🚨 OI Naik +{oi_change:.1f}% + Bearish trend — long liquidation setup"
            )
        elif oi_change >= OI_SURGE_THRESH:
            oi_pa_score += 8
            result["reasons"].append(f"📊 OI Rising +{oi_change:.1f}% — cek arah konfirmasi")
        elif oi_change < -5:
            # OI turun = deleverage / shorts covering → kontra dump
            result["reasons"].append(f"  OI falling {oi_change:.1f}% — deleverage, shorts exiting")

    # L/S long-heavy → longs crowded = dump fuel
    ls_bias  = oi_data.get("ls_bias", "UNKNOWN")
    ls_ratio = oi_data.get("ls_ratio")
    if ls_bias == "LONG_HEAVY" and ls_ratio:
        if ls_ratio >= LS_LONG_HEAVY_THRESH:
            oi_pa_score += 15
            result["reasons"].append(
                f"🎯 L/S Long-heavy EXTREME: {ls_ratio:.2f} → long liquidation cascade risk"
            )
        else:
            oi_pa_score += 8
            result["reasons"].append(f"⚠️ L/S Long-heavy: {ls_ratio:.2f} → long liq risk")
    elif ls_bias == "BALANCED":
        oi_pa_score += 3

    # ATR expanding bearish — harga bergerak kencang ke bawah
    atr_1h    = tf_1h.get("atr", 0)
    price_1h  = tf_1h.get("price", 0)
    candles_1h = tf_1h.get("candles", [])

    if atr_1h > 0 and price_1h > 0 and candles_1h and len(candles_1h) >= 10:
        recent_5  = candles_1h[-5:]
        # Cek apakah candle terakhir bergerak > ATR (expansion bearish)
        bearish_expansion = sum(
            1 for c in recent_5
            if c["close"] < c["open"] and (c["high"] - c["low"]) > atr_1h * 1.2
        )
        if bearish_expansion >= 2:
            oi_pa_score += 8
            result["reasons"].append(
                f"📉 ATR Expansion Bearish: {bearish_expansion} candle > 1.2x ATR — downside momentum kuat"
            )

    # Bearish OB dekat harga → resistance area
    ob4 = tf_4h.get("order_blocks", {})
    ob1 = tf_1h.get("order_blocks", {})
    if ob4.get("bearish_ob"):
        d = ob4["bearish_ob"].get("distance_pct", 999)
        if d < 3:
            oi_pa_score += 5
            result["reasons"].append(f"🧱 Bearish OB 4H hanya {d:.1f}% di atas — resistance kuat")
    if ob1.get("bearish_ob"):
        d = ob1["bearish_ob"].get("distance_pct", 999)
        if d < 2:
            oi_pa_score += 4
            result["reasons"].append(f"🧱 Bearish OB 1H hanya {d:.1f}% di atas — immediate resistance")

    result["oi_pa_score"] = min(oi_pa_score, 35)

    # ── TOTAL SCORE & LABEL ──────────────────────
    total = result["funding_score"] + result["momentum_score"] + result["oi_pa_score"]
    result["total_score"] = total

    if total >= 75:
        result["label"] = "💀 HOT PRE-DUMP"
    elif total >= 55:
        result["label"] = "🔴 COOLING DOWN"
    elif total >= 35:
        result["label"] = "👀 WATCH SHORT"
    else:
        result["label"] = "❄️ WEAK"

    return result


def scan_predump_candidates(symbols: list = None) -> list:
    """
    Scan sejumlah symbol untuk cari pre-dump candidates.
    Simpan tf data untuk trade plan generation.
    Returns sorted list by total_score DESC.
    """
    if symbols is None:
        symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
            "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
            "NEARUSDT", "APTUSDT", "INJUSDT", "SUIUSDT", "ARBUSDT",
            "OPUSDT", "TIAUSDT", "RENDERUSDT", "FETUSDT", "PENDLEUSDT",
            "ENAUSDT", "AAVEUSDT", "ONDOUSDT", "JUPUSDT", "HYPEUSDT",
        ]

    candidates = []
    log.info(f"🔍 Pre-dump scan: {len(symbols)} symbols...")

    for sym in symbols:
        try:
            tf_1h  = analyze_timeframe(sym, "1h")
            tf_4h  = analyze_timeframe(sym, "4h")
            tf_15m = analyze_timeframe(sym, "15m")
            oi     = get_open_interest(sym)

            if tf_1h.get("error") or tf_4h.get("error"):
                continue

            # v13: blacklist check
            if SYMBOL_MEMORY_MODULE:
                is_bl, bl_r = is_blacklisted(sym)
                if is_bl:
                    log.info(f"⛔ {sym} blacklisted, skip predump scan: {bl_r}")
                    continue

            pd_result = detect_predump(sym, tf_1h, tf_4h, oi)
            if pd_result["total_score"] >= 35:
                price = tf_1h.get("price", 0)
                pd_result["price"] = price
                atr_1h = tf_1h.get("atr", 0)
                pd_result["trade"] = calculate_trade_plan(price, "DUMP", atr_1h, tf_4h, tf_1h, tf_15m, oi)
                candidates.append(pd_result)

            time.sleep(0.3)
        except Exception as e:
            log.warning(f"Pre-dump scan error {sym}: {e}")

    candidates.sort(key=lambda x: x["total_score"], reverse=True)
    return candidates[:10]

# ─────────────────────────────────────────────
# CONFLUENCE ENGINE
# ─────────────────────────────────────────────

def calculate_confluence_v4(tf_4h: dict, tf_1h: dict, tf_15m: dict, oi_data: dict) -> dict:
    pump_score = dump_score = 0
    reasons = []

    s4 = tf_4h.get("structure", {})
    t4 = s4.get("trend", "UNKNOWN")
    if t4 == "BULLISH":
        pump_score += 25; reasons.append("✅ 4H: Bullish structure (HH+HL)")
    elif t4 == "BEARISH":
        dump_score += 25; reasons.append("🔴 4H: Bearish structure (LH+LL)")

    if s4.get("bos"):
        if t4 == "BULLISH": pump_score += 10; reasons.append("✅ 4H: BoS UP confirmed")
        elif t4 == "BEARISH": dump_score += 10; reasons.append("🔴 4H: BoS DOWN confirmed")

    if s4.get("choch"):
        reasons.append("⚡ 4H: CHoCH — trend flip in progress")
        if t4 == "BULLISH": dump_score += 8
        else: pump_score += 8

    s1 = tf_1h.get("structure", {})
    t1 = s1.get("trend", "UNKNOWN")
    if t1 == "BULLISH": pump_score += 18; reasons.append("✅ 1H: Bullish structure")
    elif t1 == "BEARISH": dump_score += 18; reasons.append("🔴 1H: Bearish structure")

    if s1.get("bos"):
        if t1 == "BULLISH": pump_score += 7; reasons.append("✅ 1H: BoS UP — momentum bullish")
        elif t1 == "BEARISH": dump_score += 7; reasons.append("🔴 1H: BoS DOWN — momentum bearish")

    rej = tf_15m.get("rejection", {})
    fvg = tf_15m.get("fvg", {})

    if rej.get("type") == "BULLISH_REJECTION":
        pump_score += 12; reasons.append(f"✅ 15M: Bullish pin bar ({rej['detail']})")
    elif rej.get("type") == "BEARISH_REJECTION":
        dump_score += 12; reasons.append(f"🔴 15M: Bearish pin bar ({rej['detail']})")

    if fvg.get("fvg_type") == "BULLISH":
        d = fvg.get("bullish_fvg", {}).get("distance_pct", 999)
        if 0 < d < 3: pump_score += 8; reasons.append(f"✅ 15M: Bullish FVG +{d:.1f}% away — price magnet UP")
    elif fvg.get("fvg_type") == "BEARISH":
        d = fvg.get("bearish_fvg", {}).get("distance_pct", 999)
        if 0 < d < 3: dump_score += 8; reasons.append(f"🔴 15M: Bearish FVG -{d:.1f}% away — price magnet DOWN")

    ob4 = tf_4h.get("order_blocks", {})
    ob1 = tf_1h.get("order_blocks", {})

    if ob4.get("bullish_ob"):
        d = ob4["bullish_ob"].get("distance_pct", 999)
        if d < 5: pump_score += 8; reasons.append(f"✅ 4H: Price near Bullish OB ({d:.1f}% away)")
    if ob4.get("bearish_ob"):
        d = ob4["bearish_ob"].get("distance_pct", 999)
        if d < 5: dump_score += 8; reasons.append(f"🔴 4H: Price near Bearish OB ({d:.1f}% away)")
    if ob1.get("bullish_ob"):
        d = ob1["bullish_ob"].get("distance_pct", 999)
        if d < 3: pump_score += 5; reasons.append(f"✅ 1H: Price near Bullish OB ({d:.1f}% away)")
    if ob1.get("bearish_ob"):
        d = ob1["bearish_ob"].get("distance_pct", 999)
        if d < 3: dump_score += 5; reasons.append(f"🔴 1H: Price near Bearish OB ({d:.1f}% away)")

    oi_chg = oi_data.get("oi_change_pct")
    ls_bias = oi_data.get("ls_bias", "UNKNOWN")
    ls_ratio = oi_data.get("ls_ratio")

    if oi_chg is not None:
        if oi_chg > 5:
            reasons.append(f"📈 OI rising +{oi_chg:.1f}% — strong conviction")
            if pump_score > dump_score: pump_score += 8
            else: dump_score += 8
        elif oi_chg < -5:
            reasons.append(f"📉 OI falling {oi_chg:.1f}% — deleverage")

    if ls_bias == "SHORT_HEAVY" and ls_ratio:
        pump_score += 7; reasons.append(f"🎯 L/S: {ls_ratio:.2f} (short-heavy) → squeeze potential → PUMP")
    elif ls_bias == "LONG_HEAVY" and ls_ratio:
        dump_score += 7; reasons.append(f"⚠️ L/S: {ls_ratio:.2f} (long-heavy) → long liq risk → DUMP")

    va4 = tf_4h.get("volume_anomaly", {})
    va1 = tf_1h.get("volume_anomaly", {})

    if va4.get("is_anomaly"):
        mult = va4.get("multiplier", 1)
        reasons.append(f"🐳 4H Vol ANOMALY: {mult:.1f}x normal (Z={va4['zscore']}) — smart money?")
        if pump_score > dump_score: pump_score += 10
        else: dump_score += 10
    if va1.get("is_anomaly"):
        mult = va1.get("multiplier", 1)
        reasons.append(f"🐳 1H Vol spike: {mult:.1f}x normal — unusual activity")
        if pump_score > dump_score: pump_score += 5
        else: dump_score += 5

    # v13: Money Flow contribution to confluence
    mf_4h = tf_4h.get("money_flow", {})
    mf_1h = tf_1h.get("money_flow", {})
    mf_15m = tf_15m.get("money_flow", {})

    # Count TFs with strong inflow/outflow signal
    inflow_count  = sum(1 for mf in [mf_4h, mf_1h, mf_15m] if mf.get("bias") == "INFLOW")
    outflow_count = sum(1 for mf in [mf_4h, mf_1h, mf_15m] if mf.get("bias") == "OUTFLOW")
    strong_count  = sum(1 for mf in [mf_4h, mf_1h, mf_15m] if mf.get("strength") == "STRONG")

    if inflow_count >= 2:
        pts = 12 if strong_count >= 1 else 7
        pump_score += pts
        mfi_avg = round(sum(mf.get("mfi", 50) for mf in [mf_4h, mf_1h, mf_15m]) / 3, 0)
        reasons.append(f"💚 Money Flow: INFLOW di {inflow_count}/3 TF (MFI avg {mfi_avg:.0f}) — buyer pressure")
    elif outflow_count >= 2:
        pts = 12 if strong_count >= 1 else 7
        dump_score += pts
        mfi_avg = round(sum(mf.get("mfi", 50) for mf in [mf_4h, mf_1h, mf_15m]) / 3, 0)
        reasons.append(f"🔴 Money Flow: OUTFLOW di {outflow_count}/3 TF (MFI avg {mfi_avg:.0f}) — seller pressure")
    elif inflow_count == 1 and outflow_count == 0:
        pump_score += 3
        reasons.append(f"🟢 Money Flow: mild inflow di 1 TF")
    elif outflow_count == 1 and inflow_count == 0:
        dump_score += 3
        reasons.append(f"🟡 Money Flow: mild outflow di 1 TF")

    # CVD divergence check: price up tapi CVD negatif = bearish divergence
    mf_1h_cvd = mf_1h.get("cvd_pct", 0)
    mf_4h_cvd = mf_4h.get("cvd_pct", 0)
    if t1 == "BULLISH" and mf_1h_cvd < -2:
        dump_score += 5
        reasons.append(f"⚠️ Money Flow divergence: 1H bullish tapi CVD negatif ({mf_1h_cvd:.1f}%) — weak buyers")
    elif t1 == "BEARISH" and mf_1h_cvd > 2:
        pump_score += 5
        reasons.append(f"⚠️ Money Flow divergence: 1H bearish tapi CVD positif ({mf_1h_cvd:.1f}%) — weak sellers")

    total = pump_score + dump_score
    if total == 0:
        direction = "NEUTRAL"; score = 0
    elif pump_score >= dump_score:
        direction = "PUMP"; score = min(100, int((pump_score / max(total, 1)) * 100))
    else:
        direction = "DUMP"; score = min(100, int((dump_score / max(total, 1)) * 100))

    if abs(pump_score - dump_score) < 10:
        direction = "NEUTRAL"; score = 50

    level = "EXCELLENT" if score >= 70 else "GOOD" if score >= 55 else "FAIR" if score >= 40 else "POOR"

    return {
        "direction": direction, "score": score, "level": level,
        "pump_score": pump_score, "dump_score": dump_score, "reasons": reasons
    }

# ─────────────────────────────────────────────
# MARKET CONTEXT (BTC)
# ─────────────────────────────────────────────

def get_btc_context() -> dict:
    # ── Coba Binance Futures klines dulu ──
    tf_4h = analyze_timeframe("BTCUSDT", "4h")
    tf_1h = analyze_timeframe("BTCUSDT", "1h")

    # ── Fallback ke exchange_resolver (OKX/Bybit) kalau Binance diblok ──
    if (tf_4h.get("error") or tf_4h.get("price", 0) == 0) and EXCHANGE_RESOLVER:
        log.info("BTC klines Binance gagal — fallback ke exchange_resolver")
        resolved = resolve_symbol_full("BTC")
        if resolved:
            exc   = resolved["exchange"]
            sym   = resolved["symbol"]
            tf_4h = analyze_timeframe_exc(sym, "4h", exc)
            tf_1h = analyze_timeframe_exc(sym, "1h", exc)
            log.info(f"BTC context via {exc}: price={tf_4h.get('price', 0)}")

    trend = tf_4h.get("structure", {}).get("trend", "UNKNOWN")
    price = tf_4h.get("price", 0)

    # ── Last resort: ticker langsung dari Futures ──
    if price == 0:
        try:
            r = requests.get(
                f"{BINANCE_FUTURES}/fapi/v1/ticker/price",
                params={"symbol": "BTCUSDT"}, timeout=8
            )
            if r.status_code == 200:
                price = float(r.json().get("price", 0))
                log.info(f"BTC price last-resort (futures ticker): ${price:,.2f}")
        except Exception as e:
            log.warning(f"BTC ticker fallback error: {e}")

    env = "BULLISH" if trend == "BULLISH" else "BEARISH" if trend == "BEARISH" else \
          "TRANSITIONING" if tf_4h.get("structure", {}).get("choch") else "NEUTRAL"

    return {
        "price": price, "trend": trend, "environment": env,
        "choch_4h": tf_4h.get("structure", {}).get("choch", False),
        "bos_4h":   tf_4h.get("structure", {}).get("bos", False),
        "rejection_1h": tf_1h.get("rejection", {}).get("type", "NONE"),
        "rsi_4h":   tf_4h.get("rsi", 50),
        "rsi_1h":   tf_1h.get("rsi", 50),
    }

# ─────────────────────────────────────────────
# COINGECKO SCREENING
# ─────────────────────────────────────────────

prev_volumes  = {}
is_first_scan = True

def get_top_coins() -> list:
    all_coins = []
    for page in [1, 2, 3]:
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": 100, "page": page,
                    "sparkline": False, "price_change_percentage": "24h"
                },
                timeout=15
            )
            if r.status_code == 200:
                all_coins.extend(r.json())
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"CoinGecko page {page} error: {e}")

    return [c for c in all_coins
            if (c.get("total_volume") or 0) >= MIN_VOLUME
            and (c.get("market_cap") or 0) >= MIN_MARKET_CAP]


def calculate_quality_score(coin: dict, vol_increase_pct: float) -> float:
    score = 0.0
    vi = min(vol_increase_pct, 150)
    if vi >= 20: score += (vi / 150) * 4.0

    pc = coin.get("price_change_percentage_24h") or 0
    if 2 <= pc <= 8: score += 3.0
    elif 1 <= pc < 2 or 8 < pc <= 15: score += 1.5
    elif -2 <= pc < 1: score += 0.5

    vol  = coin.get("total_volume") or 1
    mcap = coin.get("market_cap") or 1
    vq   = (vol / mcap) * 100
    if 1 <= vq <= 5: score += 2.0
    elif 5 < vq <= 10: score += 1.0

    if vol >= 100_000_000: score += 1.0
    elif vol >= 50_000_000: score += 0.7
    else: score += 0.3

    return round(min(score, 10.0), 2)


def screen_coins(manual: bool = False) -> list:
    global prev_volumes, is_first_scan
    coins    = get_top_coins()
    scored   = []
    new_vols = {}

    for coin in coins:
        cid = coin.get("id", "")
        vol = coin.get("total_volume") or 0
        if not cid or vol == 0: continue

        new_vols[cid] = vol
        prev_vol = prev_volumes.get(cid, 0)
        vol_increase = ((vol - prev_vol) / prev_vol) * 100 if prev_vol > 0 else 0

        # Manual scan bypass vol filter — tampilkan top coins apapun kondisinya
        if not manual and not is_first_scan and vol_increase < 3: continue
        if vol_increase > MAX_VOLUME_INCREASE: continue

        quality = calculate_quality_score(coin, vol_increase)
        scored.append({
            "id": cid, "symbol": coin.get("symbol", "").upper(),
            "name": coin.get("name", ""),
            "price": coin.get("current_price") or 0,
            "change_24h": coin.get("price_change_percentage_24h") or 0,
            "volume": vol, "volume_increase_pct": round(vol_increase, 2),
            "quality_score": quality, "market_cap": coin.get("market_cap") or 0,
        })

    prev_volumes  = new_vols
    is_first_scan = False

    scored.sort(key=lambda x: x["quality_score"], reverse=True)
    return scored[:TOP_COINS_COUNT]

# ─────────────────────────────────────────────
# TRADE PLAN
# ─────────────────────────────────────────────

def calculate_tp1_tp2(entry: float, sl: float, direction: str,
                      tf_4h: dict = None, tf_1h: dict = None,
                      liq_1h: dict = None) -> dict:
    """
    Hitung TP1 dan TP2 berbasis struktur.
    - Risk (R) = abs(entry - sl)
    - TP1: minimal 2R, diprioritaskan ke level struktural terdekat yang >= 2R
    - TP2: minimal 3.5R, ke target struktural selanjutnya atau EQH/EQL
    - Strategy: partial close 50% di TP1, sisanya jalan ke TP2
    """
    tf_4h  = tf_4h  or {}
    tf_1h  = tf_1h  or {}
    liq_1h = liq_1h or {}

    struct4 = tf_4h.get("structure", {})
    struct1 = tf_1h.get("structure", {})
    risk = abs(entry - sl) if (sl and sl != entry) else entry * 0.02

    result = {
        "tp1": None, "tp2": None,
        "tp1_r": 0.0, "tp2_r": 0.0,
        "tp1_basis": "", "tp2_basis": "",
    }

    if direction in ("LONG", "PUMP"):
        min_tp1 = round(entry + risk * 2.0, 8)
        min_tp2 = round(entry + risk * 3.5, 8)

        levels = []
        for _, lvl in struct1.get("swing_highs", []):
            if lvl > entry * 1.005: levels.append(("1H SwingH", round(lvl * 0.998, 8)))
        for _, lvl in struct4.get("swing_highs", []):
            if lvl > entry * 1.005: levels.append(("4H SwingH", round(lvl * 0.997, 8)))
        if liq_1h.get("nearest_eqh") and liq_1h["nearest_eqh"]["distance_pct"] > 0.5:
            eqh_p = entry * (1 + liq_1h["nearest_eqh"]["distance_pct"] / 100)
            levels.append(("EQH", round(eqh_p * 0.997, 8)))

        levels.sort(key=lambda x: x[1])

        for basis, lvl in levels:
            if lvl >= min_tp1 and result["tp1"] is None:
                result["tp1"], result["tp1_basis"] = lvl, basis
            elif lvl >= min_tp2 and result["tp2"] is None and lvl > (result["tp1"] or 0) * 1.005:
                result["tp2"], result["tp2_basis"] = lvl, basis

        if result["tp1"] is None:
            result["tp1"], result["tp1_basis"] = min_tp1, "2R floor"
        if result["tp2"] is None:
            result["tp2"], result["tp2_basis"] = min_tp2, "3.5R floor"

    elif direction in ("SHORT", "DUMP"):
        min_tp1 = round(entry - risk * 2.0, 8)
        min_tp2 = round(entry - risk * 3.5, 8)

        levels = []
        for _, lvl in struct1.get("swing_lows", []):
            if lvl < entry * 0.995: levels.append(("1H SwingL", round(lvl * 1.002, 8)))
        for _, lvl in struct4.get("swing_lows", []):
            if lvl < entry * 0.995: levels.append(("4H SwingL", round(lvl * 1.003, 8)))
        if liq_1h.get("nearest_eql") and liq_1h["nearest_eql"]["distance_pct"] > 0.5:
            eql_p = entry * (1 - liq_1h["nearest_eql"]["distance_pct"] / 100)
            levels.append(("EQL", round(eql_p * 1.003, 8)))

        # Sort descending → cari TP1 paling dekat (harga tertinggi yg masih <= min_tp1)
        levels.sort(key=lambda x: x[1], reverse=True)

        for basis, lvl in levels:
            if result["tp1"] is None:
                if lvl <= min_tp1:
                    result["tp1"], result["tp1_basis"] = lvl, basis
            else:
                # TP2 harus <= min_tp2 DAN lebih rendah dari TP1
                if lvl <= min_tp2 and lvl < result["tp1"] * 0.995:
                    result["tp2"], result["tp2_basis"] = lvl, basis
                    break

        if result["tp1"] is None:
            result["tp1"], result["tp1_basis"] = min_tp1, "2R floor"
        # TP2 HARUS selalu lebih rendah dari TP1 — guard wajib
        if result["tp2"] is None or result["tp2"] >= result["tp1"]:
            result["tp2"], result["tp2_basis"] = min_tp2, "3.5R floor"

    if risk > 0:
        if result["tp1"]: result["tp1_r"] = round(abs(result["tp1"] - entry) / risk, 2)
        if result["tp2"]: result["tp2_r"] = round(abs(result["tp2"] - entry) / risk, 2)

    return result


def _fmt_zone(bottom: float, top: float) -> str:
    """Format zona entry sebagai range harga."""
    def _f(v):
        if v is None: return "N/A"
        if v >= 1000:    return f"${v:,.2f}"
        elif v >= 1:     return f"${v:.4f}"
        else:            return f"${v:.6f}"
    return f"{_f(bottom)} – {_f(top)}"


def _determine_entry_mode(price: float, direction: str, entry_candidates: list,
                           tf_1h: dict, tf_15m: dict, oi_data: dict) -> dict:
    """
    Tentukan MOMENTUM_NOW vs RETEST_WAIT.
    MOMENTUM_NOW: vol spike 1H>=2x + OI change>=3% + BoS 1H&15M, atau score>=4
    RETEST_WAIT: ada OB/FVG valid di range retest, momentum moderate
    """
    vol_1h  = tf_1h.get("volume_anomaly", {})
    vol_15m = tf_15m.get("volume_anomaly", {}) if tf_15m else {}
    struct_1h  = tf_1h.get("structure", {})
    struct_15m = tf_15m.get("structure", {}) if tf_15m else {}

    oi_change     = oi_data.get("oi_change_pct", 0) or 0
    vol_spike_1h  = vol_1h.get("is_anomaly", False) and vol_1h.get("multiplier", 1) >= 2.0
    vol_spike_15m = vol_15m.get("is_anomaly", False) and vol_15m.get("multiplier", 1) >= 2.0
    bos_1h        = struct_1h.get("bos", False)
    bos_15m       = struct_15m.get("bos", False)
    choch_1h      = struct_1h.get("choch", False)

    momentum_signals = 0
    momentum_reasons = []

    if vol_spike_1h:
        momentum_signals += 2
        momentum_reasons.append(f"🔊 Vol 1H spike {vol_1h.get('multiplier',0):.1f}x")
    if vol_spike_15m:
        momentum_signals += 1
        momentum_reasons.append(f"🔊 Vol 15M spike {vol_15m.get('multiplier',0):.1f}x")
    if abs(oi_change) >= 3:
        momentum_signals += 2
        dir_word = "naik" if oi_change > 0 else "turun"
        momentum_reasons.append(f"📈 OI {dir_word} {abs(oi_change):.1f}%")
    if bos_1h and bos_15m:
        momentum_signals += 2
        momentum_reasons.append("🏗️ BoS 1H+15M")
    elif choch_1h:
        momentum_signals += 1
        momentum_reasons.append("🔄 CHoCH 1H")

    no_retest_zone   = len(entry_candidates) == 0
    strong_momentum  = momentum_signals >= 4
    moderate_no_zone = momentum_signals >= 3 and no_retest_zone

    if strong_momentum or moderate_no_zone:
        return {"mode": "MOMENTUM_NOW", "momentum_signals": momentum_signals,
                "momentum_reasons": momentum_reasons, "confirmation_hints": []}

    hints = (["Candle 15M close di atas zona (bullish engulfing/pin bar)",
              "Volume >= 1.5x rata-rata saat konfirmasi", "RSI 15M masih < 65"]
             if direction == "PUMP" else
             ["Candle 15M close di bawah zona (bearish engulfing/pin bar)",
              "Volume >= 1.5x rata-rata saat konfirmasi", "RSI 15M masih > 35"])
    return {"mode": "RETEST_WAIT", "momentum_signals": momentum_signals,
            "momentum_reasons": momentum_reasons, "confirmation_hints": hints}


def calculate_trade_plan(price: float, direction: str, atr: float = 0,
                         tf_4h: dict = None, tf_1h: dict = None, tf_15m: dict = None,
                         oi_data: dict = None) -> dict:
    """
    v13: Trade plan SMC-aware dengan EXPLICIT ENTRY MODE.
    entry_mode: MOMENTUM_NOW | RETEST_WAIT
    Tambah fields: entry_mode, confirmation_zone, momentum_context, entry_instruction
    """
    tf_4h   = tf_4h   or {}
    tf_1h   = tf_1h   or {}
    tf_15m  = tf_15m  or {}
    oi_data = oi_data or {}

    ob4    = tf_4h.get("order_blocks", {})
    ob1    = tf_1h.get("order_blocks", {})
    ob15   = tf_15m.get("order_blocks", {})
    fvg15  = tf_15m.get("fvg", {})
    fvg1   = tf_1h.get("fvg", {})
    liq_1h = tf_1h.get("liquidity", {})
    struct4 = tf_4h.get("structure", {})
    struct1 = tf_1h.get("structure", {})

    atr_buf    = atr * 0.5 if atr > 0 else price * 0.005
    entry      = price
    sl         = None
    entry_type = "MARKET"

    if direction == "PUMP":
        entry_candidates = []

        if ob4.get("bullish_ob") and ob4["bullish_ob"].get("distance_pct", 999) < 4:
            ob = ob4["bullish_ob"]
            entry_candidates.append(("4H_OB", round(ob.get("bottom", price)*1.002,8), ob.get("bottom",price), 4, ob.get("bottom",price), ob.get("top",price)))
        if ob1.get("bullish_ob") and ob1["bullish_ob"].get("distance_pct", 999) < 5:
            ob = ob1["bullish_ob"]
            entry_candidates.append(("1H_OB", round(ob.get("bottom", price)*1.002,8), ob.get("bottom",price), 3, ob.get("bottom",price), ob.get("top",price)))
        if fvg15.get("fvg_type") == "BULLISH" and fvg15.get("bullish_fvg"):
            fvg = fvg15["bullish_fvg"]
            if -3 < fvg.get("distance_pct", 999) < 0:
                entry_candidates.append(("15M_FVG", round(fvg.get("mid",price),8), fvg.get("bottom",price), 2, fvg.get("bottom",price), fvg.get("top",price)))
        if fvg1.get("fvg_type") == "BULLISH" and fvg1.get("bullish_fvg"):
            fvg = fvg1["bullish_fvg"]
            if -4 < fvg.get("distance_pct", 999) < 0:
                entry_candidates.append(("1H_FVG", round(fvg.get("mid",price),8), fvg.get("bottom",price), 2, fvg.get("bottom",price), fvg.get("top",price)))
        sl_pts = struct1.get("swing_lows", [])
        if sl_pts:
            last_sl = sl_pts[-1][1]
            dist = ((price - last_sl) / price) * 100
            if 0.5 < dist < 5:
                entry_candidates.append(("1H_SwingLow", round(last_sl*1.003,8), last_sl, 1, last_sl, round(last_sl*1.01,8)))

        # v13: entry mode detection
        mode_info = _determine_entry_mode(price, "PUMP", entry_candidates, tf_1h, tf_15m, oi_data)

        if entry_candidates:
            entry_candidates.sort(key=lambda x: (-x[3], abs(x[1] - price)))
            best        = entry_candidates[0]
            entry_type  = best[0]
            entry       = best[1]
            sl_floor    = best[2]
            zone_bottom = best[4]
            zone_top    = best[5]
            sl          = round(sl_floor - atr_buf, 8)
            sl          = max(sl, round(entry * 0.94, 8))
        else:
            entry_type  = "MARKET"
            zone_bottom = round(price * 0.99, 8)
            zone_top    = price
            sl = round(price - atr * 2.0, 8) if atr > 0 else round(price * 0.96, 8)
            sl = max(sl, round(price * 0.94, 8))

        tps = calculate_tp1_tp2(entry, sl, "LONG", tf_4h, tf_1h, liq_1h)
        rr  = tps["tp1_r"]
        entry_mode = mode_info["mode"]

        if entry_mode == "MOMENTUM_NOW":
            mom_str = " | ".join(mode_info["momentum_reasons"][:2])
            entry_instruction = f"🚀 MOMENTUM ENTRY — Trend bullish LTF aktif\n   {mom_str}"
            confirmation_zone = None
        else:
            entry_instruction = (f"⏳ TUNGGU RETEST ke zona {_fmt_zone(zone_bottom, zone_top)}\n"
                                 f"   Konfirmasi: {mode_info['confirmation_hints'][0]}")
            confirmation_zone = {"bottom": zone_bottom, "top": zone_top, "source": entry_type}

        return {
            "direction": "LONG", "entry": entry, "sl": sl,
            "tp1": tps["tp1"], "tp2": tps["tp2"],
            "tp1_r": tps["tp1_r"], "tp2_r": tps["tp2_r"],
            "tp1_basis": tps["tp1_basis"], "tp2_basis": tps["tp2_basis"],
            "rr": rr, "entry_type": entry_type,
            "is_limit": entry_type != "MARKET", "atr_based": atr > 0,
            "tp": tps["tp1"], "sl_basis": f"below {entry_type} -ATR buf",
            "entry_mode": entry_mode,
            "confirmation_zone": confirmation_zone,
            "momentum_context": mode_info.get("momentum_reasons", []),
            "entry_instruction": entry_instruction,
        }

    elif direction == "DUMP":
        entry_candidates = []

        if ob4.get("bearish_ob") and ob4["bearish_ob"].get("distance_pct", 999) < 4:
            ob = ob4["bearish_ob"]
            entry_candidates.append(("4H_OB", round(ob.get("top",price)*0.998,8), ob.get("top",price), 4, ob.get("bottom",price), ob.get("top",price)))
        if ob1.get("bearish_ob") and ob1["bearish_ob"].get("distance_pct", 999) < 5:
            ob = ob1["bearish_ob"]
            entry_candidates.append(("1H_OB", round(ob.get("top",price)*0.998,8), ob.get("top",price), 3, ob.get("bottom",price), ob.get("top",price)))
        if fvg15.get("fvg_type") == "BEARISH" and fvg15.get("bearish_fvg"):
            fvg = fvg15["bearish_fvg"]
            if 0 < fvg.get("distance_pct", 999) < 3:
                entry_candidates.append(("15M_FVG", round(fvg.get("mid",price),8), fvg.get("top",price), 2, fvg.get("bottom",price), fvg.get("top",price)))
        if fvg1.get("fvg_type") == "BEARISH" and fvg1.get("bearish_fvg"):
            fvg = fvg1["bearish_fvg"]
            if 0 < fvg.get("distance_pct", 999) < 4:
                entry_candidates.append(("1H_FVG", round(fvg.get("mid",price),8), fvg.get("top",price), 2, fvg.get("bottom",price), fvg.get("top",price)))
        sh_pts = struct1.get("swing_highs", [])
        if sh_pts:
            last_sh = sh_pts[-1][1]
            dist = ((last_sh - price) / price) * 100
            if 0.5 < dist < 5:
                entry_candidates.append(("1H_SwingHigh", round(last_sh*0.997,8), last_sh, 1, round(last_sh*0.99,8), last_sh))

        # v13: entry mode detection
        mode_info = _determine_entry_mode(price, "DUMP", entry_candidates, tf_1h, tf_15m, oi_data)

        if entry_candidates:
            entry_candidates.sort(key=lambda x: (-x[3], abs(x[1] - price)))
            best        = entry_candidates[0]
            entry_type  = best[0]
            entry       = best[1]
            sl_ceiling  = best[2]
            zone_bottom = best[4]
            zone_top    = best[5]
            sl          = round(sl_ceiling + atr_buf, 8)
            sl          = min(sl, round(entry * 1.06, 8))
        else:
            entry_type  = "MARKET"
            zone_bottom = price
            zone_top    = round(price * 1.01, 8)
            sl = round(price + atr * 2.0, 8) if atr > 0 else round(price * 1.04, 8)
            sl = min(sl, round(price * 1.06, 8))

        tps = calculate_tp1_tp2(entry, sl, "SHORT", tf_4h, tf_1h, liq_1h)
        rr  = tps["tp1_r"]
        entry_mode = mode_info["mode"]

        if entry_mode == "MOMENTUM_NOW":
            mom_str = " | ".join(mode_info["momentum_reasons"][:2])
            entry_instruction = f"🔻 MOMENTUM ENTRY — Trend bearish LTF aktif\n   {mom_str}"
            confirmation_zone = None
        else:
            entry_instruction = (f"⏳ TUNGGU RETEST ke zona {_fmt_zone(zone_bottom, zone_top)}\n"
                                 f"   Konfirmasi: {mode_info['confirmation_hints'][0]}")
            confirmation_zone = {"bottom": zone_bottom, "top": zone_top, "source": entry_type}

        return {
            "direction": "SHORT", "entry": entry, "sl": sl,
            "tp1": tps["tp1"], "tp2": tps["tp2"],
            "tp1_r": tps["tp1_r"], "tp2_r": tps["tp2_r"],
            "tp1_basis": tps["tp1_basis"], "tp2_basis": tps["tp2_basis"],
            "rr": rr, "entry_type": entry_type,
            "is_limit": entry_type != "MARKET", "atr_based": atr > 0,
            "tp": tps["tp1"], "sl_basis": f"above {entry_type} +ATR buf",
            "entry_mode": entry_mode,
            "confirmation_zone": confirmation_zone,
            "momentum_context": mode_info.get("momentum_reasons", []),
            "entry_instruction": entry_instruction,
        }

    return {
        "direction": "NEUTRAL", "entry": price, "tp": None, "tp1": None, "tp2": None,
        "sl": None, "rr": 0, "tp1_r": 0, "tp2_r": 0,
        "entry_type": "NEUTRAL", "is_limit": False, "atr_based": False,
        "tp1_basis": "", "tp2_basis": "", "sl_basis": "",
        "entry_mode": "NONE", "confirmation_zone": None,
        "momentum_context": [], "entry_instruction": "",
    }

# ─────────────────────────────────────────────
# FORMAT HELPERS
# ─────────────────────────────────────────────

def fmt_num(n: float) -> str:
    if n >= 1_000_000_000: return f"${n/1_000_000_000:.1f}B"
    elif n >= 1_000_000:   return f"${n/1_000_000:.1f}M"
    elif n >= 1_000:       return f"${n/1_000:.1f}K"
    elif n >= 1:           return f"${n:.4f}"
    else:                  return f"${n:.8f}"

# alias untuk backward compat dengan main.py patches
format_number = fmt_num

# ─────────────────────────────────────────────
# TELEGRAM UTILS
# ─────────────────────────────────────────────

def _md_to_html(text: str) -> str:
    """
    Message builder sudah native HTML — tidak perlu escape atau konversi.
    Fungsi ini jadi passthrough agar send_telegram tetap bisa dipanggil
    dari modul lain yang masih pakai Markdown legacy.
    """
    return text

def send_telegram(message: str, chat_id: str = None):
    if not TELEGRAM_BOT_TOKEN:
        log.warning("Telegram credentials missing!")
        return

    target = chat_id or TELEGRAM_CHAT_ID
    if not target:
        log.warning("No chat_id for Telegram!")
        return

    html_message = _md_to_html(message)

    max_len = 4000
    chunks  = [html_message[i:i+max_len] for i in range(0, len(html_message), max_len)]

    for chunk in chunks:
        sent = False
        # Coba kirim HTML dulu
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": target, "text": chunk, "parse_mode": "HTML"},
                    timeout=15
                )
                if r.status_code == 200:
                    log.info("✅ Telegram sent OK (HTML)")
                    sent = True
                    break
                elif r.status_code == 400:
                    # Parse error → fallback plain text
                    log.warning(f"Telegram HTML parse error, falling back to plain text: {r.text[:80]}")
                    break
                else:
                    log.warning(f"Telegram error {r.status_code}: {r.text[:80]}")
                    time.sleep(2)
            except Exception as e:
                log.error(f"Telegram exception (attempt {attempt+1}): {e}")
                time.sleep(2)

        # Fallback: plain text tanpa formatting
        if not sent:
            try:
                plain = re.sub(r"<[^>]+>", "", chunk)   # strip HTML tags
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": target, "text": plain},
                    timeout=15
                )
                if r.status_code == 200:
                    log.info("✅ Telegram sent OK (plain fallback)")
                else:
                    log.warning(f"Telegram plain fallback error {r.status_code}: {r.text[:80]}")
            except Exception as e:
                log.error(f"Telegram plain fallback exception: {e}")

        time.sleep(0.5)



# ─────────────────────────────────────────────
# MESSAGE BUILDER — RESTORED RICH FORMAT
# ─────────────────────────────────────────────

def build_coin_analysis_block(symbol: str, price: float, confluence: dict,
                               tf_4h: dict, tf_1h: dict, tf_15m: dict,
                               oi: dict, with_gemini: bool = True,
                               prepump: dict = None, predump: dict = None,
                               scalp: dict = None, swing: dict = None,
                               news_sentiment: dict = None,
                               with_risk: bool = True,
                               with_claude: bool = False) -> list:
    """
    Build rich analysis block per koin.
    with_claude=True  → panggil Claude API (manual /analyze saja)
    with_gemini=True  → panggil Gemini sentiment overlay (Gemini, bukan coin analysis)
    Auto scan selalu with_claude=False untuk hemat token.
    """
    lines = []

    dir_badge = {
        "PUMP": "🟢 PUMP SIGNAL", "DUMP": "🔴 DUMP SIGNAL", "NEUTRAL": "⚪ NEUTRAL"
    }.get(confluence["direction"], "⚪ NEUTRAL")

    level_badge = {
        "EXCELLENT": "🔥 EXCELLENT", "GOOD": "✅ GOOD",
        "FAIR": "🟡 FAIR", "POOR": "❌ POOR"
    }.get(confluence["level"], "")

    atr_1h = tf_1h.get("atr", 0)
    trade  = calculate_trade_plan(price, confluence["direction"], atr_1h, tf_4h, tf_1h, tf_15m)

    # ── Signal Header ──
    lines.append(f"<b>{dir_badge}</b>  {level_badge}")
    lines.append(f"Confluence: <b>{confluence['score']}/100</b>  (P:{confluence['pump_score']} D:{confluence['dump_score']})")

    # ── OI & Funding ──
    oi_parts = []
    if oi.get("oi_change_pct") is not None:
        oi_emoji = "📈" if oi["oi_change_pct"] > 0 else "📉"
        oi_parts.append(f"{oi_emoji} OI: {oi['oi_change_pct']:+.1f}%")
    if oi.get("ls_ratio") is not None:
        oi_parts.append(f"⚖️ L/S: {oi['ls_ratio']:.2f} ({oi['ls_bias']})")
    if oi.get("funding_rate") is not None:
        fr = oi["funding_rate"]
        fr_emoji = "🔥" if fr < -0.01 else "⚠️" if fr > 0.05 else "💸"
        oi_parts.append(f"{fr_emoji} FR: {fr:+.3f}%")
    if oi_parts:
        lines.append("  ".join(oi_parts))

    # ── MTF Trend ──
    t4  = tf_4h.get("structure", {}).get("trend", "?")
    t1  = tf_1h.get("structure", {}).get("trend", "?")
    t15 = tf_15m.get("structure", {}).get("trend", "?")
    lines.append(f"📐 <b>MTF:</b>  4H <code>{t4}</code>  1H <code>{t1}</code>  15M <code>{t15}</code>")

    # ── RSI ──
    rsi_4h = tf_4h.get("rsi", 50)
    rsi_1h = tf_1h.get("rsi", 50)
    rsi_emoji = "🔥" if rsi_1h > 70 else "❄️" if rsi_1h < 30 else "📊"
    lines.append(f"{rsi_emoji} RSI:  4H <code>{rsi_4h:.0f}</code>  1H <code>{rsi_1h:.0f}</code>")

    # ── ATR ──
    if atr_1h > 0:
        atr_pct = (atr_1h / price) * 100 if price > 0 else 0
        lines.append(f"📏 ATR (1H): <code>{fmt_num(atr_1h)}</code>  ({atr_pct:.2f}%)")

    # ── Money Flow — compact summary line ──
    mf4  = tf_4h.get("money_flow", {})
    mf1  = tf_1h.get("money_flow", {})
    mf15 = tf_15m.get("money_flow", {})
    if mf4 or mf1:
        def _mf_compact(mf: dict) -> str:
            b   = mf.get("bias", "?")[0]
            s   = mf.get("strength", "?")[0]
            cvd = mf.get("cvd_pct", 0)
            return f"{b}{s}({cvd:+.0f}%)"
        inflow_tfs  = sum(1 for mf in [mf4, mf1, mf15] if mf.get("bias") == "INFLOW")
        outflow_tfs = sum(1 for mf in [mf4, mf1, mf15] if mf.get("bias") == "OUTFLOW")
        flow_icon    = "💚" if inflow_tfs >= 2 else "🔴" if outflow_tfs >= 2 else "⚪"
        flow_verdict = "INFLOW" if inflow_tfs >= 2 else "OUTFLOW" if outflow_tfs >= 2 else "MIXED"
        lines.append(
            f"{flow_icon} Flow: <b>{flow_verdict}</b>  "
            f"4H:{_mf_compact(mf4)}  1H:{_mf_compact(mf1)}  15M:{_mf_compact(mf15)}"
        )

    # ── FVG ──
    fvg = tf_15m.get("fvg", {})
    if fvg.get("fvg_type") == "BULLISH" and fvg.get("bullish_fvg"):
        d = fvg["bullish_fvg"]["distance_pct"]
        lines.append(f"🧲 Bullish FVG: {d:+.1f}% away")
    elif fvg.get("fvg_type") == "BEARISH" and fvg.get("bearish_fvg"):
        d = fvg["bearish_fvg"]["distance_pct"]
        lines.append(f"🧲 Bearish FVG: {d:+.1f}% away")

    # ── Rejection ──
    rej = tf_15m.get("rejection", {})
    if rej.get("type") != "NONE":
        rej_emoji = "🟢" if rej["type"] == "BULLISH_REJECTION" else "🔴"
        lines.append(f"{rej_emoji} Rejection: <b>{rej['type']}</b>  (str:{rej['strength']})")

    # ── Order Blocks ──
    ob4        = tf_4h.get("order_blocks", {})
    ob1        = tf_1h.get("order_blocks", {})
    candles_4h = tf_4h.get("candles", [])
    candles_1h = tf_1h.get("candles", [])

    def _ob_mitigation_badge(ob: dict, candles: list) -> str:
        if not ob or not candles:
            return ""
        ob_top    = ob.get("top", 0)
        ob_bottom = ob.get("bottom", 0)
        touches   = sum(1 for c in candles[-30:] if c["low"] <= ob_top and c["high"] >= ob_bottom)
        if touches == 0:   return "🟢 FRESH"
        elif touches == 1: return "🟡 TESTED"
        elif touches <= 3: return "🟠 WEAK"
        else:              return "🔴 MITIGATED"

    ob_lines = []
    if ob4.get("bullish_ob"):
        d = ob4["bullish_ob"]["distance_pct"]; mid = ob4["bullish_ob"]["mid"]
        ob_lines.append(f"  🟢 4H Bull OB @ <code>{fmt_num(mid)}</code>  ({d:.1f}%)  {_ob_mitigation_badge(ob4['bullish_ob'], candles_4h)}")
    if ob4.get("bearish_ob"):
        d = ob4["bearish_ob"]["distance_pct"]; mid = ob4["bearish_ob"]["mid"]
        ob_lines.append(f"  🔴 4H Bear OB @ <code>{fmt_num(mid)}</code>  ({d:.1f}%)  {_ob_mitigation_badge(ob4['bearish_ob'], candles_4h)}")
    if ob1.get("bullish_ob"):
        d = ob1["bullish_ob"]["distance_pct"]; mid = ob1["bullish_ob"]["mid"]
        ob_lines.append(f"  🟢 1H Bull OB @ <code>{fmt_num(mid)}</code>  ({d:.1f}%)  {_ob_mitigation_badge(ob1['bullish_ob'], candles_1h)}")
    if ob1.get("bearish_ob"):
        d = ob1["bearish_ob"]["distance_pct"]; mid = ob1["bearish_ob"]["mid"]
        ob_lines.append(f"  🔴 1H Bear OB @ <code>{fmt_num(mid)}</code>  ({d:.1f}%)  {_ob_mitigation_badge(ob1['bearish_ob'], candles_1h)}")
    if ob_lines:
        lines.append("🧱 <b>Order Blocks:</b>")
        lines.extend(ob_lines)

    # ── Volume Anomaly ──
    va4 = tf_4h.get("volume_anomaly", {})
    va1 = tf_1h.get("volume_anomaly", {})
    if va4.get("is_anomaly") or va1.get("is_anomaly"):
        z   = va4.get("zscore") or va1.get("zscore")
        mul = va4.get("multiplier") or va1.get("multiplier")
        lines.append(f"🐳 <b>UNUSUAL VOLUME: {mul:.1f}x avg  (Z={z})</b>")

    # ── Money Flow — detail block ──
    mf_4h  = tf_4h.get("money_flow", {})
    mf_1h  = tf_1h.get("money_flow", {})
    mf_15m = tf_15m.get("money_flow", {})

    if mf_4h or mf_1h or mf_15m:
        def _mf_icon(mf: dict) -> str:
            bias = mf.get("bias", "NEUTRAL"); strength = mf.get("strength", "WEAK")
            if bias == "INFLOW":   return "💚" if strength == "STRONG" else "🟢" if strength == "MODERATE" else "🔵"
            elif bias == "OUTFLOW": return "🔴" if strength == "STRONG" else "🟠" if strength == "MODERATE" else "🟡"
            return "⚪"

        def _mf_label(mf: dict) -> str:
            bias = mf.get("bias", "NEUTRAL"); strength = mf.get("strength", "WEAK")
            mfi  = mf.get("mfi", 50); cvd = mf.get("cvd_pct", 0)
            vwap = mf.get("vwap_bias", "AT"); sig = mf.get("mfi_signal", "NEUTRAL")
            parts = [bias]
            if strength != "WEAK": parts.append(strength)
            if sig not in ("NEUTRAL",): parts.append(f"MFI:{mfi:.0f}({sig[:4]})")
            parts.append(f"CVD:{cvd:+.1f}%")
            parts.append(f"VWAP:{vwap}")
            return " | ".join(parts)

        lines.append("")
        lines.append("💰 <b>MONEY FLOW:</b>")
        lines.append(f"  {_mf_icon(mf_4h)} 4H  → {_mf_label(mf_4h)}")
        lines.append(f"  {_mf_icon(mf_1h)} 1H  → {_mf_label(mf_1h)}")
        lines.append(f"  {_mf_icon(mf_15m)} 15M → {_mf_label(mf_15m)}")

        inflow_count  = sum(1 for mf in [mf_4h, mf_1h, mf_15m] if mf.get("bias") == "INFLOW")
        outflow_count = sum(1 for mf in [mf_4h, mf_1h, mf_15m] if mf.get("bias") == "OUTFLOW")
        if inflow_count >= 2:
            lines.append(f"  ✅ <i>INFLOW dominan ({inflow_count}/3 TF) — buyer pressure aktif</i>")
        elif outflow_count >= 2:
            lines.append(f"  ⚠️ <i>OUTFLOW dominan ({outflow_count}/3 TF) — seller pressure aktif</i>")
        else:
            lines.append(f"  ⚪ <i>Mixed flow — konfirmasi price action dulu</i>")

    # ── Pre-Pump ──
    if prepump and prepump.get("total_score", 0) >= 35:
        lines.append("")
        lines.append(f"🎯 <b>Pre-Pump: {prepump['total_score']}/100 — {prepump['label']}</b>")
        lines.append(
            f"  💰 Funding: {prepump['funding_score']}/30  "
            f"⚡ Momentum: {prepump['momentum_score']}/35  "
            f"📊 OI+PA: {prepump['oi_pa_score']}/35"
        )

    # ── Pre-Dump ──
    if predump and predump.get("total_score", 0) >= 35:
        lines.append("")
        lines.append(f"💀 <b>Pre-Dump: {predump['total_score']}/100 — {predump['label']}</b>")
        lines.append(
            f"  💸 Funding: {predump['funding_score']}/30  "
            f"🔴 Momentum: {predump['momentum_score']}/35  "
            f"📉 OI+PA: {predump['oi_pa_score']}/35"
        )

    # ── Liquidity Zones ──
    liq_1h   = tf_1h.get("liquidity", {})
    liq_lines = []
    if liq_1h.get("nearest_eqh"):
        eqh = liq_1h["nearest_eqh"]
        liq_lines.append(f"  🔴 EQH: {eqh['distance_pct']:.1f}% atas  ({eqh['count']}x equal highs)")
    if liq_1h.get("nearest_eql"):
        eql = liq_1h["nearest_eql"]
        liq_lines.append(f"  🟢 EQL: {eql['distance_pct']:.1f}% bawah  ({eql['count']}x equal lows)")
    if liq_lines:
        lines.append("")
        lines.append("💧 <b>Liquidity Zones:</b>")
        lines.extend(liq_lines)

    # ── Liquidity Sweep ──
    sweep_1h  = tf_1h.get("sweep", {})
    sweep_15m = tf_15m.get("sweep", {})
    for sw, tf_label in [(sweep_15m, "15M"), (sweep_1h, "1H")]:
        if sw and sw.get("swept"):
            st       = sw["sweep_type"]
            sw_emoji = "🟢" if st == "BULLISH_SWEEP" else "🔴"
            lines.append(
                f"{sw_emoji} <b>{tf_label} Sweep: {st}</b>  "
                f"(recovery {sw['recovery_strength']:.0f}%,  {sw['candles_ago']} candle ago)"
            )

    # ── Trendline ──
    tl_sup = tf_4h.get("trendline_sup", {})
    tl_res = tf_4h.get("trendline_res", {})
    if tl_sup.get("valid"):
        dir_sym = "↗" if tl_sup["direction"] == "ASCENDING" else "↘"
        lines.append(f"📐 4H Support TL {dir_sym}: {tl_sup['distance_pct']:+.1f}%  ({tl_sup['touches']} touches)")
    if tl_res.get("valid"):
        dir_sym = "↗" if tl_res["direction"] == "ASCENDING" else "↘"
        lines.append(f"📐 4H Resist TL {dir_sym}: {tl_res['distance_pct']:+.1f}%  ({tl_res['touches']} touches)")

    # ── Scalp Setup ──
    if scalp and scalp.get("score", 0) >= SCALP_MIN_SCORE:
        lines.append("")
        lines.append(f"⚡ <b>SCALP: {scalp['label']}  ({scalp['score']}/100)</b>")
        lines.append(f"  Arah: {scalp['direction']}")
        if scalp.get("entry_zone"):
            ez = scalp["entry_zone"]
            lines.append(f"  Entry: <code>{fmt_num(ez['bottom'])}</code> — <code>{fmt_num(ez['top'])}</code>  ({ez['width_pct']:.1f}%)")
        if scalp.get("scalp_tp"):
            lines.append(
                f"  TP: <code>{fmt_num(scalp['scalp_tp'])}</code>  (+{SCALP_TP_PCT*100:.1f}%)  "
                f"SL: <code>{fmt_num(scalp['scalp_sl'])}</code>  (-{SCALP_SL_PCT*100:.1f}%)"
            )
        for r in [r for r in scalp.get("reasons", []) if not r.startswith("  ")][:2]:
            lines.append(f"  {r}")

    # ── Swing Setup ──
    if swing and swing.get("score", 0) >= 50:
        lines.append("")
        lines.append(f"📈 <b>SWING: {swing['label']}  ({swing['score']}/100)</b>")
        lines.append(f"  Arah: {swing['direction']}  |  Hold: {swing.get('hold_estimate','')}")
        if swing.get("entry_zone"):
            ez = swing["entry_zone"]
            lines.append(f"  Entry: <code>{fmt_num(ez['bottom'])}</code> — <code>{fmt_num(ez['top'])}</code>")
        if swing.get("swing_tp"):
            lines.append(
                f"  TP: <code>{fmt_num(swing['swing_tp'])}</code>  (+{SWING_TP_PCT*100:.1f}%)  "
                f"SL: <code>{fmt_num(swing['swing_sl'])}</code>  (-{SWING_SL_PCT*100:.1f}%)  "
                f"R:R {swing.get('rr',0):.1f}:1"
            )
        for r in [r for r in swing.get("reasons", []) if not r.startswith("  ")][:2]:
            lines.append(f"  {r}")

    # ── Signals ──
    top_reasons = confluence["reasons"][:5]
    if top_reasons:
        lines.append("")
        lines.append("<b>Signals:</b>")
        for r in top_reasons:
            lines.append(f"  {r}")

    # ── CHoCH / BoS ──
    struct4 = tf_4h.get("structure", {})
    struct1 = tf_1h.get("structure", {})
    badges  = []
    if struct4.get("choch"): badges.append("⚡ CHoCH 4H")
    if struct4.get("bos"):   badges.append("💥 BoS 4H")
    if struct1.get("choch"): badges.append("⚡ CHoCH 1H")
    if struct1.get("bos"):   badges.append("💥 BoS 1H")
    if badges:
        lines.append("  " + "   |   ".join(badges))

    # ── Trade Plan ──
    if confluence["direction"] != "NEUTRAL" and confluence["level"] in ["EXCELLENT", "GOOD"]:
        is_limit    = trade.get("is_limit", False)
        entry_icon  = "🎯" if is_limit else "⚡"
        entry_label = "LIMIT" if is_limit else "MARKET"
        tp1_r       = trade.get("tp1_r", trade.get("rr", 0))
        tp2_r       = trade.get("tp2_r", 0)
        rr_ok       = tp1_r >= 2.0

        lines.append("")
        lines.append(f"📍 <b>Trade Plan — {trade['direction']} ({entry_label})</b>")
        lines.append(f"  {entry_icon} Entry : <code>{fmt_num(trade['entry'])}</code>  ← {trade.get('entry_type','')}")
        lines.append(f"  🔴 SL    : <code>{fmt_num(trade['sl'])}</code>")
        if trade.get("tp1"):
            lines.append(f"  🟡 TP1   : <code>{fmt_num(trade['tp1'])}</code>  ← {trade.get('tp1_basis','')}  ({tp1_r}R)  50% close")
        if trade.get("tp2"):
            lines.append(f"  🟢 TP2   : <code>{fmt_num(trade['tp2'])}</code>  ← {trade.get('tp2_basis','')}  ({tp2_r}R)  runner")
        lines.append(f"  R:R = {tp1_r}:1  {'✅' if rr_ok else '⚠️ &lt;2R — skip'}")
        if not is_limit:
            lines.append("  <i>⚠️ Tidak ada OB/FVG dekat — tunggu retrace dulu</i>")

    elif confluence["level"] == "FAIR":
        lines.append("🟡 <b>FAIR setup</b> — skip atau tunggu konfirmasi lebih lanjut")
    else:
        lines.append("❌ <b>POOR confluence</b> — no trade")

    # ── News Sentiment ──
    if news_sentiment and NEWS_MODULE:
        lines.append("")
        lines.append(format_sentiment_block(news_sentiment, mode="short"))

    # ── Risk Management ──
    if with_risk and RISK_MODULE and confluence.get("direction") not in [None, "NEUTRAL"]:
        trade = confluence.get("trade", {})
        entry = trade.get("entry", price)
        sl    = trade.get("sl", 0)
        direc = confluence.get("direction", "LONG")
        if sl and sl > 0:
            lines.append("")
            lines.append(format_risk_block(entry, sl, direc))

    # ── Gemini AI Insight (manual /analyze only) ──
    if with_gemini and GEMINI_API_KEY:
        lines.append("")
        lines.append("🤖 <b>AI Insight (Gemini):</b>")
        insight = gemini_analyze_coin(
            symbol, confluence, tf_4h, tf_1h, tf_15m, oi, price,
            prepump, predump, scalp, swing
        )
        lines.append(f"<i>{insight}</i>" if insight else "<i>Gemini tidak merespons saat ini.</i>")

        sentiment = gemini_sentiment_overlay(symbol)
        if sentiment:
            lines.append("")
            lines.append("🌐 <b>Sentiment &amp; Event Overlay:</b>")
            lines.append(f"<i>{sentiment}</i>")

    elif with_gemini and not GEMINI_API_KEY:
        lines.append("\n⚠️ <i>GEMINI_API_KEY belum diset di .env</i>")

    return lines


def build_telegram_message(btc: dict, coins: list) -> tuple:
    """
    Build pesan Telegram lengkap dengan format rich.
    Returns: (message_str, enriched_coins_list)

    enriched_coins berisi tf data + semua detector results per coin
    untuk dipakai oleh confirmed_signal engine tanpa re-fetch.
    """
    ts    = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = []

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🤖 <b>CRYPTO SCREENER v13</b>")
    lines.append(f"🕐 {ts}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    env_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "TRANSITIONING": "🟡", "NEUTRAL": "⚪"}.get(
        btc["environment"], "⚪")
    lines.append("")
    lines.append("📊 <b>BTC MARKET CONTEXT</b>")
    lines.append(f"Price : <code>{fmt_num(btc['price'])}</code>")
    lines.append(f"Bias  : {env_emoji} <b>{btc['environment']}</b>")
    lines.append(f"Trend : {btc['trend']}  |  RSI 4H={btc['rsi_4h']:.0f}  1H={btc['rsi_1h']:.0f}")
    if btc.get("choch_4h"): lines.append("⚡ CHoCH on 4H — potential trend flip!")
    if btc.get("bos_4h"):   lines.append("💥 BoS confirmed on 4H")
    if btc.get("rejection_1h") != "NONE":
        lines.append(f"🕯 1H Candle: {btc['rejection_1h']}")

    lines.append("")
    lines.append("═══════════════════════")
    lines.append("🎯 <b>TOP COINS ANALYSIS</b>")
    lines.append("═══════════════════════")

    enriched_coins = []  # kumpul tf data + detector results

    for i, coin in enumerate(coins, 1):
        sym         = coin["symbol"]
        binance_sym = SYMBOL_MAP.get(coin["id"])

        rank_emoji = "🔥" if i == 1 else f"#{i}"
        lines.append(f"\n{rank_emoji} <b>{sym}</b>  (Q: {coin['quality_score']}/10)")
        lines.append(f"💰 <code>{fmt_num(coin['price'])}</code>  📊 {coin['change_24h']:+.2f}%  📈 Vol +{coin['volume_increase_pct']:.1f}%")

        # ── Step 1: Cari symbol di Binance Futures ──
        if not binance_sym:
            candidate = f"{sym}USDT"
            try:
                r = requests.get(
                    f"{BINANCE_FUTURES}/fapi/v1/ticker/price",
                    params={"symbol": candidate}, timeout=5
                )
                if r.status_code == 200:
                    binance_sym = candidate
                    SYMBOL_MAP[coin["id"]] = candidate
                    TICKER_TO_BINANCE[sym] = candidate
                    log.info(f"Auto-resolved {sym} → {candidate} (futures)")
            except Exception:
                pass

        # ── Step 2: Fetch klines Binance (Futures → Spot) ──
        exchange_used = "binance_futures"
        if binance_sym:
            tf_4h  = analyze_timeframe(binance_sym, "4h")
            tf_1h  = analyze_timeframe(binance_sym, "1h")
            tf_15m = analyze_timeframe(binance_sym, "15m")
            oi     = get_open_interest(binance_sym)
        else:
            tf_4h = tf_1h = tf_15m = {"error": True}
            oi = {}

        # ── Step 3: Fallback ke exchange_resolver (OKX/Bybit/Gate) ──
        if (tf_4h.get("error") or tf_1h.get("error")) and EXCHANGE_RESOLVER:
            log.info(f"Binance miss for {sym} — trying exchange_resolver fallback")
            resolved = resolve_symbol_full(sym)
            if resolved and resolved.get("exchange") != "binance_futures":
                exc      = resolved["exchange"]
                exc_sym  = resolved["symbol"]
                exc_lbl  = resolved.get("exchange_label", exc)
                tf_4h    = analyze_timeframe_exc(exc_sym, "4h",  exc)
                tf_1h    = analyze_timeframe_exc(exc_sym, "1h",  exc)
                tf_15m   = analyze_timeframe_exc(exc_sym, "15m", exc)
                oi       = get_open_interest(exc_sym) if binance_sym else {}
                exchange_used = exc
                if not tf_4h.get("error"):
                    lines.append(f"📡 Data via <b>{exc_lbl}</b>")
                    log.info(f"Fallback success: {sym} → {exc_sym} on {exc}")

        # ── Step 4: Kalau masih error setelah semua fallback ──
        if tf_4h.get("error") or tf_1h.get("error"):
            lines.append(f"⚠️ {sym} — data tidak tersedia di semua exchange")
            lines.append("─────────────────────")
            continue

        # Pakai binance_sym untuk OI/internal tracking, exc_sym untuk analysis
        analysis_sym = binance_sym or sym + "USDT"

        confluence = calculate_confluence_v4(tf_4h, tf_1h, tf_15m, oi)
        prepump    = detect_prepump(analysis_sym, tf_1h, tf_4h, oi)
        predump    = detect_predump(analysis_sym, tf_1h, tf_4h, oi)
        eqh_eql    = tf_1h.get("liquidity", {})
        scalp      = detect_scalp_setup(analysis_sym, tf_15m, tf_1h, tf_4h, oi)
        swing      = detect_swing_setup(analysis_sym, tf_4h, tf_1h, tf_15m, oi, eqh_eql)

        coin_lines = build_coin_analysis_block(
            sym, coin["price"], confluence, tf_4h, tf_1h, tf_15m, oi,
            with_gemini=False, prepump=prepump, predump=predump,
            scalp=scalp, swing=swing,
            with_claude=False  # auto scan: no AI call
        )
        lines.extend(coin_lines)
        lines.append("─────────────────────")

        # ── v12: Kumpul enriched data untuk confirmed signal engine ──
        enriched_coins.append({
            "symbol":     analysis_sym,
            "price":      coin["price"],
            "confluence": confluence,
            "prepump":    prepump,
            "predump":    predump,
            "scalp":      scalp,
            "swing":      swing,
            "oi":         oi,
            "tf_4h":      tf_4h,
            "tf_1h":      tf_1h,
            "tf_15m":     tf_15m,
        })

    lines.append("\n<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines), enriched_coins


def build_prepump_message(candidates: list) -> str:
    """v13: Pre-pump message dengan EXPLICIT ENTRY MODE (MOMENTUM_NOW/RETEST_WAIT)."""
    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "🎯 *PRE-PUMP ALERT* 🔥",
             f"🕐 {ts}", "━━━━━━━━━━━━━━━━━━━━━━━━",
             "_Funding Squeeze + Momentum + OI Conviction_\n"]

    if not candidates:
        lines += ["❄️ Tidak ada kandidat pre-pump terdeteksi saat ini.", "⚠️ _Not financial advice. DYOR._"]
        return "\n".join(lines)

    for i, pp in enumerate(candidates[:5], 1):
        sym   = pp["symbol"].replace("USDT", "")
        trade = pp.get("trade", {})
        lines.append(f"{'🔥' if i==1 else f'#{i}'} <b>{sym}</b> — {pp['label']}")
        lines.append(f"  Score: <b>{pp['total_score']}/100</b> | Price: {fmt_num(pp.get('price',0))}")
        lines.append(f"  💰 F:{pp['funding_score']}/30  ⚡ M:{pp['momentum_score']}/35  📊 OI:{pp['oi_pa_score']}/35")
        for r in [r for r in pp.get("reasons",[]) if not r.startswith("  ")][:2]:
            lines.append(f"  {r}")
        if pp.get("funding_rate") is not None:
            lines.append(f"  Funding: {pp['funding_rate']:+.3f}% | RSI 1H: {pp.get('rsi',0):.0f}")

        if trade and trade.get("direction") == "LONG":
            tp1_r = trade.get("tp1_r", 0)
            tp2_r = trade.get("tp2_r", 0)
            entry_mode = trade.get("entry_mode", "MOMENTUM_NOW")
            lines.append(f"\n  📍 <b>Trade Plan LONG:</b>")
            if entry_mode == "MOMENTUM_NOW":
                lines.append(f"  🚀 <b>ENTRY NOW</b> — Breakout confirmed, trend bullish aktif")
                for ctx in trade.get("momentum_context", [])[:2]:
                    lines.append(f"  ↳ {ctx}")
                lines.append(f"  ⚡ Entry : <b>{fmt_num(trade['entry'])}</b>  ← MARKET sekarang")
            else:
                cz = trade.get("confirmation_zone")
                if cz:
                    lines.append(f"  ⏳ <b>TUNGGU RETEST</b> → zona: <b>{_fmt_zone(cz['bottom'], cz['top'])}</b>  [{cz.get('source','?')}]")
                lines.append(f"  🎯 Entry : <b>{fmt_num(trade['entry'])}</b>  ← LIMIT order")
                lines.append(f"  ✅ Konfirmasi: candle 15M close di atas zona + volume spike")
            lines.append(f"  🔴 SL    : {fmt_num(trade['sl'])}")
            if trade.get("tp1"):
                lines.append(f"  🟡 TP1   : {fmt_num(trade['tp1'])}  ({tp1_r}R) ← {trade.get('tp1_basis','')} | close 50%")
            if trade.get("tp2"):
                lines.append(f"  🟢 TP2   : {fmt_num(trade['tp2'])}  ({tp2_r}R) ← {trade.get('tp2_basis','')} | runner")
            lines.append(f"  R:R = {tp1_r:.1f}:1 {'✅' if tp1_r >= 2.0 else '⚠️ < 2R'}")
        else:
            lines.append("  ⚠️ Trade plan tidak tersedia (data tidak cukup)")
        lines.append("─────────────────────")

    lines.append("\n⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def build_predump_message(candidates: list) -> str:
    """v13: Pre-dump message dengan EXPLICIT ENTRY MODE."""
    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "💀 *PRE-DUMP ALERT* 🔻",
             f"🕐 {ts}", "━━━━━━━━━━━━━━━━━━━━━━━━",
             "_Funding Bearish + Bearish Momentum + OI Distribution_\n"]

    if not candidates:
        lines += ["❄️ Tidak ada kandidat pre-dump terdeteksi saat ini.", "⚠️ _Not financial advice. DYOR._"]
        return "\n".join(lines)

    for i, pd_c in enumerate(candidates[:5], 1):
        sym   = pd_c["symbol"].replace("USDT", "")
        trade = pd_c.get("trade", {})
        lines.append(f"{'💀' if i==1 else f'#{i}'} <b>{sym}</b> — {pd_c['label']}")
        lines.append(f"  Score: <b>{pd_c['total_score']}/100</b> | Price: {fmt_num(pd_c.get('price',0))}")
        lines.append(f"  💸 F:{pd_c['funding_score']}/30  🔴 M:{pd_c['momentum_score']}/35  📉 OI:{pd_c['oi_pa_score']}/35")
        for r in [r for r in pd_c.get("reasons",[]) if not r.startswith("  ")][:2]:
            lines.append(f"  {r}")
        if pd_c.get("funding_rate") is not None:
            lines.append(f"  Funding: {pd_c['funding_rate']:+.3f}% | RSI 1H: {pd_c.get('rsi',0):.0f}")

        if trade and trade.get("direction") == "SHORT":
            tp1_r = trade.get("tp1_r", 0)
            tp2_r = trade.get("tp2_r", 0)
            entry_mode = trade.get("entry_mode", "MOMENTUM_NOW")
            lines.append(f"\n  📍 <b>Trade Plan SHORT:</b>")
            if entry_mode == "MOMENTUM_NOW":
                lines.append(f"  🔻 <b>ENTRY NOW</b> — Breakdown confirmed, trend bearish aktif")
                for ctx in trade.get("momentum_context", [])[:2]:
                    lines.append(f"  ↳ {ctx}")
                lines.append(f"  ⚡ Entry : <b>{fmt_num(trade['entry'])}</b>  ← MARKET sekarang")
            else:
                cz = trade.get("confirmation_zone")
                if cz:
                    lines.append(f"  ⏳ <b>TUNGGU RETEST</b> → zona: <b>{_fmt_zone(cz['bottom'], cz['top'])}</b>  [{cz.get('source','?')}]")
                lines.append(f"  🎯 Entry : <b>{fmt_num(trade['entry'])}</b>  ← LIMIT order")
                lines.append(f"  ✅ Konfirmasi: candle 15M close di bawah zona + volume spike")
            lines.append(f"  🔴 SL    : {fmt_num(trade['sl'])}")
            if trade.get("tp1"):
                lines.append(f"  🟡 TP1   : {fmt_num(trade['tp1'])}  ({tp1_r}R) ← {trade.get('tp1_basis','')} | close 50%")
            if trade.get("tp2"):
                lines.append(f"  🟢 TP2   : {fmt_num(trade['tp2'])}  ({tp2_r}R) ← {trade.get('tp2_basis','')} | runner")
            lines.append(f"  R:R = {tp1_r:.1f}:1 {'✅' if tp1_r >= 2.0 else '⚠️ < 2R'}")
        else:
            lines.append("  ⚠️ Trade plan tidak tersedia (data tidak cukup)")
        lines.append("─────────────────────")

    lines.append("\n⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# SYMBOL RESOLUTION — MULTI-EXCHANGE
# ─────────────────────────────────────────────

_symbol_exc_cache: dict = {}

def resolve_symbol_to_binance(user_input: str) -> str | None:
    """Legacy shim — returns symbol string, internally uses multi-exchange resolver."""
    info = resolve_symbol_full(user_input)
    return info["symbol"] if info else None


def resolve_symbol_full(user_input: str) -> dict | None:
    """
    Resolve symbol dengan fallback Binance Futures → Bybit → OKX → Gate.io.
    Returns {"symbol", "exchange", "exchange_label", "has_futures"} atau None.
    """
    inp = user_input.strip().upper()
    if inp in _symbol_exc_cache:
        return _symbol_exc_cache[inp]

    result = None
    if EXCHANGE_RESOLVER:
        result = _exc_resolve(inp)
    else:
        sym = inp.replace("/", "").replace("-", "")
        if not (sym.endswith("USDT") or sym.endswith("UST")):
            sym = sym + "USDT"
        try:
            r = requests.get(f"{BINANCE_BASE}/ticker/price",
                             params={"symbol": sym}, timeout=5)
            if r.status_code == 200:
                result = {"symbol": sym, "exchange": "binance_futures",
                          "exchange_label": "Binance Futures", "has_futures": True}
        except Exception:
            pass

    if result:
        _symbol_exc_cache[inp] = result
    return result


def analyze_timeframe_exc(symbol: str, interval: str, exchange: str = "binance_futures") -> dict:
    """Multi-exchange version of analyze_timeframe."""
    if exchange == "binance_futures" or not EXCHANGE_RESOLVER:
        return analyze_timeframe(symbol, interval)

    candles = _exc_ohlcv(symbol, interval, exchange, limit=101)
    if not candles or len(candles) < 10:
        return {"error": True, "interval": interval}

    current_candle = candles[-1]
    closed_candles = candles[:-1]
    if len(closed_candles) < 10:
        return {"error": True, "interval": interval}

    structure = detect_market_structure(closed_candles)
    return {
        "interval": interval, "error": False,
        "price": current_candle["close"],
        "candles": closed_candles,
        "current_candle": current_candle,
        "structure": structure,
        "fvg": detect_fvg(closed_candles),
        "order_blocks": detect_order_blocks(closed_candles),
        "rejection": detect_candle_rejection(closed_candles),
        "volume_anomaly": detect_volume_anomaly(closed_candles),
        "rsi": calculate_rsi(closed_candles),
        "atr": calculate_atr(closed_candles),
        "liquidity": detect_equal_highs_lows(closed_candles),
        "sweep": detect_liquidity_sweep(closed_candles, structure),
        "trendline_sup": detect_trendline(closed_candles, "lows"),
        "trendline_res": detect_trendline(closed_candles, "highs"),
        "money_flow": detect_money_flow(closed_candles),
        "_anti_lookahead": True,
        "_closed_count": len(closed_candles),
        "_exchange": exchange,
    }


def get_ticker_exc(symbol: str, exchange: str = "binance_futures") -> dict | None:
    """Fetch ticker dari exchange yang benar."""
    if exchange == "binance_futures":
        raw = get_binance_ticker(symbol)
        if not raw:
            return None
        return {"price": float(raw.get("lastPrice", 0)),
                "change_24h": float(raw.get("priceChangePercent", 0)),
                "volume_24h": float(raw.get("quoteVolume", 0)), "raw": raw}
    if EXCHANGE_RESOLVER:
        return _exc_ticker(symbol, exchange)
    return None

# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

def handle_analyze_command(user_input: str, chat_id: str):
    """Handle /analyze <COIN> — full SMC + pre-pump analysis. Multi-exchange aware."""
    send_telegram(f"🔍 Menganalisa <b>{user_input.upper()}</b>... tunggu sebentar ⏳", chat_id)

    exc_info = resolve_symbol_full(user_input)
    if not exc_info:
        msg = format_not_found_message(user_input) if EXCHANGE_RESOLVER else (
            f"❌ Symbol <b>{user_input.upper()}</b> tidak ditemukan di Binance.\n"
            "Coba: <code>BTC</code>, <code>SOLUSDT</code>, <code>ETH</code>, dll."
        )
        send_telegram(msg, chat_id)
        return

    binance_sym = exc_info["symbol"]
    exchange    = exc_info["exchange"]

    # Notif kalau resolve ke exchange selain Binance Futures
    if exchange != "binance_futures":
        send_telegram(format_found_on_other_exchange(exc_info), chat_id)

    ticker = get_ticker_exc(binance_sym, exchange)
    if not ticker:
        send_telegram(f"❌ Tidak bisa fetch data untuk <b>{binance_sym}</b>.", chat_id)
        return

    price     = ticker["price"]
    change_24 = ticker["change_24h"]
    volume    = ticker["volume_24h"]

    tf_4h  = analyze_timeframe_exc(binance_sym, "4h",  exchange)
    tf_1h  = analyze_timeframe_exc(binance_sym, "1h",  exchange)
    tf_15m = analyze_timeframe_exc(binance_sym, "15m", exchange)

    oi = get_open_interest(binance_sym) if exchange == "binance_futures" \
         else {"oi": None, "oi_change_pct": None, "ls_ratio": None,
               "ls_bias": "UNKNOWN", "funding_rate": None}

    if tf_4h.get("error") or tf_1h.get("error"):
        send_telegram(
            f"⚠️ Chart data untuk <b>{binance_sym}</b> tidak tersedia di {exc_info.get('exchange_label','?')}.",
            chat_id)
        return

    confluence = calculate_confluence_v4(tf_4h, tf_1h, tf_15m, oi)
    prepump    = detect_prepump(binance_sym, tf_1h, tf_4h, oi)
    predump    = detect_predump(binance_sym, tf_1h, tf_4h, oi)
    eqh_eql    = tf_1h.get("liquidity", {})
    scalp      = detect_scalp_setup(binance_sym, tf_15m, tf_1h, tf_4h, oi)
    swing      = detect_swing_setup(binance_sym, tf_4h, tf_1h, tf_15m, oi, eqh_eql)

    # v9: News sentiment
    news_s = None
    if NEWS_MODULE and NEWSAPI_KEY:
        try:
            news_s = get_coin_sentiment(binance_sym)
        except Exception as e:
            log.warning(f"News sentiment error: {e}")
    ts         = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔎 <b>ANALISA ON-DEMAND</b>",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💎 <b>{binance_sym}</b>",
        f"💰 Price: <code>{fmt_num(price)}</code>  📊 {change_24:+.2f}%  📈 Vol: {fmt_num(volume)}",
        "",
    ]

    coin_lines = build_coin_analysis_block(
        binance_sym, price, confluence, tf_4h, tf_1h, tf_15m, oi,
        with_gemini=True, prepump=prepump, predump=predump,
        scalp=scalp, swing=swing,
        news_sentiment=news_s, with_risk=True
    )
    lines.extend(coin_lines)
    lines.append("\n⚠️ <i>Not financial advice. DYOR.</i>")
    send_telegram("\n".join(lines), chat_id)


def handle_chart_command(chat_id: str, photo_file_id: str):
    """Handle gambar chart yang dikirim user via Telegram."""
    if not GEMINI_API_KEY:
        send_telegram("⚠️ Fitur analisa chart butuh GEMINI_API_KEY di .env", chat_id)
        return

    send_telegram("📸 Chart diterima! Menganalisa dengan AI... ⏳", chat_id)

    try:
        # Fetch file info dari Telegram
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": photo_file_id},
            timeout=10
        )
        if r.status_code != 200:
            send_telegram("❌ Gagal fetch file dari Telegram.", chat_id)
            return

        file_path = r.json()["result"]["file_path"]
        file_url  = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"

        # Download image
        img_r = requests.get(file_url, timeout=15)
        if img_r.status_code != 200:
            send_telegram("❌ Gagal download image.", chat_id)
            return

        image_base64 = base64.b64encode(img_r.content).decode("utf-8")

        # Deteksi mime type dari extension
        ext = file_path.split(".")[-1].lower()
        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/jpeg")

        # Analisa via Gemini Vision
        analysis = gemini_analyze_chart_image(image_base64, mime_type)

        if not analysis:
            send_telegram("⚠️ Gemini tidak bisa menganalisa chart ini. Coba kirim gambar yang lebih jelas.", chat_id)
            return

        ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
        msg = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📸 *ANALISA CHART AI*\n"
            f"🕐 {ts}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{analysis}\n\n"
            f"⚠️ _Not financial advice. DYOR._"
        )
        send_telegram(msg, chat_id)

    except Exception as e:
        log.error(f"Chart analysis error: {e}")
        send_telegram(f"❌ Error saat analisa chart: {str(e)[:100]}", chat_id)


def handle_prepump_command(chat_id: str):
    """Handle /prepump — scan pre-pump candidates."""
    send_telegram("🎯 Scanning pre-pump candidates... ⏳ (ini butuh ~1-2 menit)", chat_id)

    candidates = scan_prepump_candidates()
    msg = build_prepump_message(candidates)
    send_telegram(msg, chat_id)


def handle_predump_command(chat_id: str):
    """Handle /predump — scan pre-dump candidates."""
    send_telegram("💀 Scanning pre-dump candidates... ⏳ (ini butuh ~1-2 menit)", chat_id)

    candidates = scan_predump_candidates()
    msg = build_predump_message(candidates)
    send_telegram(msg, chat_id)


def build_scalp_message(candidates: list) -> str:
    """v13: Scalp message dengan EXPLICIT ENTRY MODE dan confirmation zone."""
    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "⚡ *SCALP RADAR*",
             f"🕐 {ts}", "━━━━━━━━━━━━━━━━━━━━━━━━",
             "_Liquidity Sweep + Rejection + OB/FVG + HTF Bias (15M/1H)_\n"]

    if not candidates:
        lines += ["😴 Tidak ada scalping setup valid saat ini.", "\n⚠️ _Not financial advice. DYOR._"]
        return "\n".join(lines)

    for i, sc in enumerate(candidates[:6], 1):
        sym   = sc["symbol"].replace("USDT", "")
        direc = sc["direction"]
        trade = sc.get("trade", {})
        dir_emoji = "🟢" if direc == "LONG" else "🔴" if direc == "SHORT" else "⚪"

        lines.append(f"{'⚡' if i==1 else f'#{i}'} <b>{sym}</b> — {sc['label']}")
        lines.append(f"  Score: <b>{sc['score']}/100</b> | {dir_emoji} {direc} | Price: {fmt_num(sc.get('price',0))}")

        entry_mode = trade.get("entry_mode", "RETEST_WAIT") if trade else "RETEST_WAIT"

        if entry_mode == "MOMENTUM_NOW":
            lines.append(f"  🚀 <b>ENTRY NOW</b> — Trend {'bullish' if direc=='LONG' else 'bearish'} aktif")
            for ctx in (trade.get("momentum_context",[]) if trade else [])[:2]:
                lines.append(f"  ↳ {ctx}")
            if trade and trade.get("entry"):
                lines.append(f"  ⚡ Entry : <b>{fmt_num(trade['entry'])}</b> ← MARKET")
        else:
            cz = trade.get("confirmation_zone") if trade else None
            if cz:
                lines.append(f"  ⏳ <b>TUNGGU ke zona</b>: <b>{_fmt_zone(cz['bottom'], cz['top'])}</b>  [{cz.get('source','?')}]")
            elif sc.get("entry_zone"):
                ez = sc["entry_zone"]
                lines.append(f"  ⏳ <b>TUNGGU ke zona</b>: <b>{fmt_num(ez['bottom'])} – {fmt_num(ez['top'])}</b>")
            if trade and trade.get("entry"):
                lines.append(f"  🎯 Entry  : <b>{fmt_num(trade['entry'])}</b> ← LIMIT")
            conf_word = "bullish engulfing/pin bar 15M" if direc == "LONG" else "bearish engulfing/pin bar 15M"
            lines.append(f"  ✅ Konfirmasi: {conf_word} + vol ≥1.5x")

        if trade and trade.get("sl"):
            lines.append(f"  🔴 SL     : {fmt_num(trade['sl'])}")
        elif sc.get("scalp_sl"):
            lines.append(f"  🔴 SL     : {fmt_num(sc['scalp_sl'])}")

        if trade and trade.get("tp1"):
            tp1_r = trade.get("tp1_r", 0)
            lines.append(f"  🟡 TP1    : {fmt_num(trade['tp1'])}  ({tp1_r}R) | close 50%")
        elif sc.get("scalp_tp"):
            lines.append(f"  🟡 TP     : {fmt_num(sc['scalp_tp'])}")

        sw = sc.get("sweep", {})
        if sw.get("swept"):
            lines.append(f"  🎯 Liq Sweep: {sw['sweep_type']} (recovery {sw['recovery_strength']:.0f}%)")

        for r in [r for r in sc.get("reasons",[]) if not r.startswith("  ")][:2]:
            lines.append(f"  {r}")
        lines.append("─────────────────────")

    lines.append("\n⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def handle_scalp_command(chat_id: str):
    """Handle /scalp — scan scalping setups terbaik."""
    send_telegram("⚡ Scanning scalp setups... ⏳ (butuh ~1-2 menit)", chat_id)
    candidates = scan_scalp_candidates()
    msg = build_scalp_message(candidates)
    send_telegram(msg, chat_id)


# ── v9: News handlers ────────────────────────

def handle_news_command(coin: str, chat_id: str):
    """Handle /news <COIN>"""
    if not NEWS_MODULE:
        send_telegram("❌ News module tidak tersedia. Pastikan news_sentiment.py ada.", chat_id)
        return
    if not NEWSAPI_KEY:
        send_telegram("❌ NEWSAPI_KEY belum diset di .env", chat_id)
        return
    sym = coin.upper().strip().replace("USDT","")
    if not sym:
        send_telegram("❓ Format: `/news BTC` atau `/news SOL`", chat_id)
        return
    send_telegram(f"📰 Mencari berita untuk *{sym}*... ⏳", chat_id)
    try:
        s   = get_coin_sentiment(sym)
        msg = format_sentiment_block(s, mode="full")
        send_telegram(msg, chat_id)
    except Exception as e:
        send_telegram(f"❌ Error fetch news: {e}", chat_id)


def handle_macro_command(chat_id: str):
    """Handle /macro — macro news sentiment"""
    if not NEWS_MODULE:
        send_telegram("❌ News module tidak tersedia.", chat_id)
        return
    if not NEWSAPI_KEY:
        send_telegram("❌ NEWSAPI_KEY belum diset di .env", chat_id)
        return
    send_telegram("🌐 Mengambil macro news... ⏳", chat_id)
    try:
        s   = get_macro_sentiment()
        s["symbol"] = "MACRO"
        msg = format_sentiment_block(s, mode="full")
        send_telegram(msg, chat_id)
    except Exception as e:
        send_telegram(f"❌ Error fetch macro news: {e}", chat_id)


# ── v9: Risk handlers ────────────────────────

def handle_risk_command(chat_id: str):
    """Handle /risk — tampilkan risk status hari ini"""
    if not RISK_MODULE:
        send_telegram("❌ Risk module tidak tersedia.", chat_id)
        return
    msg = format_risk_status()
    send_telegram(msg, chat_id)


def handle_setmodal_command(args: str, chat_id: str):
    """Handle /setmodal <USDT>"""
    if not RISK_MODULE:
        send_telegram("❌ Risk module tidak tersedia.", chat_id)
        return
    try:
        amount = float(args.strip().replace(",",""))
        set_capital(amount)
        send_telegram(f"✅ Modal diset: *${amount:,.2f} USDT*\nGunakan `/risk` untuk lihat summary.", chat_id)
    except ValueError:
        send_telegram("❓ Format: `/setmodal 1000` (angka USDT)", chat_id)


def handle_setrisk_command(args: str, chat_id: str):
    """Handle /setrisk <PCT>"""
    if not RISK_MODULE:
        send_telegram("❌ Risk module tidak tersedia.", chat_id)
        return
    try:
        pct = float(args.strip().replace("%",""))
        set_risk_pct(pct)
        send_telegram(f"✅ Risk per trade diset: *{pct:.1f}%*", chat_id)
    except ValueError:
        send_telegram("❓ Format: `/setrisk 2` (angka persen)", chat_id)


def handle_setdailyloss_command(args: str, chat_id: str):
    """Handle /setdailyloss <PCT>"""
    if not RISK_MODULE:
        send_telegram("❌ Risk module tidak tersedia.", chat_id)
        return
    try:
        pct = float(args.strip().replace("%",""))
        set_daily_loss_limit(pct)
        send_telegram(f"✅ Daily loss limit diset: *{pct:.1f}%*", chat_id)
    except ValueError:
        send_telegram("❓ Format: `/setdailyloss 5` (angka persen)", chat_id)


def handle_logpnl_command(args: str, chat_id: str):
    """Handle /logpnl <USDT> — catat hasil trade"""
    if not RISK_MODULE:
        send_telegram("❌ Risk module tidak tersedia.", chat_id)
        return
    try:
        pnl = float(args.strip())
        record_trade_result(pnl)
        emoji = "🟢" if pnl >= 0 else "🔴"
        send_telegram(
            f"{emoji} Trade dicatat: *{pnl:+.2f} USDT*\n"
            f"Ketik `/risk` untuk lihat summary harian.",
            chat_id
        )
    except ValueError:
        send_telegram("❓ Format: `/logpnl +50` atau `/logpnl -30` (dalam USDT)", chat_id)


# ── v9: Portfolio handlers ───────────────────

def handle_portfolio_command(chat_id: str, show_spot: bool = False):
    """Portfolio tracker dinonaktifkan."""
    send_telegram("ℹ️ Portfolio tracker tidak diaktifkan.", chat_id)


def run_portfolio_alert():
    """Portfolio alert dinonaktifkan."""
    pass


# ── v11: Trade Journal handlers ───────────────

def handle_logtrade_command(args: str, chat_id: str):
    """Handle /logtrade — log trade via wizard atau one-liner."""
    if not JOURNAL_MODULE:
        send_telegram("❌ Trade journal module tidak tersedia.", chat_id)
        return
    if not args.strip():
        # Mode wizard
        msg = wizard_start(chat_id)
        send_telegram(msg, chat_id, parse_mode="HTML")
    else:
        # One-liner: /logtrade BTC LONG 65000 50 10 +25
        data, err = parse_oneliner(args)
        if err:
            send_telegram(err, chat_id, parse_mode="HTML")
            return
        t = log_trade(
            coin=data["coin"], direction=data["direction"],
            entry_price=float(data["entry"]), margin_usdt=float(data["margin"]),
            leverage=int(data["leverage"]), pnl_usdt=float(data["pnl"]),
            note=data.get("note", "")
        )
        send_telegram(format_trade_logged(t), chat_id, parse_mode="HTML")


def handle_trades_command(chat_id: str):
    """Handle /trades — lihat 5 trade terakhir."""
    if not JOURNAL_MODULE:
        send_telegram("❌ Trade journal module tidak tersedia.", chat_id)
        return
    trades = get_recent_trades(5)
    msg = format_recent_trades(trades)
    send_telegram(msg, chat_id, parse_mode="HTML")


def handle_weeksummary_command(chat_id: str):
    """Handle /weeksummary — weekly summary + AI analysis."""
    if not JOURNAL_MODULE:
        send_telegram("❌ Trade journal module tidak tersedia.", chat_id)
        return
    send_telegram("📊 Menyusun weekly summary + AI analysis... ⏳", chat_id)
    msg = format_weekly_summary()
    send_telegram(msg, chat_id, parse_mode="HTML")


def handle_setbalance_command(args: str, chat_id: str):
    """Handle /setbalance <USDT> — set saldo awal."""
    if not JOURNAL_MODULE:
        send_telegram("❌ Trade journal module tidak tersedia.", chat_id)
        return
    try:
        amount = float(args.strip().replace(",", ""))
        msg = set_initial_balance(amount)
        send_telegram(msg, chat_id, parse_mode="HTML")
    except ValueError:
        send_telegram("❓ Format: <code>/setbalance 500</code> (angka USDT)", chat_id, parse_mode="HTML")


def handle_journal_wizard_message(text: str, chat_id: str):
    """Handle pesan saat user sedang dalam wizard mode."""
    if not JOURNAL_MODULE:
        return False
    if not is_in_wizard(chat_id):
        return False
    reply, done = wizard_process(chat_id, text=text)
    send_telegram(reply, chat_id, parse_mode="HTML")
    return True


def handle_journal_wizard_image(file_id: str, chat_id: str):
    """Handle gambar yang dikirim saat dalam wizard mode (sebagai bukti trade)."""
    if not JOURNAL_MODULE:
        return False
    if not is_wizard_expecting_image(chat_id):
        return False
    # Fetch file URL dari Telegram
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        )
        if r.status_code == 200:
            file_path = r.json()["result"]["file_path"]
            image_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        else:
            image_url = ""
    except Exception:
        image_url = ""
    reply, done = wizard_process(chat_id, image_url=image_url)
    send_telegram(reply, chat_id, parse_mode="HTML")
    return True


def handle_ask_command(question: str, chat_id: str):
    if not question.strip():
        send_telegram("❓ Format: `/ask <pertanyaan>` — contoh: `/ask apa itu order block?`", chat_id)
        return
    send_telegram("🤖 Tanya ke Gemini AI... ⏳", chat_id)
    answer = gemini_free_ask(question)
    send_telegram(f"🤖 *Gemini AI:*\n\n{answer}", chat_id)


def handle_status_command(chat_id: str):
    gemini_status   = "✅ Connected" if GEMINI_API_KEY else "❌ No API Key"
    learning_status = "✅ Active" if LEARNING_MODULE else "⚠️ Module missing"
    journal_status  = "✅ Active" if JOURNAL_MODULE else "⚠️ Module missing"
    backtest_status  = "✅ Active" if BACKTEST_MODULE  else "⚠️ Module missing"
    tracker_status   = "✅ Active" if TRACKER_MODULE   else "⚠️ Module missing"
    confirmed_status = "✅ Active" if CONFIRMED_MODULE else "⚠️ Module missing"
    saldo = f"${get_current_balance():,.2f} USDT" if JOURNAL_MODULE else "N/A"
    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *CRYPTO SCREENER v12*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Status          : ✅ Running\n"
        f"Gemini AI       : {gemini_status}\n"
        f"Learning Engine : {learning_status}\n"
        f"Trade Journal   : {journal_status}\n"
        f"Backtest Engine : {backtest_status}\n"
        f"Signal Tracker  : {tracker_status}\n"
        f"Confirmed Signal: {confirmed_status}\n"
        f"Saldo Journal   : {saldo}\n"
        f"Scan interval   : {SCAN_INTERVAL_MINUTES} menit (gated — hanya kirim kalau signal lolos 4 gate)\n"
        f"Top coins       : {TOP_COINS_COUNT}\n"
        f"Pre-pump/dump   : tiap {PREPUMP_SCAN_INTERVAL} menit\n"
        f"Server time     : {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}\n\n"
        "📌 *Quick Commands:*\n"
        "`/analyze BTC` | `/chart` | `/scalp` | `/prepump` | `/predump`\n"
        "`/logtrade` | `/trades` | `/weeksummary` | `/setbalance`\n"
        "`/backtest BTC scalp 30` | `/signals` | `/btcompare ETH 14`\n"
        "`/help` — list lengkap"
    )
    if LEARNING_MODULE:
        try:
            msg += "\n\n" + get_performance_stats_text()
        except Exception:
            pass
    send_telegram(msg, chat_id)


# ── v13: Symbol Memory Command Handlers ──────
def handle_symbolmemory_command(args: str, chat_id: str):
    if not SYMBOL_MEMORY_MODULE:
        send_telegram("⚠️ symbol_memory.py tidak ditemukan.", chat_id); return
    symbol = args.strip().upper().replace("USDT","")
    if not symbol:
        send_telegram("❓ Format: <code>/symbolmemory BTC</code>", chat_id); return
    send_telegram(get_symbol_detail(symbol), chat_id)

def handle_symbolstats_command(chat_id: str):
    if not SYMBOL_MEMORY_MODULE:
        send_telegram("⚠️ symbol_memory.py tidak ditemukan.", chat_id); return
    send_telegram(get_all_stats_summary(), chat_id)

def handle_blacklist_command(args: str, chat_id: str):
    if not SYMBOL_MEMORY_MODULE:
        send_telegram("⚠️ symbol_memory.py tidak ditemukan.", chat_id); return
    parts = args.strip().split(maxsplit=1)
    symbol = parts[0].upper() if parts else ""
    reason = parts[1] if len(parts) > 1 else "Manual blacklist"
    if not symbol:
        send_telegram("❓ Format: <code>/blacklist BTC [alasan]</code>", chat_id); return
    send_telegram(manual_blacklist(symbol, reason), chat_id)

def handle_unblacklist_command(args: str, chat_id: str):
    if not SYMBOL_MEMORY_MODULE:
        send_telegram("⚠️ symbol_memory.py tidak ditemukan.", chat_id); return
    symbol = args.strip().upper()
    if not symbol:
        send_telegram("❓ Format: <code>/unblacklist BTC</code>", chat_id); return
    send_telegram(manual_unblacklist(symbol), chat_id)


def handle_help_command(chat_id: str):
    msg = (
        "🤖 *CRYPTO SCREENER v13 — HELP*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *SCREENING & ANALISA*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 `/analyze BTC` — Full SMC analysis on-demand\n"
        "📸 `/chart` — Kirim gambar chart → analisa AI (SMC verdict)\n"
        "⚡ `/scalp` — Scan scalping setups (15M/1H)\n"
        "🎯 `/prepump` — Scan pre-pump candidates\n"
        "💀 `/predump` — Scan pre-dump candidates\n"
        "💬 `/ask <pertanyaan>` — Tanya crypto ke Gemini\n"
        "📡 `/scan` — Trigger manual scan sekarang\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔬 *BACKTEST ENGINE* _(v12 baru!)_\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 `/backtest BTC scalp 30` — Backtest sinyal bot ke data historis\n"
        "   strategies: `scalp` | `swing` | `prepump` | `predump` | `combined`\n"
        "📋 `/btresult` — Hasil backtest terakhir\n"
        "🔬 `/btcompare BTC 14` — Compare semua strategy untuk 1 coin\n"
        "📚 `/btstats` — History aggregate semua backtest session\n"
        "📡 `/signals` — Status semua signal yg ditrack (pending & resolved)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 *CONFIRMED ENTRY* _(auto, no command needed)_\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Bot otomatis gabungkan:\n"
        "  confluence + prepump + predump + scalp + swing\n"
        "Divalidasi backtest 7 hari → kalau bagus, langsung kirim.\n"
        "Threshold: master score >= 75 + backtest profit factor >= 1.0\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📝 *TRADE JOURNAL*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📝 `/logtrade` — Log trade baru (wizard step-by-step)\n"
        "   atau `/logtrade BTC LONG 65000 50 10 +25` (one-liner)\n"
        "📋 `/trades` — Lihat 5 trade terakhir\n"
        "📊 `/weeksummary` — Weekly summary + AI analysis\n"
        "💰 `/setbalance 500` — Set saldo awal\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📚 *LEARNING ENGINE*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 `/logoutcome BTC TP` — Catat hasil signal (TP/SL hit)\n"
        "📖 `/lessons` — Lihat lessons yang tersimpan\n"
        "🔍 `/decisions` — Log keputusan screening\n"
        "🧬 `/evolve` — Auto-tune scoring thresholds\n"
        "✏️ `/addlesson <teks>` — Tambah lesson manual\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧠 *SYMBOL MEMORY* _(v13 baru!)_\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 `/symbolmemory BTC` — History & lessons per coin\n"
        "📈 `/symbolstats` — Ringkasan semua coin di memory\n"
        "⛔ `/blacklist BTC [alasan]` — Blacklist coin dari scan\n"
        "✅ `/unblacklist BTC` — Hapus dari blacklist\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ *RISK & STATUS*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 `/status` — Status bot + saldo\n"
        "💼 `/risk` — Risk status harian\n"
        "💵 `/setmodal 1000` — Set modal\n"
        "🎚️ `/setrisk 2` — Set risk per trade (%)\n"
        "🛑 `/setdailyloss 5` — Set daily loss limit (%)\n"
        "📈 `/logpnl +50` — Catat PnL ke risk manager\n\n"
        "💡 *Tips:* Kirim nama koin langsung (`BTC`, `SOL`) juga bisa!\n"
        "⚠️ _Not financial advice. DYOR._"
    )
    send_telegram(msg, chat_id)

def handle_btresult_wrapper(chat_id: str):
    """Wrapper untuk /btresult — tampilkan hasil backtest terakhir."""
    _bt_result(chat_id, send_telegram)


# ─────────────────────────────────────────────
# TELEGRAM POLLER
# ─────────────────────────────────────────────

last_update_id    = 0
_awaiting_chart   = {}  # chat_id → True (user habis kirim /chart, tunggu foto)

def get_telegram_updates() -> list:
    global last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 30, "limit": 10},
            timeout=35
        )
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception as e:
        log.warning(f"Telegram getUpdates error: {e}")
    return []


def process_update(update: dict):
    global last_update_id, _awaiting_chart

    update_id = update.get("update_id", 0)
    if update_id > last_update_id:
        last_update_id = update_id

    message = update.get("message", {})
    if not message:
        return

    chat_id = str(message.get("chat", {}).get("id", ""))
    text    = message.get("text", "").strip()
    photos  = message.get("photo", [])

    if not chat_id:
        return

    # ── SECURITY: whitelist check ────────────
    if not is_allowed(chat_id):
        return  # silent drop — unauthorized user tidak dapat response apapun
    # ─────────────────────────────────────────

    log.info(f"📩 [{chat_id}] text='{text[:60]}' photos={len(photos)}")

    # ── Handle foto chart ────────────────────────
    if photos:
        photo   = photos[-1]
        file_id = photo.get("file_id")
        if file_id:
            # v11: Cek apakah user lagi di wizard journal (butuh foto sebagai bukti)
            if JOURNAL_MODULE and is_wizard_expecting_image(chat_id):
                threading.Thread(
                    target=handle_journal_wizard_image,
                    args=(file_id, chat_id),
                    daemon=True
                ).start()
            else:
                # Normal: analisa chart AI
                threading.Thread(
                    target=handle_chart_command,
                    args=(chat_id, file_id),
                    daemon=True
                ).start()
        _awaiting_chart.pop(chat_id, None)
        return

    if not text:
        return

    text_lower = text.lower()

    # ── Command routing ──────────────────────────
    if text_lower.startswith("/analyze") or text_lower.startswith("/a "):
        parts = text.split(maxsplit=1)
        coin  = parts[1].strip() if len(parts) > 1 else ""
        if coin:
            threading.Thread(target=handle_analyze_command, args=(coin, chat_id), daemon=True).start()
        else:
            send_telegram("❓ Format: `/analyze BTC` atau `/analyze SOLUSDT`", chat_id)

    elif text_lower.startswith("/chart"):
        _awaiting_chart[chat_id] = True
        send_telegram("📸 Siap! Sekarang kirimkan gambar chart kamu dan akan langsung dianalisa AI 🤖", chat_id)

    elif text_lower.startswith("/prepump"):
        threading.Thread(target=handle_prepump_command, args=(chat_id,), daemon=True).start()

    elif text_lower.startswith("/predump"):
        threading.Thread(target=handle_predump_command, args=(chat_id,), daemon=True).start()

    elif text_lower.startswith("/scalp"):
        threading.Thread(target=handle_scalp_command, args=(chat_id,), daemon=True).start()

    # ── v9: News ──────────────────────────────
    elif text_lower.startswith("/news"):
        parts = text.split(maxsplit=1)
        coin  = parts[1].strip() if len(parts) > 1 else ""
        threading.Thread(target=handle_news_command, args=(coin, chat_id), daemon=True).start()

    elif text_lower.startswith("/macro"):
        threading.Thread(target=handle_macro_command, args=(chat_id,), daemon=True).start()

    # ── v9: Risk ──────────────────────────────
    elif text_lower.startswith("/risk"):
        handle_risk_command(chat_id)

    elif text_lower.startswith("/setmodal"):
        parts = text.split(maxsplit=1)
        handle_setmodal_command(parts[1] if len(parts)>1 else "", chat_id)

    elif text_lower.startswith("/setrisk"):
        parts = text.split(maxsplit=1)
        handle_setrisk_command(parts[1] if len(parts)>1 else "", chat_id)

    elif text_lower.startswith("/setdailyloss"):
        parts = text.split(maxsplit=1)
        handle_setdailyloss_command(parts[1] if len(parts)>1 else "", chat_id)

    elif text_lower.startswith("/logpnl"):
        parts = text.split(maxsplit=1)
        handle_logpnl_command(parts[1] if len(parts)>1 else "", chat_id)

    # ── v11: Trade Journal ────────────────────
    elif text_lower.startswith("/logtrade"):
        parts = text.split(maxsplit=1)
        threading.Thread(
            target=handle_logtrade_command,
            args=(parts[1] if len(parts)>1 else "", chat_id),
            daemon=True
        ).start()

    elif text_lower.startswith("/trades"):
        threading.Thread(target=handle_trades_command, args=(chat_id,), daemon=True).start()

    elif text_lower.startswith("/weeksummary"):
        threading.Thread(target=handle_weeksummary_command, args=(chat_id,), daemon=True).start()

    elif text_lower.startswith("/setbalance"):
        parts = text.split(maxsplit=1)
        threading.Thread(
            target=handle_setbalance_command,
            args=(parts[1] if len(parts)>1 else "", chat_id),
            daemon=True
        ).start()

    # ── v11: Learning Engine ──────────────────
    elif text_lower.startswith("/logoutcome"):
        parts = text.split(maxsplit=1)
        if LEARNING_MODULE:
            msg = handle_logoutcome_command(parts[1] if len(parts)>1 else "", send_telegram)
            send_telegram(msg, chat_id, parse_mode="HTML")
        else:
            send_telegram("⚠️ Learning module tidak aktif.", chat_id)

    elif text_lower.startswith("/lessons"):
        if LEARNING_MODULE:
            parts = text.split(maxsplit=1)
            send_telegram(handle_lessons_command(parts[1] if len(parts)>1 else ""), chat_id, parse_mode="HTML")
        else:
            send_telegram("⚠️ Learning module tidak aktif.", chat_id)

    elif text_lower.startswith("/decisions"):
        if LEARNING_MODULE:
            parts = text.split(maxsplit=1)
            send_telegram(handle_decisions_command(parts[1] if len(parts)>1 else ""), chat_id, parse_mode="HTML")
        else:
            send_telegram("⚠️ Learning module tidak aktif.", chat_id)

    elif text_lower.startswith("/evolve"):
        if LEARNING_MODULE:
            threading.Thread(
                target=lambda: send_telegram(handle_evolve_command(), chat_id, parse_mode="HTML"),
                daemon=True
            ).start()
        else:
            send_telegram("⚠️ Learning module tidak aktif.", chat_id)

    elif text_lower.startswith("/addlesson"):
        if LEARNING_MODULE:
            parts = text.split(maxsplit=1)
            send_telegram(handle_addlesson_command(parts[1] if len(parts)>1 else ""), chat_id, parse_mode="HTML")
        else:
            send_telegram("⚠️ Learning module tidak aktif.", chat_id)

    # ── v9: Portfolio ─────────────────────────
    elif text_lower.startswith("/backtest"):
        parts = text.split(maxsplit=1)
        args  = parts[1].strip() if len(parts) > 1 else ""
        if BACKTEST_MODULE:
            threading.Thread(
                target=_bt_backtest,
                args=(args, chat_id, send_telegram),
                daemon=True
            ).start()
        else:
            send_telegram(
                "❌ Backtest module tidak tersedia.\n"
                "Pastikan `backtest_engine.py` ada di folder yang sama.",
                chat_id
            )

    elif text_lower.startswith("/btresult"):
        if BACKTEST_MODULE:
            handle_btresult_wrapper(chat_id)
        else:
            send_telegram("❌ Backtest module tidak tersedia.", chat_id)

    elif text_lower.startswith("/btcompare"):
        parts = text.split(maxsplit=1)
        args  = parts[1].strip() if len(parts) > 1 else ""
        if BACKTEST_MODULE:
            threading.Thread(
                target=_bt_compare,
                args=(args, chat_id, send_telegram),
                daemon=True
            ).start()
        else:
            send_telegram("❌ Backtest module tidak tersedia.", chat_id)

    elif text_lower.startswith("/btstats"):
        if BACKTEST_MODULE:
            _bt_stats(chat_id, send_telegram)
        else:
            send_telegram("❌ Backtest module tidak tersedia.", chat_id)

    elif text_lower.startswith("/signals"):
        if TRACKER_MODULE:
            send_telegram(format_tracker_summary(), chat_id)
        else:
            send_telegram("❌ Signal tracker module tidak tersedia.", chat_id)

    # ── v13: Symbol Memory ───────────────────────
    elif text_lower.startswith("/symbolmemory"):
        parts = text.split(maxsplit=1)
        threading.Thread(
            target=handle_symbolmemory_command,
            args=(parts[1] if len(parts)>1 else "", chat_id),
            daemon=True
        ).start()

    elif text_lower.startswith("/symbolstats"):
        threading.Thread(target=handle_symbolstats_command, args=(chat_id,), daemon=True).start()

    elif text_lower.startswith("/blacklist"):
        parts = text.split(maxsplit=1)
        handle_blacklist_command(parts[1] if len(parts)>1 else "", chat_id)

    elif text_lower.startswith("/unblacklist"):
        parts = text.split(maxsplit=1)
        handle_unblacklist_command(parts[1] if len(parts)>1 else "", chat_id)

    elif text_lower.startswith("/ask"):
        parts    = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""
        threading.Thread(target=handle_ask_command, args=(question, chat_id), daemon=True).start()

    elif text_lower.startswith("/scan"):
        send_telegram("📡 Manual scan dimulai... ⏳", chat_id)
        threading.Thread(target=run_scan, kwargs={"manual": True, "chat_id": chat_id}, daemon=True).start()

    elif text_lower.startswith("/status"):
        handle_status_command(chat_id)

    elif text_lower.startswith("/security"):
        send_telegram(get_security_status(), chat_id)

    elif text_lower.startswith("/help") or text_lower.startswith("/start"):
        handle_help_command(chat_id)

    # v11: Journal wizard intercept (harus di atas free-form)
    elif JOURNAL_MODULE and is_in_wizard(chat_id):
        threading.Thread(
            target=handle_journal_wizard_message,
            args=(text, chat_id),
            daemon=True
        ).start()

    # Direct coin name → analyze
    elif len(text.split()) == 1 and text.upper().replace("USDT", "") in TICKER_TO_BINANCE:
        threading.Thread(target=handle_analyze_command, args=(text.strip(), chat_id), daemon=True).start()

    # Free-form question → Gemini
    elif len(text) > 3:
        threading.Thread(target=handle_ask_command, args=(text, chat_id), daemon=True).start()


def polling_loop():
    log.info("📡 Telegram polling loop started")
    while True:
        try:
            updates = get_telegram_updates()
            for update in updates:
                process_update(update)
        except Exception as e:
            log.warning(f"Polling loop error: {e}")
            time.sleep(5)
        time.sleep(1)

# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════
# v13: SIGNAL GATE ENGINE
# ═══════════════════════════════════════════════════════════
# Arsitektur:
#   Scan tiap 10 menit → evaluasi semua gate → TIDAK kirim Telegram
#   kalau belum lolos semua 4 gate.
#   Gate pass → kirim ALERT SETUP
#   RETEST_WAIT: monitor zone → kirim ENTRY NOTIFICATION kedua saat harga masuk
#   Heartbeat tiap 4 jam: kirim status + watchlist
# ─────────────────────────────────────────────────────────

import json as _json

_GATE_STATE_FILE  = "gate_state.json"
_RETEST_QUEUE_KEY = "retest_queue"
_SENT_SIGNALS_KEY = "sent_signals"
_LAST_HB_KEY      = "last_heartbeat"
_WATCHLIST_KEY    = "watchlist"


def _load_gate_state() -> dict:
    return secure_load(_GATE_STATE_FILE, default={
        _RETEST_QUEUE_KEY: [],
        _SENT_SIGNALS_KEY: {},
        _LAST_HB_KEY:      "",
        _WATCHLIST_KEY:    {},
    })


def _save_gate_state(state: dict):
    if not secure_save(_GATE_STATE_FILE, state):
        log.warning("gate_state save error")


def _gate_cooldown_ok(symbol: str, state: dict) -> bool:
    """Cek apakah symbol masih dalam cooldown setelah signal dikirim."""
    sent = state.get(_SENT_SIGNALS_KEY, {})
    last_ts = sent.get(symbol)
    if not last_ts:
        return True
    from datetime import datetime, timezone, timedelta
    last = datetime.fromisoformat(last_ts)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return elapsed >= GATE_COOLDOWN_HOURS


def _gate_mark_sent(symbol: str, state: dict):
    """Tandai symbol sebagai sudah dikirim signal."""
    state.setdefault(_SENT_SIGNALS_KEY, {})[symbol] = datetime.now(timezone.utc).isoformat()


def _check_money_flow_gate(tf_4h: dict, tf_1h: dict, tf_15m: dict, direction: str) -> tuple[bool, str]:
    """
    Gate: money flow >= GATE_MONEYFLOW_TF_MIN TF harus align dengan direction.
    PUMP → butuh INFLOW, DUMP → butuh OUTFLOW.
    Returns (passed, reason_str)
    """
    expected = "INFLOW" if direction in ("LONG", "PUMP") else "OUTFLOW"
    tfs = {"4H": tf_4h, "1H": tf_1h, "15M": tf_15m}
    aligned = [name for name, tf in tfs.items()
               if tf.get("money_flow", {}).get("bias") == expected]
    passed = len(aligned) >= GATE_MONEYFLOW_TF_MIN
    reason = f"MoneyFlow {expected}: {len(aligned)}/3 TF aligned ({', '.join(aligned) if aligned else 'none'})"
    return passed, reason


def _check_entry_mode_gate(trade: dict) -> tuple[bool, str]:
    """Gate: entry mode harus MOMENTUM_NOW atau RETEST_WAIT dengan zona."""
    if not trade:
        return False, "No trade plan"
    em = trade.get("entry_mode", "")
    if em == "MOMENTUM_NOW":
        return True, "MOMENTUM_NOW — entry market sekarang"
    if em == "RETEST_WAIT":
        cz = trade.get("confirmation_zone")
        if cz and cz.get("bottom") and cz.get("top"):
            return True, f"RETEST_WAIT — zona {_fmt_zone(cz['bottom'], cz['top'])}"
        return False, "RETEST_WAIT tapi confirmation_zone kosong"
    return False, f"Entry mode tidak jelas: '{em}'"


def _build_gated_signal_message(
    symbol: str, price: float, direction: str,
    master_score: int, confidence: str,
    trade: dict, oi_data: dict,
    confluence: dict, mf_reasons: list,
    gate_reasons: list,
    alert_type: str = "SETUP"  # "SETUP" | "ENTRY_NOW"
) -> str:
    """
    Build pesan signal yang sudah lolos semua gate.
    alert_type SETUP = pertama kali setup terdeteksi
    alert_type ENTRY_NOW = price masuk retest zone (notif kedua)
    """
    from datetime import datetime, timezone
    ts  = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    sym = symbol.replace("USDT", "")
    dir_emoji = "🟢" if direction in ("LONG", "PUMP") else "🔴"
    dir_label = "LONG ▲" if direction in ("LONG", "PUMP") else "SHORT ▼"

    entry_mode = trade.get("entry_mode", "")
    tp1_r = trade.get("tp1_r", 0)
    tp2_r = trade.get("tp2_r", 0)
    conf_emoji = "🔥" if confidence == "HIGH" else "✅" if confidence == "MEDIUM" else "🟡"

    def _f(v):
        if v is None: return "N/A"
        if v >= 1000: return f"${v:,.2f}"
        elif v >= 1:  return f"${v:.4f}"
        else:         return f"${v:.6f}"

    def _pct(entry, target):
        if not entry or not target: return ""
        return f"({abs(target-entry)/entry*100:.1f}%)"

    if alert_type == "SETUP":
        header_title = "🚦 <b>SIGNAL SETUP TERDETEKSI</b>"
        header_sub   = "<i>Semua gate lolos — setup sedang forming</i>"
    else:
        header_title = "🚨 <b>ENTRY NOW — PRICE DI ZONA!</b>"
        header_sub   = "<i>Price masuk confirmation zone — cek candle konfirmasi</i>"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{conf_emoji} {header_title}",
        f"🕐 {ts}",
        f"{header_sub}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💎 <b>{sym}</b>  {dir_emoji} <b>{dir_label}</b>",
        f"💰 Harga   : <code>{_f(price)}</code>",
        f"🎯 Score   : <b>{master_score}/100</b> — Confidence: <b>{confidence}</b>",
        "",
        "─── TRADE PLAN ───",
    ]

    if entry_mode == "MOMENTUM_NOW":
        lines.append(f"🚀 <b>ENTRY NOW</b> — Momentum confirmed")
        for ctx in trade.get("momentum_context", [])[:2]:
            lines.append(f"   ↳ {ctx}")
        lines.append(f"⚡ Entry  : <code>{_f(trade.get('entry', price))}</code> ← MARKET")
    else:
        cz = trade.get("confirmation_zone", {})
        if alert_type == "ENTRY_NOW":
            lines.append(f"🎯 <b>PRICE DI ZONA!</b> {_fmt_zone(cz.get('bottom',0), cz.get('top',0))}")
            lines.append(f"   Tunggu candle 15M close konfirmasi lalu entry")
        else:
            lines.append(f"⏳ <b>TUNGGU RETEST</b> ke zona: <b>{_fmt_zone(cz.get('bottom',0), cz.get('top',0))}</b>")
            lines.append(f"   Sumber zone: {cz.get('source','?')}")
        lines.append(f"🎯 Entry  : <code>{_f(trade.get('entry', price))}</code> ← LIMIT")
        conf_word = "bullish engulfing/pin bar 15M" if "LONG" in direction or "PUMP" in direction else "bearish engulfing/pin bar 15M"
        lines.append(f"✅ Konfirmasi: {conf_word} + vol ≥1.5x")

    lines += [
        f"🔴 SL     : <code>{_f(trade.get('sl'))}</code>  {_pct(trade.get('entry', price), trade.get('sl'))}",
        f"🟡 TP1    : <code>{_f(trade.get('tp1'))}</code>  {_pct(trade.get('entry', price), trade.get('tp1'))}  ({tp1_r}R) | close 50%",
        f"🟢 TP2    : <code>{_f(trade.get('tp2'))}</code>  {_pct(trade.get('entry', price), trade.get('tp2'))}  ({tp2_r}R) | runner",
        f"📐 R:R    : <b>{trade.get('rr', tp1_r):.1f}:1</b>  {'✅' if tp1_r >= 2.0 else '⚠️ &lt;2R'}",
        "",
        "─── GATE SUMMARY ───",
    ]

    for r in gate_reasons:
        lines.append(f"  ✅ {r}")

    # Money flow summary
    if mf_reasons:
        lines.append("")
        lines.append("─── MONEY FLOW ───")
        for r in mf_reasons[:3]:
            lines.append(f"  {r}")

    # OI context
    fr  = oi_data.get("funding_rate")
    oi_c = oi_data.get("oi_change_pct")
    ls   = oi_data.get("ls_ratio")
    if fr is not None or oi_c is not None:
        lines.append("")
        lines.append("─── MARKET CONTEXT ───")
        if fr  is not None: lines.append(f"  Funding : {fr:+.3f}%")
        if oi_c is not None: lines.append(f"  OI      : {oi_c:+.1f}%")
        if ls   is not None: lines.append(f"  L/S     : {ls:.2f} ({oi_data.get('ls_bias','?')})")

    # ─── WHALE CONFLUENCE ───
    try:
        import whale_tracker as _wt
        _coin_ticker = symbol.replace("USDT", "").replace("UST", "")
        wctx      = _wt.get_whale_context_for_coin(_coin_ticker)
        whale_bias = wctx.get("whale_bias", "NEUTRAL")

        has_data = (
            wctx.get("whale_long_vol", 0) + wctx.get("whale_short_vol", 0) > 0 or
            wctx.get("wallet_long_count", 0) + wctx.get("wallet_short_count", 0) > 0
        )

        if has_data:
            long_signal   = direction in ("LONG", "PUMP")
            whale_aligns  = (long_signal and whale_bias == "BULLISH") or (not long_signal and whale_bias == "BEARISH")
            whale_against = (long_signal and whale_bias == "BEARISH") or (not long_signal and whale_bias == "BULLISH")
            bias_emoji    = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(whale_bias, "⚪")

            lines.append("")
            lines.append("─── WHALE TRACKER ───")

            if whale_aligns:
                lines.append(f"  {bias_emoji} <b>{whale_bias}</b> ✅ — <i>align sama sinyal</i>")
            elif whale_against:
                lines.append(f"  {bias_emoji} <b>{whale_bias}</b> ⚠️ — <i>berlawanan, hati-hati</i>")
            else:
                lines.append(f"  {bias_emoji} <b>NEUTRAL</b>")

            wl_vol = wctx.get("whale_long_vol", 0)
            ws_vol = wctx.get("whale_short_vol", 0)
            wl_cnt = wctx.get("wallet_long_count", 0)
            ws_cnt = wctx.get("wallet_short_count", 0)
            if wl_vol > 0 or ws_vol > 0:
                lines.append(f"  Trades : 🟢 {_wt.fmt_usd(wl_vol)} buy  🔴 {_wt.fmt_usd(ws_vol)} sell")
            if wl_cnt > 0 or ws_cnt > 0:
                lines.append(f"  Wallets: {wl_cnt}L / {ws_cnt}S")

    except ImportError:
        pass
    except Exception as _e:
        log.debug(f"Whale inject error: {_e}")

    lines.append("")
    lines.append("<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def _build_heartbeat_message(watchlist: dict, last_signal_ts: str) -> str:
    """Build pesan heartbeat tiap 4 jam: status + watchlist."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🤖 *SCREENER STATUS*",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "😴 *Tidak ada signal baru* sejak:",
        f"   {last_signal_ts if last_signal_ts else 'Bot baru start'}",
        "",
    ]
    if watchlist:
        lines.append("👀 <b>WATCHLIST</b> — mendekati threshold:")
        for sym, info in list(watchlist.items())[:5]:
            score  = info.get("score", 0)
            direc  = info.get("direction", "?")
            needs  = info.get("needs", "")
            d_icon = "🟢" if direc in ("LONG","PUMP") else "🔴"
            lines.append(f"  {d_icon} {sym.replace('USDT','')} — score {score}/100 | kurang: {needs}")
    else:
        lines.append("👀 Tidak ada coin yang mendekati threshold saat ini.")
    lines += ["", "💡 Scan terus berjalan setiap 10 menit.", "⚠️ _Not financial advice. DYOR._"]
    return "\n".join(lines)


def _is_price_in_zone(price: float, zone: dict, buffer_pct: float = 0.002) -> bool:
    """Cek apakah harga sudah masuk confirmation zone (dengan buffer kecil)."""
    if not zone:
        return False
    bottom = zone.get("bottom", 0) * (1 - buffer_pct)
    top    = zone.get("top", 0)    * (1 + buffer_pct)
    return bottom <= price <= top


def run_gated_scan():
    """
    v13 MAIN SCAN — hanya kirim Telegram kalau semua gate lolos.

    Tiap 10 menit:
    1. Screen coins + compute semua indikator
    2. Evaluasi 4 gate per coin
    3. Gate semua lolos → kirim ALERT SETUP + tandai cooldown
    4. Cek retest queue → kalau harga masuk zona → kirim ENTRY_NOW
    5. Update watchlist
    6. Tiap 4 jam → kirim heartbeat

    TIDAK ADA lagi kiriman Telegram tiap scan (kecuali gate lolos).
    """
    from datetime import datetime, timezone, timedelta

    state = _load_gate_state()

    # ── 0. Signal tracker resolve ──────────────────
    if TRACKER_MODULE:
        try:
            resolved = on_scan_start(send_telegram)
            if resolved:
                log.info(f"📊 Signal tracker: {len(resolved)} signals resolved")
        except Exception as e:
            log.warning(f"Signal tracker error: {e}")

    # ── 1. Screen + compute indicators ────────────
    btc   = get_btc_context()
    coins = screen_coins(manual=True)  # bypass vol filter — auto scan juga butuh semua coins
    log.info(f"🔍 Gated scan: BTC={btc['environment']} | {len(coins)} coins screened")

    if not coins:
        log.info("No coins passed CoinGecko filters.")
        _check_heartbeat(state)
        return

    signals_sent   = 0
    watchlist_new  = {}

    # ── 2. Evaluate each coin through all gates ───
    for coin in coins:
        sym         = coin["symbol"]
        binance_sym = SYMBOL_MAP.get(coin["id"])
        log.info(f"── Evaluating {sym} | binance_map={'yes' if binance_sym else 'no'}")

        # ── Auto-resolve Binance Futures symbol ──
        if not binance_sym:
            candidate = f"{sym}USDT"
            try:
                r = requests.get(
                    f"{BINANCE_FUTURES}/fapi/v1/ticker/price",
                    params={"symbol": candidate}, timeout=5
                )
                if r.status_code == 200:
                    binance_sym = candidate
                    SYMBOL_MAP[coin["id"]] = candidate
                    TICKER_TO_BINANCE[sym] = candidate
            except Exception:
                pass

        # ── Blacklist check ──
        if binance_sym and SYMBOL_MEMORY_MODULE:
            is_bl, bl_r = is_blacklisted(binance_sym)
            if is_bl:
                log.info(f"⛔ {binance_sym} blacklisted, skip: {bl_r}")
                continue

        # ── Cooldown check ──
        track_sym = binance_sym or f"{sym}USDT"
        if not _gate_cooldown_ok(track_sym, state):
            log.info(f"⏳ {track_sym} in gate cooldown, skip")
            continue

        # ── Fetch klines: Binance → exchange_resolver fallback ──
        exchange_used = "binance_futures"
        analysis_sym  = track_sym
        try:
            if binance_sym:
                tf_4h  = analyze_timeframe(binance_sym, "4h")
                tf_1h  = analyze_timeframe(binance_sym, "1h")
                tf_15m = analyze_timeframe(binance_sym, "15m")
                oi     = get_open_interest(binance_sym)
            else:
                tf_4h = tf_1h = tf_15m = {"error": True}
                oi = {}
        except Exception as e:
            log.warning(f"Data fetch error {track_sym}: {e}")
            tf_4h = tf_1h = tf_15m = {"error": True}
            oi = {}

        if (tf_4h.get("error") or tf_1h.get("error")) and EXCHANGE_RESOLVER:
            log.info(f"Binance klines failed for {sym} — trying exchange_resolver")
            resolved = resolve_symbol_full(sym)
            if resolved:
                exc          = resolved["exchange"]
                exc_sym      = resolved["symbol"]
                tf_4h_try    = analyze_timeframe_exc(exc_sym, "4h",  exc)
                tf_1h_try    = analyze_timeframe_exc(exc_sym, "1h",  exc)
                tf_15m_try   = analyze_timeframe_exc(exc_sym, "15m", exc)
                if not tf_4h_try.get("error") and not tf_1h_try.get("error"):
                    tf_4h        = tf_4h_try
                    tf_1h        = tf_1h_try
                    tf_15m       = tf_15m_try
                    oi           = get_open_interest(exc_sym) if binance_sym else {}
                    exchange_used = exc
                    analysis_sym  = exc_sym
                    log.info(f"✅ Gated fallback OK: {sym} → {exc_sym} on {exc}")
                else:
                    log.info(f"❌ Gated fallback failed: {sym} on {exc} also error")

        if tf_4h.get("error") or tf_1h.get("error"):
            log.info(f"Skip {sym} — no data from any exchange")
            continue

        confluence = calculate_confluence_v4(tf_4h, tf_1h, tf_15m, oi)
        direction  = confluence.get("direction", "NEUTRAL")

        if direction == "NEUTRAL":
            continue

        # Detectors
        prepump = detect_prepump(analysis_sym, tf_1h, tf_4h, oi)
        predump = detect_predump(analysis_sym, tf_1h, tf_4h, oi)
        eqh_eql = tf_1h.get("liquidity", {})
        scalp   = detect_scalp_setup(analysis_sym, tf_15m, tf_1h, tf_4h, oi)
        swing   = detect_swing_setup(analysis_sym, tf_4h, tf_1h, tf_15m, oi, eqh_eql)

        # ─────────────────────────────────────────
        # GATE EVALUATION
        # ─────────────────────────────────────────
        gate_results  = {}
        gate_reasons  = []
        failed_reasons = []

        # GATE 1: Master Score via confirmed_signal logic
        # Hitung master score inline (simplified — full version di confirmed_signal.py)
        pump_dir  = direction == "PUMP"
        conf_score = confluence.get("score", 0)
        conf_level = confluence.get("level", "POOR")
        pp_score   = prepump.get("total_score", 0) if prepump else 0
        pd_score   = predump.get("total_score", 0) if predump else 0
        sc_score   = scalp.get("score", 0)   if scalp else 0
        sw_score   = swing.get("score", 0)   if swing else 0

        if pump_dir:
            raw_master = int(conf_score * 0.40 + pp_score * 0.25 + sc_score * 0.20 + sw_score * 0.15)
        else:
            raw_master = int(conf_score * 0.40 + pd_score * 0.25 + sc_score * 0.20 + sw_score * 0.15)

        gate_results["master_score"] = raw_master >= GATE_MASTER_SCORE_MIN
        if gate_results["master_score"]:
            gate_reasons.append(f"Master score {raw_master}/100 ≥ {GATE_MASTER_SCORE_MIN}")
        else:
            failed_reasons.append(f"Master score {raw_master} < {GATE_MASTER_SCORE_MIN}")

        # GATE 2: Money Flow
        mf_pass, mf_reason = _check_money_flow_gate(tf_4h, tf_1h, tf_15m, direction)
        gate_results["money_flow"] = mf_pass
        if mf_pass:
            gate_reasons.append(mf_reason)
        else:
            failed_reasons.append(mf_reason)

        # GATE 3: Backtest (quick inline check via backtest_engine kalau available)
        bt_pass = True
        bt_reason = "Backtest skipped (module N/A)"
        if BACKTEST_MODULE:
            try:
                from backtest_engine import quick_validate_signal
                bt_dir = "LONG" if pump_dir else "SHORT"
                bt_result = quick_validate_signal(analysis_sym, bt_dir)
                bt_pass   = bt_result.get("valid", False) and bt_result.get("profit_factor", 0) >= GATE_BT_PF_MIN
                bt_pf     = bt_result.get("profit_factor", 0)
                bt_reason = f"Backtest PF={bt_pf:.2f} ({'valid' if bt_pass else 'FAILED'})"
            except Exception:
                bt_pass   = True  # fallback: tidak block kalau BT error
                bt_reason = "Backtest not available (fallback pass)"
        gate_results["backtest"] = bt_pass
        if bt_pass:
            gate_reasons.append(bt_reason)
        else:
            failed_reasons.append(bt_reason)

        # GATE 4: Entry Mode
        price = tf_1h.get("price", coin.get("price", 0))
        atr_1h = tf_1h.get("atr", 0)
        trade  = calculate_trade_plan(price, direction, atr_1h, tf_4h, tf_1h, tf_15m, oi)
        em_pass, em_reason = _check_entry_mode_gate(trade) if GATE_REQUIRE_ENTRY_MODE else (True, "Gate disabled")
        gate_results["entry_mode"] = em_pass
        if em_pass:
            gate_reasons.append(em_reason)
        else:
            failed_reasons.append(em_reason)

        all_pass = all(gate_results.values())

        # Collect money flow reasons for message
        mf_all_reasons = []
        for tf_name, tf_data in [("4H", tf_4h), ("1H", tf_1h), ("15M", tf_15m)]:
            mf = tf_data.get("money_flow", {})
            for r in mf.get("reasons", [])[:1]:
                mf_all_reasons.append(f"{tf_name}: {r}")

        if all_pass:
            # ── ALL GATES PASSED → SEND ALERT SETUP ──
            confidence_label = ("HIGH" if raw_master >= 85 else
                                "MEDIUM" if raw_master >= 75 else "LOW")
            msg = _build_gated_signal_message(
                symbol=analysis_sym, price=price,
                direction="LONG" if pump_dir else "SHORT",
                master_score=raw_master, confidence=confidence_label,
                trade=trade, oi_data=oi, confluence=confluence,
                mf_reasons=mf_all_reasons, gate_reasons=gate_reasons,
                alert_type="SETUP"
            )
            send_telegram(msg)
            _gate_mark_sent(analysis_sym, state)
            signals_sent += 1
            log.info(f"🚀 SIGNAL SENT: {analysis_sym} {direction} score={raw_master}")

            # Track ke signal tracker
            if TRACKER_MODULE:
                try:
                    _bt_dir    = "LONG" if pump_dir else "SHORT"
                    _tp_val    = float(trade.get("tp1", 0))
                    _sl_val    = float(trade.get("sl", 0))
                    _entry_val = float(trade.get("entry") or price)
                    _sane = (_bt_dir == "LONG"  and _tp_val > _entry_val) or \
                            (_bt_dir == "SHORT" and _tp_val < _entry_val)
                    if _sane and _tp_val and _sl_val:
                        on_signal_sent(
                            symbol          = analysis_sym,
                            signal_type     = "GATED_SIGNAL",
                            direction       = _bt_dir,
                            entry_price     = _entry_val,
                            tp              = _tp_val,
                            sl              = _sl_val,
                            score           = raw_master,
                            confluence_level= conf_level,
                            reasons         = gate_reasons[:3],
                        )
                    else:
                        log.warning(f"⚠️ GATED_SIGNAL sanity fail {analysis_sym}: dir={_bt_dir} entry={_entry_val} tp={_tp_val}")
                except Exception as e:
                    log.debug(f"Tracker error: {e}")

            # Kalau RETEST_WAIT → masukkan ke retest queue
            if trade.get("entry_mode") == "RETEST_WAIT" and trade.get("confirmation_zone"):
                cz = trade["confirmation_zone"]
                retest_entry = {
                    "symbol":    analysis_sym,
                    "direction": "LONG" if pump_dir else "SHORT",
                    "zone":      cz,
                    "entry":     trade.get("entry", price),
                    "sl":        trade.get("sl"),
                    "tp1":       trade.get("tp1"),
                    "tp2":       trade.get("tp2"),
                    "tp1_r":     trade.get("tp1_r", 0),
                    "tp2_r":     trade.get("tp2_r", 0),
                    "score":     raw_master,
                    "expires":   (datetime.now(timezone.utc) +
                                  timedelta(hours=GATE_COOLDOWN_HOURS)).isoformat(),
                    "notified":  False,
                }
                queue = state.get(_RETEST_QUEUE_KEY, [])
                queue = [q for q in queue if q.get("symbol") != analysis_sym]
                queue.append(retest_entry)
                state[_RETEST_QUEUE_KEY] = queue
                log.info(f"⏳ {analysis_sym} masuk retest queue: zona {cz}")

        else:
            # Gate not passed — cek apakah masuk watchlist
            missing_count = sum(1 for v in gate_results.values() if not v)
            if raw_master >= WATCHLIST_THRESHOLD and missing_count <= 2:
                needs = " + ".join(
                    k for k, v in gate_results.items() if not v
                )
                watchlist_new[analysis_sym] = {
                    "score":     raw_master,
                    "direction": direction,
                    "needs":     needs,
                    "ts":        datetime.now(timezone.utc).isoformat()[:16],
                }
            log.info(f"  {analysis_sym}: gates failed — {'; '.join(failed_reasons)}")

        time.sleep(0.3)  # rate limit guard

    # ── 3. Check retest queue ──────────────────────
    queue     = state.get(_RETEST_QUEUE_KEY, [])
    new_queue = []
    now_ts    = datetime.now(timezone.utc)
    for entry in queue:
        if entry.get("notified"):
            continue
        expires = datetime.fromisoformat(entry["expires"])
        if now_ts > expires:
            log.info(f"⏰ Retest queue expired: {entry['symbol']}")
            continue  # drop expired
        # Fetch current price
        try:
            ticker = get_binance_ticker(entry["symbol"])
            if ticker:
                cur_price = float(ticker.get("lastPrice", 0))
                if _is_price_in_zone(cur_price, entry["zone"]):
                    # PRICE IN ZONE → send ENTRY_NOW notification
                    trade_snap = {
                        "entry_mode":        "RETEST_WAIT",
                        "entry":             entry["entry"],
                        "sl":                entry["sl"],
                        "tp1":               entry["tp1"],
                        "tp2":               entry["tp2"],
                        "tp1_r":             entry["tp1_r"],
                        "tp2_r":             entry["tp2_r"],
                        "rr":                entry["tp1_r"],
                        "confirmation_zone": entry["zone"],
                        "momentum_context":  [],
                    }
                    msg2 = _build_gated_signal_message(
                        symbol    = entry["symbol"],
                        price     = cur_price,
                        direction = entry["direction"],
                        master_score = entry["score"],
                        confidence   = "HIGH",
                        trade        = trade_snap,
                        oi_data      = {},
                        confluence   = {},
                        mf_reasons   = [],
                        gate_reasons = [f"Price {cur_price:.6f} masuk zona {_fmt_zone(entry['zone']['bottom'], entry['zone']['top'])}"],
                        alert_type   = "ENTRY_NOW",
                    )
                    send_telegram(msg2)
                    entry["notified"] = True
                    signals_sent += 1
                    log.info(f"🎯 RETEST ENTRY NOTIF: {entry['symbol']} price={cur_price}")
        except Exception as e:
            log.debug(f"Retest check error {entry['symbol']}: {e}")
        new_queue.append(entry)

    state[_RETEST_QUEUE_KEY] = new_queue

    # ── 4. Update watchlist ────────────────────────
    state[_WATCHLIST_KEY] = watchlist_new

    # ── 5. Heartbeat ──────────────────────────────
    _check_heartbeat(state, signals_sent)

    _save_gate_state(state)
    log.info(f"Gated scan complete — signals sent: {signals_sent}")


def _check_heartbeat(state: dict, signals_sent: int = 0):
    """Kirim heartbeat kalau sudah >= HEARTBEAT_INTERVAL_HRS jam tanpa signal."""
    from datetime import datetime, timezone, timedelta
    now     = datetime.now(timezone.utc)
    last_hb = state.get(_LAST_HB_KEY, "")
    last_signal = max(state.get(_SENT_SIGNALS_KEY, {}).values(), default="") if state.get(_SENT_SIGNALS_KEY) else ""

    should_hb = True
    if last_hb:
        elapsed = (now - datetime.fromisoformat(last_hb)).total_seconds() / 3600
        should_hb = elapsed >= HEARTBEAT_INTERVAL_HRS

    # Kalau ada signal baru dikirim di scan ini, reset timer tapi tidak kirim HB
    if signals_sent > 0:
        state[_LAST_HB_KEY] = now.isoformat()
        return

    if should_hb:
        msg = _build_heartbeat_message(
            watchlist        = state.get(_WATCHLIST_KEY, {}),
            last_signal_ts   = last_signal[:16].replace("T", " ") if last_signal else "",
        )
        send_telegram(msg)
        state[_LAST_HB_KEY] = now.isoformat()
        log.info("💓 Heartbeat sent")

def run_scan(manual: bool = False, chat_id: str = None):
    log.info("=" * 50)
    log.info(f"🚀 Starting scan... (manual={manual})")

    # ── v12: Cek outcome sinyal sebelumnya sebelum scan ──
    if TRACKER_MODULE:
        try:
            resolved = on_scan_start(send_telegram)
            if resolved:
                log.info(f"📊 Signal tracker: {len(resolved)} signals resolved")
        except Exception as e:
            log.warning(f"Signal tracker on_scan_start error: {e}")

    btc   = get_btc_context()
    coins = screen_coins(manual=manual)

    log.info(f"BTC: {btc['environment']} | Coins screened: {len(coins)}")

    if not coins:
        log.info("No coins passed filters this scan.")
        if manual and chat_id:
            send_telegram(
                "📭 <b>Scan selesai</b> — tidak ada coin yang memenuhi filter saat ini.\n"
                "Market mungkin sedang sidewalk / volume tidak bergerak.",
                chat_id
            )
        return

    msg, enriched_coins = build_telegram_message(btc, coins)
    send_telegram(msg, chat_id if manual else None)

    # ── v12: Confirmed Entry Signal Engine ─────────────────
    # Jalankan di background — tidak block scan berikutnya
    # Backtest validasi butuh ~10-30 detik per coin
    if CONFIRMED_MODULE and enriched_coins:
        tracker_fn = on_signal_sent if TRACKER_MODULE else None
        # Kirim confirmed signal ke chat yang sama dengan peminta scan
        _confirmed_send = (lambda msg, cid=chat_id: send_telegram(msg, cid)) if (manual and chat_id) else send_telegram
        threading.Thread(
            target=run_confirmed_signal_scan,
            args=(enriched_coins, _confirmed_send, tracker_fn),
            daemon=True
        ).start()
        log.info(f"🔬 Confirmed signal scan started ({len(enriched_coins)} coins) in background")

    # ── v12: Record setiap sinyal yang dikirim ke tracker ──
    if TRACKER_MODULE:
        for coin_data in coins[:5]:
            try:
                sym  = coin_data.get("symbol", "")
                conf = coin_data.get("confluence", {})
                if not sym or not conf:
                    continue

                direction  = conf.get("direction", "NEUTRAL")
                score      = conf.get("score", 0)
                conf_level = conf.get("level", "")

                if direction not in ("PUMP", "DUMP"):
                    continue

                # Ambil price & trade plan
                price = coin_data.get("price", 0)
                trade = coin_data.get("trade", {})
                tp    = trade.get("tp1") or trade.get("tp") or 0
                sl    = trade.get("sl") or 0
                # Pakai trade plan entry (LIMIT level di OB/FVG), bukan current price
                trade_entry = float(trade.get("entry") or price)

                if not tp or not sl or not trade_entry:
                    continue

                bt_direction = "LONG" if direction == "PUMP" else "SHORT"

                # Sanity check: SHORT entry > TP, LONG entry < TP
                if bt_direction == "SHORT" and float(tp) >= trade_entry:
                    log.warning(f"⚠️ Signal sanity fail {sym}: SHORT tp={tp} >= entry={trade_entry}, skip")
                    continue
                if bt_direction == "LONG" and float(tp) <= trade_entry:
                    log.warning(f"⚠️ Signal sanity fail {sym}: LONG tp={tp} <= entry={trade_entry}, skip")
                    continue

                on_signal_sent(
                    symbol          = sym,
                    signal_type     = "SCREENER",
                    direction       = bt_direction,
                    entry_price     = trade_entry,
                    tp              = float(tp),
                    sl              = float(sl),
                    score           = score,
                    confluence_level= conf_level,
                    reasons         = conf.get("reasons", [])[:3],
                )
            except Exception as e:
                log.debug(f"Signal tracker record error {coin_data.get('symbol','')}: {e}")

    # v11: Log decisions ke learning engine
    if LEARNING_MODULE:
        for coin_data in coins[:5]:
            sym = coin_data.get("symbol", "")
            conf = coin_data.get("confluence", {})
            pp = coin_data.get("prepump", {})
            pd_ = coin_data.get("predump", {})
            if sym and conf:
                conf_level = conf.get("level", "")
                direction  = conf.get("direction", "NEUTRAL")
                score      = conf.get("score", 0)
                decision   = "ALERT" if conf_level in ("EXCELLENT", "GOOD") else "SKIP" if conf_level == "POOR" else "WATCH"
                summary    = f"confluence={conf_level}, direction={direction}, score={score}"
                try:
                    log_decision(
                        actor="SCREENER", symbol=sym, decision=decision,
                        summary=summary, score=score, confluence_level=conf_level,
                        direction=direction, reasons=conf.get("reasons", [])[:3],
                        trade_plan=conf.get("trade", {}),
                    )
                except Exception as e:
                    log.debug(f"log_decision error: {e}")

    log.info("Scan complete ✅")


def run_prepump_auto():
    """
    Auto pre-pump scan tiap 5 menit.
    Kirim alert HANYA kalau ada kandidat dengan score >= PREPUMP_ALERT_THRESHOLD (HOT).
    """
    log.info("🎯 Auto pre-pump scan triggered")
    candidates = scan_prepump_candidates()

    hot = [c for c in candidates if c["total_score"] >= PREPUMP_ALERT_THRESHOLD]

    if hot:
        msg = build_prepump_message(hot)
        send_telegram(msg)

        # ── v12: Track sinyal prepump ke signal tracker ──
        if TRACKER_MODULE:
            for c in hot:
                try:
                    trade     = c.get("trade", {})
                    tp        = trade.get("tp1") or trade.get("tp") or 0
                    sl        = trade.get("sl") or 0
                    price     = c.get("price", 0)
                    entry_val = float(trade.get("entry") or price)
                    # LONG: tp harus > entry
                    if tp and sl and entry_val and float(tp) > entry_val:
                        on_signal_sent(
                            symbol          = c["symbol"],
                            signal_type     = "PREPUMP",
                            direction       = "LONG",
                            entry_price     = entry_val,
                            tp              = float(tp),
                            sl              = float(sl),
                            score           = c["total_score"],
                            confluence_level= c.get("label", ""),
                            reasons         = c.get("reasons", [])[:3],
                        )
                    else:
                        log.warning(f"⚠️ PREPUMP sanity fail {c.get('symbol','')}: entry={entry_val} tp={tp}")
                except Exception as e:
                    log.debug(f"Prepump tracker error {c.get('symbol','')}: {e}")

        # v11: Log ke learning engine
        if LEARNING_MODULE:
            for c in hot:
                try:
                    log_decision(actor="PREPUMP", symbol=c["symbol"], decision="ALERT",
                        summary=f"score={c['total_score']}, label={c['label']}",
                        score=c["total_score"], confluence_level=c.get("label",""),
                        direction="LONG", reasons=c.get("reasons",[])[:3])
                except Exception as e:
                    log.debug(f"prepump log_decision error: {e}")
        log.info(f"🔥 Pre-pump HOT alert: {len(hot)} kandidat (score >= {PREPUMP_ALERT_THRESHOLD})")
    else:
        best = candidates[0]["total_score"] if candidates else 0
        log.info(f"Pre-pump scan: no HOT signal (best score={best}, threshold={PREPUMP_ALERT_THRESHOLD})")


def run_predump_auto():
    """
    Auto pre-dump scan tiap 5 menit.
    Kirim alert HANYA kalau ada kandidat dengan score >= PREDUMP_ALERT_THRESHOLD (HOT).
    """
    log.info("💀 Auto pre-dump scan triggered")
    candidates = scan_predump_candidates()

    hot = [c for c in candidates if c["total_score"] >= PREDUMP_ALERT_THRESHOLD]

    if hot:
        msg = build_predump_message(hot)
        send_telegram(msg)

        # ── v12: Track sinyal predump ke signal tracker ──
        if TRACKER_MODULE:
            for c in hot:
                try:
                    trade     = c.get("trade", {})
                    tp        = trade.get("tp1") or trade.get("tp") or 0
                    sl        = trade.get("sl") or 0
                    price     = c.get("price", 0)
                    entry_val = float(trade.get("entry") or price)
                    # SHORT: tp harus < entry
                    if tp and sl and entry_val and float(tp) < entry_val:
                        on_signal_sent(
                            symbol          = c["symbol"],
                            signal_type     = "PREDUMP",
                            direction       = "SHORT",
                            entry_price     = entry_val,
                            tp              = float(tp),
                            sl              = float(sl),
                            score           = c["total_score"],
                            confluence_level= c.get("label", ""),
                            reasons         = c.get("reasons", [])[:3],
                        )
                    else:
                        log.warning(f"⚠️ PREDUMP sanity fail {c.get('symbol','')}: entry={entry_val} tp={tp}")
                except Exception as e:
                    log.debug(f"Predump tracker error {c.get('symbol','')}: {e}")

        # v11: Log ke learning engine
        if LEARNING_MODULE:
            for c in hot:
                try:
                    log_decision(actor="PREDUMP", symbol=c["symbol"], decision="ALERT",
                        summary=f"score={c['total_score']}, label={c['label']}",
                        score=c["total_score"], confluence_level=c.get("label",""),
                        direction="SHORT", reasons=c.get("reasons",[])[:3])
                except Exception as e:
                    log.debug(f"predump log_decision error: {e}")
        log.info(f"🔴 Pre-dump HOT alert: {len(hot)} kandidat (score >= {PREDUMP_ALERT_THRESHOLD})")
    else:
        best = candidates[0]["total_score"] if candidates else 0
        log.info(f"Pre-dump scan: no HOT signal (best score={best}, threshold={PREDUMP_ALERT_THRESHOLD})")


def run_scalp_auto():
    """
    Auto scalp scan tiap 5 menit.
    Kirim alert HANYA kalau ada kandidat dengan score >= SCALP_ALERT_THRESHOLD (GOOD atau A+).
    """
    log.info("⚡ Auto scalp scan triggered")
    candidates = scan_scalp_candidates()

    hot = [c for c in candidates if c.get("score", 0) >= SCALP_ALERT_THRESHOLD]

    if hot:
        msg = build_scalp_message(hot)
        send_telegram(msg)

        # Track sinyal scalp ke signal tracker
        if TRACKER_MODULE:
            for c in hot:
                try:
                    trade     = c.get("trade", {})
                    tp        = trade.get("tp1") or trade.get("tp") or 0
                    sl        = trade.get("sl") or 0
                    price     = c.get("price", 0)
                    entry_val = float(trade.get("entry") or price)
                    direc     = c.get("direction", "NONE")
                    bt_dir    = "LONG" if direc == "LONG" else "SHORT"
                    # Sanity: LONG tp > entry, SHORT tp < entry
                    sane = (bt_dir == "LONG" and float(tp) > entry_val) or \
                           (bt_dir == "SHORT" and float(tp) < entry_val)
                    if tp and sl and entry_val and sane:
                        on_signal_sent(
                            symbol          = c["symbol"],
                            signal_type     = "SCALP",
                            direction       = bt_dir,
                            entry_price     = entry_val,
                            tp              = float(tp),
                            sl              = float(sl),
                            score           = c["score"],
                            confluence_level= c.get("label", ""),
                            reasons         = c.get("reasons", [])[:3],
                        )
                    else:
                        log.warning(f"⚠️ SCALP sanity fail {c.get('symbol','')}: entry={entry_val} tp={tp}")
                except Exception as e:
                    log.debug(f"Scalp tracker error {c.get('symbol','')}: {e}")

        if LEARNING_MODULE:
            for c in hot:
                try:
                    log_decision(actor="SCALP", symbol=c["symbol"], decision="ALERT",
                        summary=f"score={c['score']}, label={c['label']}",
                        score=c["score"], confluence_level=c.get("label",""),
                        direction=c.get("direction","NONE"), reasons=c.get("reasons",[])[:3])
                except Exception as e:
                    log.debug(f"scalp log_decision error: {e}")

        log.info(f"⚡ Scalp HOT alert: {len(hot)} kandidat (score >= {SCALP_ALERT_THRESHOLD})")
    else:
        best = candidates[0]["score"] if candidates else 0
        log.info(f"Scalp scan: no HOT signal (best score={best}, threshold={SCALP_ALERT_THRESHOLD})")


# ─────────────────────────────────────────────
# STANDALONE ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  CRYPTO SCREENER v13 — STARTING UP (GATED MODE)  ")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info(f"Interval  : {SCAN_INTERVAL_MINUTES} minutes")
    log.info(f"Pre-pump  : scan tiap {PREPUMP_SCAN_INTERVAL}m, alert jika score >= {PREPUMP_ALERT_THRESHOLD}")
    log.info(f"Pre-dump  : scan tiap {PREPUMP_SCAN_INTERVAL}m, alert jika score >= {PREDUMP_ALERT_THRESHOLD}")
    log.info(f"Scalp     : scan tiap {PREPUMP_SCAN_INTERVAL}m, alert jika score >= {SCALP_ALERT_THRESHOLD}")
    log.info(f"Top coins : {TOP_COINS_COUNT}")
    log.info(f"Gemini    : {'✅ Key set' if GEMINI_API_KEY else '⚠️ No key'} (manual: /analyze /ask /chart /news /macro)")
    log.info(f"NewsAPI   : {'✅ Key set' if NEWSAPI_KEY else '⚠️ No key — /news disabled'}")
    log.info(f"Risk Mgr  : {'✅ Module loaded' if RISK_MODULE else '⚠️ Module missing'}")
    log.info(f"Learning  : {'✅ Module loaded' if LEARNING_MODULE else '⚠️ Module missing — /logoutcome /lessons disabled'}")
    log.info(f"Journal   : {'✅ Module loaded' if JOURNAL_MODULE else '⚠️ Module missing — /logtrade /weeksummary disabled'}")
    log.info(f"Backtest  : {'✅ Module loaded — /backtest /btresult /btcompare /btstats' if BACKTEST_MODULE else '⚠️ Module missing — /backtest disabled'}")
    log.info(f"Tracker   : {'✅ Module loaded — auto signal tracking aktif' if TRACKER_MODULE else '⚠️ Module missing — signal tracking disabled'}")
    log.info(f"Confirmed : {'✅ Module loaded — confirmed entry signal aktif (auto tiap scan)' if CONFIRMED_MODULE else '⚠️ Module missing — confirmed signal disabled'}")

    # Start Telegram polling
    poll_thread = threading.Thread(target=polling_loop, daemon=True)
    poll_thread.start()
    log.info("📡 Telegram chat handler: RUNNING")

    # Whale Tracker (opsional)
    try:
        import whale_tracker
        whale_tracker.init(telegram_fn=send_telegram)
        log.info("🐳 Whale Tracker: LargeTradeMonitor + WalletMonitor RUNNING")
    except ImportError:
        log.warning("⚠️ whale_tracker.py tidak ditemukan — whale tracking disabled")
    except Exception as e:
        log.warning(f"⚠️ Whale Tracker gagal start: {e}")

    # First scan — pakai gated scan
    run_gated_scan()

    scheduler = BlockingScheduler(timezone="UTC")
    # v13: run_gated_scan menggantikan run_scan untuk auto scheduling
    # run_scan tetap tersedia via /scan command (manual trigger, kirim full report)
    scheduler.add_job(run_gated_scan,   "interval", minutes=SCAN_INTERVAL_MINUTES,  id="screener_scan")
    scheduler.add_job(run_prepump_auto, "interval", minutes=PREPUMP_SCAN_INTERVAL,  id="prepump_scan")
    scheduler.add_job(run_predump_auto, "interval", minutes=PREPUMP_SCAN_INTERVAL,  id="predump_scan")
    scheduler.add_job(run_scalp_auto,   "interval", minutes=PREPUMP_SCAN_INTERVAL,  id="scalp_scan")

    # Risk daily reset jam 00:00 UTC
    if RISK_MODULE:
        scheduler.add_job(risk_reset_daily, "cron", hour=0, minute=0, id="risk_daily_reset")

    log.info(
        f"⏱️ Schedulers: Scan={SCAN_INTERVAL_MINUTES}m | "
        f"PrePump/Dump/Scalp={PREPUMP_SCAN_INTERVAL}m | "
        f"Risk reset=00:00 UTC"
    )

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
