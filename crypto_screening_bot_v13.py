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
# Thread-local context: stores message_thread_id for the current handler thread
# so send_telegram() automatically replies to the same Telegram topic without
# needing to change every handler signature.
_msg_ctx = threading.local()
import requests
import numpy as np
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler

_WIB = timezone(timedelta(hours=7))

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
    from news_sentiment    import (
        get_coin_sentiment, get_macro_sentiment,
        format_sentiment_block, get_news_gate,
        get_structured_news_for_ai,
    )
    NEWS_MODULE = True
except ImportError:
    NEWS_MODULE = False
    log_tmp = logging.getLogger("v9")
    log_tmp.warning("news_sentiment.py tidak ditemukan — fitur news dinonaktifkan")

# ── v15: DeepSeek AI — primary strategist ──────
try:
    from deepseek_ai import (
        deepseek_signal_review,
        deepseek_analyze_coin,
        deepseek_free_ask,
        deepseek_macro_analysis,
        is_available as deepseek_available,
    )
    DEEPSEEK_MODULE = True
except ImportError:
    DEEPSEEK_MODULE = False
    log_tmp = logging.getLogger("v15")
    log_tmp.warning("deepseek_ai.py tidak ditemukan — DeepSeek AI dinonaktifkan")

# ── News Agent (hourly auto-fetch) ─────────
try:
    from news_agent import run_news_fetch, get_cached_news, get_active_lessons_from_news
    NEWS_AGENT_MODULE = True
except ImportError:
    NEWS_AGENT_MODULE = False

# ── Market Context ─────────────────────────
try:
    from market_context import get_market_context, format_market_context_block
    MARKET_CONTEXT_MODULE = True
except ImportError:
    MARKET_CONTEXT_MODULE = False

# ── X (Twitter) Sentiment ──────────────────
try:
    from x_sentiment import (
        get_x_coin_analysis, get_dca_signal,
        format_x_block, format_dca_block, get_x_source_status,
    )
    X_MODULE = True
except ImportError:
    X_MODULE = False
    logging.getLogger("x_sent").warning("x_sentiment.py tidak ditemukan — fitur X/DCA dinonaktifkan")
    logging.getLogger("market_ctx").warning("market_context.py tidak ditemukan — market context dinonaktifkan")

try:
    from risk_manager      import (calc_position_size, format_risk_block,
                                   format_risk_status, set_capital, set_risk_pct,
                                   set_daily_loss_limit, record_trade_result,
                                   get_risk_summary, reset_daily as risk_reset_daily,
                                   format_personal_trade_plan_block,
                                   update_capital_after_trade, is_balance_set)
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
        analyze_signal_outcomes_daily,
        get_dynamic_thresholds,
    )
    LEARNING_MODULE = True
except ImportError:
    LEARNING_MODULE = False
    get_dynamic_thresholds = lambda: {}
    logging.getLogger("v11").warning("learning_engine.py tidak ditemukan — fitur learning dinonaktifkan")

# ── v11: Trade Journal ────────────────────────
try:
    from trade_journal import (
        wizard_start, wizard_process, is_in_wizard, is_wizard_expecting_image,
        parse_oneliner, format_trade_logged, log_trade,
        get_recent_trades, format_recent_trades,
        format_weekly_summary, set_initial_balance,
        get_current_balance,
        build_trade_from_screenshot, format_shot_preview,
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
        handle_signal_bt_command as _bt_signal,
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
        format_tracker_summary, take_lesson_snapshot,
    )
    TRACKER_MODULE = True
except ImportError:
    TRACKER_MODULE = False
    logging.getLogger("v12").warning("signal_tracker.py tidak ditemukan — auto signal tracking dinonaktifkan")

# ── Manual Trade Manager ──────────────────────
try:
    from trade_manager import (
        record_trade, close_trade, get_active_trades,
        check_active_trades, format_trade_opened,
        format_trades_list, format_closed_trade, parse_trade_command,
        set_balance, set_stake_pct, format_compound_status,
    )
    TRADE_MANAGER_MODULE = True
except ImportError:
    TRADE_MANAGER_MODULE = False
    logging.getLogger("trade").warning("trade_manager.py tidak ditemukan — /trade dinonaktifkan")

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

# ── v14: Market Regime & Advanced Candle Patterns ─
try:
    from market_regime import (
        detect_candle_patterns,
        detect_market_regime,
        calculate_bb_squeeze,
        detect_volume_coil,
        detect_sudden_breakout,
        calculate_adx,
    )
    MARKET_REGIME_MODULE = True
except ImportError:
    MARKET_REGIME_MODULE = False
    logging.getLogger("v14").warning("market_regime.py tidak ditemukan — candle patterns & regime dinonaktifkan")
    def detect_candle_patterns(c): return {"pattern": "NONE", "direction": "NEUTRAL", "strength": 0, "detail": "", "patterns_found": []}
    def detect_market_regime(c): return {"regime": "UNKNOWN", "adx": 0, "squeeze": False, "detail": "", "is_trending": False, "is_ranging": False, "breakout_confirmed": False, "breakout_direction": "NONE"}
    def calculate_bb_squeeze(c): return {"squeeze": False, "width_pct": 50, "bb_width": 0, "expanding": False, "squeeze_bars": 0}
    def detect_volume_coil(c, lookback=10): return {"coiling": False, "spike_detected": False, "compression_bars": 0, "vol_ratio": 1.0, "detail": ""}
    def detect_sudden_breakout(c, **kw): return {"sudden_breakout": False, "direction": "NONE", "vol_spike": 1.0, "range_break_pct": 0.0, "detail": "", "was_consolidating": False}
    def calculate_adx(c, period=14): return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}

# ── v16: Reversal Pattern Engine (V-Shape + Quasimodo) ──────────
try:
    from reversal_patterns import detect_v_shape, detect_qm_pattern
    REVERSAL_MODULE = True
except ImportError:
    REVERSAL_MODULE = False
    logging.getLogger("v16").warning("reversal_patterns.py tidak ditemukan — V-Shape & QM detector dinonaktifkan")
    def detect_v_shape(c, *a, **k): return {"type": "NONE", "direction": "NONE", "stage": "NONE", "score": 0, "entry_ref": None, "invalidation": None, "zone": None, "reasons": [], "meta": {}}
    def detect_qm_pattern(c, *a, **k): return {"type": "NONE", "direction": "NONE", "stage": "NONE", "score": 0, "entry_ref": None, "invalidation": None, "zone": None, "reasons": [], "meta": {}}

# ── v15: Signal Chat / Discussion + Trading-Style Learning ──────
try:
    import signal_chat
    SIGNAL_CHAT_MODULE = True
except ImportError:
    SIGNAL_CHAT_MODULE = False
    logging.getLogger("v15").warning("signal_chat.py tidak ditemukan — diskusi sinyal dinonaktifkan")

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

# ── Liquidation Cascade Tracker ───────────────
try:
    from liquidation_tracker import start_liq_tracker, get_liq_data
    LIQ_TRACKER_MODULE = True
except ImportError:
    LIQ_TRACKER_MODULE = False
    def get_liq_data(symbol): return {}

# ── Glassnode Macro Filter ─────────────────────
_GLASSNODE_API_KEY = os.environ.get("GLASSNODE_API_KEY", "")
_glassnode_cache: dict = {}   # {"btc_netflow": {"z": 0.0, "ts": 0}}

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY          = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL            = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_VISION_MODEL     = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
# Model vision cadangan kalau model utama error (mis. di-decommission Groq).
GROQ_VISION_MODEL_FB  = os.getenv("GROQ_VISION_MODEL_FALLBACK", "meta-llama/llama-4-maverick-17b-128e-instruct")
GROQ_API_URL          = "https://api.groq.com/openai/v1/chat/completions"
CLAUDE_MODEL          = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# v9: New module env vars
NEWSAPI_KEY           = os.getenv("NEWSAPI_KEY", "")

# v15: DeepSeek — primary AI strategist sebelum sinyal dikirim ke Telegram
DEEPSEEK_API_KEY      = os.getenv("DEEPSEEK_API_KEY", "")

# v15: Topic routing — pisahkan pesan ke topic (thread) berbeda dalam satu grup
# Set THREAD_ID tiap topic di .env (ambil dari URL: t.me/c/groupid/THREAD_ID)
SIGNAL_THREAD_ID        = os.getenv("SIGNAL_THREAD_ID", "")        or None
MARKET_UPDATE_THREAD_ID = os.getenv("MARKET_UPDATE_THREAD_ID", "") or None
TRADE_REPORT_THREAD_ID  = os.getenv("TRADE_REPORT_THREAD_ID", "")  or None

# AI priority: DeepSeek (signal review + analyze + ask) → Gemini (chart image) → Groq (fallback)

SCAN_INTERVAL_MINUTES   = 10
TOP_COINS_COUNT         = 5
PREPUMP_SCAN_INTERVAL   = 5    # menit — scan cepat, alert HANYA kalau HOT
PREPUMP_ALERT_THRESHOLD = 65   # score >= 65 → kirim alert
PREDUMP_ALERT_THRESHOLD = 65   # score >= 65 → kirim alert

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
# CATATAN: funding_rate sudah dalam satuan PERSEN (raw fundingRate * 100 di
# get_open_interest). Funding normal Binance ~±0.01%/8h, jadi threshold harus
# pakai magnitude persen yang realistis — bukan -0.01/-0.03 yang ter-trigger di
# kondisi pasar normal dan over-score sinyal.
FUNDING_SQUEEZE_THRESH  = -0.05   # funding < -0.05% → squeeze potential
FUNDING_EXTREME_THRESH  = -0.10   # funding < -0.10% → extreme squeeze
OI_SURGE_THRESH         = 5.0     # OI naik >5% dalam 1h
ATR_COIL_RATIO          = 0.015   # price range < 1.5% ATR → coiling
MOMENTUM_RSI_THRESH     = 55      # RSI > 55 → momentum building
VOLUME_SURGE_MULT       = 2.0     # volume spike 2x normal

# Pre-dump thresholds (opposite of pre-pump) — juga dalam satuan PERSEN
FUNDING_DUMP_THRESH     = 0.05    # funding > +0.05% → long squeeze potential
FUNDING_DUMP_EXTREME    = 0.10    # funding > +0.10% → extreme long squeeze
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
SCALP_MIN_SCORE         = 55      # minimal score buat scalp signal
SCALP_ALERT_THRESHOLD   = 62      # score >= 62 → auto alert

# ── v16 — REVERSAL PATTERN ENGINE (V-Shape + Quasimodo) ──────────
# Scanner level config (threshold internal pola ada di reversal_patterns.py).
REVERSAL_SCAN_ENABLED      = os.getenv("REVERSAL_SCAN_ENABLED", "1") not in ("0", "false", "False")
REVERSAL_SCAN_INTERVAL     = 5     # menit — sama cadence dgn prepump/predump
REVERSAL_EARLY_MIN_SCORE   = 60    # score minimum buat kirim EARLY heads-up
REVERSAL_CONFIRM_MIN_SCORE = 66    # score minimum buat kirim CONFIRM/IGNITION
REVERSAL_COOLDOWN_HOURS    = 4     # cooldown re-alert stage yg sama per pola
REVERSAL_STATE_FILE        = "reversal_state.json"
# Momentum ignition (overlay LIVE di TF mikro) — "detik-detik mau pump/dump"
IGNITION_ENABLED           = os.getenv("REVERSAL_IGNITION", "1") not in ("0", "false", "False")
IGNITION_TF                = "5m"
IGNITION_RANGE_MULT        = 1.8   # range candle ≥ 1.8x rata-rata
IGNITION_VOL_MULT          = 2.0   # volume ≥ 2.0x rata-rata
IGNITION_NEAR_ZONE_PCT     = 1.5   # harga ≤ 1.5% dari entry_ref pola

# ── v16 — MARKET PULSE (status SEMUA koin → Market Update thread) ─
MARKET_PULSE_ENABLED       = os.getenv("MARKET_PULSE_ENABLED", "1") not in ("0", "false", "False")
MARKET_PULSE_INTERVAL      = int(os.getenv("MARKET_PULSE_INTERVAL", "60"))  # menit
MARKET_PULSE_ADX_MIN       = 20    # ADX minimum buat dianggap trending (pump/dump)


# ── EFFECTIVE THRESHOLDS (overlay dynamic_thresholds.json dari learning engine) ──
# evolve_thresholds() menulis override ke dynamic_thresholds.json; helper ini
# memakai nilai itu kalau ada, kalau tidak fallback ke konstanta di atas.
def _eff_scalp_min_score() -> int:
    try:
        return int(get_dynamic_thresholds().get("SCALP_MIN_SCORE", SCALP_MIN_SCORE))
    except Exception:
        return SCALP_MIN_SCORE

def _eff_predump_threshold() -> int:
    try:
        return int(get_dynamic_thresholds().get("PREDUMP_ALERT_THRESHOLD", PREDUMP_ALERT_THRESHOLD))
    except Exception:
        return PREDUMP_ALERT_THRESHOLD

def _eff_prepump_threshold() -> int:
    try:
        return int(get_dynamic_thresholds().get("PREPUMP_ALERT_THRESHOLD", PREPUMP_ALERT_THRESHOLD))
    except Exception:
        return PREPUMP_ALERT_THRESHOLD

# ── SIGNAL GATE ──────────────────────────────────────────────────────────────
GATE_MASTER_SCORE_MIN   = 65      # min score buat sinyal lolos — lebih tinggi = lebih selektif
GATE_MONEYFLOW_TF_MIN   = 2       # minimal 2 TF harus align money flow (4H+1H atau 1H+15M)
GATE_BT_PF_MIN          = 1.0     # backtest profit factor minimum
GATE_REQUIRE_ENTRY_MODE = True    # entry mode HARUS jelas (MOMENTUM_NOW atau RETEST_WAIT)
GATE_COOLDOWN_HOURS     = 4       # cooldown per coin setelah sinyal dikirim
HEARTBEAT_INTERVAL_HRS  = 4       # interval "no signal" update
WATCHLIST_THRESHOLD     = 60      # master score ambang batas masuk watchlist

# Gemini
_GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_URL   = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent"
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

        # Gunakan touches yang sudah dihitung di detect_order_blocks bila ada,
        # fallback ke hitung ulang kalau field tidak tersedia
        if "touches" in ob:
            touches = ob["touches"]
        else:
            touches = sum(1 for c in candles[-30:] if c["low"] <= ob_top and c["high"] >= ob_bottom)

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

    _c_regime_4h = tf_4h.get("market_regime", {}).get("regime", "UNKNOWN")
    _c_adx_4h    = tf_4h.get("market_regime", {}).get("adx", 0)

    coin_name = symbol.replace("USDT", "")
    prompt = f"""Kamu adalah trader crypto senior. Tugasmu bukan menjelaskan ulang data, tapi langsung kasih VERDICT actionable.
ATURAN KERAS: Market Regime RANGING → wajib SKIP (fakeout sangat tinggi).
ATURAN KERAS: TP < 2R dari entry → wajib SKIP atau WAIT RETEST.
ATURAN KERAS: Tidak ada OB/FVG fresh → wajib WAIT RETEST bukan ENTRY NOW.

DATA {coin_name} @ ${price}:
- Signal: {confluence['direction']} | Score: {confluence['score']}/100
- Market Regime → 4H: {_c_regime_4h} (ADX={_c_adx_4h:.0f})
- 4H: {s4.get('trend','?')} | 1H: {s1.get('trend','?')} | 15M rejection: {rej.get('type','NONE')}
- FVG 15M: {fvg.get('fvg_type','NONE')} | OI: {oi_data.get('oi_change_pct','N/A')}% | Funding: {oi_data.get('funding_rate','N/A')}%
- L/S global: {oi_data.get('ls_ratio','N/A')} ({oi_data.get('ls_bias','N/A')}) | Top Trader: {oi_data.get('top_ls_ratio','N/A')} ({oi_data.get('top_ls_bias','N/A')})
- Basis: {oi_data.get('perp_spot_basis','N/A')}% | Taker: {oi_data.get('taker_bias','N/A')} | Vol 4H: {va4.get('multiplier',1):.1f}x
- Liquidity: {liq_ctx.strip() if liq_ctx else 'none'}
- OB: {ob_ctx.strip()}
- Setup:{prepump_ctx}{predump_ctx}{scalp_ctx}{swing_ctx}

Jawab dalam Bahasa Indonesia, max 5-6 kalimat, format wajib:

🎯 **[LONG NOW / SHORT NOW / WAIT RETEST / SKIP]**
Alasan: [1-2 alasan paling decisive saja]

📋 **Trade Plan**: Entry ... | SL ... | TP ... (wajib ≥ 2R) | Konfirmasi: ... (hanya jika bukan SKIP)

⚠️ **Invalidasi**: [level/kondisi spesifik yang batalkan setup — pakai angka]"""

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

    coin_name = symbol.replace("USDT", "")
    mf4  = tf_4h.get("money_flow", {})
    mf1  = tf_1h.get("money_flow", {})
    mf15 = tf_15m.get("money_flow", {})

    _g_regime_4h = tf_4h.get("market_regime", {}).get("regime", "UNKNOWN")
    _g_regime_1h = tf_1h.get("market_regime", {}).get("regime", "UNKNOWN")
    _g_adx_4h    = tf_4h.get("market_regime", {}).get("adx", 0)

    prompt = f"""Kamu adalah trader crypto senior — tugasmu BUKAN menjelaskan ulang data, tapi langsung kasih VERDICT actionable.
ATURAN KERAS: Kalau Market Regime RANGING → wajib SKIP (ranging = noise tinggi, fakeout).
ATURAN KERAS: Kalau TP < 2R dari entry → wajib SKIP atau WAIT RETEST.
ATURAN KERAS: Kalau tidak ada OB/FVG fresh sebagai entry zone → wajib WAIT RETEST, bukan ENTRY NOW.
{sym_memory_ctx + chr(10) if sym_memory_ctx else ""}
DATA {coin_name} @ ${price}:
- Signal: {confluence['direction']} | Score: {confluence['score']}/100
- Market Regime → 4H: {_g_regime_4h} (ADX={_g_adx_4h:.0f}) | 1H: {_g_regime_1h}
- 4H: {s4.get('trend','?')} | 1H: {s1.get('trend','?')} | 15M rejection: {rej.get('type','NONE')}
- FVG 15M: {fvg.get('fvg_type','NONE')} | OI: {oi_data.get('oi_change_pct','N/A')}% | Funding: {oi_data.get('funding_rate','N/A')}%
- MF → 4H: {mf4.get('bias','?')}/{mf4.get('strength','?')} CVD{mf4.get('cvd_pct',0):+.1f}% | 1H: {mf1.get('bias','?')} CVD{mf1.get('cvd_pct',0):+.1f}% | 15M: {mf15.get('bias','?')}
- L/S global: {oi_data.get('ls_ratio','N/A')} ({oi_data.get('ls_bias','N/A')}) | Top Trader L/S: {oi_data.get('top_ls_ratio','N/A')} ({oi_data.get('top_ls_bias','N/A')})
- Perp-Spot Basis: {oi_data.get('perp_spot_basis','N/A')}% | Taker: {oi_data.get('taker_bias','N/A')} ({oi_data.get('taker_buy_sell_ratio','N/A')}) | Vol 4H: {va4.get('multiplier',1):.1f}x
- EMA9/21 1H: {tf_1h.get('ema9',0):.4f} / {tf_1h.get('ema21',0):.4f} | EMA21 4H: {tf_4h.get('ema21',0):.4f}
- Liquidity: {liq_ctx.strip() if liq_ctx else 'none'}
- OB: {ob_ctx.strip()}
- Setup:{prepump_ctx}{predump_ctx}{scalp_ctx}{swing_ctx}

Jawab dalam Bahasa Indonesia, max 5-6 kalimat total.
JANGAN pakai markdown (**bold** atau *italic*) — gunakan emoji sebagai pengganti.
Format wajib:

🎯 [LONG NOW / SHORT NOW / WAIT RETEST / SKIP]
Alasan: [1-2 alasan paling decisive]

📋 Trade Plan: Entry ... | SL ... | TP ... (wajib ≥ 2R) | Konfirmasi: ... (isi hanya jika bukan SKIP)

⚠️ Invalidasi: [level/kondisi spesifik yang batalkan setup — pakai angka]"""

    return _gemini_request({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 500}
    }) or "⚠️ Gemini tidak merespons saat ini, coba lagi sebentar."


def gemini_free_ask(question: str) -> str:
    """Jawab pertanyaan crypto via Gemini."""
    if not GEMINI_API_KEY:
        return "⚠️ GEMINI_API_KEY belum diset di .env"

    prompt = f"""Lo asisten trading crypto pribadi-nya user, ngobrol santai kayak temen yang jago (boleh 'gue/lo').
Jawab Bahasa Indonesia, langsung ke poin & spesifik, maksimal 5 kalimat. Jangan ngasih definisi/teori dasar kecuali diminta:

{question}"""

    return _gemini_request({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 800}
    }) or "⚠️ Gemini tidak merespons saat ini."


# ─────────────────────────────────────────────
# v14: GROQ AI — Cepat, Free Tier, OpenAI-compatible
# Pro: inference tercepat (~200 tok/s), free tier 30 RPM, bagus untuk analisa panjang
# Con: tidak ada web search grounding, context window lebih kecil dari Gemini 2.5
# ─────────────────────────────────────────────

def _groq_request(messages: list, max_tokens: int = 1800, temperature: float = 0.65) -> str:
    """
    Call Groq API (OpenAI-compatible endpoint).
    Model: llama-3.3-70b-versatile — reasoning bagus, context 128k, gratis.
    """
    if not GROQ_API_KEY:
        return ""

    for attempt in range(3):
        try:
            r = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       GROQ_MODEL,
                    "messages":    messages,
                    "max_tokens":  max_tokens,
                    "temperature": temperature,
                },
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            elif r.status_code == 429:
                wait = 15 * (2 ** attempt)
                log.warning(f"Groq 429 rate limit (attempt {attempt+1}), retry in {wait}s...")
                time.sleep(wait)
            elif r.status_code in (503, 502):
                time.sleep(10 * (attempt + 1))
            else:
                log.warning(f"Groq API error {r.status_code}: {r.text[:200]}")
                return ""
        except Exception as e:
            log.warning(f"Groq exception (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(8)

    log.warning("Groq: max retries reached.")
    return ""


def _groq_vision_request(image_b64: str, prompt: str, mime: str = "image/jpeg",
                         max_tokens: int = 700, temperature: float = 0.0,
                         model: str = None) -> tuple:
    """
    Call Groq vision model (Llama 4 multimodal) dengan satu gambar inline (base64).
    Dipakai untuk baca screenshot order-details → ekstrak field trade.

    Return (content, error). Kalau sukses → (text, ""). Kalau gagal → ("", alasan).
    """
    if not GROQ_API_KEY:
        return "", "GROQ_API_KEY belum diset"

    model = model or GROQ_VISION_MODEL
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ],
    }]
    last_err = "unknown"
    for attempt in range(3):
        try:
            r = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       model,
                    "messages":    messages,
                    "max_tokens":  max_tokens,
                    "temperature": temperature,
                },
                timeout=40,
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip(), ""
            elif r.status_code == 429:
                last_err = "rate limit (429)"
                wait = 12 * (2 ** attempt)
                log.warning(f"Groq vision 429 (attempt {attempt+1}), retry in {wait}s...")
                time.sleep(wait)
            elif r.status_code in (503, 502):
                last_err = f"server {r.status_code}"
                time.sleep(8 * (attempt + 1))
            else:
                body = r.text[:160].replace("\n", " ")
                log.warning(f"Groq vision error {r.status_code} (model={model}): {body}")
                return "", f"API {r.status_code}: {body}"
        except Exception as e:
            last_err = str(e)
            log.warning(f"Groq vision exception (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(6)
    log.warning(f"Groq vision: max retries reached (model={model}).")
    return "", last_err


def groq_analyze_coin(symbol: str, confluence: dict, tf_4h: dict, tf_1h: dict,
                       tf_15m: dict, oi_data: dict, price: float,
                       prepump: dict = None, predump: dict = None,
                       scalp: dict = None, swing: dict = None,
                       realtime: dict = None) -> str:
    """
    Analisa coin via Groq (Llama 3.3 70B) — dipanggil dari /analyze.
    Lebih cepat dan lebih panjang dari Gemini free tier.
    """
    if not GROQ_API_KEY:
        return ""

    s4  = tf_4h.get("structure", {})
    s1  = tf_1h.get("structure", {})
    rej = tf_15m.get("rejection", {})
    fvg = tf_15m.get("fvg", {})
    ob4 = tf_4h.get("order_blocks", {})
    ob1 = tf_1h.get("order_blocks", {})
    va4 = tf_4h.get("volume_anomaly", {})
    mf4 = tf_4h.get("money_flow", {})
    mf1 = tf_1h.get("money_flow", {})
    mf15 = tf_15m.get("money_flow", {})
    signals_summary = "\n".join(confluence.get("reasons", [])[:10])

    candles_4h = tf_4h.get("candles", [])
    candles_1h = tf_1h.get("candles", [])
    ob_ctx = _build_ob_mitigation_context(ob4, ob1, price, candles_4h, candles_1h)

    liq_1h   = tf_1h.get("liquidity", {})
    sweep_1h = tf_1h.get("sweep", {})
    sweep_15m = tf_15m.get("sweep", {})
    tl_sup = tf_4h.get("trendline_sup", {})
    tl_res = tf_4h.get("trendline_res", {})

    liq_ctx = ""
    if liq_1h.get("nearest_eqh"):
        eqh = liq_1h["nearest_eqh"]
        liq_ctx += f"- EQH (target LONG): {eqh['distance_pct']:.1f}% di atas (level {eqh['count']} touches)\n"
    if liq_1h.get("nearest_eql"):
        eql = liq_1h["nearest_eql"]
        liq_ctx += f"- EQL (target SHORT): {eql['distance_pct']:.1f}% di bawah (level {eql['count']} touches)\n"
    if sweep_1h.get("swept"):
        liq_ctx += f"- 1H Liquidity Sweep: {sweep_1h['sweep_type']} (recovery {sweep_1h['recovery_strength']:.0f}%)\n"
    if sweep_15m.get("swept"):
        liq_ctx += f"- 15M Liquidity Sweep: {sweep_15m['sweep_type']} (recovery {sweep_15m['recovery_strength']:.0f}%)\n"
    if tl_sup.get("valid"):
        liq_ctx += f"- Trendline Support: {tl_sup['distance_pct']:+.1f}% ({tl_sup['touches']} touches, {tl_sup['direction']})\n"
    if tl_res.get("valid"):
        liq_ctx += f"- Trendline Resist : {tl_res['distance_pct']:+.1f}% ({tl_res['touches']} touches, {tl_res['direction']})\n"

    pp_ctx = ""
    if prepump and prepump.get("total_score", 0) >= 35:
        pp_ctx = (f"\nPre-Pump Score: {prepump['total_score']}/100 ({prepump['label']})\n"
                  f"- Funding: {prepump['funding_score']}/30 | Momentum: {prepump['momentum_score']}/35 | OI+PA: {prepump['oi_pa_score']}/35")
    pd_ctx = ""
    if predump and predump.get("total_score", 0) >= 35:
        pd_ctx = (f"\nPre-Dump Score: {predump['total_score']}/100 ({predump['label']})\n"
                  f"- Funding: {predump['funding_score']}/30 | Momentum: {predump['momentum_score']}/35 | OI+PA: {predump['oi_pa_score']}/35")
    sc_ctx = ""
    if scalp and scalp.get("score", 0) >= 45:
        sc_ctx = f"\nScalp: {scalp['label']} (score {scalp['score']}/100, arah {scalp['direction']})"
    sw_ctx = ""
    if swing and swing.get("score", 0) >= 45:
        sw_ctx = f"\nSwing: {swing['label']} (score {swing['score']}/100, est hold {swing.get('hold_estimate','')})"

    rt_ctx = ""
    if realtime and not realtime.get("error"):
        rt_ctx = (f"\nReal-time Momentum (1M candles):\n"
                  f"- Price velocity 15m: {realtime.get('velocity_15m', 0):+.2f}%\n"
                  f"- Volume burst: {realtime.get('vol_burst', 1):.1f}x vs 10m avg\n"
                  f"- Momentum label: {realtime.get('momentum_label', 'N/A')}\n"
                  f"- Short-term bias: {realtime.get('short_bias', 'NEUTRAL')}")

    sym_memory_ctx = ""
    if SYMBOL_MEMORY_MODULE:
        try:
            sym_memory_ctx = build_symbol_context_block(symbol)
        except Exception:
            pass

    coin_name = symbol.replace("USDT", "")

    system_msg = ("Kamu adalah trader crypto senior spesialis SMC dan order flow. "
                  "Tugasmu adalah memberi VERDICT yang langsung actionable — bukan menjelaskan ulang data. "
                  "Data teknikal sudah ditampilkan ke user, kamu cukup beri judgment: bisa entry atau tidak, kenapa, dan trade plan-nya. "
                  "WAJIB SKIP kalau market RANGING (tidak ada trend jelas) — setup di ranging market = fakeout. "
                  "WAJIB SKIP kalau R:R < 2:1 (TP terlalu dekat dari entry vs SL). "
                  "WAJIB WAIT RETEST kalau tidak ada OB/FVG yang fresh sebagai entry zone. "
                  "Jawab singkat, padat, Bahasa Indonesia. Max 5-6 kalimat.")

    rsi_4h  = tf_4h.get("rsi", 0)
    rsi_1h  = tf_1h.get("rsi", 0)
    rsi_15m = tf_15m.get("rsi", 0)
    atr_4h  = tf_4h.get("atr", 0)
    va1     = tf_1h.get("volume_anomaly", {})
    va15    = tf_15m.get("volume_anomaly", {})

    # Beri label RSI agar AI lebih mudah interpret
    def _rsi_label(v):
        if v >= 70: return "OB"
        if v >= 60: return "bullish"
        if v <= 30: return "OS"
        if v <= 40: return "bearish"
        return "neutral"

    _regime_4h  = tf_4h.get("market_regime", {}).get("regime", "UNKNOWN")
    _regime_1h  = tf_1h.get("market_regime", {}).get("regime", "UNKNOWN")
    _adx_4h     = tf_4h.get("market_regime", {}).get("adx", 0)

    user_msg = f"""{sym_memory_ctx + chr(10) if sym_memory_ctx else ""}
DATA: {coin_name} @ ${price}
- Signal: {confluence['direction']} | Score: {confluence['score']}/100
- Market Regime → 4H: {_regime_4h} (ADX={_adx_4h:.0f}) | 1H: {_regime_1h}
- 4H: {s4.get('trend','?')} | 1H: {s1.get('trend','?')} | 15M Rejection: {rej.get('type','NONE')}
- RSI → 4H: {rsi_4h:.0f} ({_rsi_label(rsi_4h)}) | 1H: {rsi_1h:.0f} ({_rsi_label(rsi_1h)}) | 15M: {rsi_15m:.0f} ({_rsi_label(rsi_15m)})
- ATR 4H: {atr_4h:.4f} ({atr_4h/price*100:.2f}% dari harga) — ukuran candle normal
- FVG 15M: {fvg.get('fvg_type','NONE')} | OI: {oi_data.get('oi_change_pct','N/A')}% | Funding: {oi_data.get('funding_rate','N/A')}%
- Money Flow → 4H: {mf4.get('bias','?')}/{mf4.get('strength','?')} CVD{mf4.get('cvd_pct',0):+.1f}% | 1H: {mf1.get('bias','?')} CVD{mf1.get('cvd_pct',0):+.1f}% | 15M: {mf15.get('bias','?')}
- Volume → 4H: {va4.get('multiplier',1):.1f}x | 1H: {va1.get('multiplier',1):.1f}x | 15M: {va15.get('multiplier',1):.1f}x
- L/S global: {oi_data.get('ls_ratio','N/A')} ({oi_data.get('ls_bias','N/A')}) | Top Trader: {oi_data.get('top_ls_ratio','N/A')} ({oi_data.get('top_ls_bias','N/A')})
- Perp-Spot Basis: {oi_data.get('perp_spot_basis','N/A')}% | Taker: {oi_data.get('taker_bias','N/A')} ({oi_data.get('taker_buy_sell_ratio','N/A')})
- EMA21 1H: {tf_1h.get('ema21',0):.5g} | EMA21 4H: {tf_4h.get('ema21',0):.5g}
- Liquidity: {liq_ctx.strip() if liq_ctx else 'none'}
- OB: {ob_ctx.strip()}{pp_ctx}{pd_ctx}{sc_ctx}{sw_ctx}{rt_ctx}
- Confluence signals: {signals_summary}

INSTRUKSI:
Berikan analisa singkat dalam Bahasa Indonesia — JANGAN ulangi data di atas, langsung ke kesimpulan dan judgment kamu.
Kalau Market Regime = RANGING → wajib SKIP (jelaskan kenapa ranging = bahaya fakeout).
Kalau TP < 2R dari entry → wajib SKIP atau WAIT RETEST.

Format WAJIB (max 5-6 kalimat total):

🎯 **[LONG NOW / SHORT NOW / WAIT RETEST / SKIP]**
Kenapa verdict ini? Sebutkan 1-2 alasan PALING KUAT (bukan semua indikator, cukup yang decisive).

📋 **Trade Plan** (isi hanya jika bukan SKIP, wajib cek R:R ≥ 2:1):
Entry: ... | SL: ... | TP: ... (harus ≥ 2R) | Konfirmasi: ...

⚠️ **Risk / Invalidasi**: Level atau kondisi spesifik yang membatalkan setup ini."""

    result = _groq_request(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": user_msg},
        ],
        max_tokens=500,
        temperature=0.55,
    )
    return result or ""


def groq_free_ask(question: str) -> str:
    """Jawab pertanyaan crypto via Groq (Llama 70B) — lebih cepat dan lebih detail."""
    if not GROQ_API_KEY:
        return ""

    system_msg = ("Lo asisten trading crypto pribadi-nya user, ngobrol santai kayak temen yang "
                  "jago (boleh 'gue/lo'). Jawab Bahasa Indonesia, langsung ke poin & spesifik, "
                  "maksimal ~5 kalimat. JANGAN ngasih definisi/teori dasar kecuali diminta — "
                  "fokus jawab pertanyaannya. Jujur kalau nggak yakin.")
    result = _groq_request(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": question},
        ],
        max_tokens=1200,
        temperature=0.6,
    )
    return result or ""


def groq_signal_insight(
    symbol: str, direction: str, master_score: int,
    gate_reasons: list, trade: dict,
    tf_4h: dict, tf_1h: dict, tf_15m: dict, oi_data: dict,
) -> str:
    """
    AI commentary singkat untuk confirmed/gated signal — dipanggil otomatis saat sinyal lolos gate.
    Fokus: kenapa sinyal ini valid SEKARANG + satu risk utama.
    Max 3 kalimat — tidak ulangi data, langsung ke judgment.
    """
    if not GROQ_API_KEY:
        return ""

    coin    = symbol.replace("USDT", "")
    s4      = tf_4h.get("structure", {})
    s1      = tf_1h.get("structure", {})
    mf4     = tf_4h.get("money_flow", {})
    mf1     = tf_1h.get("money_flow", {})
    mf15    = tf_15m.get("money_flow", {})
    cp15    = tf_15m.get("candle_patterns", {})
    cp1h    = tf_1h.get("candle_patterns", {})
    em      = trade.get("entry_mode", "")
    entry   = trade.get("entry", 0)
    tp1     = trade.get("tp1", 0)
    sl      = trade.get("sl", 0)
    is_long = "LONG" in direction or "PUMP" in direction
    bias_word = "LONG/bullish" if is_long else "SHORT/bearish"

    gate_str  = " | ".join(gate_reasons[:3])
    cp15_str  = f"{cp15.get('pattern','NONE')} ({cp15.get('direction','-')})" if cp15.get("pattern") not in (None, "NONE") else "tidak ada"
    cp1h_str  = f"{cp1h.get('pattern','NONE')} ({cp1h.get('direction','-')})" if cp1h.get("pattern") not in (None, "NONE") else "tidak ada"

    user_msg = (
        f"Setup {direction} ({bias_word}) {coin}, score {master_score}/100.\n"
        f"Gate lolos: {gate_str}\n"
        f"Entry mode: {em} | Entry: {entry} | TP1: {tp1} | SL: {sl}\n"
        f"Struktur — 4H: {s4.get('trend','?')} | 1H: {s1.get('trend','?')}\n"
        f"Money Flow — 4H: {mf4.get('bias','?')}/{mf4.get('strength','?')} CVD{mf4.get('cvd_pct',0):+.1f}% | "
        f"1H: {mf1.get('bias','?')} CVD{mf1.get('cvd_pct',0):+.1f}% | 15M: {mf15.get('bias','?')}\n"
        f"Candle — 15M: {cp15_str} | 1H: {cp1h_str}\n"
        f"Derivatif — Funding {oi_data.get('funding_rate','N/A')}% | OI {oi_data.get('oi_change_pct','N/A')}% | "
        f"L/S {oi_data.get('ls_ratio','N/A')} ({oi_data.get('ls_bias','N/A')}) | "
        f"Top Trader {oi_data.get('top_ls_ratio','N/A')} ({oi_data.get('top_ls_bias','N/A')}) | "
        f"Basis {oi_data.get('perp_spot_basis','N/A')}%\n\n"
        f"Kamu reviewer independen — TUGASMU mengkritik, bukan menjual setup. "
        f"Jawab dalam Bahasa Indonesia, format persis seperti ini (3 baris, tiap baris diawali emoji):\n"
        f"✅ EDGE: kenapa setup {bias_word} ini punya edge nyata SEKARANG — confluence faktor mana yg paling kuat & saling mendukung (bukan sekadar ngulang data, tapi APA artinya).\n"
        f"⚠️ KONFLIK: sebutkan faktor yang KONTRA arah trade (mis. candle/MF/struktur TF yg berlawanan). Kalau ada konflik serius, bilang terus terang setup ini lemah. Kalau benar-benar clean, bilang 'tidak ada konflik berarti'.\n"
        f"🛑 INVALIDASI: di level/kondisi harga SPESIFIK apa thesis ini batal (pakai angka, acuan ke SL {sl} atau level struktur). Bukan jawaban umum 'kalau sentimen berubah'.\n"
        f"Maksimal 1-2 kalimat per baris. Tajam dan jujur. JANGAN markdown **bold**/*italic* — plain text + emoji saja."
    )

    result = _groq_request(
        messages=[
            {"role": "system", "content": (
                "Kamu trader crypto senior yang bertugas sebagai DEVIL'S ADVOCATE untuk setiap setup. "
                "Kamu tidak takut bilang sebuah sinyal lemah kalau datanya konflik. "
                "Selalu sebut level harga konkret untuk invalidasi. Bahasa Indonesia, plain text tanpa markdown, langsung ke poin."
            )},
            {"role": "user",   "content": user_msg},
        ],
        max_tokens=320,
        temperature=0.4,
    )
    return result or ""


def gemini_analyze_chart_image(image_base64: str, mime_type: str = "image/jpeg") -> str:
    """Analisa chart image via Gemini Vision."""
    if not GEMINI_API_KEY:
        return "⚠️ GEMINI_API_KEY belum di-set."

    prompt = """Kamu adalah analis crypto profesional SMC (Smart Money Concepts). Bedah chart ini secara KRITIS dan berikan verdict actionable.
JANGAN pakai markdown **bold** atau *italic* — gunakan emoji sebagai pengganti header.

Analisa WAJIB mencakup:

📊 MARKET STRUCTURE — Bullish/Bearish/Ranging? Ada CHoCH atau BoS? Di mana last BoS-nya?
🧱 ORDER BLOCKS & FVG — OB yang FRESH (belum mitigated). Ada FVG yang belum diisi? Di range harga berapa?
💧 LIQUIDITY — Di mana liquidity pool terkumpul? Equal highs/lows? Sweep sudah terjadi atau belum?
⚠️ RISIKO & INVALIDASI — Apa yang bisa bikin setup ini GAGAL? Level invalidasi-nya?
🎯 TRADE PLAN (jika ada setup valid): Entry zone | TP1 | TP2 | SL | R:R ratio
🏁 VERDICT — Pilih SATU: TRADE / SKIP / WAIT
   TRADE: setup valid, entry sekarang
   WAIT: setup forming, belum konfirmasi
   SKIP: tidak ada setup valid

PENTING:
- Setup LEMAH atau TIDAK JELAS → bilang SKIP dengan alasan spesifik, JANGAN kasih trade plan
- Sebutkan kesalahan umum trader retail di chart ini jika ada
- Jawab to the point dalam Bahasa Indonesia, tanpa basa-basi."""

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
    Captures taker_buy_vol (kolom [9]) untuk real CVD.
    """
    def _parse(raw):
        result = []
        for c in raw:
            candle = {
                "open":   float(c[1]), "high": float(c[2]),
                "low":    float(c[3]), "close": float(c[4]),
                "volume": float(c[5]), "time":  c[0],
            }
            # Real taker buy volume — Binance futures klines kolom [9]
            # Jauh lebih akurat dari OHLC approximation untuk CVD
            if len(c) > 9 and c[9] not in (None, ""):
                try:
                    candle["taker_buy_vol"] = float(c[9])
                except (ValueError, TypeError):
                    pass
            result.append(candle)
        return result

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
        "top_ls_ratio": None, "top_ls_bias": "UNKNOWN",
        "perp_spot_basis": None,
        "funding_rate": None,
    }

    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/openInterest",
                         params={"symbol": symbol}, timeout=8)
        if r.status_code == 200:
            result["oi"] = float(r.json().get("openInterest", 0))
    except Exception:
        pass

    # Global L/S ratio (all accounts)
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

    # Top Trader L/S ratio (accounts in top 20% by volume = smart money proxy)
    try:
        r = requests.get(f"{BINANCE_FUTURES}/futures/data/topLongShortAccountRatio",
                         params={"symbol": symbol, "period": "1h", "limit": 2}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                tls = float(data[0].get("longShortRatio", 1.0))
                result["top_ls_ratio"] = round(tls, 3)
                result["top_ls_bias"] = ("LONG_HEAVY"  if tls > 1.5 else
                                         "SHORT_HEAVY" if tls < 0.67 else "BALANCED")
    except Exception:
        pass

    # Perp-spot basis: (markPrice - indexPrice) / indexPrice × 100
    # Positive basis = perp premium → crowded longs (dump fuel)
    # Negative basis = perp discount → crowded shorts (squeeze fuel)
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/premiumIndex",
                         params={"symbol": symbol}, timeout=8)
        if r.status_code == 200:
            d = r.json()
            mark  = float(d.get("markPrice", 0))
            index = float(d.get("indexPrice", 0))
            if index > 0:
                result["perp_spot_basis"] = round((mark - index) / index * 100, 4)
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

    # Funding Rate + Trend (3 periode terakhir)
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/fundingRate",
                         params={"symbol": symbol, "limit": 3}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                rates = [float(d.get("fundingRate", 0)) * 100 for d in data]
                result["funding_rate"] = rates[-1]
                if len(rates) >= 3:
                    # Funding trend: apakah makin ekstrem ke satu arah?
                    if rates[-1] < rates[-2] <= rates[0]:
                        result["funding_trend"] = "MORE_NEGATIVE"   # menuju short squeeze
                    elif rates[-1] > rates[-2] >= rates[0]:
                        result["funding_trend"] = "MORE_POSITIVE"   # menuju long squeeze
                    else:
                        result["funding_trend"] = "STABLE"
                else:
                    result["funding_trend"] = "STABLE"
    except Exception:
        pass

    # Taker Buy/Sell Ratio — actual aggressive order flow (bukan account ratio)
    # Endpoint: /futures/data/takerlongshortRatio
    # buySellRatio > 1.1 = buyer agresif | < 0.9 = seller agresif
    try:
        r = requests.get(f"{BINANCE_FUTURES}/futures/data/takerlongshortRatio",
                         params={"symbol": symbol, "period": "5m", "limit": 3}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                bsr = float(data[-1].get("buySellRatio", 1.0))
                result["taker_buy_sell_ratio"] = round(bsr, 3)
                result["taker_bias"] = (
                    "BUY_DOMINANT"  if bsr > 1.1 else
                    "SELL_DOMINANT" if bsr < 0.9 else
                    "BALANCED"
                )
    except Exception:
        pass

    return result


def get_glassnode_btc_netflow_zscore() -> float | None:
    """
    Fetch BTC exchange netflow Z-score from Glassnode (daily, free tier).
    Returns Z-score of last value vs 30-day baseline, or None if unavailable.
    Requires GLASSNODE_API_KEY env var. Result cached for 12 hours.
    Z-score > 1.5 = unusual inflows (sell pressure) → suppress LONG signals.
    """
    global _glassnode_cache
    cache = _glassnode_cache.get("btc_netflow", {})
    if cache and time.time() - cache.get("ts", 0) < 43200:  # 12h TTL
        return cache.get("z")

    if not _GLASSNODE_API_KEY:
        return None

    try:
        r = requests.get(
            "https://api.glassnode.com/v1/metrics/transactions/transfers_volume_exchanges_net",
            params={"a": "BTC", "i": "24h", "limit": 30, "api_key": _GLASSNODE_API_KEY},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data and len(data) >= 5:
                values = [d.get("v", 0) for d in data]
                last   = values[-1]
                mean   = float(np.mean(values[:-1]))
                std    = float(np.std(values[:-1]))
                z      = round((last - mean) / std, 2) if std > 0 else 0.0
                _glassnode_cache["btc_netflow"] = {"z": z, "ts": time.time()}
                return z
    except Exception as e:
        log.debug(f"Glassnode fetch error: {e}")
    return None


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
                ob_top_val    = round(c["open"], 6)
                ob_bottom_val = round(c["close"], 6)
                touches_bull  = sum(1 for cc in candles[-30:] if cc["low"] <= ob_top_val and cc["high"] >= ob_bottom_val)
                result["bullish_ob"] = {
                    "top": ob_top_val, "bottom": ob_bottom_val,
                    "mid": round((c["open"]+c["close"])/2, 6),
                    "distance_pct": round(((current_price-c["close"])/current_price)*100, 2),
                    "touches": touches_bull,
                    "is_fresh": touches_bull <= 1,
                }

        if move_down and c["close"] > c["open"] and current_price < c["low"]:
            if result["bearish_ob"] is None:
                ob_top_val    = round(c["close"], 6)
                ob_bottom_val = round(c["open"], 6)
                touches_bear  = sum(1 for cc in candles[-30:] if cc["low"] <= ob_top_val and cc["high"] >= ob_bottom_val)
                result["bearish_ob"] = {
                    "top": ob_top_val, "bottom": ob_bottom_val,
                    "mid": round((c["open"]+c["close"])/2, 6),
                    "distance_pct": round(((c["open"]-current_price)/current_price)*100, 2),
                    "touches": touches_bear,
                    "is_fresh": touches_bear <= 1,
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
    """Hitung RSI dengan Wilder smoothing.

    Konsisten dengan detect_rsi_divergence() dan platform charting
    (TradingView/Binance). Sebelumnya pakai SMA atas `period` delta terakhir
    sehingga nilainya berbeda dari versi Wilder di file yang sama.
    """
    if len(candles) < period + 1:
        return 50.0

    closes = [c["close"] for c in candles]
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    if len(gains) < period:
        return 50.0

    avg_gain = float(np.mean(gains[:period]))    # seed = SMA pertama
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_atr(candles: list, period: int = 14) -> float:
    """Hitung ATR dengan Wilder smoothing (RMA) — konsisten dengan charting.

    Sebelumnya pakai SMA atas `period` TR terakhir.
    """
    if len(candles) < period + 1:
        return 0.0

    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))

    if len(trs) < period:
        return float(np.mean(trs)) if trs else 0.0

    atr = float(np.mean(trs[:period]))           # seed = SMA pertama
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calculate_ema(candles: list, period: int) -> float:
    """Hitung EMA (Exponential Moving Average) dari candles."""
    if len(candles) < period:
        return 0.0
    closes = [c["close"] for c in candles]
    k = 2.0 / (period + 1)
    ema = float(np.mean(closes[:period]))
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def calculate_ema_series(candles: list, period: int) -> list:
    """Hitung EMA sebagai series (satu nilai per candle)."""
    if len(candles) < period:
        return []
    closes = [c["close"] for c in candles]
    k = 2.0 / (period + 1)
    ema = float(np.mean(closes[:period]))
    series = [ema]
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
        series.append(ema)
    return series


def calculate_macd(candles: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD(12,26,9) — trend + momentum confirmation. Research: RSI+MACD combo ~77% win rate."""
    empty = {"macd": None, "signal_line": None, "hist": None, "above": False,
             "cross_bull": False, "cross_bear": False}
    if len(candles) < slow + signal:
        return empty
    ema_f = calculate_ema_series(candles, fast)
    ema_s = calculate_ema_series(candles, slow)
    if not ema_f or not ema_s:
        return empty
    # ema_f has len(candles)-fast+1 values; ema_s has len(candles)-slow+1.
    # offset aligns them to same candle: ema_f[slow-fast + j] vs ema_s[j]
    offset = slow - fast
    macd_line = [ema_f[offset + j] - ema_s[j] for j in range(len(ema_s))]
    if len(macd_line) < signal:
        return empty
    k = 2.0 / (signal + 1)
    sig_series = [float(np.mean(macd_line[:signal]))]
    for v in macd_line[signal:]:
        sig_series.append(v * k + sig_series[-1] * (1 - k))
    m_last = macd_line[-1]
    m_prev = macd_line[-2] if len(macd_line) > 1 else m_last
    s_last = sig_series[-1]
    s_prev = sig_series[-2] if len(sig_series) > 1 else s_last
    return {
        "macd":       round(m_last, 8),
        "signal_line": round(s_last, 8),
        "hist":       round(m_last - s_last, 8),
        "above":      (m_last - s_last) > 0,
        "cross_bull": m_prev < s_prev and m_last >= s_last,
        "cross_bear": m_prev > s_prev and m_last <= s_last,
    }


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
    # Prioritas: real taker_buy_vol dari Binance klines [9]
    # Fallback:  OHLC approximation (kurang akurat, tapi tetap useful)
    buy_vols  = []
    sell_vols = []
    using_real_taker = any("taker_buy_vol" in c for c in window)

    for c in window:
        if "taker_buy_vol" in c:
            bv = min(float(c["taker_buy_vol"]), c["volume"])
            buy_vols.append(bv)
            sell_vols.append(c["volume"] - bv)
        else:
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

        # MFI kept for context/display only — score handled by RSI (avoids redundancy)
        if mfi >= 80:
            result["mfi_signal"] = "OVERBOUGHT"
            reasons.append(f"⚠️ MFI: {mfi:.0f} — overbought zone")
        elif mfi <= 20:
            result["mfi_signal"] = "OVERSOLD"
            reasons.append(f"💡 MFI: {mfi:.0f} — oversold zone")
        elif mfi >= 60:
            result["mfi_signal"] = "BULLISH"
        elif mfi <= 40:
            result["mfi_signal"] = "BEARISH"
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
    # CVD dead-zone: jika CVD sedikit negatif (-1% s/d 0%) tapi faktor lain
    # (VWAP/momentum) mendorong score ke positif, cap ke 0 supaya tidak flip
    # ke INFLOW. Hanya berlaku saat score > 0 — tidak menambah penalti baru.
    if -1.0 < cvd_pct < 0 and score_pts > 0:
        score_pts = 0
        reasons.append(f"⚠️ CVD dead-zone: {cvd_pct:+.1f}% — capped ke NEUTRAL")

    # score_pts: max ~+5 (strong inflow) to min ~-5 (strong outflow)
    ltf_score = min(100, max(0, 50 + (score_pts * 10)))
    result["ltf_score"] = ltf_score
    result["reasons"]   = reasons

    # Bias/strength di-drive dari score_pts (integer) langsung. ltf_score selalu
    # kelipatan 10 sehingga band berbasis ltf_score (57/55/43/45) tidak pernah
    # tercapai → tier WEAK jadi dead code. score_pts memberi granularitas bersih.
    if score_pts >= 3:
        result["bias"]     = "INFLOW"
        result["strength"] = "STRONG"
    elif score_pts == 2:
        result["bias"]     = "INFLOW"
        result["strength"] = "MODERATE"
    elif score_pts == 1:
        result["bias"]     = "INFLOW"
        result["strength"] = "WEAK"
    elif score_pts <= -3:
        result["bias"]     = "OUTFLOW"
        result["strength"] = "STRONG"
    elif score_pts == -2:
        result["bias"]     = "OUTFLOW"
        result["strength"] = "MODERATE"
    elif score_pts == -1:
        result["bias"]     = "OUTFLOW"
        result["strength"] = "WEAK"
    else:
        # score_pts == 0: terlalu tipis untuk arah apapun
        result["bias"]     = "NEUTRAL"
        result["strength"] = "WEAK"

    return result

# ─────────────────────────────────────────────
# v14: REAL-TIME MOMENTUM DETECTOR
# Tidak pakai indikator lagging — hanya raw price velocity & volume burst
# dari 1m candles (15 candles = 15 menit terakhir data live)
# ─────────────────────────────────────────────

def detect_realtime_momentum(symbol: str) -> dict:
    """
    Deteksi momentum REAL-TIME dari 1m candles — zero lagging indicators.

    Metrik:
    1. Price velocity 5m / 10m / 15m: % change close vs close N menit lalu
    2. Volume burst: avg volume 3 candle terakhir vs 10 candle sebelumnya
    3. Short-term direction: HH atau LL di 1m (5 candle terakhir)
    4. Breakout check: apakah price breakout dari high/low 15 candle sebelumnya

    Returns: dict dengan velocity, vol_burst, momentum_label, short_bias, breakout
    """
    result = {
        "error":          True,
        "velocity_5m":    0.0,
        "velocity_10m":   0.0,
        "velocity_15m":   0.0,
        "vol_burst":      1.0,
        "momentum_label": "UNKNOWN",
        "short_bias":     "NEUTRAL",
        "breakout_up":    False,
        "breakout_down":  False,
    }

    try:
        candles_1m = get_binance_klines(symbol, "1m", limit=20)
        if not candles_1m or len(candles_1m) < 16:
            return result

        current_close = candles_1m[-1]["close"]
        result["error"] = False

        # ── 1. Price velocity ──────────────────────
        if len(candles_1m) >= 6:
            result["velocity_5m"]  = round(
                (current_close - candles_1m[-6]["close"]) / candles_1m[-6]["close"] * 100, 3)
        if len(candles_1m) >= 11:
            result["velocity_10m"] = round(
                (current_close - candles_1m[-11]["close"]) / candles_1m[-11]["close"] * 100, 3)
        if len(candles_1m) >= 16:
            result["velocity_15m"] = round(
                (current_close - candles_1m[-16]["close"]) / candles_1m[-16]["close"] * 100, 3)

        # ── 2. Volume burst ────────────────────────
        recent_vols = [c["volume"] for c in candles_1m[-3:]]
        base_vols   = [c["volume"] for c in candles_1m[-13:-3]]
        avg_recent  = sum(recent_vols) / len(recent_vols) if recent_vols else 1
        avg_base    = sum(base_vols)   / len(base_vols)   if base_vols   else 1
        result["vol_burst"] = round(avg_recent / max(avg_base, 1), 2)

        # ── 3. Short-term direction (HH/LL last 5 candles) ──
        last5 = candles_1m[-5:]
        highs5 = [c["high"]  for c in last5]
        lows5  = [c["low"]   for c in last5]
        hh = highs5[-1] > max(highs5[:-1])
        hl = lows5[-1]  > min(lows5[:-1])
        lh = highs5[-1] < max(highs5[:-1])
        ll = lows5[-1]  < min(lows5[:-1])

        if hh and hl:   result["short_bias"] = "BULLISH"
        elif lh and ll: result["short_bias"] = "BEARISH"
        else:           result["short_bias"] = "RANGING"

        # ── 4. Breakout dari range 15 candle sebelumnya ──
        # False breakout filter: butuh 2 candle close di luar range (bukan cuma wick)
        prev15 = candles_1m[-16:-1]
        if prev15:
            prev_high = max(c["high"] for c in prev15)
            prev_low  = min(c["low"]  for c in prev15)
            last2     = candles_1m[-2:] if len(candles_1m) >= 2 else candles_1m[-1:]
            # Confirmed breakout: semua candle terakhir close di luar range
            result["breakout_up"]   = all(c["close"] > prev_high for c in last2)
            result["breakout_down"] = all(c["close"] < prev_low  for c in last2)
            # Tentative: hanya 1 candle (bisa fake/wick)
            result["breakout_up_tentative"]   = candles_1m[-1]["close"] > prev_high
            result["breakout_down_tentative"] = candles_1m[-1]["close"] < prev_low

        # ── 5. Label ─────────────────────────────
        v = result["velocity_15m"]
        burst = result["vol_burst"]
        if v > 0.8 and burst >= 1.5:
            result["momentum_label"] = "STRONG_BULL_MOMENTUM"
        elif v > 0.4:
            result["momentum_label"] = "BULL_MOMENTUM"
        elif v < -0.8 and burst >= 1.5:
            result["momentum_label"] = "STRONG_BEAR_MOMENTUM"
        elif v < -0.4:
            result["momentum_label"] = "BEAR_MOMENTUM"
        elif result["breakout_up"]:
            result["momentum_label"] = "BREAKOUT_UP"
        elif result["breakout_down"]:
            result["momentum_label"] = "BREAKOUT_DOWN"
        else:
            result["momentum_label"] = "CONSOLIDATING"

    except Exception as e:
        log.debug(f"detect_realtime_momentum error {symbol}: {e}")

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
    # calculate_entry_zone memilih sisi (bullish/bearish OB) secara internal
    # sesuai direction, jadi cukup pass OB 15M (primary TF scalp).
    entry_zone  = calculate_entry_zone(ob_15m, fvg_15m, result["sweep"], price, direction)
    result["entry_zone"] = entry_zone

    if price > 0:
        # ATR-based SL: 1.5 × ATR_15m. Falls back to fixed % if ATR not available.
        atr_15m = tf_15m.get("atr", 0)
        if atr_15m > 0:
            sl_dist = atr_15m * 1.5
        else:
            sl_dist = price * SCALP_SL_PCT
        sl_dist = max(sl_dist, price * 0.003)  # floor: 0.3% to avoid tiny SL

        if direction == "LONG":
            tp = round(price * (1 + SCALP_TP_PCT), 8)
            sl = round(price - sl_dist, 8)
        elif direction == "SHORT":
            tp = round(price * (1 - SCALP_TP_PCT), 8)
            sl = round(price + sl_dist, 8)
        else:
            tp = sl = None

        result["scalp_tp"]   = tp
        result["scalp_sl"]   = sl
        result["scalp_sl_pct"] = round(sl_dist / price * 100, 3) if price > 0 else SCALP_SL_PCT * 100
        result["price"]      = price

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

    # ── 6. OI CONFLUENCE (with Top Trader ratio + Basis) ─────
    top_ls_bias = oi_data.get("top_ls_bias", "UNKNOWN")
    ls_bias     = oi_data.get("ls_bias", "UNKNOWN")
    oi_chg      = oi_data.get("oi_change_pct")
    _basis_sw   = oi_data.get("perp_spot_basis")

    if direction == "LONG":
        if top_ls_bias == "SHORT_HEAVY":
            score += 10
            result["reasons"].append(f"⚖️ Top Trader Short-heavy → smart money squeeze setup")
        elif ls_bias == "SHORT_HEAVY":
            score += 7
            result["reasons"].append(f"⚖️ L/S Short-heavy → squeeze fuel untuk swing long")
        if _basis_sw is not None and _basis_sw < -0.05:
            score += 5
            result["reasons"].append(f"📉 Basis negatif {_basis_sw:.3f}% → perp discount, shorts crowded")
    elif direction == "SHORT":
        if top_ls_bias == "LONG_HEAVY":
            score += 10
            result["reasons"].append(f"⚖️ Top Trader Long-heavy → smart money trapped long")
        elif ls_bias == "LONG_HEAVY":
            score += 7
            result["reasons"].append(f"⚖️ L/S Long-heavy → liquidation fuel untuk swing short")
        if _basis_sw is not None and _basis_sw > 0.1:
            score += 5
            result["reasons"].append(f"📈 Basis positif {_basis_sw:.3f}% → perp premium, longs crowded")
    if oi_chg and abs(oi_chg) > 5:
        score += 5
        result["reasons"].append(f"📊 OI Change: {oi_chg:+.1f}% — conviction tinggi")

    # ── ENTRY ZONE & TRADE PLAN ───────────────────
    fvg_for_zone = fvg_1h
    entry_zone   = calculate_entry_zone(ob_1h, fvg_for_zone, result["sweep"], price, direction)
    result["entry_zone"] = entry_zone

    if price > 0:
        # ATR-based SL: 2.0 × ATR_1h. Falls back to fixed % if ATR not available.
        atr_1h_sl = tf_1h.get("atr", 0)
        if atr_1h_sl > 0:
            sl_dist = atr_1h_sl * 2.0
        else:
            sl_dist = price * SWING_SL_PCT
        sl_dist = max(sl_dist, price * 0.008)  # floor: 0.8% to avoid tiny SL

        if direction == "LONG":
            tp = round(price * (1 + SWING_TP_PCT), 8)
            sl = round(price - sl_dist, 8)
        elif direction == "SHORT":
            tp = round(price * (1 - SWING_TP_PCT), 8)
            sl = round(price + sl_dist, 8)
        else:
            tp = sl = None

        rr = 0
        if tp and sl and price != sl:
            rr = round(abs(tp - price) / abs(sl - price), 2)

        result["swing_tp"]   = tp
        result["swing_sl"]   = sl
        result["swing_sl_pct"] = round(sl_dist / price * 100, 3) if price > 0 else SWING_SL_PCT * 100
        result["rr"]         = rr
        result["price"]      = price

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
            if scalp["score"] >= _eff_scalp_min_score():
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
        "ema9":           calculate_ema(closed_candles, 9),
        "ema21":          calculate_ema(closed_candles, 21),
        "ema50":          calculate_ema(closed_candles, 50),
        # v16: reversal patterns (V-Shape high-probability + Quasimodo shift)
        "v_shape":         detect_v_shape(closed_candles),
        "qm_pattern":      detect_qm_pattern(closed_candles),
        # v14: advanced structure
        "candle_patterns": detect_candle_patterns(closed_candles),
        "market_regime":   detect_market_regime(closed_candles),
        "bb_squeeze":      calculate_bb_squeeze(closed_candles),
        "volume_coil":     detect_volume_coil(closed_candles),
        "sudden_breakout": detect_sudden_breakout(closed_candles),
        "adx":             calculate_adx(closed_candles).get("adx", 0),
        "macd":            calculate_macd(closed_candles),
        "_anti_lookahead": True,
        "_closed_count":   len(closed_candles),
    }

# ─────────────────────────────────────────────
# RSI DIVERGENCE DETECTOR
# ─────────────────────────────────────────────

def detect_rsi_divergence(candles: list, lookback: int = 25, period: int = 14) -> dict:
    """
    Deteksi RSI divergence — salah satu sinyal reversal momentum paling reliable.

    Regular Bullish : price LL + RSI HL → akumulasi tersembunyi, pump likely
    Regular Bearish : price HH + RSI LH → distribusi tersembunyi, dump likely

    Digunakan sebagai bonus score di prepump/predump detector untuk sinyal
    yang muncul SEBELUM price action terlihat jelas.

    Return:
      type: REGULAR_BULLISH | REGULAR_BEARISH | NONE
      bull_score: 0-15  (boost ke prepump momentum)
      bear_score: 0-15  (boost ke predump momentum)
      details: list reason strings
    """
    empty = {"type": "NONE", "strength": "NONE", "bull_score": 0, "bear_score": 0, "details": []}

    if not candles or len(candles) < period + lookback + 5:
        return empty

    work   = candles[-(period + lookback + 5):]
    cls    = [c["close"] for c in work]
    h_arr  = [c["high"]  for c in work]
    l_arr  = [c["low"]   for c in work]

    # RSI series (Wilder smoothing)
    gains  = [max(cls[i] - cls[i-1], 0) for i in range(1, len(cls))]
    losses = [max(cls[i-1] - cls[i], 0) for i in range(1, len(cls))]
    if len(gains) < period:
        return empty

    avg_g = float(np.mean(gains[:period]))
    avg_l = float(np.mean(losses[:period]))
    rsi_s = []
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs    = avg_g / avg_l if avg_l > 0 else 100
        rsi_s.append(100 - 100 / (1 + rs))

    if len(rsi_s) < lookback:
        return empty

    rsi_w = rsi_s[-lookback:]
    h_w   = h_arr[-lookback:]
    l_w   = l_arr[-lookback:]
    n     = lookback

    # Find 2-bar confirmed swing pivots
    swing_highs: list = []
    swing_lows:  list = []

    for i in range(2, n - 2):
        if (h_w[i] >= h_w[i-1] and h_w[i] >= h_w[i-2] and
                h_w[i] >= h_w[i+1] and h_w[i] >= h_w[i+2]):
            swing_highs.append((i, h_w[i], rsi_w[i]))
        if (l_w[i] <= l_w[i-1] and l_w[i] <= l_w[i-2] and
                l_w[i] <= l_w[i+1] and l_w[i] <= l_w[i+2]):
            swing_lows.append((i, l_w[i], rsi_w[i]))

    bull_score = 0
    bear_score = 0
    details: list = []
    div_type   = "NONE"

    # Regular Bearish: price HH + RSI LH (momentum melemah di puncak)
    if len(swing_highs) >= 2:
        ph, lh = swing_highs[-2], swing_highs[-1]
        p_diff = (lh[1] - ph[1]) / ph[1] * 100  if ph[1] > 0 else 0
        r_diff = lh[2] - ph[2]
        if p_diff > 0.5 and r_diff < -3:
            bear_score = min(15, int(abs(r_diff) * 1.1))
            strength   = "HIGH" if abs(r_diff) >= 8 else "MEDIUM"
            details.append(
                f"⚡ RSI Bearish Div ({strength}): price +{p_diff:.1f}% HH "
                f"tapi RSI {r_diff:.0f}pt LH — distribusi tersembunyi, dump imminent"
            )
            div_type = "REGULAR_BEARISH"

    # Regular Bullish: price LL + RSI HL (momentum membangun di bawah)
    if len(swing_lows) >= 2:
        pl, ll_p = swing_lows[-2], swing_lows[-1]
        p_diff = (ll_p[1] - pl[1]) / pl[1] * 100  if pl[1] > 0 else 0  # negative = LL
        r_diff = ll_p[2] - pl[2]                                          # positive = HL
        if p_diff < -0.5 and r_diff > 3:
            bull_score = min(15, int(r_diff * 1.1))
            strength   = "HIGH" if r_diff >= 8 else "MEDIUM"
            details.append(
                f"⚡ RSI Bullish Div ({strength}): price {p_diff:.1f}% LL "
                f"tapi RSI +{r_diff:.0f}pt HL — akumulasi tersembunyi, pump imminent"
            )
            if div_type == "NONE":
                div_type = "REGULAR_BULLISH"

    max_s = max(bull_score, bear_score)
    return {
        "type":       div_type,
        "strength":   "HIGH" if max_s >= 12 else ("MEDIUM" if max_s >= 7 else "LOW"),
        "bull_score": bull_score,
        "bear_score": bear_score,
        "details":    details,
    }


# ─────────────────────────────────────────────
# PRE-PUMP DETECTOR
# ─────────────────────────────────────────────

def detect_prepump(symbol: str, tf_1h: dict, tf_4h: dict, oi_data: dict,
                   tf_15m: dict = None) -> dict:
    """
    Deteksi pre-pump setup berdasarkan 3 indikator inti + 1 bonus:
    1. Funding Squeeze       (max 30 poin)
    2. Momentum Runner       (max 35 poin)
    3. OI + PA + ATR         (max 35 poin)   → inti = 100 poin
    4. Early Warning + v14    (bonus aditif, max 30 poin) BB Squeeze / Volume Coil /
       Sudden Breakout — untuk men-trigger sinyal lebih dini.
    Total = inti + bonus, lalu di-clamp ke max 100 poin.
    """
    tf_15m = tf_15m or {}
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

    # Taker flow + Funding Trend (dari get_open_interest)
    _taker_bias     = oi_data.get("taker_bias", "BALANCED")
    _funding_trend  = oi_data.get("funding_trend", "STABLE")
    _fs_curr        = result["funding_score"]

    if _taker_bias == "BUY_DOMINANT":
        result["funding_score"] = min(_fs_curr + 5, 30)
        result["reasons"].append(
            f"📈 Taker flow: BUY dominant (ratio {oi_data.get('taker_buy_sell_ratio', 0):.2f}) "
            f"— buyer agresif market-order masuk"
        )
    if _funding_trend == "MORE_NEGATIVE":
        result["funding_score"] = min(result["funding_score"] + 5, 30)
        result["reasons"].append(
            "🔄 Funding trend makin negatif — short squeeze tekanan terus membangun"
        )

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

    # Volume surge — full bonus hanya kalau candle bullish (akumulasi),
    # bukan high-volume down candle (mirror logika detect_predump).
    if vol_1h.get("is_anomaly") or (vol_1h.get("multiplier", 1) >= VOLUME_SURGE_MULT):
        mult = vol_1h.get("multiplier", 1)
        candles_1h = tf_1h.get("candles", [])
        if candles_1h and candles_1h[-1]["close"] > candles_1h[-1]["open"]:  # bullish candle
            mom_score += 10
            result["reasons"].append(f"🐳 Volume surge 1H: {mult:.1f}x normal — smart money in")
        else:
            mom_score += 5
            result["reasons"].append(f"  Volume spike 1H: {mult:.1f}x (arah perlu dikonfirmasi)")
    elif vol_4h.get("is_anomaly"):
        mom_score += 5
        mult = vol_4h.get("multiplier", 1)
        result["reasons"].append(f"  Volume spike 4H: {mult:.1f}x normal")

    # RSI Divergence — akumulasi tersembunyi yang muncul SEBELUM pump terlihat
    _div_pp = detect_rsi_divergence(tf_1h.get("candles", []), lookback=25)
    if _div_pp["bull_score"] >= 7:
        _pp_bonus = min(_div_pp["bull_score"], 12)
        mom_score += _pp_bonus
        result["reasons"].extend(_div_pp["details"])
    elif _div_pp["bear_score"] >= 7:
        mom_score = max(0, mom_score - 5)
        result["reasons"].append("⚠️ RSI Bearish Div — momentum downside lebih kuat dari upside")

    # MACD confirmation (research: RSI+MACD combo ~77% win rate)
    _macd_1h = tf_1h.get("macd", {})
    _macd_4h = tf_4h.get("macd", {})
    if _macd_1h.get("cross_bull"):
        mom_score += 10
        result["reasons"].append("📈 MACD 1H: Bullish crossover — momentum acceleration dikonfirmasi")
    elif _macd_1h.get("above") and _macd_4h.get("above"):
        mom_score += 6
        result["reasons"].append("✅ MACD: 1H+4H histogram positif — sustained bullish momentum")
    elif _macd_1h.get("above"):
        mom_score += 3
        result["reasons"].append("🟢 MACD 1H: Histogram positif — mild bullish momentum")
    elif _macd_1h.get("cross_bear"):
        mom_score = max(0, mom_score - 7)
        result["reasons"].append("⚠️ MACD 1H: Bearish crossover — momentum flip DOWN, hati-hati")

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

    # Top Trader L/S ratio (smart money proxy — top 20% by volume accounts)
    top_ls_bias  = oi_data.get("top_ls_bias", "UNKNOWN")
    top_ls_ratio = oi_data.get("top_ls_ratio")
    ls_bias      = oi_data.get("ls_bias", "UNKNOWN")
    ls_ratio     = oi_data.get("ls_ratio")

    if top_ls_bias == "SHORT_HEAVY" and top_ls_ratio:
        oi_pa_score += 15
        result["reasons"].append(
            f"🎯 Top Trader SHORT-heavy: {top_ls_ratio:.2f} → smart money short = squeeze powder keg"
        )
    elif top_ls_bias == "BALANCED" and ls_bias == "SHORT_HEAVY":
        oi_pa_score += 10
        result["reasons"].append(f"⚖️ Top Trader balanced vs global short-heavy → mixed squeeze setup")
    elif ls_bias == "SHORT_HEAVY" and ls_ratio:
        oi_pa_score += 8
        result["reasons"].append(f"🎯 L/S Short-heavy: {ls_ratio:.2f} → squeeze fuel (retail)")
    elif top_ls_bias == "BALANCED" or ls_bias == "BALANCED":
        oi_pa_score += 4

    # Perp-spot basis: negative basis = crowded shorts = potential squeeze
    _basis = oi_data.get("perp_spot_basis")
    if _basis is not None:
        if _basis < -0.05:
            oi_pa_score += 8
            result["reasons"].append(
                f"📉 Basis negatif: {_basis:.3f}% — perp di bawah spot, shorts crowded → squeeze setup"
            )
        elif _basis > 0.1:
            result["reasons"].append(
                f"⚠️ Basis positif tinggi: {_basis:.3f}% — perp premium, longs berat, hati-hati"
            )

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

    # ── 4. EARLY WARNING: kondisi SEBELUM pump (bonus aditif, max 30 poin) ──
    # Deteksi akumulasi tersembunyi yang muncul SEBELUM pump terlihat jelas.
    # Ini yang bikin sinyal bisa dikirim lebih awal.
    early_score = 0
    candles_1h  = tf_1h.get("candles", [])

    # 4a. BB Squeeze Duration — semakin lama kompresi, semakin besar potensi breakout
    if candles_1h and len(candles_1h) >= 22:
        closes_arr = [c["close"] for c in candles_1h[-22:]]
        squeeze_count = 0
        for _j in range(2, 12):
            w = closes_arr[max(0, _j - 20):_j]
            if len(w) < 15:
                continue
            mid   = float(np.mean(w))
            std   = float(np.std(w))
            bw_pct = (4.0 * std / mid * 100.0) if mid > 0 else 99.0
            if bw_pct < 4.0:
                squeeze_count += 1
        if squeeze_count >= 8:
            early_score += 10
            result["reasons"].append(
                f"🌀 BB Squeeze panjang ({squeeze_count}/10 candle) — energi terkompresi, pump imminent"
            )
        elif squeeze_count >= 5:
            early_score += 5
            result["reasons"].append(f"  BB Squeeze forming ({squeeze_count}/10 candle) — watch ketat")

    # 4b. Stealth accumulation: volume naik tapi harga flat = smart money masuk diam-diam
    if candles_1h and len(candles_1h) >= 20:
        recent_5  = candles_1h[-5:]
        baseline  = candles_1h[-20:-5]
        v_recent  = float(np.mean([c["volume"] for c in recent_5]))
        v_base    = float(np.mean([c["volume"] for c in baseline]))
        p_start   = recent_5[0]["close"]
        p_end     = recent_5[-1]["close"]
        price_chg = abs(p_end - p_start) / p_start * 100 if p_start > 0 else 99.0
        if v_base > 0:
            vol_ratio = v_recent / v_base
            if vol_ratio >= 1.4 and price_chg < 1.0:
                early_score += 8
                result["reasons"].append(
                    f"🔍 Stealth accumulation: vol +{(vol_ratio - 1) * 100:.0f}% saat harga flat "
                    f"— smart money akumulasi diam-diam"
                )
            elif vol_ratio >= 1.2 and price_chg < 0.5:
                early_score += 4
                result["reasons"].append(
                    f"  Volume quietly building ({(vol_ratio - 1) * 100:.0f}% above baseline)"
                )

    # 4c. OI/Price divergence: OI naik, harga flat/turun = akumulasi conviction tersembunyi
    oi_c_ew  = oi_data.get("oi_change_pct", 0) or 0
    candles_1h_ew = tf_1h.get("candles", [])
    if candles_1h_ew and len(candles_1h_ew) >= 2:
        _p_now  = candles_1h_ew[-1]["close"]
        _p_prev = candles_1h_ew[-2]["close"]
        _p1h_chg = (_p_now - _p_prev) / _p_prev * 100 if _p_prev > 0 else 0
        if oi_c_ew >= 3 and abs(_p1h_chg) < 0.5:
            early_score += 6
            result["reasons"].append(
                f"📊 OI/Price divergence: OI +{oi_c_ew:.1f}% vs harga flat "
                f"— conviction long tersembunyi, pre-pump setup"
            )
        elif oi_c_ew >= 2 and _p1h_chg <= 0:
            early_score += 3
            result["reasons"].append(
                f"  OI +{oi_c_ew:.1f}% saat harga turun — akumulasi stealth"
            )

    # 4d. Pullback volume quality: harga turun (pullback) tapi volume kecil
    # = smart money absorb diam-diam, bukan distribusi. Ideal setup sebelum pump.
    if candles_1h and len(candles_1h) >= 20:
        _pb_recent = candles_1h[-5:]
        _pb_base   = candles_1h[-20:-5]
        _pb_v_now  = float(np.mean([c["volume"] for c in _pb_recent]))
        _pb_v_base = float(np.mean([c["volume"] for c in _pb_base]))
        # Price direction: is it pulling back? (recent close < 5-bar-ago close)
        _pb_p_start = _pb_recent[0]["close"]
        _pb_p_end   = _pb_recent[-1]["close"]
        _pb_p_chg   = (_pb_p_end - _pb_p_start) / _pb_p_start * 100 if _pb_p_start > 0 else 0
        _pb_v_ratio = _pb_v_now / _pb_v_base if _pb_v_base > 0 else 1.0
        if _pb_p_chg < -0.3 and _pb_v_ratio < 0.70:
            # Price pulling back + volume below 70% baseline = weak pullback (smart money absorb)
            early_score += 7
            result["reasons"].append(
                f"🔍 Pullback volume lemah: vol {_pb_v_ratio*100:.0f}% baseline saat harga pullback "
                f"({_pb_p_chg:.1f}%) — smart money absorb, pump potential"
            )
        elif _pb_p_chg < -0.3 and _pb_v_ratio < 0.85:
            early_score += 3
            result["reasons"].append(
                f"  Pullback low-vol ({_pb_v_ratio*100:.0f}% baseline) — mild absorption"
            )

    # 4e. Liquidation cascade: LONG liq surge = oversold bounce potential
    _liq = get_liq_data(symbol) if LIQ_TRACKER_MODULE else {}
    if _liq.get("liq_surge_long"):
        early_score += 8
        _lusd = _liq.get("long_liq_usd", 0)
        result["reasons"].append(
            f"⚡ Long liq surge: ${_lusd/1e6:.1f}M liq'd (15m) — forced selling = bounce fuel"
        )
    elif _liq.get("liq_surge_short"):
        early_score += 5
        _susd = _liq.get("short_liq_usd", 0)
        result["reasons"].append(
            f"🚀 Short squeeze active: ${_susd/1e6:.1f}M shorts liq'd — momentum continuation"
        )

    # ── v14: BB SQUEEZE / VOLUME COIL / SUDDEN BREAKOUT ──────────
    # Detect "compressed spring" setup — ciri khas pump tiba-tiba (ALLO-type)

    # BB Squeeze dari 1H / 4H → coiling sebelum explosion
    bb_4h = tf_4h.get("bb_squeeze", {})
    if not bb_4h and tf_4h.get("candles"):
        bb_4h = calculate_bb_squeeze(tf_4h["candles"])
    bb_1h = tf_1h.get("bb_squeeze", {})
    if not bb_1h and tf_1h.get("candles"):
        bb_1h = calculate_bb_squeeze(tf_1h["candles"])

    if bb_4h.get("squeeze"):
        sq_bars = bb_4h.get("squeeze_bars", 0)
        early_score += 15
        result["reasons"].append(
            f"🔵 BB SQUEEZE 4H ({sq_bars} bar, width {bb_4h.get('bb_width',0):.1f}%) "
            f"— harga coiling, spring load imminent!"
        )
    elif bb_1h.get("squeeze"):
        sq_bars_1h = bb_1h.get("squeeze_bars", 0)
        early_score += 8
        result["reasons"].append(
            f"🔵 BB Squeeze 1H ({sq_bars_1h} bar) — kompresi volatilitas, potensi breakout"
        )

    # Volume Coil dari 1H → declining volume = akumulasi silent, spring terload
    vc_1h = tf_1h.get("volume_coil", {})
    if not vc_1h and tf_1h.get("candles"):
        vc_1h = detect_volume_coil(tf_1h["candles"])
    vc_15m = tf_15m.get("volume_coil", {})
    if not vc_15m and tf_15m.get("candles"):
        vc_15m = detect_volume_coil(tf_15m["candles"])

    if vc_1h.get("coiling") and vc_1h.get("spike_detected"):
        early_score += 15
        result["reasons"].append(
            f"🌊 Volume Coil RELEASE 1H: spike {vc_1h.get('vol_ratio',1):.1f}x setelah "
            f"{vc_1h.get('compression_bars',0)} bar declining — spring terlepas!"
        )
    elif vc_1h.get("coiling"):
        early_score += 7
        result["reasons"].append(
            f"🔍 Volume Coil 1H: {vc_1h.get('compression_bars',0)} bar declining "
            f"— silent accumulation, pre-pump loading"
        )

    # Sudden Breakout detector (ALLO-type) — explosive dari konsolidasi
    sb_4h = tf_4h.get("sudden_breakout", {})
    if not sb_4h and tf_4h.get("candles"):
        sb_4h = detect_sudden_breakout(tf_4h["candles"])
    sb_1h = tf_1h.get("sudden_breakout", {})
    if not sb_1h and tf_1h.get("candles"):
        sb_1h = detect_sudden_breakout(tf_1h["candles"])
    sb_15m = tf_15m.get("sudden_breakout", {})
    if not sb_15m and tf_15m.get("candles"):
        sb_15m = detect_sudden_breakout(tf_15m["candles"])

    if sb_4h.get("sudden_breakout") and sb_4h.get("direction") == "UP":
        early_score += 25
        result["reasons"].append(
            f"🚀 SUDDEN BREAKOUT UP 4H: vol {sb_4h.get('vol_spike',1):.1f}x, "
            f"+{sb_4h.get('range_break_pct',0):.1f}% range break — momentum explosive!"
        )
    elif sb_1h.get("sudden_breakout") and sb_1h.get("direction") == "UP":
        early_score += 18
        result["reasons"].append(
            f"⚡ Sudden Breakout UP 1H: vol {sb_1h.get('vol_spike',1):.1f}x — pump rally starting"
        )
    elif sb_15m.get("sudden_breakout") and sb_15m.get("direction") == "UP":
        early_score += 10
        result["reasons"].append(
            f"💥 Sudden Breakout UP 15M: vol {sb_15m.get('vol_spike',1):.1f}x — scalp momentum aktif"
        )
    elif sb_1h.get("was_consolidating") and sb_1h.get("vol_spike", 1) >= 1.8:
        early_score += 5
        result["reasons"].append(
            f"  Volume building {sb_1h.get('vol_spike',1):.1f}x di zona konsolidasi — watch for breakout"
        )

    result["early_warning_score"] = min(early_score, 30)

    # ── GLASSNODE MACRO FILTER ───────────────────
    # If BTC exchange netflow Z-score is extreme (big inflows = sell pressure),
    # suppress pump signals for ALL coins (macro risk-off).
    _gl_z = get_glassnode_btc_netflow_zscore()
    _macro_suppressed = False
    if _gl_z is not None and _gl_z > 1.5:
        result["reasons"].append(
            f"🚨 Glassnode BTC netflow Z={_gl_z:.1f} — massive inflows to exchanges "
            f"(sell pressure macro) — LONG signal quality reduced"
        )
        _macro_suppressed = True

    # ── TOTAL SCORE & LABEL ──────────────────────
    total = (result["funding_score"] + result["momentum_score"] +
             result["oi_pa_score"] + result["early_warning_score"])
    if _macro_suppressed:
        total = int(total * 0.80)  # reduce score 20% in macro risk-off environment
    result["total_score"] = min(total, 100)

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

            pp = detect_prepump(sym, tf_1h, tf_4h, oi, tf_15m)
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

    # Taker flow + Funding Trend (dari get_open_interest)
    _taker_bias_pd    = oi_data.get("taker_bias", "BALANCED")
    _funding_trend_pd = oi_data.get("funding_trend", "STABLE")
    _fs_curr_pd       = result["funding_score"]

    if _taker_bias_pd == "SELL_DOMINANT":
        result["funding_score"] = min(_fs_curr_pd + 5, 30)
        result["reasons"].append(
            f"📉 Taker flow: SELL dominant (ratio {oi_data.get('taker_buy_sell_ratio', 0):.2f}) "
            f"— seller agresif market-order masuk"
        )
    if _funding_trend_pd == "MORE_POSITIVE":
        result["funding_score"] = min(result["funding_score"] + 5, 30)
        result["reasons"].append(
            "🔄 Funding trend makin positif — long squeeze tekanan terus membangun"
        )

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

    # RSI Divergence — distribusi tersembunyi yang muncul SEBELUM dump terlihat
    _div_pd = detect_rsi_divergence(tf_1h.get("candles", []), lookback=25)
    if _div_pd["bear_score"] >= 7:
        _pd_bonus = min(_div_pd["bear_score"], 12)
        mom_score += _pd_bonus
        result["reasons"].extend(_div_pd["details"])
    elif _div_pd["bull_score"] >= 7:
        mom_score = max(0, mom_score - 5)
        result["reasons"].append("⚠️ RSI Bullish Div — potensi bounce lebih besar dari dump")

    # MACD confirmation (bearish side)
    _macd_1h_pd = tf_1h.get("macd", {})
    _macd_4h_pd = tf_4h.get("macd", {})
    if _macd_1h_pd.get("cross_bear"):
        mom_score += 10
        result["reasons"].append("📉 MACD 1H: Bearish crossover — momentum flip DOWN dikonfirmasi")
    elif not _macd_1h_pd.get("above", True) and not _macd_4h_pd.get("above", True):
        mom_score += 6
        result["reasons"].append("🔴 MACD: 1H+4H histogram negatif — sustained bearish momentum")
    elif not _macd_1h_pd.get("above", True):
        mom_score += 3
        result["reasons"].append("🟡 MACD 1H: Histogram negatif — mild bearish momentum")
    elif _macd_1h_pd.get("cross_bull"):
        mom_score = max(0, mom_score - 7)
        result["reasons"].append("⚠️ MACD 1H: Bullish crossover — momentum flip UP, kontra dump")

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

    # Top Trader L/S ratio — smart money proxy
    top_ls_bias  = oi_data.get("top_ls_bias", "UNKNOWN")
    top_ls_ratio = oi_data.get("top_ls_ratio")
    ls_bias      = oi_data.get("ls_bias", "UNKNOWN")
    ls_ratio     = oi_data.get("ls_ratio")

    if top_ls_bias == "LONG_HEAVY" and top_ls_ratio:
        oi_pa_score += 15
        result["reasons"].append(
            f"🎯 Top Trader LONG-heavy: {top_ls_ratio:.2f} → smart money trapped long = cascade risk"
        )
    elif top_ls_bias == "BALANCED" and ls_bias == "LONG_HEAVY":
        oi_pa_score += 10
        result["reasons"].append(f"⚖️ Top Trader balanced vs global long-heavy → longs crowded (retail)")
    elif ls_bias == "LONG_HEAVY" and ls_ratio:
        if ls_ratio >= LS_LONG_HEAVY_THRESH:
            oi_pa_score += 12
            result["reasons"].append(
                f"🎯 L/S Long-heavy EXTREME: {ls_ratio:.2f} → long liquidation cascade risk"
            )
        else:
            oi_pa_score += 7
            result["reasons"].append(f"⚠️ L/S Long-heavy: {ls_ratio:.2f} → long liq risk")
    elif top_ls_bias == "BALANCED" or ls_bias == "BALANCED":
        oi_pa_score += 3

    # Perp-spot basis: positive = crowded longs = dump fuel
    _basis_pd = oi_data.get("perp_spot_basis")
    if _basis_pd is not None:
        if _basis_pd > 0.1:
            oi_pa_score += 8
            result["reasons"].append(
                f"📈 Basis positif tinggi: {_basis_pd:.3f}% — perp premium, longs crowded → dump setup"
            )
        elif _basis_pd < -0.05:
            result["reasons"].append(
                f"  Basis negatif: {_basis_pd:.3f}% — perp di bawah spot, shorts heavy (kontra dump)"
            )

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

    # ── 4. EARLY WARNING: kondisi SEBELUM dump (max 20 poin) ────────────
    # Deteksi distribusi tersembunyi sebelum dump terlihat jelas.
    early_score = 0
    candles_1h  = tf_1h.get("candles", [])

    # 4a. BB Squeeze Duration — kompresi sebelum dump breakout bawah
    if candles_1h and len(candles_1h) >= 22:
        closes_arr = [c["close"] for c in candles_1h[-22:]]
        squeeze_count = 0
        for _j in range(2, 12):
            w = closes_arr[max(0, _j - 20):_j]
            if len(w) < 15:
                continue
            mid   = float(np.mean(w))
            std   = float(np.std(w))
            bw_pct = (4.0 * std / mid * 100.0) if mid > 0 else 99.0
            if bw_pct < 4.0:
                squeeze_count += 1
        if squeeze_count >= 8:
            early_score += 10
            result["reasons"].append(
                f"🌀 BB Squeeze panjang ({squeeze_count}/10 candle) — energi terkompresi, dump imminent"
            )
        elif squeeze_count >= 5:
            early_score += 5
            result["reasons"].append(f"  BB Squeeze forming ({squeeze_count}/10 candle) — watch ketat")

    # 4b. Stealth distribution: volume tinggi + dominasi candle bearish + harga belum turun banyak
    if candles_1h and len(candles_1h) >= 20:
        recent_5   = candles_1h[-5:]
        baseline   = candles_1h[-20:-5]
        v_recent   = float(np.mean([c["volume"] for c in recent_5]))
        v_base     = float(np.mean([c["volume"] for c in baseline]))
        bear_count = sum(1 for c in recent_5 if c["close"] < c["open"])
        p_start    = recent_5[0]["close"]
        p_end      = recent_5[-1]["close"]
        price_chg  = (p_end - p_start) / p_start * 100 if p_start > 0 else 0
        if v_base > 0:
            vol_ratio = v_recent / v_base
            if vol_ratio >= 1.4 and bear_count >= 3 and abs(price_chg) < 2.0:
                early_score += 8
                result["reasons"].append(
                    f"📤 Stealth distribution: vol +{(vol_ratio - 1) * 100:.0f}% + "
                    f"{bear_count}/5 bearish candle saat harga flat "
                    f"— smart money exit diam-diam"
                )
            elif vol_ratio >= 1.2 and bear_count >= 2 and price_chg <= 0.5:
                early_score += 4
                result["reasons"].append(
                    f"  Volume spike + {bear_count}/5 bearish candle — distribusi potential"
                )

    # 4c. OI/Price divergence bearish: OI naik + long-heavy + harga flat = longs terperangkap
    oi_c_ew  = oi_data.get("oi_change_pct", 0) or 0
    ls_bias_ew = oi_data.get("ls_bias", "BALANCED")
    if candles_1h and len(candles_1h) >= 2:
        _p_now  = candles_1h[-1]["close"]
        _p_prev = candles_1h[-2]["close"]
        _p1h_chg = (_p_now - _p_prev) / _p_prev * 100 if _p_prev > 0 else 0
        if oi_c_ew >= 3 and ls_bias_ew == "LONG_HEAVY" and _p1h_chg < 1.0:
            early_score += 8
            result["reasons"].append(
                f"📊 OI +{oi_c_ew:.1f}% + Long-heavy + harga flat "
                f"— longs terperangkap, dump setup forming silently"
            )
        elif oi_c_ew >= 2 and ls_bias_ew == "LONG_HEAVY":
            early_score += 4
            result["reasons"].append(
                f"  OI naik + long-heavy ({ls_bias_ew}) — distribusi potential"
            )
        elif oi_c_ew >= 3 and _p1h_chg < 0.5:
            early_score += 3
            result["reasons"].append(
                f"  OI divergence: OI +{oi_c_ew:.1f}% vs harga flat — konsolidasi sebelum dump"
            )

    # 4d. Pullback volume quality (bearish): harga naik tapi volume kecil
    # = bearish pullback (dead cat bounce) bukan pembalikan nyata.
    # Rally saat volume rendah di downtrend = distribusi tersembunyi.
    _candles_1h_pd = tf_1h.get("candles", [])
    if _candles_1h_pd and len(_candles_1h_pd) >= 20:
        _pb_r_pd  = _candles_1h_pd[-5:]
        _pb_b_pd  = _candles_1h_pd[-20:-5]
        _pb_vn_pd = float(np.mean([c["volume"] for c in _pb_r_pd]))
        _pb_vb_pd = float(np.mean([c["volume"] for c in _pb_b_pd]))
        _pb_ps_pd = _pb_r_pd[0]["close"]
        _pb_pe_pd = _pb_r_pd[-1]["close"]
        _pb_pc_pd = (_pb_pe_pd - _pb_ps_pd) / _pb_ps_pd * 100 if _pb_ps_pd > 0 else 0
        _pb_vr_pd = _pb_vn_pd / _pb_vb_pd if _pb_vb_pd > 0 else 1.0
        if _pb_pc_pd > 0.3 and _pb_vr_pd < 0.70:
            # Price rallying + volume below 70% baseline = weak rally (dead cat, distribution)
            early_score += 7
            result["reasons"].append(
                f"🔍 Rally volume lemah: vol {_pb_vr_pd*100:.0f}% baseline saat harga naik "
                f"({_pb_pc_pd:.1f}%) — rally palsu, dump imminent"
            )
        elif _pb_pc_pd > 0.3 and _pb_vr_pd < 0.85:
            early_score += 3
            result["reasons"].append(
                f"  Low-vol rally ({_pb_vr_pd*100:.0f}% baseline) — distribusi potential"
            )

    # 4e. Liquidation cascade: SHORT liq surge = overbought cascade potential
    _liq_pd = get_liq_data(symbol) if LIQ_TRACKER_MODULE else {}
    if _liq_pd.get("liq_surge_short"):
        early_score += 8
        _susd = _liq_pd.get("short_liq_usd", 0)
        result["reasons"].append(
            f"⚡ Short liq surge: ${_susd/1e6:.1f}M shorts liq'd (15m) — short squeeze = dump risk"
        )
    elif _liq_pd.get("liq_surge_long"):
        early_score += 8
        _lusd = _liq_pd.get("long_liq_usd", 0)
        result["reasons"].append(
            f"🔥 Long cascade active: ${_lusd/1e6:.1f}M longs liq'd — momentum dump kuat"
        )

    result["early_warning_score"] = min(early_score, 30)

    # ── GLASSNODE MACRO FILTER ───────────────────
    # High BTC exchange inflows = selling pressure = amplifies dump signals.
    _gl_z_pd = get_glassnode_btc_netflow_zscore()
    _macro_amplified = False
    if _gl_z_pd is not None and _gl_z_pd > 1.5:
        result["reasons"].append(
            f"🚨 Glassnode BTC netflow Z={_gl_z_pd:.1f} — massive inflows "
            f"(macro sell pressure aktif) — SHORT signal quality boosted"
        )
        _macro_amplified = True

    # ── TOTAL SCORE & LABEL ──────────────────────
    total = (result["funding_score"] + result["momentum_score"] +
             result["oi_pa_score"] + result["early_warning_score"])
    if _macro_amplified:
        total = int(total * 1.10)  # boost 10% when macro confirms dump
    result["total_score"] = min(total, 100)

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

def calculate_confluence_v4(tf_4h: dict, tf_1h: dict, tf_15m: dict, oi_data: dict,
                             realtime: dict = None) -> dict:
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

    # ── v14: MARKET REGIME GATE ─────────────────
    # Cek regime dari 4H dan 1H. RANGING tanpa breakout = sinyal noise.
    # BB_SQUEEZE = watch, BREAKOUT = strong boost, TRENDING = moderate boost.
    regime_4h = tf_4h.get("market_regime", {})
    regime_1h = tf_1h.get("market_regime", {})
    regime_str = regime_4h.get("regime", "UNKNOWN")
    regime_detail = regime_4h.get("detail", "")

    if regime_str == "RANGING":
        # Ranging market: kurangi confidence keduanya — sinyal sering fakeout
        pump_score = int(pump_score * 0.70)
        dump_score = int(dump_score * 0.70)
        reasons.append(f"⚠️ REGIME 4H: RANGING (ADX={regime_4h.get('adx',0):.0f}) — sinyal noise tinggi, hati-hati fakeout")
    elif regime_str == "BB_SQUEEZE":
        # Squeeze = coiling — siapkan diri untuk breakout explosive
        sq_bars = regime_4h.get("bb_width_pct", 50)
        reasons.append(f"🔵 REGIME 4H: BB SQUEEZE (width {sq_bars:.0f}%ile) — coiling! Tunggu arah breakout")
        # Belum boost — tunggu konfirmasi arah
    elif regime_str == "BREAKOUT_UP":
        pump_score += 18
        reasons.append(f"🚀 REGIME 4H: BREAKOUT UP — harga menembus range, momentum kuat!")
    elif regime_str == "BREAKOUT_DOWN":
        dump_score += 18
        reasons.append(f"🔻 REGIME 4H: BREAKOUT DOWN — harga breakdown range, bearish confirmed!")
    elif regime_str == "BULLISH_TREND":
        pump_score += 8
        reasons.append(f"📈 REGIME 4H: BULLISH TREND (ADX={regime_4h.get('adx',0):.0f}) — trend filter GREEN untuk LONG")
    elif regime_str == "BEARISH_TREND":
        dump_score += 8
        reasons.append(f"📉 REGIME 4H: BEARISH TREND (ADX={regime_4h.get('adx',0):.0f}) — trend filter GREEN untuk SHORT")

    # 1H regime alignment bonus
    regime_1h_str = regime_1h.get("regime", "UNKNOWN")
    if regime_str == "BULLISH_TREND" and regime_1h_str in ("BULLISH_TREND", "BREAKOUT_UP"):
        pump_score += 5
        reasons.append("✅ REGIME 1H+4H aligned BULLISH — double confirmation")
    elif regime_str == "BEARISH_TREND" and regime_1h_str in ("BEARISH_TREND", "BREAKOUT_DOWN"):
        dump_score += 5
        reasons.append("✅ REGIME 1H+4H aligned BEARISH — double confirmation")

    # Sudden breakout detector: ALLO-type pumps (explosive dari nowhere)
    sb_4h = tf_4h.get("sudden_breakout", {})
    sb_1h = tf_1h.get("sudden_breakout", {})
    if sb_4h.get("sudden_breakout"):
        if sb_4h.get("direction") == "UP":
            pump_score += 20
            reasons.append(f"🔥 SUDDEN BREAKOUT UP (4H): vol {sb_4h['vol_spike']:.1f}x, +{sb_4h['range_break_pct']:.1f}% range break!")
        else:
            dump_score += 20
            reasons.append(f"🔥 SUDDEN BREAKOUT DOWN (4H): vol {sb_4h['vol_spike']:.1f}x, -{sb_4h['range_break_pct']:.1f}% range break!")
    elif sb_1h.get("sudden_breakout"):
        if sb_1h.get("direction") == "UP":
            pump_score += 12
            reasons.append(f"⚡ Sudden breakout UP (1H): vol {sb_1h['vol_spike']:.1f}x — momentum aktif")
        else:
            dump_score += 12
            reasons.append(f"⚡ Sudden breakout DOWN (1H): vol {sb_1h['vol_spike']:.1f}x — momentum aktif")

    # ── MACD Momentum Confirmation (research: RSI+MACD ~77% win rate) ──────
    macd_4h = tf_4h.get("macd", {})
    macd_1h = tf_1h.get("macd", {})
    macd_1h_above = macd_1h.get("above", False)
    macd_4h_above = macd_4h.get("above", False)

    if macd_1h.get("cross_bull"):
        pump_score += 14
        reasons.append("📈 MACD 1H: Bullish crossover — momentum flip UP confirmed")
    elif macd_1h_above and macd_4h_above:
        pump_score += 8
        reasons.append(f"✅ MACD 1H+4H: Histogram positif — bullish momentum sustained")
    elif macd_1h_above:
        pump_score += 4
        reasons.append("🟢 MACD 1H: Histogram positif — mild bullish momentum")

    if macd_1h.get("cross_bear"):
        dump_score += 14
        reasons.append("📉 MACD 1H: Bearish crossover — momentum flip DOWN confirmed")
    elif not macd_1h_above and not macd_4h_above:
        dump_score += 8
        reasons.append(f"🔴 MACD 1H+4H: Histogram negatif — bearish momentum sustained")
    elif not macd_1h_above:
        dump_score += 4
        reasons.append("🟡 MACD 1H: Histogram negatif — mild bearish momentum")

    rej    = tf_15m.get("rejection", {})
    rej_1h = tf_1h.get("rejection", {})
    fvg    = tf_15m.get("fvg", {})

    if rej.get("type") == "BULLISH_REJECTION":
        pump_score += 12; reasons.append(f"✅ 15M: Bullish pin bar ({rej['detail']})")
    elif rej.get("type") == "BEARISH_REJECTION":
        dump_score += 12; reasons.append(f"🔴 15M: Bearish pin bar ({rej['detail']})")

    # 1H rejection candle: lebih berat dari 15M (TF lebih tinggi = lebih signifikan)
    # Hanya dihitung jika strength ≥ 60 (hindari noise wick kecil)
    _rej1h_str = rej_1h.get("strength", 0)
    if rej_1h.get("type") == "BULLISH_REJECTION" and _rej1h_str >= 60:
        pump_score += 10; reasons.append(f"✅ 1H: Bullish rejection candle (str:{_rej1h_str}) — buyer defended level")
    elif rej_1h.get("type") == "BEARISH_REJECTION" and _rej1h_str >= 60:
        dump_score += 10; reasons.append(f"🔴 1H: Bearish rejection candle (str:{_rej1h_str}) — seller rejected price")

    # ── v14: CANDLE STRUCTURE PATTERNS ───────────
    # 15M patterns (scalp entry precision)
    cp15 = tf_15m.get("candle_patterns", {})
    cp1h = tf_1h.get("candle_patterns", {})

    # Pattern score table: (pump_pts, dump_pts) keyed by pattern name
    _pattern_pts = {
        "BULLISH_ENGULFING":   (18, 0),
        "BEARISH_ENGULFING":   (0, 18),
        "MORNING_STAR":        (15, 0),
        "EVENING_STAR":        (0, 15),
        "THREE_WHITE_SOLDIERS":(12, 0),
        "THREE_BLACK_CROWS":   (0, 12),
        "BULLISH_MARUBOZU":    (10, 0),
        "BEARISH_MARUBOZU":    (0, 10),
        "DOJI":                (0,  0),   # neutral — no score
        "INSIDE_BAR":          (0,  0),   # neutral — no score
    }

    p15 = cp15.get("pattern", "NONE")
    if p15 in _pattern_pts:
        pp, dp = _pattern_pts[p15]
        if pp: pump_score += pp; reasons.append(f"🕯️ 15M Candle: {cp15['detail']} (+{pp}pts PUMP)")
        if dp: dump_score += dp; reasons.append(f"🕯️ 15M Candle: {cp15['detail']} (+{dp}pts DUMP)")

    p1h = cp1h.get("pattern", "NONE")
    if p1h in _pattern_pts:
        pp1, dp1 = _pattern_pts[p1h]
        # Reversal patterns (Evening Star, Morning Star, Engulfing) dapat full weight
        # karena mereka sinyal AKHIR dari trend yang ada — tidak boleh di-halve
        # Continuation/neutral patterns tetap half weight (konteks saja)
        _reversal_patterns = {"EVENING_STAR", "MORNING_STAR", "BULLISH_ENGULFING", "BEARISH_ENGULFING"}
        if p1h in _reversal_patterns:
            pp1h, dp1h = pp1, dp1
            _w_label = "full weight — reversal konfirmasi"
        else:
            pp1h, dp1h = pp1 // 2, dp1 // 2
            _w_label = "half weight"
        if pp1h: pump_score += pp1h; reasons.append(f"🕯️ 1H Candle: {cp1h['detail']} (+{pp1h}pts PUMP, {_w_label})")
        if dp1h: dump_score += dp1h; reasons.append(f"🕯️ 1H Candle: {cp1h['detail']} (+{dp1h}pts DUMP, {_w_label})")

    # INSIDE BAR on 15M = breakout setup — flag direction warning
    if p15 == "INSIDE_BAR":
        reasons.append("📦 15M: Inside Bar — kompresi, tunggu breakout konfirmasi sebelum entry")

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
        if d < 5:
            if ob4["bullish_ob"].get("is_fresh", True):
                pump_score += 8; reasons.append(f"✅ 4H: Price near Bullish OB ({d:.1f}% away)")
            else:
                reasons.append(f"⚠️ 4H: Bullish OB ({d:.1f}% away) sudah MITIGATED — tidak valid sebagai support")
    if ob4.get("bearish_ob"):
        d = ob4["bearish_ob"].get("distance_pct", 999)
        if d < 5:
            if ob4["bearish_ob"].get("is_fresh", True):
                dump_score += 8; reasons.append(f"🔴 4H: Price near Bearish OB ({d:.1f}% away)")
            else:
                reasons.append(f"⚠️ 4H: Bearish OB ({d:.1f}% away) sudah MITIGATED — tidak valid sebagai resistance")
    if ob1.get("bullish_ob"):
        d = ob1["bullish_ob"].get("distance_pct", 999)
        if d < 3:
            if ob1["bullish_ob"].get("is_fresh", True):
                pump_score += 5; reasons.append(f"✅ 1H: Price near Bullish OB ({d:.1f}% away)")
            else:
                reasons.append(f"⚠️ 1H: Bullish OB ({d:.1f}% away) sudah MITIGATED — tidak valid sebagai support")
    if ob1.get("bearish_ob"):
        d = ob1["bearish_ob"].get("distance_pct", 999)
        if d < 3:
            if ob1["bearish_ob"].get("is_fresh", True):
                dump_score += 5; reasons.append(f"🔴 1H: Price near Bearish OB ({d:.1f}% away)")
            else:
                reasons.append(f"⚠️ 1H: Bearish OB ({d:.1f}% away) sudah MITIGATED — tidak valid sebagai resistance")

    oi_chg      = oi_data.get("oi_change_pct")
    ls_bias     = oi_data.get("ls_bias", "UNKNOWN")
    ls_ratio    = oi_data.get("ls_ratio")
    top_ls_bias = oi_data.get("top_ls_bias", "UNKNOWN")
    top_ls_ratio = oi_data.get("top_ls_ratio")
    _basis_sc   = oi_data.get("perp_spot_basis")

    if oi_chg is not None:
        if oi_chg > 5:
            reasons.append(f"📈 OI rising +{oi_chg:.1f}% — strong conviction")
            if pump_score > dump_score: pump_score += 8
            else: dump_score += 8
        elif oi_chg < -5:
            reasons.append(f"📉 OI falling {oi_chg:.1f}% — deleverage")

    # Top Trader ratio takes precedence over global ratio
    if top_ls_bias == "SHORT_HEAVY" and top_ls_ratio:
        pump_score += 9
        reasons.append(f"🎯 Top Trader short-heavy: {top_ls_ratio:.2f} → smart money squeeze → PUMP")
    elif top_ls_bias == "LONG_HEAVY" and top_ls_ratio:
        dump_score += 9
        reasons.append(f"⚠️ Top Trader long-heavy: {top_ls_ratio:.2f} → smart money longs trapped → DUMP")
    elif ls_bias == "SHORT_HEAVY" and ls_ratio:
        pump_score += 6
        reasons.append(f"🎯 L/S: {ls_ratio:.2f} (short-heavy) → squeeze potential → PUMP")
    elif ls_bias == "LONG_HEAVY" and ls_ratio:
        dump_score += 6
        reasons.append(f"⚠️ L/S: {ls_ratio:.2f} (long-heavy) → long liq risk → DUMP")

    # Perp-spot basis
    if _basis_sc is not None:
        if _basis_sc < -0.05:
            pump_score += 5
            reasons.append(f"📉 Basis {_basis_sc:.3f}% → shorts crowded → squeeze → PUMP")
        elif _basis_sc > 0.1:
            dump_score += 5
            reasons.append(f"📈 Basis {_basis_sc:.3f}% → longs crowded → dump risk → SHORT")

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

    _mf15m_strong_contra_inflow  = (mf_15m.get("bias") == "OUTFLOW" and mf_15m.get("strength") == "STRONG")
    _mf15m_strong_contra_outflow = (mf_15m.get("bias") == "INFLOW"  and mf_15m.get("strength") == "STRONG")

    if inflow_count >= 2:
        # Jika 15M STRONG OUTFLOW sementara HTF inflow, LTF sedang memimpin reversal — kurangi bonus
        pts = (6 if _mf15m_strong_contra_inflow else (12 if strong_count >= 1 else 7))
        pump_score += pts
        mfi_avg = round(sum(mf.get("mfi", 50) for mf in [mf_4h, mf_1h, mf_15m]) / 3, 0)
        _contra_note = " ⚠️ 15M kontra OUTFLOW" if _mf15m_strong_contra_inflow else ""
        reasons.append(f"💚 Money Flow: INFLOW di {inflow_count}/3 TF (MFI avg {mfi_avg:.0f}) — buyer pressure{_contra_note}")
    elif outflow_count >= 2:
        pts = (6 if _mf15m_strong_contra_outflow else (12 if strong_count >= 1 else 7))
        dump_score += pts
        mfi_avg = round(sum(mf.get("mfi", 50) for mf in [mf_4h, mf_1h, mf_15m]) / 3, 0)
        _contra_note = " ⚠️ 15M kontra INFLOW" if _mf15m_strong_contra_outflow else ""
        reasons.append(f"🔴 Money Flow: OUTFLOW di {outflow_count}/3 TF (MFI avg {mfi_avg:.0f}) — seller pressure{_contra_note}")
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

    # v14: Real-time momentum contribution (1m candles — zero lag)
    if realtime and not realtime.get("error"):
        rt_label = realtime.get("momentum_label", "")
        rt_burst = realtime.get("vol_burst", 1.0)
        rt_v15   = realtime.get("velocity_15m", 0)
        rt_bias  = realtime.get("short_bias", "NEUTRAL")
        rt_bo_up = realtime.get("breakout_up", False)
        rt_bo_dn = realtime.get("breakout_down", False)

        if "STRONG_BULL" in rt_label:
            pump_score += 12
            reasons.append(f"⚡ RT: Strong bull momentum 15m (+{rt_v15:.2f}%) vol {rt_burst:.1f}x burst")
        elif "BULL_MOMENTUM" in rt_label or rt_bias == "BULLISH":
            pump_score += 7
            reasons.append(f"🟢 RT: Bull momentum 1m (+{rt_v15:.2f}%) — buying pressure aktif")
        elif "STRONG_BEAR" in rt_label:
            dump_score += 12
            reasons.append(f"⚡ RT: Strong bear momentum 15m ({rt_v15:.2f}%) vol {rt_burst:.1f}x burst")
        elif "BEAR_MOMENTUM" in rt_label or rt_bias == "BEARISH":
            dump_score += 7
            reasons.append(f"🔴 RT: Bear momentum 1m ({rt_v15:.2f}%) — selling pressure aktif")

        if rt_bo_up:
            pump_score += 8
            reasons.append(f"💥 RT: Breakout UP dari range 15 menit terakhir — momentum")
        elif rt_bo_dn:
            dump_score += 8
            reasons.append(f"💥 RT: Breakout DOWN dari range 15 menit terakhir — momentum")

    # ── v14: ENTRY ZONE PROXIMITY CHECK ─────────────
    # Kalau price sudah extended jauh dari semua key zones, sinyal berisiko tinggi (SL-prone).
    price_now = tf_1h.get("price", 0)
    ob1       = tf_1h.get("order_blocks", {})
    ob4       = tf_4h.get("order_blocks", {})
    fvg1      = tf_1h.get("fvg", {})
    fvg15_chk = tf_15m.get("fvg", {})

    _zone_distances = []
    if ob1.get("bullish_ob"): _zone_distances.append(abs(ob1["bullish_ob"].get("distance_pct", 999)))
    if ob1.get("bearish_ob"): _zone_distances.append(abs(ob1["bearish_ob"].get("distance_pct", 999)))
    if ob4.get("bullish_ob"): _zone_distances.append(abs(ob4["bullish_ob"].get("distance_pct", 999)))
    if ob4.get("bearish_ob"): _zone_distances.append(abs(ob4["bearish_ob"].get("distance_pct", 999)))
    if fvg1.get("bullish_fvg"): _zone_distances.append(abs(fvg1["bullish_fvg"].get("distance_pct", 999)))
    if fvg1.get("bearish_fvg"): _zone_distances.append(abs(fvg1["bearish_fvg"].get("distance_pct", 999)))
    if fvg15_chk.get("bullish_fvg"): _zone_distances.append(abs(fvg15_chk["bullish_fvg"].get("distance_pct", 999)))
    if fvg15_chk.get("bearish_fvg"): _zone_distances.append(abs(fvg15_chk["bearish_fvg"].get("distance_pct", 999)))

    _nearest_zone = min(_zone_distances) if _zone_distances else 999
    entry_extended = _nearest_zone > 3.5  # price >3.5% dari semua zones = extended/chasing

    if entry_extended and _nearest_zone < 999:
        # Extended entry: kurangi score, flag ke user
        pump_score = int(pump_score * 0.80)
        dump_score = int(dump_score * 0.80)
        reasons.append(
            f"⚠️ ENTRY EXTENDED: price {_nearest_zone:.1f}% dari nearest zone — "
            f"risiko entry di puncak/bottom, tunggu pullback ke OB/FVG"
        )
    elif _nearest_zone <= 1.0:
        # Sniper zone: price at key level — bonus
        reasons.append(
            f"🎯 SNIPER ZONE: price {_nearest_zone:.1f}% dari key zone — "
            f"ini entry presisi, konfirmasi candle 15M!"
        )

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
        "pump_score": pump_score, "dump_score": dump_score, "reasons": reasons,
        "regime": regime_str, "entry_extended": entry_extended,
        "nearest_zone_pct": round(_nearest_zone, 2) if _nearest_zone < 999 else None,
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

def _compute_tp_profile(tf_4h: dict, tf_1h: dict, direction: str) -> dict:
    """Skema TP/SL adaptif ke kondisi market.

    Default condong KONSERVATIF; target jauh (AGGRESSIVE) hanya saat trend
    searah & kuat (ADX tinggi). Tujuannya mengurangi over-optimisme TP di market
    ranging/choppy/volatile yang bikin TP2 jarang kena dan trade malah balik arah.

    Returns: {label, tp1_mult, tp2_mult, sl_atr}
      - tp1_mult/tp2_mult: kelipatan R untuk TP1/TP2
      - sl_atr: kelipatan ATR untuk SL fallback (MARKET entry)
    """
    r4 = (tf_4h or {}).get("market_regime", {}) or {}
    r1 = (tf_1h or {}).get("market_regime", {}) or {}
    reg4 = r4.get("regime", "UNKNOWN")
    reg1 = r1.get("regime", "UNKNOWN")
    adx4 = r4.get("adx", 0) or 0
    is_long = direction in ("LONG", "PUMP")

    trend_aligned = (
        (is_long     and reg4 in ("BULLISH_TREND", "BREAKOUT_UP")) or
        (not is_long and reg4 in ("BEARISH_TREND", "BREAKOUT_DOWN"))
    )
    choppy = (reg4 in ("RANGING", "BB_SQUEEZE", "VOLATILE") or
              reg1 in ("RANGING", "BB_SQUEEZE", "VOLATILE") or adx4 < 18)

    # `ladder` = kelipatan R untuk TP bertahap (TP1..TPn). ladder[0]/[1] = tp1/tp2.
    # Jumlah rung ikut agresivitas: choppy = sedikit (3), trending = sampai 5.
    if trend_aligned and adx4 >= 28:
        return {"label": "AGGRESSIVE",   "tp1_mult": 2.0, "tp2_mult": 3.5, "sl_atr": 2.0,
                "ladder": [2.0, 3.5, 5.0, 7.0, 10.0]}
    if choppy:
        return {"label": "CONSERVATIVE", "tp1_mult": 1.2, "tp2_mult": 2.0, "sl_atr": 1.5,
                "ladder": [1.2, 2.0, 3.0]}
    return {"label": "BALANCED",         "tp1_mult": 1.5, "tp2_mult": 2.5, "sl_atr": 1.8,
            "ladder": [1.5, 2.5, 3.5, 4.5]}


def _build_tp_ladder(entry: float, sl: float, direction: str,
                     tp1: float, tp2: float, ladder_mults: list) -> list:
    """Bangun TP bertahap (ladder TP1..TPn).

    Rung 1 & 2 = tp1/tp2 (sudah struktural dari calculate_tp1_tp2). Rung 3+ =
    target murni R-multiple yang lebih jauh (runner). Strictly monotonic menjauh
    dari entry. Tiap rung: {level, price, r, pct}.
    """
    is_long = direction in ("LONG", "PUMP")
    risk = abs(entry - sl) if (sl and sl != entry) else entry * 0.02
    prices = []
    for p in (tp1, tp2):
        if p is not None:
            prices.append(round(float(p), 8))
    prev = prices[-1] if prices else entry
    for mult in (ladder_mults[2:] if ladder_mults else []):
        p = round(entry + risk * mult, 8) if is_long else round(entry - risk * mult, 8)
        if (is_long and p <= prev) or ((not is_long) and p >= prev):
            continue   # lewati rung yang tidak lebih jauh dari rung sebelumnya
        prices.append(p)
        prev = p
    out = []
    for i, p in enumerate(prices):
        r   = round(abs(p - entry) / risk, 2) if risk > 0 else 0.0
        pct = round((p - entry) / entry * 100 * (1 if is_long else -1), 2) if entry else 0.0
        out.append({"level": i + 1, "price": p, "r": r, "pct": pct})
    return out


def _entry_action_reco(direction: str, confluence: dict = None,
                       realtime: dict = None, entry_mode: str = None) -> str:
    """Rekomendasi aksi singkat: '{DIR} NOW' vs 'WAIT'.

    Kalau entry_mode sudah diketahui (trade plan final) → pakai itu (definitif).
    Kalau belum (mis. saat alert bias flip) → heuristik dari momentum + confluence.
    """
    dir_word = "LONG" if direction in ("LONG", "PUMP") else "SHORT"
    is_long  = dir_word == "LONG"

    if entry_mode == "MOMENTUM_NOW":
        return f"🚀 {dir_word} NOW — entry market, momentum aktif"
    if entry_mode == "RETEST_WAIT":
        return f"⏳ WAIT — tunggu harga retest ke zona entry"

    conf  = confluence or {}
    rt    = realtime or {}
    level = conf.get("level", "POOR")
    if conf.get("entry_extended"):
        return "⏳ WAIT — harga sudah extended, tunggu pullback/retest"
    mlabel = rt.get("momentum_label", "CONSOLIDATING")
    mom_aligned = (
        (is_long     and mlabel in ("STRONG_BULL_MOMENTUM", "BULL_MOMENTUM", "BREAKOUT_UP")) or
        (not is_long and mlabel in ("STRONG_BEAR_MOMENTUM", "BEAR_MOMENTUM", "BREAKOUT_DOWN"))
    )
    if mom_aligned and level in ("EXCELLENT", "GOOD"):
        return f"🚀 {dir_word} NOW — momentum & confluence mendukung entry sekarang"
    return f"⏳ WAIT — tunggu konfirmasi/retest ke zona entry"


def calculate_tp1_tp2(entry: float, sl: float, direction: str,
                      tf_4h: dict = None, tf_1h: dict = None,
                      liq_1h: dict = None,
                      tp1_mult: float = 2.0, tp2_mult: float = 3.5) -> dict:
    """
    Hitung TP1 dan TP2 berbasis struktur.
    - Risk (R) = abs(entry - sl)
    - TP1: minimal tp1_mult×R, diprioritaskan ke level struktural terdekat
    - TP2: minimal tp2_mult×R, ke target struktural selanjutnya atau EQH/EQL
    - tp1_mult/tp2_mult diset adaptif ke kondisi market oleh caller (lihat
      _compute_tp_profile) supaya target tidak terlalu optimis di market choppy.
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
        min_tp1 = round(entry + risk * tp1_mult, 8)
        min_tp2 = round(entry + risk * tp2_mult, 8)

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
            result["tp1"], result["tp1_basis"] = min_tp1, f"{tp1_mult:g}R floor"
        if result["tp2"] is None:
            result["tp2"], result["tp2_basis"] = min_tp2, f"{tp2_mult:g}R floor"

    elif direction in ("SHORT", "DUMP"):
        min_tp1 = round(entry - risk * tp1_mult, 8)
        min_tp2 = round(entry - risk * tp2_mult, 8)

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
            result["tp1"], result["tp1_basis"] = min_tp1, f"{tp1_mult:g}R floor"
        # TP2 HARUS selalu lebih rendah dari TP1 — guard wajib
        if result["tp2"] is None or result["tp2"] >= result["tp1"]:
            result["tp2"], result["tp2_basis"] = min_tp2, f"{tp2_mult:g}R floor"

    if risk > 0:
        if result["tp1"]: result["tp1_r"] = round(abs(result["tp1"] - entry) / risk, 2)
        if result["tp2"]: result["tp2_r"] = round(abs(result["tp2"] - entry) / risk, 2)

    return result


def _sanitize_trade_levels(trade: dict, direction: str) -> dict:
    """Pastikan TP/SL ada di sisi yang BENAR relatif entry, perbaiki kalau tidak.

    Level struktural (atau hasil override AI di deepseek_signal_review) kadang
    menghasilkan TP di sisi salah dari entry — mis. LONG dengan TP < entry —
    yang bikin sinyal langsung "kena TP" tapi rugi. Helper ini memperbaiki
    level yang salah-sisi via fallback R:R, supaya:
      LONG : sl < entry < tp1 < tp2
      SHORT: tp2 < tp1 < entry < sl
    Mutasi & kembalikan dict yang sama. Kalau entry tidak valid, dibiarkan.
    """
    if not isinstance(trade, dict):
        return trade
    try:
        entry = float(trade.get("entry") or 0)
    except (TypeError, ValueError):
        return trade
    if entry <= 0:
        return trade

    is_long = direction in ("LONG", "PUMP")

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    sl = _f(trade.get("sl"))
    # SL salah-sisi atau hilang → set default 3% (jaga R:R tetap masuk akal).
    if sl is None or (is_long and sl >= entry) or (not is_long and sl <= entry):
        sl = round(entry * (0.97 if is_long else 1.03), 8)
        trade["sl"] = sl
        trade["sl_basis"] = (str(trade.get("sl_basis", "")) + " | repaired 3%").strip(" |")

    risk = abs(entry - sl) or entry * 0.02

    def _wrong_side(tp):
        tp = _f(tp)
        if tp is None or tp <= 0:
            return True
        return (is_long and tp <= entry) or (not is_long and tp >= entry)

    if _wrong_side(trade.get("tp1")):
        trade["tp1"] = round(entry + risk * 2.0, 8) if is_long else round(entry - risk * 2.0, 8)
        trade["tp1_basis"] = "2R floor (repaired)"
    if _wrong_side(trade.get("tp2")):
        trade["tp2"] = round(entry + risk * 3.5, 8) if is_long else round(entry - risk * 3.5, 8)
        trade["tp2_basis"] = "3.5R floor (repaired)"

    # TP2 harus lebih jauh dari TP1.
    tp1, tp2 = _f(trade["tp1"]), _f(trade["tp2"])
    if is_long and tp2 <= tp1:
        trade["tp2"] = round(entry + risk * 3.5, 8)
    elif (not is_long) and tp2 >= tp1:
        trade["tp2"] = round(entry - risk * 3.5, 8)

    # Recompute R multiples & sinkron alias "tp".
    if risk > 0:
        trade["tp1_r"] = round(abs(_f(trade["tp1"]) - entry) / risk, 2)
        trade["tp2_r"] = round(abs(_f(trade["tp2"]) - entry) / risk, 2)
    if "tp" in trade:
        trade["tp"] = trade["tp1"]

    # Rebuild ladder TP biar konsisten dengan entry/sl (mis. setelah override AI):
    # rung 1/2 = tp1/tp2 final, rung 3+ dihitung ulang dari R-multiple tersimpan.
    tps = trade.get("tps")
    if isinstance(tps, list) and tps:
        rebuilt = []
        for rung in tps:
            lvl = rung.get("level")
            if lvl == 1:
                p = _f(trade["tp1"])
            elif lvl == 2:
                p = _f(trade["tp2"])
            else:
                rr = rung.get("r") or 0
                p = round(entry + risk * rr, 8) if is_long else round(entry - risk * rr, 8)
            if p is None:
                continue
            rmult = round(abs(p - entry) / risk, 2) if risk > 0 else 0.0
            pct   = round((p - entry) / entry * 100 * (1 if is_long else -1), 2) if entry else 0.0
            rebuilt.append({"level": lvl, "price": p, "r": rmult, "pct": pct})
        trade["tps"] = rebuilt
    return trade


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

    # ── v14: SNIPER ZONE CHECK ────────────────────────
    # Periksa apakah candle 15M sudah konfirmasi di zona (engulfing/pin bar at zone)
    cp15 = tf_15m.get("candle_patterns", {}) if tf_15m else {}
    rej15 = tf_15m.get("rejection", {}) if tf_15m else {}
    sniper_patterns = {"BULLISH_ENGULFING", "MORNING_STAR", "BULLISH_MARUBOZU", "THREE_WHITE_SOLDIERS"}
    sniper_patterns_short = {"BEARISH_ENGULFING", "EVENING_STAR", "BEARISH_MARUBOZU", "THREE_BLACK_CROWS"}
    has_sniper_candle = (
        (direction == "PUMP" and (
            cp15.get("pattern") in sniper_patterns or
            rej15.get("type") == "BULLISH_REJECTION"))
        or
        (direction == "DUMP" and (
            cp15.get("pattern") in sniper_patterns_short or
            rej15.get("type") == "BEARISH_REJECTION"))
    )

    # ── v14: EXTENDED ENTRY CHECK ─────────────────────
    # Cek apakah price sudah jauh dari semua zones (chasing = SL-prone)
    _all_zone_dist = []
    for cand in entry_candidates:
        _all_zone_dist.append(abs(cand[1] - price) / price * 100 if price > 0 else 999)
    nearest_zone_dist = min(_all_zone_dist) if _all_zone_dist else 999
    price_extended = nearest_zone_dist > 2.5 and not no_retest_zone  # ada zone tapi price jauh

    # Check regime: di RANGING market, butuh breakout konfirmasi
    regime_1h_mode = tf_1h.get("market_regime", {}) if tf_1h else {}
    is_ranging      = regime_1h_mode.get("regime") == "RANGING"
    bb_sq_1h        = tf_1h.get("bb_squeeze", {}) if tf_1h else {}
    is_squeeze      = bb_sq_1h.get("squeeze", False)

    if strong_momentum or moderate_no_zone:
        if price_extended:
            # Momentum ada tapi price extended — downgrade ke RETEST_WAIT
            hints_ext = (
                [f"Price {nearest_zone_dist:.1f}% dari nearest zone — TUNGGU pullback ke OB/FVG",
                 "Jangan chasing — entry di zone = SL lebih kecil",
                 "Konfirmasi: bullish engulfing/pin bar saat retest"]
                if direction == "PUMP" else
                [f"Price {nearest_zone_dist:.1f}% dari nearest zone — TUNGGU rally ke OB/FVG",
                 "Jangan chasing — entry di zone = SL lebih kecil",
                 "Konfirmasi: bearish engulfing/pin bar saat retest"]
            )
            return {"mode": "RETEST_WAIT", "momentum_signals": momentum_signals,
                    "momentum_reasons": momentum_reasons + [f"⚠️ Extended {nearest_zone_dist:.1f}% dari zone"],
                    "confirmation_hints": hints_ext}
        return {"mode": "MOMENTUM_NOW", "momentum_signals": momentum_signals,
                "momentum_reasons": momentum_reasons, "confirmation_hints": []}

    # SNIPER_ENTRY: price at zone + candle confirms → immediate entry
    if has_sniper_candle and entry_candidates and nearest_zone_dist <= 1.0:
        sniper_candle_name = cp15.get("pattern", rej15.get("type", "rejection"))
        return {
            "mode": "SNIPER_ENTRY",
            "momentum_signals": momentum_signals,
            "momentum_reasons": momentum_reasons + [f"🎯 {sniper_candle_name} confirmed at zone"],
            "confirmation_hints": [
                f"🎯 SNIPER: {cp15.get('detail', rej15.get('detail', 'candle confirmed at zone'))}",
                "Entry NOW — zone + candle confluence terpenuhi",
                "SL ketat: 1x ATR di bawah zone bottom",
            ]
        }

    hints = (["Candle 15M close di atas zona (bullish engulfing/pin bar at OB/FVG)",
              "Volume >= 1.5x saat konfirmasi — buyer commitment",
              "RSI 15M masih < 65 — tidak overbought"]
             if direction == "PUMP" else
             ["Candle 15M close di bawah zona (bearish engulfing/pin bar at OB/FVG)",
              "Volume >= 1.5x saat konfirmasi — seller commitment",
              "RSI 15M masih > 35 — tidak oversold"])
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
            if ob.get("is_fresh", True):
                entry_candidates.append(("4H_OB", round(ob.get("bottom", price)*1.002,8), ob.get("bottom",price), 4, ob.get("bottom",price), ob.get("top",price)))
        if ob1.get("bullish_ob") and ob1["bullish_ob"].get("distance_pct", 999) < 5:
            ob = ob1["bullish_ob"]
            if ob.get("is_fresh", True):
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

        # EMA pullback entry: price pulling back to EMA21 (1H) or EMA21 (4H)
        # EMA21 = strong dynamic support in uptrend — common pro entry zone
        ema21_1h = tf_1h.get("ema21", 0)
        ema9_1h  = tf_1h.get("ema9", 0)
        ema21_4h = tf_4h.get("ema21", 0)
        t4_pump  = struct4.get("trend", "UNKNOWN")
        t1_pump  = struct1.get("trend", "UNKNOWN")
        if ema21_1h > 0 and t1_pump == "BULLISH":
            dist_ema = (price - ema21_1h) / price * 100
            if 0.1 < dist_ema < 3.0:  # price 0.1–3% above EMA21 1H = near support
                entry_candidates.append(("1H_EMA21", round(ema21_1h * 1.001, 8), round(ema21_1h * 0.997, 8), 2,
                                         round(ema21_1h * 0.997, 8), round(ema21_1h * 1.003, 8)))
        if ema21_4h > 0 and t4_pump == "BULLISH":
            dist_ema4 = (price - ema21_4h) / price * 100
            if 0.1 < dist_ema4 < 5.0:  # 4H EMA21 = macro pullback zone
                entry_candidates.append(("4H_EMA21", round(ema21_4h * 1.001, 8), round(ema21_4h * 0.995, 8), 3,
                                         round(ema21_4h * 0.995, 8), round(ema21_4h * 1.005, 8)))

        # v13: entry mode detection
        mode_info = _determine_entry_mode(price, "PUMP", entry_candidates, tf_1h, tf_15m, oi_data)
        _tp_prof  = _compute_tp_profile(tf_4h, tf_1h, "LONG")

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
            sl = round(price - atr * _tp_prof["sl_atr"], 8) if atr > 0 else round(price * 0.96, 8)
            sl = max(sl, round(price * 0.94, 8))

        tps = calculate_tp1_tp2(entry, sl, "LONG", tf_4h, tf_1h, liq_1h,
                                tp1_mult=_tp_prof["tp1_mult"], tp2_mult=_tp_prof["tp2_mult"])
        tp_ladder = _build_tp_ladder(entry, sl, "LONG", tps["tp1"], tps["tp2"], _tp_prof["ladder"])
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
            "tp_profile": _tp_prof["label"],
            "tps": tp_ladder,
            "confirmation_zone": confirmation_zone,
            "momentum_context": mode_info.get("momentum_reasons", []),
            "entry_instruction": entry_instruction,
        }

    elif direction == "DUMP":
        entry_candidates = []

        if ob4.get("bearish_ob") and ob4["bearish_ob"].get("distance_pct", 999) < 4:
            ob = ob4["bearish_ob"]
            if ob.get("is_fresh", True):
                entry_candidates.append(("4H_OB", round(ob.get("top",price)*0.998,8), ob.get("top",price), 4, ob.get("bottom",price), ob.get("top",price)))
        if ob1.get("bearish_ob") and ob1["bearish_ob"].get("distance_pct", 999) < 5:
            ob = ob1["bearish_ob"]
            if ob.get("is_fresh", True):
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

        # EMA pullback entry for DUMP: price rallying back to EMA21 (dynamic resistance)
        ema21_1h_d = tf_1h.get("ema21", 0)
        ema21_4h_d = tf_4h.get("ema21", 0)
        t4_dump    = struct4.get("trend", "UNKNOWN")
        t1_dump    = struct1.get("trend", "UNKNOWN")
        if ema21_1h_d > 0 and t1_dump == "BEARISH":
            dist_ema_d = (ema21_1h_d - price) / price * 100
            if 0.1 < dist_ema_d < 3.0:  # price 0.1–3% below EMA21 1H = near resistance
                entry_candidates.append(("1H_EMA21", round(ema21_1h_d * 0.999, 8), round(ema21_1h_d * 1.003, 8), 2,
                                         round(ema21_1h_d * 0.997, 8), round(ema21_1h_d * 1.003, 8)))
        if ema21_4h_d > 0 and t4_dump == "BEARISH":
            dist_ema4_d = (ema21_4h_d - price) / price * 100
            if 0.1 < dist_ema4_d < 5.0:
                entry_candidates.append(("4H_EMA21", round(ema21_4h_d * 0.999, 8), round(ema21_4h_d * 1.005, 8), 3,
                                         round(ema21_4h_d * 0.995, 8), round(ema21_4h_d * 1.005, 8)))

        # v13: entry mode detection
        mode_info = _determine_entry_mode(price, "DUMP", entry_candidates, tf_1h, tf_15m, oi_data)
        _tp_prof  = _compute_tp_profile(tf_4h, tf_1h, "SHORT")

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
            sl = round(price + atr * _tp_prof["sl_atr"], 8) if atr > 0 else round(price * 1.04, 8)
            sl = min(sl, round(price * 1.06, 8))

        tps = calculate_tp1_tp2(entry, sl, "SHORT", tf_4h, tf_1h, liq_1h,
                                tp1_mult=_tp_prof["tp1_mult"], tp2_mult=_tp_prof["tp2_mult"])
        tp_ladder = _build_tp_ladder(entry, sl, "SHORT", tps["tp1"], tps["tp2"], _tp_prof["ladder"])
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
            "tp_profile": _tp_prof["label"],
            "tps": tp_ladder,
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


def _fmt_price(v) -> str:
    """Format harga presisi penuh (bukan singkatan K/M) untuk entry/TP/SL."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "$0"
    return f"${v:,.4f}" if v >= 1 else f"${v:.8f}"

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


def _sanitize_ai_output(text: str) -> str:
    """
    Bersihkan output AI sebelum dimasukkan ke pesan HTML Telegram.

    Masalah umum:
    1. AI kadang pakai **bold** atau *italic* (markdown) → convert ke HTML
    2. AI kadang output karakter < > & → harus di-escape agar tidak break HTML parser Telegram
    3. AI kadang pakai # header → strip

    Urutan penting: escape dulu, BARU convert markdown ke HTML tags.
    Kalau dibalik, karakter < dari HTML tag yang baru dibuat malah ikut di-escape.
    """
    import html as _html_lib

    if not text:
        return text

    # 1. Strip markdown headers (## Heading → Heading)
    text = re.sub(r"^#{1,4}\s*", "", text, flags=re.MULTILINE)

    # 2. Escape karakter HTML special SEBELUM insert tag apapun
    #    Tapi skip kalau sudah ada HTML tag (misalnya dari bagian pesan lain)
    #    Untuk AI output yang seharusnya plain/markdown, aman di-escape semua
    text = _html_lib.escape(text)

    # 3. Convert **bold** → <b>bold</b>  (setelah escape, ** tidak kena escape)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # 4. Convert *italic* (single asterisk, bukan bagian dari **)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)

    # 5. Convert __bold__ → <b>bold</b>
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    return text.strip()

def send_telegram(message: str, chat_id: str = None, parse_mode: str = None,
                  thread_id: str = None):
    """Kirim pesan ke Telegram. Return message_id pesan pertama (atau None).

    thread_id: message_thread_id untuk Telegram Topics (forum supergroup).
               Kalau diset, pesan masuk ke topic yang sesuai dalam grup yang sama.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.warning("Telegram credentials missing!")
        return None

    target = chat_id or TELEGRAM_CHAT_ID
    if not target:
        log.warning("No chat_id for Telegram!")
        return None

    html_message = _md_to_html(message)

    max_len = 4000
    chunks  = [html_message[i:i+max_len] for i in range(0, len(html_message), max_len)]

    # Resolve effective thread_id: explicit arg → thread-local ctx → None
    effective_tid = thread_id or getattr(_msg_ctx, "thread_id", None)

    # Build base payload — tambahkan message_thread_id kalau ada
    def _make_payload(text: str, plain: bool = False) -> dict:
        p = {"chat_id": target, "text": text}
        if not plain:
            p["parse_mode"] = "HTML"
        if effective_tid:
            p["message_thread_id"] = int(effective_tid)
        return p

    first_message_id = None
    for chunk in chunks:
        sent = False
        # Coba kirim HTML dulu
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json=_make_payload(chunk),
                    timeout=15
                )
                if r.status_code == 200:
                    log.info("✅ Telegram sent OK (HTML)")
                    if first_message_id is None:
                        try:
                            first_message_id = r.json()["result"]["message_id"]
                        except Exception:
                            pass
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
                    json=_make_payload(plain, plain=True),
                    timeout=15
                )
                if r.status_code == 200:
                    log.info("✅ Telegram sent OK (plain fallback)")
                    if first_message_id is None:
                        try:
                            first_message_id = r.json()["result"]["message_id"]
                        except Exception:
                            pass
                else:
                    log.warning(f"Telegram plain fallback error {r.status_code}: {r.text[:80]}")
            except Exception as e:
                log.error(f"Telegram plain fallback exception: {e}")

        time.sleep(0.5)

    return first_message_id


# ─────────────────────────────────────────────
# ROOM-ROUTED SENDERS
# ─────────────────────────────────────────────

def send_signal(message: str, parse_mode: str = None):
    """Kirim sinyal ke topic Signal (SIGNAL_THREAD_ID). Fallback ke General kalau thread belum diset."""
    return send_telegram(message, thread_id=SIGNAL_THREAD_ID, parse_mode=parse_mode)


def send_market_update(message: str, parse_mode: str = None):
    """Kirim news/market update ke topic Market Update (MARKET_UPDATE_THREAD_ID)."""
    return send_telegram(message, thread_id=MARKET_UPDATE_THREAD_ID, parse_mode=parse_mode)


def send_trade_report(message: str, parse_mode: str = None):
    """Kirim trade outcome/report ke topic Trade Reports (TRADE_REPORT_THREAD_ID)."""
    return send_telegram(message, thread_id=TRADE_REPORT_THREAD_ID, parse_mode=parse_mode)


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
                               with_claude: bool = False,
                               realtime: dict = None) -> list:
    """
    Build rich analysis block per koin.
    v14: AI chain — Groq (cepat) → Gemini (grounding) → Claude (fallback)
    with_gemini=True → trigger AI analysis (nama dipertahankan untuk kompatibilitas)
    Auto scan selalu with_gemini=False untuk hemat token.
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
            _sl_pct = scalp.get("scalp_sl_pct", SCALP_SL_PCT * 100)
            lines.append(
                f"  TP: <code>{fmt_num(scalp['scalp_tp'])}</code>  (+{SCALP_TP_PCT*100:.1f}%)  "
                f"SL: <code>{fmt_num(scalp['scalp_sl'])}</code>  (-{_sl_pct:.2f}% ATR)"
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
            _sw_sl_pct = swing.get("swing_sl_pct", SWING_SL_PCT * 100)
            lines.append(
                f"  TP: <code>{fmt_num(swing['swing_tp'])}</code>  (+{SWING_TP_PCT*100:.1f}%)  "
                f"SL: <code>{fmt_num(swing['swing_sl'])}</code>  (-{_sw_sl_pct:.2f}% ATR)  "
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
        _t_dir  = trade.get("direction", "")
        _t_tp1  = trade.get("tp1") or 0
        _t_sl   = trade.get("sl") or 0
        _t_ent  = trade.get("entry") or price
        _tp_ok  = (_t_dir == "LONG"  and _t_tp1 > _t_ent) or \
                  (_t_dir == "SHORT" and 0 < _t_tp1 < _t_ent)

        is_limit    = trade.get("is_limit", False)
        entry_icon  = "🎯" if is_limit else "⚡"
        entry_label = "LIMIT" if is_limit else "MARKET"
        tp1_r       = trade.get("tp1_r", trade.get("rr", 0))
        tp2_r       = trade.get("tp2_r", 0)
        rr_ok       = tp1_r >= 2.0

        lines.append("")
        if not _tp_ok or not _t_sl:
            lines.append("📍 <b>Trade Plan</b> — <i>⚠️ setup belum valid, tunggu konfirmasi struktur dulu</i>")
        else:
            lines.append(f"📍 <b>Trade Plan — {_t_dir} ({entry_label})</b>")
            lines.append(f"  {entry_icon} Entry : <code>{fmt_num(_t_ent)}</code>  ← {trade.get('entry_type','')}")
            lines.append(f"  🔴 SL    : <code>{fmt_num(_t_sl)}</code>")
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

    # ── AI Insight untuk /analyze, /scalp, /prepump, dll ──
    # v15: DeepSeek primary → Groq fallback → Gemini fallback → Claude last resort
    if with_gemini:
        lines.append("")
        insight  = ""
        ai_label = ""

        # 1. DeepSeek — primary AI strategist (analisa SMC + news + memory)
        if DEEPSEEK_MODULE and DEEPSEEK_API_KEY and not insight:
            _news_ctx = None
            if NEWS_MODULE:
                try:
                    _news_ctx = get_structured_news_for_ai(symbol)
                except Exception:
                    pass
            _sym_mem = None
            try:
                from symbol_memory import get_symbol_memory
                _sym_mem = get_symbol_memory(symbol)
            except Exception:
                pass
            _learn_ctx = None
            if LEARNING_MODULE:
                try:
                    _learn_ctx = build_ai_context_block("SCREENER") or None
                except Exception:
                    pass
            insight = deepseek_analyze_coin(
                symbol, confluence, tf_4h, tf_1h, tf_15m, oi, price,
                prepump, predump, scalp, swing,
                news_context=_news_ctx, symbol_memory=_sym_mem,
                learning_context=_learn_ctx,
            )
            if insight:
                ai_label = "🤖 <b>AI Insight (DeepSeek):</b>"

        # 2. Groq — fallback kalau DeepSeek tidak tersedia
        if GROQ_API_KEY and not insight:
            insight = groq_analyze_coin(
                symbol, confluence, tf_4h, tf_1h, tf_15m, oi, price,
                prepump, predump, scalp, swing
            )
            if insight:
                ai_label = "🤖 <b>AI Insight (Groq):</b>"

        # 3. Gemini — fallback dengan search grounding
        if GEMINI_API_KEY and not insight:
            insight = gemini_analyze_coin(
                symbol, confluence, tf_4h, tf_1h, tf_15m, oi, price,
                prepump, predump, scalp, swing
            )
            if insight:
                ai_label = "🤖 <b>AI Insight (Gemini):</b>"

        # 4. Claude — last resort
        if ANTHROPIC_API_KEY and not insight:
            insight = claude_analyze_coin(
                symbol, confluence, tf_4h, tf_1h, tf_15m, oi, price,
                prepump, predump, scalp, swing
            )
            if insight:
                ai_label = "🤖 <b>AI Insight (Claude):</b>"

        if insight:
            lines.append(ai_label)
            lines.append(_sanitize_ai_output(insight))
        else:
            lines.append("<i>⚠️ AI tidak merespons saat ini — coba lagi sebentar.</i>")

        # Sentiment overlay (tetap via Gemini karena butuh search grounding)
        if GEMINI_API_KEY:
            sentiment = gemini_sentiment_overlay(symbol)
            if sentiment:
                lines.append("")
                lines.append("🌐 <b>Sentiment &amp; Event Overlay:</b>")
                lines.append(f"<i>{sentiment}</i>")

    elif with_gemini and not GEMINI_API_KEY and not GROQ_API_KEY and not DEEPSEEK_API_KEY:
        lines.append("\n⚠️ <i>Set DEEPSEEK_API_KEY di .env untuk AI insight</i>")

    return lines


def build_telegram_message(btc: dict, coins: list) -> tuple:
    """
    Build pesan Telegram lengkap dengan format rich.
    Returns: (message_str, enriched_coins_list)

    enriched_coins berisi tf data + semua detector results per coin
    untuk dipakai oleh confirmed_signal engine tanpa re-fetch.
    """
    ts    = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
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
        prepump    = detect_prepump(analysis_sym, tf_1h, tf_4h, oi, tf_15m)
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
    ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
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

        _pp_tp1  = trade.get("tp1", 0) or 0 if trade else 0
        _pp_ent  = trade.get("entry", 0) or 0 if trade else 0
        _pp_ok   = trade and trade.get("direction") == "LONG" and _pp_tp1 > _pp_ent > 0 and trade.get("sl")
        if _pp_ok:
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
            lines.append(f"  R:R = {tp1_r:.1f}:1 {'✅' if tp1_r >= 2.0 else '⚠️ < 2R — pertimbangkan skip'}")
        else:
            lines.append("  ⚠️ Trade plan belum valid — tunggu konfirmasi entry zone")

        # v15: DeepSeek AI insight
        if pp.get("ai_insight"):
            _verdict = pp.get("ai_verdict", "CONFIRM")
            _v_emoji = {"CONFIRM": "✅", "CAUTION": "⚠️"}.get(_verdict, "🤖")
            lines.append(f"\n  ─── 🤖 DeepSeek AI ───")
            lines.append(f"  {_v_emoji} <b>{_verdict}</b>")
            for _line in pp["ai_insight"].split("\n"):
                if _line.strip():
                    lines.append(f"  {_line.strip()}")
            if pp.get("ai_adjusted"):
                lines.append("  🔧 <i>Level harga disesuaikan oleh AI</i>")

        lines.append("─────────────────────")

    lines.append("\n⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def build_predump_message(candidates: list) -> str:
    """v13: Pre-dump message dengan EXPLICIT ENTRY MODE."""
    ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
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

        _pd_tp1  = trade.get("tp1", 0) or 0 if trade else 0
        _pd_ent  = trade.get("entry", 0) or 0 if trade else 0
        _pd_ok   = trade and trade.get("direction") == "SHORT" and 0 < _pd_tp1 < _pd_ent and trade.get("sl")
        if _pd_ok:
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
            lines.append(f"  R:R = {tp1_r:.1f}:1 {'✅' if tp1_r >= 2.0 else '⚠️ < 2R — pertimbangkan skip'}")
        else:
            lines.append("  ⚠️ Trade plan belum valid — tunggu konfirmasi entry zone")

        # v15: DeepSeek AI insight
        if pd_c.get("ai_insight"):
            _verdict = pd_c.get("ai_verdict", "CONFIRM")
            _v_emoji = {"CONFIRM": "✅", "CAUTION": "⚠️"}.get(_verdict, "🤖")
            lines.append(f"\n  ─── 🤖 DeepSeek AI ───")
            lines.append(f"  {_v_emoji} <b>{_verdict}</b>")
            for _line in pd_c["ai_insight"].split("\n"):
                if _line.strip():
                    lines.append(f"  {_line.strip()}")
            if pd_c.get("ai_adjusted"):
                lines.append("  🔧 <i>Level harga disesuaikan oleh AI</i>")

        lines.append("─────────────────────")

    lines.append("\n⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# v16: REVERSAL (V-Shape + QM) MESSAGE BUILDERS
# ─────────────────────────────────────────────
_REVERSAL_PATTERN_NAME = {
    "V_SHAPE_BULLISH": "V-Shape Reversal (Bullish)",
    "V_SHAPE_BEARISH": "Inverted-V Reversal (Bearish)",
    "QM_BULLISH":      "Quasimodo Shift (Bear→Bull)",
    "QM_BEARISH":      "Quasimodo Shift (Bull→Bear)",
}
_REVERSAL_STAGE_DISPLAY = {
    "EARLY":    ("🟡", "EARLY HEADS-UP", "pola sedang forming — siap-siap, belum entry"),
    "CONFIRM":  ("🟢", "CONFIRM", "entry zone tervalidasi — entry window aktif"),
    "IGNITION": ("🚀", "IGNITION", "momentum meledak SEKARANG di level pola"),
}


def build_reversal_message(candidates: list) -> str:
    """v16: Pesan DETAIL reversal (V-Shape/QM) untuk Signal thread."""
    ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "🔄 <b>REVERSAL SIGNAL</b> — V-Shape / QM",
             f"🕐 {ts}", "━━━━━━━━━━━━━━━━━━━━━━━━",
             "<i>Deteksi titik balik dini (TF 1H) — sweep + CHoCH + momentum</i>\n"]

    if not candidates:
        lines += ["❄️ Tidak ada pola reversal terdeteksi saat ini.", "⚠️ <i>Not financial advice. DYOR.</i>"]
        return "\n".join(lines)

    for i, rv in enumerate(candidates[:5], 1):
        sym     = rv["symbol"].replace("USDT", "")
        is_long = rv.get("direction") == "LONG"
        trade   = rv.get("trade", {}) or {}
        st_emoji, st_label, st_desc = _REVERSAL_STAGE_DISPLAY.get(
            rv.get("stage", "EARLY"), ("🟡", "WATCH", ""))
        pname   = _REVERSAL_PATTERN_NAME.get(rv.get("type", ""), rv.get("type", "REVERSAL"))
        dir_tag = "🟢 LONG ▲" if is_long else "🔴 SHORT ▼"

        lines.append(f"{st_emoji} <b>{sym}</b> — {dir_tag}")
        lines.append(f"  {st_label} · <i>{st_desc}</i>")
        lines.append(f"  🧩 Pola : <b>{pname}</b>")
        lines.append(f"  🎯 Score: <b>{rv.get('score',0)}/100</b> | 💰 Price: {fmt_num(rv.get('price',0))}")

        for r in rv.get("reasons", [])[:3]:
            lines.append(f"  {r}")

        if rv.get("ignition", {}).get("ignited"):
            ig = rv["ignition"]
            lines.append(f"  🚀 <b>IGNITION 5M</b>: range {ig.get('range_mult',0):.1f}x · vol {ig.get('vol_mult',0):.1f}x — gerak SEKARANG")

        # Trade plan — reuse calculate_trade_plan output (sama seperti prepump/predump)
        _tp1 = trade.get("tp1", 0) or 0
        _ent = trade.get("entry", 0) or 0
        _ok  = trade and trade.get("sl") and (
            (is_long and _tp1 > _ent > 0) or (not is_long and 0 < _tp1 < _ent))
        if _ok:
            tp1_r = trade.get("tp1_r", 0)
            tp2_r = trade.get("tp2_r", 0)
            entry_mode = trade.get("entry_mode", "RETEST_WAIT")
            lines.append(f"\n  📍 <b>Trade Plan {'LONG' if is_long else 'SHORT'}:</b>")
            if entry_mode == "MOMENTUM_NOW" or rv.get("stage") == "IGNITION":
                lines.append(f"  {'🚀' if is_long else '🔻'} <b>ENTRY NOW</b> — momentum aktif")
                lines.append(f"  ⚡ Entry : <b>{fmt_num(trade['entry'])}</b>  ← MARKET sekarang")
            else:
                cz = trade.get("confirmation_zone")
                if cz:
                    lines.append(f"  ⏳ <b>TUNGGU RETEST</b> → zona: <b>{_fmt_zone(cz['bottom'], cz['top'])}</b>  [{cz.get('source','?')}]")
                elif rv.get("zone"):
                    z = rv["zone"]
                    lines.append(f"  ⏳ <b>TUNGGU RETEST</b> → zona pola: <b>{_fmt_zone(z['bottom'], z['top'])}</b>  [{rv.get('type','')}]")
                lines.append(f"  🎯 Entry : <b>{fmt_num(trade['entry'])}</b>  ← LIMIT order")
                _conf_dir = "atas" if is_long else "bawah"
                lines.append(f"  ✅ Konfirmasi: candle 15M close di {_conf_dir} zona + volume spike")
            lines.append(f"  🔴 SL    : {fmt_num(trade['sl'])}")
            if trade.get("tp1"):
                lines.append(f"  🟡 TP1   : {fmt_num(trade['tp1'])}  ({tp1_r}R) ← {trade.get('tp1_basis','')} | close 50%")
            if trade.get("tp2"):
                lines.append(f"  🟢 TP2   : {fmt_num(trade['tp2'])}  ({tp2_r}R) ← {trade.get('tp2_basis','')} | runner")
            lines.append(f"  R:R = {tp1_r:.1f}:1 {'✅' if tp1_r >= 2.0 else '⚠️ < 2R — pertimbangkan skip'}")
        else:
            if rv.get("zone"):
                z = rv["zone"]
                lines.append(f"  🎯 Entry zone pola: <b>{_fmt_zone(z['bottom'], z['top'])}</b>")
            if rv.get("invalidation"):
                lines.append(f"  🚫 Invalid jika tembus: {fmt_num(rv['invalidation'])}")
            lines.append("  ⚠️ <i>EARLY — tunggu konfirmasi entry zone sebelum eksekusi</i>")

        # DeepSeek AI insight (jika ada)
        if rv.get("ai_insight"):
            _verdict = rv.get("ai_verdict", "CONFIRM")
            _v_emoji = {"CONFIRM": "✅", "CAUTION": "⚠️"}.get(_verdict, "🤖")
            lines.append(f"\n  ─── 🤖 DeepSeek AI ───")
            lines.append(f"  {_v_emoji} <b>{_verdict}</b>")
            for _line in rv["ai_insight"].split("\n"):
                if _line.strip():
                    lines.append(f"  {_line.strip()}")

        lines.append("─────────────────────")

    lines.append("\n⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def build_reversal_mu_brief(candidates: list) -> str:
    """v16: Versi SINGKAT reversal untuk Market Update thread."""
    if not candidates:
        return ""
    ts = datetime.now(_WIB).strftime("%H:%M WIB")
    lines = [f"🔄 <b>REVERSAL RADAR</b> · {ts}"]
    for rv in candidates[:6]:
        sym = rv["symbol"].replace("USDT", "")
        st_emoji, st_label, _ = _REVERSAL_STAGE_DISPLAY.get(rv.get("stage", "EARLY"), ("🟡", "WATCH", ""))
        dir_tag = "🟢 LONG" if rv.get("direction") == "LONG" else "🔴 SHORT"
        ptag = "V-Shape" if rv.get("type", "").startswith("V_SHAPE") else "QM"
        ig = " 🚀" if rv.get("ignition", {}).get("ignited") else ""
        lines.append(
            f"{st_emoji} <b>{sym}</b> {dir_tag} · {ptag} · {st_label} · "
            f"{fmt_num(rv.get('price',0))} · {rv.get('score',0)}/100{ig}"
        )
    lines.append("<i>Detail lengkap → room Signal. DYOR.</i>")
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
        "ema9": calculate_ema(closed_candles, 9),
        "ema21": calculate_ema(closed_candles, 21),
        "ema50": calculate_ema(closed_candles, 50),
        "v_shape": detect_v_shape(closed_candles),
        "qm_pattern": detect_qm_pattern(closed_candles),
        "candle_patterns": detect_candle_patterns(closed_candles),
        "market_regime": detect_market_regime(closed_candles),
        "bb_squeeze": calculate_bb_squeeze(closed_candles),
        "volume_coil": detect_volume_coil(closed_candles),
        "sudden_breakout": detect_sudden_breakout(closed_candles),
        "adx": calculate_adx(closed_candles).get("adx", 0),
        "macd": calculate_macd(closed_candles),
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

    # v14: real-time momentum untuk /analyze juga
    realtime_momentum = detect_realtime_momentum(binance_sym)

    confluence = calculate_confluence_v4(tf_4h, tf_1h, tf_15m, oi, realtime_momentum)
    prepump    = detect_prepump(binance_sym, tf_1h, tf_4h, oi, tf_15m)
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
    ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")

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

    # v14: real-time momentum info
    if not realtime_momentum.get("error"):
        rt_label = realtime_momentum.get("momentum_label", "")
        rt_v15   = realtime_momentum.get("velocity_15m", 0)
        rt_burst = realtime_momentum.get("vol_burst", 1)
        lines.append(f"⚡ <b>Real-time (1M):</b> {rt_label}  {rt_v15:+.2f}% / 15m  Vol {rt_burst:.1f}x")
        lines.append("")

    # v14: AI Analysis chain — Groq first (fast), then Gemini, then Claude
    coin_lines = build_coin_analysis_block(
        binance_sym, price, confluence, tf_4h, tf_1h, tf_15m, oi,
        with_gemini=True, prepump=prepump, predump=predump,
        scalp=scalp, swing=swing,
        news_sentiment=news_s, with_risk=True,
        realtime=realtime_momentum
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

        ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
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


def _deepseek_enrich_candidates(candidates: list, direction: str) -> list:
    """
    v15: Jalankan DeepSeek review untuk setiap kandidat sinyal.
    Adjust entry/TP/SL kalau AI merekomendasikan.
    Buang kandidat yang di-SKIP oleh AI.
    """
    if not DEEPSEEK_MODULE or not DEEPSEEK_API_KEY or not candidates:
        return candidates

    enriched = []
    _news_cache: dict = {}   # symbol → news_ctx, hindari double fetch

    # Lessons global dari sinyal lalu — sama untuk semua kandidat, hitung sekali.
    _learn_ctx = None
    if LEARNING_MODULE:
        try:
            _learn_ctx = build_ai_context_block("SCREENER") or None
        except Exception:
            _learn_ctx = None

    for cand in candidates:
        sym   = cand.get("symbol", "")
        trade = cand.get("trade", {})
        if not trade or not trade.get("entry"):
            enriched.append(cand)
            continue

        # Fetch news context (cache per symbol)
        news_ctx = _news_cache.get(sym)
        if news_ctx is None and NEWS_MODULE:
            try:
                news_ctx = get_structured_news_for_ai(sym)
                _news_cache[sym] = news_ctx
            except Exception:
                news_ctx = None
                _news_cache[sym] = None

        try:
            review = deepseek_signal_review(
                symbol       = sym,
                direction    = direction,
                trade        = trade,
                master_score = cand.get("total_score", cand.get("score", 0)),
                reasons      = cand.get("reasons", []),
                oi_data      = cand.get("oi_data", {}),
                tf_4h        = cand.get("tf_4h", {}),
                tf_1h        = cand.get("tf_1h", {}),
                tf_15m       = cand.get("tf_15m", {}),
                news_context = news_ctx,
                signal_type  = direction,
                learning_context = _learn_ctx,
            )

            if review.get("ai_verdict") == "SKIP":
                log.info(f"🤖 DeepSeek SKIP {sym} {direction}")
                continue

            cand = dict(cand)   # copy agar tidak mutate original

            if review.get("was_adjusted"):
                cand["trade"] = dict(trade)
                cand["trade"]["entry"] = review["entry"]
                cand["trade"]["tp1"]   = review["tp1"]
                cand["trade"]["tp2"]   = review["tp2"]
                cand["trade"]["sl"]    = review["sl"]

            if review.get("insight"):
                cand["ai_insight"]  = review["insight"]
                cand["ai_verdict"]  = review.get("ai_verdict", "CONFIRM")
                cand["ai_adjusted"] = review.get("was_adjusted", False)

        except Exception as e:
            log.warning(f"DeepSeek enrich error {sym}: {e}")

        enriched.append(cand)

    return enriched


def handle_prepump_command(chat_id: str):
    """Handle /prepump — scan pre-pump candidates."""
    send_telegram("🎯 Scanning pre-pump candidates... ⏳ (ini butuh ~1-2 menit)", chat_id)

    candidates = scan_prepump_candidates()
    candidates = _deepseek_enrich_candidates(candidates, "PREPUMP")
    msg = build_prepump_message(candidates)
    send_telegram(msg, chat_id)


def handle_predump_command(chat_id: str):
    """Handle /predump — scan pre-dump candidates."""
    send_telegram("💀 Scanning pre-dump candidates... ⏳ (ini butuh ~1-2 menit)", chat_id)

    candidates = scan_predump_candidates()
    candidates = _deepseek_enrich_candidates(candidates, "PREDUMP")
    msg = build_predump_message(candidates)
    send_telegram(msg, chat_id)


def build_scalp_message(candidates: list) -> str:
    """v13: Scalp message dengan EXPLICIT ENTRY MODE dan confirmation zone."""
    ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
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

        _sc_sl  = (trade.get("sl") or 0) if trade else (sc.get("scalp_sl") or 0)
        _sc_tp1 = (trade.get("tp1") or 0) if trade else (sc.get("scalp_tp") or 0)
        _sc_ent = (trade.get("entry") or sc.get("price", 0)) if trade else sc.get("price", 0)
        _sc_tp_ok = (direc == "LONG"  and _sc_tp1 > _sc_ent > 0) or \
                    (direc == "SHORT" and 0 < _sc_tp1 < _sc_ent)

        if _sc_sl:
            lines.append(f"  🔴 SL     : {fmt_num(_sc_sl)}")

        if _sc_tp_ok and _sc_tp1:
            tp1_r = trade.get("tp1_r", 0) if trade else 0
            lines.append(f"  🟡 TP1    : {fmt_num(_sc_tp1)}  ({tp1_r}R) | close 50%")
        elif _sc_tp1:
            lines.append(f"  ⚠️ TP tidak valid — tunggu retrace ke OB/FVG dulu")

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
    """Handle /news <COIN> — cek cache news agent dulu, fallback NewsAPI live."""
    sym = coin.upper().strip().replace("USDT", "")
    if not sym:
        send_telegram("❓ Format: `/news BTC` atau `/news SOL`", chat_id)
        return

    # Cek news_agent cache dulu (lebih cepat, tidak spam API)
    if NEWS_AGENT_MODULE:
        cached = get_cached_news(sym, max_age_seconds=3900)
        if cached:
            ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
            lines = [
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                f"📰 <b>NEWS INTEL: {sym}</b>",
                f"🕐 {ts} (dari cache hourly)",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "",
                f"🌐 Session: {cached.get('trading_session', '?')}",
                f"📊 Sentiment: <b>{cached.get('sentiment_label','NEUTRAL')}</b> "
                f"(score: {cached.get('sentiment_score', 0):+d})",
            ]
            events = cached.get("high_impact_events", [])
            if events:
                lines.append(f"\n⚠️ <b>High-Impact Events:</b>")
                for ev in events[:5]:
                    lines.append(f"  • {ev}")
            unlocks = cached.get("upcoming_unlocks", [])
            if unlocks:
                lines.append(f"\n🔓 <b>Token Unlock:</b>")
                for ul in unlocks:
                    lines.append(f"  • {ul}")
            heads = cached.get("headlines", [])
            if heads:
                lines.append(f"\n📋 <b>Headlines:</b>")
                for h in heads[:4]:
                    lines.append(f"  • {h[:100]}")
            coin_lesson = cached.get("coin_lesson", "")
            macro_risk  = cached.get("macro_risk", "")
            if coin_lesson:
                lines.append(f"\n💡 <b>AI Lesson:</b> {coin_lesson}")
            if macro_risk:
                lines.append(f"🌐 <b>Macro:</b> {macro_risk}")

            # X (Twitter) sentiment
            x_sent     = cached.get("x_sentiment", "")
            x_kol_cnt  = cached.get("x_kol_count", 0)
            x_euphoria = cached.get("x_euphoria", False)
            x_kol_ments= cached.get("x_kol_mentions", [])
            x_source   = cached.get("x_source", "nitter")
            if x_sent and x_sent != "NEUTRAL":
                x_em = "🟢" if x_sent == "BULLISH" else "🔴" if x_sent == "BEARISH" else "🟡"
                x_line = f"\n🐦 <b>X Sentiment:</b> {x_em} {x_sent}"
                if x_kol_cnt:
                    x_line += f" | KOL aktif: <b>{x_kol_cnt}</b>"
                if x_euphoria:
                    x_line += " | ⚠️ <b>EUPHORIA!</b>"
                x_line += f" <i>({x_source})</i>"
                lines.append(x_line)
                if x_kol_ments:
                    lines.append("  KOL:")
                    for m in x_kol_ments[:2]:
                        lines.append(f"  • {str(m)[:100]}")

            lines.append("\n⚠️ <i>Data dari News Agent hourly fetch. DYOR.</i>")
            send_telegram("\n".join(lines), chat_id, parse_mode="HTML")
            return

    # Fallback: live fetch via NewsAPI
    if not NEWS_MODULE:
        send_telegram("❌ News module tidak tersedia. Pastikan news_sentiment.py ada.", chat_id)
        return
    if not NEWSAPI_KEY:
        send_telegram("❌ NEWSAPI_KEY belum diset di .env", chat_id)
        return
    send_telegram(f"📰 Mencari berita untuk *{sym}*... ⏳", chat_id)
    try:
        s   = get_coin_sentiment(sym)
        msg = format_sentiment_block(s, mode="full")
        send_telegram(msg, chat_id)
    except Exception as e:
        send_telegram(f"❌ Error fetch news: {e}", chat_id)


def handle_newsagent_command(chat_id: str):
    """Handle /newsagent — manual trigger news agent fetch + tampilkan summary."""
    if not NEWS_AGENT_MODULE:
        send_telegram("❌ news_agent.py tidak ditemukan.", chat_id)
        return
    send_telegram("📰 Menjalankan News Agent fetch... ⏳ (30-60 detik)", chat_id)
    try:
        import threading as _thr
        def _run():
            intel = run_news_fetch(send_telegram_fn=None)
            ts    = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
            n_ev  = sum(len(c.get("events", [])) for c in intel.get("coins", {}).values())
            n_ev += len(intel.get("macro", {}).get("events", []))
            lessons = intel.get("derived_lessons", [])
            macro_s = intel.get("macro", {}).get("sentiment", "NEUTRAL")
            macro_l = intel.get("macro", {}).get("lesson", "")

            lines = [
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                f"📰 <b>NEWS AGENT REPORT</b>",
                f"🕐 {ts}",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "",
                f"🌐 Session: {intel.get('trading_session','?')}",
                f"📊 Macro Sentiment: <b>{macro_s}</b>",
                f"⚡ Events Terdeteksi: {n_ev}",
                f"📚 Lessons Derived: {len(lessons)}",
            ]
            if macro_l:
                lines.append(f"\n🌐 <b>Macro Insight:</b> {macro_l}")

            # Tampilkan top events per koin
            hot_coins = [
                (sym, data) for sym, data in intel.get("coins", {}).items()
                if data.get("events") or data.get("urgency") == "HIGH"
            ][:5]
            if hot_coins:
                lines.append("\n⚠️ <b>Coin Events:</b>")
                for sym, data in hot_coins:
                    evs = " | ".join(data.get("events", [])[:2])
                    lines.append(f"  • <b>{sym}</b>: {evs}")

            # X Sentiment per koin (koin dengan KOL aktif atau euphoria)
            x_active = [
                (sym, data.get("x", {}))
                for sym, data in intel.get("coins", {}).items()
                if data.get("x", {}).get("kol_count", 0) >= 1
                or data.get("x", {}).get("euphoria", False)
            ][:6]
            if x_active:
                lines.append("\n🐦 <b>X Sentiment:</b>")
                for sym, xd in x_active:
                    x_sent  = xd.get("sentiment_label", "?")
                    kol_cnt = xd.get("kol_count", 0)
                    euph    = xd.get("euphoria", False)
                    x_em    = "🟢" if x_sent == "BULLISH" else "🔴" if x_sent == "BEARISH" else "🟡"
                    suffix  = " ⚠️EUPHORIA" if euph else ""
                    lines.append(f"  • <b>{sym}</b>: {x_em} {x_sent} | KOL: {kol_cnt}{suffix}")

            # X euphoria summary
            x_euphorias = intel.get("x_euphoria_coins", [])
            if x_euphorias:
                lines.append(f"\n🚨 <b>Euphoria Coins:</b> {', '.join(x_euphorias)} — hindari LONG baru!")

            if lessons:
                lines.append("\n💡 <b>Top Lessons:</b>")
                for ls in lessons[:4]:
                    lines.append(f"  • {ls['text']}")

            lines.append("\n<i>Cache diperbarui — berlaku 1 jam ke depan.</i>")
            send_telegram("\n".join(lines), chat_id, parse_mode="HTML")

        _thr.Thread(target=_run, daemon=True).start()
    except Exception as e:
        send_telegram(f"❌ Error news agent: {e}", chat_id)


def handle_newslessons_command(chat_id: str):
    """Handle /newslessons — tampilkan active lessons dari news agent."""
    if not NEWS_AGENT_MODULE:
        send_telegram("❌ news_agent.py tidak ditemukan.", chat_id)
        return
    lessons = get_active_lessons_from_news()
    if not lessons:
        send_telegram("📚 Tidak ada lessons aktif dari news agent saat ini.", chat_id)
        return
    ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📚 <b>NEWS-DERIVED LESSONS</b>",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"<i>{len(lessons)} lessons aktif dari news agent (max 6 jam):</i>",
        "",
    ]
    for i, lesson in enumerate(lessons[:10], 1):
        lines.append(f"{i}. {lesson}")
    lines.append("\n<i>Lessons ini otomatis diinjeksikan ke DeepSeek saat review sinyal.</i>")
    send_telegram("\n".join(lines), chat_id, parse_mode="HTML")


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


def handle_dca_command(coin: str, chat_id: str):
    """Handle /dca <COIN> — X sentiment + DCA signal analysis."""
    if not X_MODULE:
        send_telegram("❌ x_sentiment.py tidak tersedia.", chat_id)
        return
    sym = coin.upper().strip().replace("USDT", "")
    if not sym:
        send_telegram("❓ Format: `/dca BTC` atau `/dca SOL`", chat_id)
        return

    send_telegram(
        f"🐦 Fetching X (Twitter) data untuk *{sym}*... ⏳\n"
        f"_(ini butuh ~15-30 detik karena fetch dari X)_",
        chat_id
    )
    try:
        # Fetch price context from Binance
        price_change_7d   = 0.0
        price_from_low_30d = 0.0
        current_price      = 0.0
        try:
            binance_sym = f"{sym}USDT"
            # 7d change: 168 hourly candles
            klines_7d = get_binance_klines(binance_sym, "1h", limit=168)
            if klines_7d and len(klines_7d) >= 2:
                open_7d     = float(klines_7d[0]["open"])
                current_price = float(klines_7d[-1]["close"])
                price_change_7d = (current_price - open_7d) / open_7d * 100 if open_7d else 0

            # 30d low: 720 hourly candles
            klines_30d = get_binance_klines(binance_sym, "4h", limit=180)
            if klines_30d:
                low_30d = min(float(c["low"]) for c in klines_30d)
                if low_30d > 0:
                    price_from_low_30d = (current_price - low_30d) / low_30d * 100
        except Exception as _e:
            log.debug(f"Price fetch for DCA {sym}: {_e}")

        dca = get_dca_signal(
            sym,
            price=current_price,
            price_change_7d=price_change_7d,
            price_from_low_30d=price_from_low_30d,
        )

        if dca.get("_error"):
            send_telegram(
                f"⚠️ Tidak bisa fetch data X untuk *{sym}*.\n"
                f"Coba lagi nanti atau cek koneksi ke nitter instances.\n\n"
                f"_Source: {dca.get('x_source', '?')}_",
                chat_id
            )
            return

        msg = format_dca_block(dca)
        send_telegram(msg, chat_id)

        # Juga kirim X sentiment block
        x_data = get_x_coin_analysis(sym)
        if not x_data.get("_error") and x_data.get("analysis", {}).get("total_count", 0) > 0:
            x_msg = format_x_block(x_data, mode="full")
            send_telegram(x_msg, chat_id)

    except Exception as e:
        log.error(f"DCA command error {sym}: {e}", exc_info=True)
        send_telegram(f"❌ Error saat analisa DCA {sym}: {str(e)[:150]}", chat_id)


def handle_xsentiment_command(coin: str, chat_id: str):
    """Handle /xsenti <COIN> — quick X sentiment without DCA."""
    if not X_MODULE:
        send_telegram("❌ x_sentiment.py tidak tersedia.", chat_id)
        return
    sym = coin.upper().strip().replace("USDT", "")
    if not sym:
        send_telegram("❓ Format: `/xsenti BTC`", chat_id)
        return
    send_telegram(f"🐦 Fetching X sentiment untuk *{sym}*... ⏳", chat_id)
    try:
        x_data = get_x_coin_analysis(sym)
        if x_data.get("_error"):
            send_telegram(f"⚠️ Tidak bisa fetch data X untuk {sym}. Coba lagi nanti.", chat_id)
            return
        msg = format_x_block(x_data, mode="full")
        send_telegram(msg, chat_id)
    except Exception as e:
        send_telegram(f"❌ Error fetch X sentiment: {e}", chat_id)


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
        pnl = float(data["pnl"])
        t = log_trade(
            coin=data["coin"], direction=data["direction"],
            entry_price=float(data["entry"]), margin_usdt=float(data["margin"]),
            leverage=int(data["leverage"]), pnl_usdt=pnl,
            note=data.get("note", "")
        )
        msg = format_trade_logged(t)

        # v14: auto-update capital + catat daily PnL di risk manager
        if RISK_MODULE:
            try:
                record_trade_result(pnl)           # update daily PnL
                new_cap = update_capital_after_trade(pnl)  # update capital permanen
                result_emoji = "🟢" if pnl >= 0 else "🔴"
                msg += (
                    f"\n\n{result_emoji} <b>Balance diperbarui</b>: "
                    f"<code>${new_cap:,.2f} USDT</code>  ({pnl:+.2f})\n"
                    f"Sinyal berikutnya akan pakai balance terbaru."
                )
            except Exception as e:
                log.debug(f"Risk update after logtrade error: {e}")

        send_telegram(msg, chat_id, parse_mode="HTML")


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


def handle_refreshdashboard_command(chat_id: str):
    """Handle /refreshdashboard — rebuild Dashboard sheet di Google Sheets."""
    if not JOURNAL_MODULE:
        send_telegram("❌ Trade journal module tidak tersedia.", chat_id)
        return
    try:
        from trade_journal import _get_sheet, _setup_dashboard
        send_telegram("🔄 Rebuilding dashboard spreadsheet...", chat_id)
        sheet = _get_sheet()
        if not sheet:
            send_telegram(
                "❌ Tidak bisa connect ke Google Sheets.\n"
                "Pastikan <code>GOOGLE_SPREADSHEET_ID</code> dan credentials sudah diset.",
                chat_id, parse_mode="HTML"
            )
            return
        ok = _setup_dashboard(sheet)
        if ok:
            send_telegram(
                "✅ <b>Dashboard berhasil diperbarui!</b>\n\n"
                "📊 Buka spreadsheet kamu dan cek tab <b>Dashboard</b>.\n"
                "Semua formula, warna, dan layout sudah di-refresh.",
                chat_id, parse_mode="HTML"
            )
        else:
            send_telegram("❌ Gagal rebuild dashboard. Cek log Railway untuk detail.", chat_id)
    except Exception as e:
        send_telegram(f"❌ Error: <code>{e}</code>", chat_id, parse_mode="HTML")


def handle_setbalance_command(args: str, chat_id: str):
    """
    Handle /setbalance <USDT> — set balance trading.
    v14: sync ke BOTH trade_journal DAN risk_manager sekaligus.
    """
    try:
        s = args.strip()
        # Koma sebagai desimal (57,28 → 57.28) vs ribuan (1,000 → 1000)
        import re as _re
        if _re.match(r'^\d+,\d{1,2}$', s):
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
        amount = float(s)
        if amount <= 0:
            raise ValueError("Amount harus > 0")
    except ValueError:
        send_telegram(
            "❓ Format: <code>/setbalance 500</code> (angka USDT)\n"
            "Contoh: <code>/setbalance 1000</code>",
            chat_id, parse_mode="HTML"
        )
        return

    # Auto-adjust risk% berdasarkan ukuran akun
    # Akun kecil: risk% lebih kecil biar tidak blown dengan 1 trade
    if amount < 100:
        auto_risk = 1.5    # $60 → max rugi $0.90/trade
    elif amount < 300:
        auto_risk = 2.0
    elif amount < 1000:
        auto_risk = 2.0
    else:
        auto_risk = 2.0

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "💰 <b>BALANCE DISET</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"\n💵 Balance: <b>${amount:,.2f} USDT</b>",
        f"🎚 Risk/trade: <b>{auto_risk}%</b> = max rugi <b>${amount * auto_risk / 100:.2f}</b> per trade\n",
    ]

    # 1. Sync ke trade journal
    if JOURNAL_MODULE:
        try:
            set_initial_balance(amount)
            lines.append("✅ Trade journal: tercatat")
        except Exception as e:
            lines.append(f"⚠️ Journal error: {e}")

    # 2. Sync ke risk manager + set risk%
    if RISK_MODULE:
        try:
            set_capital(amount)
            set_risk_pct(auto_risk)
            lines.append("✅ Risk manager: modal + risk% diperbarui")
        except Exception as e:
            lines.append(f"⚠️ Risk manager error: {e}")

    # Pesan kontekstual berdasarkan ukuran akun
    lines.append("")
    if amount < 100:
        lines += [
            "📌 <b>Mode akun kecil aktif</b>",
            f"  Dari <b>${amount:.0f}</b> ke <b>$1,000</b> butuh ~{int((1000/amount - 1) * 100)}x growth.",
            "  Kuncinya: <b>konsistensi + disiplin SL</b>, bukan all-in.",
            "  Dengan 2% compounding per trade menang, perlu ±180 trade profit.",
            "",
            "  Tips untuk akun kecil:",
            "  • Prioritaskan sinyal CONFIDENCE HIGH / score ≥ 80",
            "  • Skip sinyal FAIR — tunggu yang GOOD atau EXCELLENT",
            "  • Jangan balas dendam setelah SL",
        ]
    elif amount < 500:
        lines += [
            "📌 Akun medium — tetap disiplin risk management.",
            "  Gunakan leverage sesuai rekomendasi, jangan naikkan manual.",
        ]

    lines += [
        "",
        "📊 Mulai sekarang setiap sinyal akan tampilkan:",
        "  • Margin yang disarankan + leverage",
        "  • Estimasi profit (TP) dan loss (SL) dalam USDT",
        "",
        f"⚙️ Ubah risk: <code>/setrisk 1.5</code>",
        f"📈 Setelah trade: <code>/logtrade</code>",
        f"📊 Cek status: <code>/risk</code>",
    ]

    send_telegram("\n".join(lines), chat_id, parse_mode="HTML")


def handle_journal_wizard_message(text: str, chat_id: str):
    """Handle pesan saat user sedang dalam wizard mode."""
    if not JOURNAL_MODULE:
        return False
    if not is_in_wizard(chat_id):
        return False
    if is_wizard_expecting_image(chat_id):
        send_telegram("⏳ <i>Mencatat trade ke Google Sheets...</i>", chat_id, parse_mode="HTML")
    reply, done = wizard_process(chat_id, text=text)
    send_telegram(reply, chat_id, parse_mode="HTML")
    return True


def handle_journal_wizard_image(file_id: str, chat_id: str):
    """Handle gambar yang dikirim saat dalam wizard mode (sebagai bukti trade)."""
    if not JOURNAL_MODULE:
        return False
    if not is_wizard_expecting_image(chat_id):
        return False
    send_telegram("⏳ <i>Mencatat trade ke Google Sheets...</i>", chat_id, parse_mode="HTML")
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


# ── Screenshot → trade (vision import) ────────

_SHOT_PROMPT = (
    "Kamu membaca screenshot detail order trading futures crypto (mis. Bitget/Binance). "
    "Ekstrak data berikut dan balas HANYA JSON valid tanpa teks lain, tanpa markdown:\n"
    '{"coin": "...", "direction": "LONG|SHORT", "leverage": <angka>, '
    '"entry_price": <angka>, "exit_price": <angka>, "realized_pnl": <angka>, "roi_pct": <angka>}\n'
    "Aturan:\n"
    "- coin: simbol tanpa USDT (contoh HYPEUSDT -> HYPE).\n"
    "- direction: posisi yang DITUTUP. 'Close short' -> SHORT, 'Close long' -> LONG.\n"
    "- entry_price: angka di field 'Entry price'.\n"
    "- exit_price: 'Avg. filled price' atau 'Filled price' atau 'Exit price'.\n"
    "- realized_pnl: 'Realized PnL' dalam USDT (pertahankan tanda minus).\n"
    "- roi_pct: 'Realized ROI' dalam persen (pertahankan tanda minus, tanpa simbol %).\n"
    "- Kalau suatu field tidak ada, isi null. Jangan mengarang."
)


# Limit aman di bawah batas base64 Groq (4 MB). Gambar lebih besar dari ini
# bikin request vision ditolak — penyebab umum "screenshot gagal dibaca".
_VISION_MAX_B64_BYTES = 3_500_000


def _prepare_image_for_vision(img_bytes: bytes, mime: str) -> tuple:
    """
    Kecilkan/re-compress gambar yang kelewat besar supaya muat di limit base64 Groq.
    Aman kalau Pillow tidak terpasang — kembalikan gambar apa adanya.
    Return (image_b64, mime).
    """
    b64 = base64.b64encode(img_bytes).decode("ascii")
    if len(b64) <= _VISION_MAX_B64_BYTES:
        return b64, mime
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(img_bytes))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        long_edge = max(im.size)
        last_b64 = b64
        # Turunkan dimensi & kualitas bertahap; berhenti begitu muat.
        for max_edge, quality in ((2048, 85), (1600, 80), (1280, 75), (1024, 70)):
            work = im
            if long_edge > max_edge:
                scale = max_edge / long_edge
                work = im.resize((max(1, int(im.width * scale)),
                                  max(1, int(im.height * scale))))
            buf = io.BytesIO()
            work.save(buf, format="JPEG", quality=quality)
            last_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            if len(last_b64) <= _VISION_MAX_B64_BYTES:
                log.info(f"Vision image dikecilkan: {len(b64)}→{len(last_b64)} "
                         f"b64 bytes (max_edge={max_edge}, q={quality})")
                return last_b64, "image/jpeg"
        return last_b64, "image/jpeg"  # tetap kirim yang terkecil
    except Exception as e:
        log.warning(f"Image downscale gagal ({e}), kirim apa adanya")
        return b64, mime


def _extract_shot_json(image_b64: str, mime: str) -> tuple:
    """
    Panggil Groq vision, parse JSON hasilnya jadi dict mentah.
    Return (data_dict, error_str). Sukses → (dict, ""). Gagal → ({}, alasan).
    Kalau model utama error, otomatis coba model cadangan.
    """
    import json
    raw, err = _groq_vision_request(image_b64, _SHOT_PROMPT, mime=mime)
    if not raw and GROQ_VISION_MODEL_FB and GROQ_VISION_MODEL_FB != GROQ_VISION_MODEL:
        log.warning(f"Vision model utama gagal ({err}); coba fallback {GROQ_VISION_MODEL_FB}")
        raw, err2 = _groq_vision_request(image_b64, _SHOT_PROMPT, mime=mime,
                                         model=GROQ_VISION_MODEL_FB)
        if not raw:
            return {}, f"{err} (fallback: {err2})"
    if not raw:
        return {}, err or "respons vision kosong"

    txt = raw.strip()
    # buang code fence kalau model bandel
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.lower().startswith("json"):
            txt = txt[4:]
    start, end = txt.find("{"), txt.rfind("}")
    if start == -1 or end == -1:
        log.warning(f"Vision shot: no JSON in response: {raw[:200]}")
        return {}, "vision tidak mengembalikan data terstruktur"
    try:
        return json.loads(txt[start:end + 1]), ""
    except Exception as e:
        log.warning(f"Vision shot JSON parse error: {e} | raw={raw[:200]}")
        return {}, "format data dari vision tidak valid"


# Daftar command yang dikenal — dipakai untuk saran "maksud kamu ...?"
# saat user salah ketik command (mis. /logshoot → /logshot).
_KNOWN_COMMANDS = [
    "/logshot", "/logtrade", "/logpnl", "/logoutcome", "/trades", "/trade",
    "/close", "/weeksummary", "/refreshdashboard", "/setbalance", "/lessons",
    "/decisions", "/evolve", "/addlesson", "/backtest", "/btall", "/btresult",
    "/btcompare", "/btstats", "/signalbt", "/balance", "/setstake", "/compound",
    "/liqstatus", "/marketstatus", "/signals", "/symbolmemory", "/symbolstats",
    "/blacklist", "/unblacklist", "/ask", "/scan", "/status", "/security",
    "/why", "/style", "/help", "/start", "/done",
]


def _suggest_command(cmd: str):
    """Cari command terdekat dari typo (mis. /logshoot → /logshot). None kalau tidak ada."""
    import difflib
    m = difflib.get_close_matches(cmd.lower(), _KNOWN_COMMANDS, n=1, cutoff=0.6)
    return m[0] if m else None


def handle_logshot_command(chat_id: str):
    """Handle /logshot — minta user kirim screenshot order details buat dicatat."""
    if not JOURNAL_MODULE:
        send_telegram("❌ Trade journal module tidak tersedia.", chat_id)
        return
    if not GROQ_API_KEY:
        send_telegram("⚠️ Fitur baca screenshot butuh GROQ_API_KEY. Set dulu di .env.", chat_id)
        return
    _awaiting_tradeshot[chat_id] = True
    send_telegram(
        "📸 Kirim <b>screenshot order details</b> (Bitget/Binance/dll) yang ada "
        "Entry, Exit, Realized PnL & ROI.\nGue baca otomatis terus konfirmasi sebelum disimpan.",
        chat_id, parse_mode="HTML",
    )


def handle_trade_screenshot(file_id: str, chat_id: str):
    """Download screenshot, baca via Groq vision, tampilkan preview untuk konfirmasi."""
    if not JOURNAL_MODULE:
        return False
    _awaiting_tradeshot.pop(chat_id, None)
    send_telegram("⏳ <i>Lagi baca screenshot...</i>", chat_id, parse_mode="HTML")
    # Fetch file URL + bytes dari Telegram
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10,
        )
        file_path = r.json()["result"]["file_path"]
        img = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
            timeout=15,
        )
        img_bytes = img.content
        mime = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
    except Exception as e:
        log.warning(f"Trade screenshot fetch error: {e}")
        send_telegram("⚠️ Gagal ambil gambar dari Telegram. Coba kirim ulang.", chat_id)
        return False

    image_b64, mime = _prepare_image_for_vision(img_bytes, mime)
    raw, verr = _extract_shot_json(image_b64, mime)
    if not raw:
        detail = f"\n\n🔧 <i>Detail: {verr}</i>" if verr else ""
        send_telegram(
            "⚠️ Nggak bisa baca data dari screenshot itu. Pastikan ini halaman "
            "<b>Order details</b> (ada Entry, Realized PnL, ROI), atau catat manual via /logtrade."
            + detail,
            chat_id, parse_mode="HTML",
        )
        return False

    trade, err = build_trade_from_screenshot(raw)
    if err:
        send_telegram(
            f"⚠️ {err}\nCoba kirim screenshot yang lebih jelas, atau /logtrade buat manual.",
            chat_id, parse_mode="HTML",
        )
        return False

    _pending_shot[chat_id] = trade
    send_telegram(format_shot_preview(trade), chat_id, parse_mode="HTML")
    return True


def handle_shot_confirm(text: str, chat_id: str):
    """Proses jawaban ya/batal untuk screenshot yang sudah dibaca."""
    trade = _pending_shot.pop(chat_id, None)
    if not trade:
        return
    ans = text.strip().lower()
    if ans in ("batal", "skip", "no", "ga", "gak", "nggak", "cancel", "x"):
        send_telegram("❌ Oke, nggak jadi disimpan.", chat_id)
        return
    pnl = float(trade["pnl"])
    t = log_trade(
        coin=trade["coin"], direction=trade["direction"],
        entry_price=float(trade["entry"]), margin_usdt=float(trade["margin"]),
        leverage=int(trade["leverage"]), pnl_usdt=pnl,
        note=trade.get("note", ""),
    )
    msg = format_trade_logged(t)
    if RISK_MODULE:
        try:
            record_trade_result(pnl)
            new_cap = update_capital_after_trade(pnl)
            result_emoji = "🟢" if pnl >= 0 else "🔴"
            msg += (
                f"\n\n{result_emoji} <b>Balance diperbarui</b>: "
                f"<code>${new_cap:,.2f} USDT</code>  ({pnl:+.2f})\n"
                f"Sinyal berikutnya akan pakai balance terbaru."
            )
        except Exception as e:
            log.debug(f"Risk update after logshot error: {e}")
    send_telegram(msg, chat_id, parse_mode="HTML")


def _signal_chat_ai(prompt: str) -> str:
    """AI call untuk diskusi sinyal — kirim prompt penuh (Groq → Gemini → Claude)."""
    if GROQ_API_KEY:
        out = _groq_request([{"role": "user", "content": prompt}],
                            max_tokens=900, temperature=0.6)
        if out:
            return out
    if GEMINI_API_KEY:
        out = _gemini_request({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 900},
        })
        if out:
            return out
    if ANTHROPIC_API_KEY:
        return claude_free_ask(prompt) or ""
    return ""


def handle_ask_command(question: str, chat_id: str):
    if not question.strip():
        send_telegram("❓ Format: `/ask <pertanyaan>` — contoh: `/ask apa itu order block?`", chat_id)
        return

    # v15: DeepSeek primary → Groq → Gemini → Claude chain
    if DEEPSEEK_MODULE and DEEPSEEK_API_KEY:
        send_telegram("🤖 Tanya ke DeepSeek AI... ⏳", chat_id)
        answer = deepseek_free_ask(question)
        if answer:
            send_telegram(f"🤖 <b>DeepSeek AI:</b>\n\n{answer}", chat_id)
            return

    if GROQ_API_KEY:
        send_telegram("🤖 Tanya ke Groq AI (Llama 70B)... ⏳", chat_id)
        answer = groq_free_ask(question)
        if answer:
            send_telegram(f"🤖 <b>Groq AI:</b>\n\n{answer}", chat_id)
            return

    if GEMINI_API_KEY:
        send_telegram("🤖 Tanya ke Gemini AI... ⏳", chat_id)
        answer = gemini_free_ask(question)
        if answer:
            send_telegram(f"🤖 <b>Gemini AI:</b>\n\n{answer}", chat_id)
            return

    if ANTHROPIC_API_KEY:
        send_telegram("🤖 Tanya ke Claude AI... ⏳", chat_id)
        answer = claude_free_ask(question)
        if answer:
            send_telegram(f"🤖 <b>Claude AI:</b>\n\n{answer}", chat_id)
            return

    send_telegram("⚠️ Tidak ada AI key yang aktif. Set GROQ_API_KEY, GEMINI_API_KEY, atau ANTHROPIC_API_KEY di .env", chat_id)


def handle_status_command(chat_id: str):
    gemini_status    = "✅ Connected" if GEMINI_API_KEY else "❌ No API Key"
    groq_status      = f"✅ {GROQ_MODEL}" if GROQ_API_KEY else "❌ No API Key"
    claude_status    = "✅ Connected" if ANTHROPIC_API_KEY else "⚪ Not set"
    learning_status  = "✅ Active" if LEARNING_MODULE else "⚠️ Module missing"
    journal_status   = "✅ Active" if JOURNAL_MODULE else "⚠️ Module missing"
    backtest_status  = "✅ Active" if BACKTEST_MODULE  else "⚠️ Module missing"
    tracker_status   = "✅ Active" if TRACKER_MODULE   else "⚠️ Module missing"
    confirmed_status = "✅ Active" if CONFIRMED_MODULE else "⚠️ Module missing"
    saldo = f"${get_current_balance():,.2f} USDT" if JOURNAL_MODULE else "N/A"
    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>CRYPTO SCREENER v14</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Status          : ✅ Running\n"
        f"🤖 Groq AI      : {groq_status}\n"
        f"🤖 Gemini AI    : {gemini_status}\n"
        f"🤖 Claude AI    : {claude_status}\n"
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
        "📈 *MANUAL TRADE MANAGER* _(BARU!)_\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 `/trade BTC LONG 95000 60` — Daftarkan posisi manual\n"
        "   `/trade BTC LONG 95000` — tanpa size → auto dari compound balance\n"
        "   → Bot hitung SL/TP1/TP2/BE/trailing otomatis (ATR-based)\n"
        "   → Monitor tiap scan: alert BE, TP1 partial, trailing, SL\n"
        "🏁 `/close BTC [harga]` — Manual full close + auto-log ke journal\n"
        "📊 `/trades` — Lihat semua posisi aktif + P&L realtime\n"
        "📈 `/compound` — Status compound stake (balance + next trade size)\n"
        "💰 `/balance 500` — Set modal saat ini $500\n"
        "📊 `/setstake 10` — Set stake 10% per trade dari balance\n\n"
        "🔬 *BACKTEST ENGINE*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧪 `/btall 30` — Batch backtest TOP 20 coins × combined × 30 hari\n"
        "   → Rank coins by WR%. Cache dipakai untuk validasi sinyal otomatis\n"
        "📊 `/backtest BTC scalp 30` — Backtest sinyal bot ke data historis\n"
        "   strategies: `scalp` | `swing` | `prepump` | `predump` | `combined`\n"
        "📋 `/btresult` — Hasil backtest terakhir\n"
        "🔬 `/btcompare BTC 14` — Compare semua strategy untuk 1 coin\n"
        "📚 `/btstats` — History aggregate semua backtest session\n"
        "📡 `/signals` — Status semua signal + per-coin win rate\n"
        "🌐 `/marketstatus` — Fear&Greed + BTC Regime + Market Breadth + Dominance\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🐦 *X (TWITTER) SENTIMENT & DCA*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💎 `/dca BTC` — DCA signal: KOL activity + narrative cycle + price context\n"
        "   → Early Narrative = 💎 ACCUMULATE | Top Signal = 🚨 AVOID\n"
        "🐦 `/xsenti SOL` — Quick X sentiment untuk 1 coin\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 *CONFIRMED ENTRY* _(auto, no command needed)_\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Bot otomatis gabungkan:\n"
        "  confluence + prepump + predump + scalp + swing\n"
        "Divalidasi backtest 7 hari → kalau bagus, langsung kirim.\n"
        "Threshold: master score >= 75 + backtest profit factor >= 1.0\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💬 *DISKUSI SINYAL & GAYA TRADING* _(v15 baru!)_\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "❓ `/why [BTC]` — Kenapa sinyal ini? (indikator pendorong + saran sesuai gaya kamu)\n"
        "💬 *Reply* ke pesan bot mana aja → diskusi nyambung soal coin/sinyal itu\n"
        "   Lanjut ngobrol tanpa reply juga bisa; ketik `/done` kalau udah.\n"
        "   Bot bisa koreksi kamu, kamu bisa koreksi bot.\n"
        "🪞 *Refleksi → lesson*: cerita hasil sinyal (mis. \"sinyal BNB tadi kena SL\")\n"
        "   → bot rangkum pelajaran & tawarin simpan ke memori (ya/skip).\n"
        "   Pelajaran yang disimpan dipakai pas bot bikin sinyal berikutnya.\n"
        "🎚️ `/style` — Lihat gaya trading yang dipelajari bot (hapus: `/style del 2`)\n"
        "   Insight dari diskusi disimpan setelah kamu konfirmasi (ya/skip).\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📝 *TRADE JOURNAL*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📝 `/logtrade` — Log trade baru (wizard step-by-step)\n"
        "   atau `/logtrade BTC LONG 65000 50 10 +25` (one-liner)\n"
        "📸 `/logshot` — Kirim screenshot order details, AI baca otomatis\n"
        "📋 `/trades` — Lihat 5 trade terakhir\n"
        "📊 `/weeksummary` — Weekly summary + AI analysis\n"
        "💰 `/setbalance 500` — Set saldo awal\n"
        "🔄 `/refreshdashboard` — Rebuild layout Dashboard di Sheets\n\n"
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


def run_btall_scheduled():
    """
    Scheduled auto-batch-backtest (silent, tanpa spam Telegram).
    Generate/refresh btall_results.json supaya GATE 3 pakai cache cepat
    bukan live backtest per-signal. Dijadwalkan harian (cache valid 7 hari).
    """
    if not BACKTEST_MODULE:
        return
    try:
        from backtest_engine import run_batch_backtest

        all_coins = get_top_coins()
        if not all_coins:
            log.warning("auto-btall: gagal fetch coin list, skip")
            return

        top20 = sorted(all_coins, key=lambda c: c.get("market_cap", 0), reverse=True)[:20]
        symbols = []
        for c in top20:
            sym = c.get("symbol", "").upper()
            if sym and sym not in ("USDT", "BUSD", "USDC", "DAI"):
                symbols.append(sym + "USDT")

        if not symbols:
            log.warning("auto-btall: symbol list kosong, skip")
            return

        log.info(f"🧪 auto-btall: backtest {len(symbols)} coins (combined, 30d)...")
        results = run_batch_backtest(symbols, strategy="combined", days=30)
        n_valid = sum(1 for r in results if r.get("_grade") in ("STRONG", "MODERATE", "WEAK"))
        log.info(f"✅ auto-btall selesai: {len(results)} coins, {n_valid} punya data valid → cache updated")
    except Exception as e:
        log.error(f"auto-btall error: {e}", exc_info=True)


def handle_btall_command(args: str, chat_id: str):
    """
    /btall [days] — Batch backtest top 20 coins × combined strategy.
    Hasilnya disimpan ke btall_results.json dan dipakai sebagai cache
    untuk validasi sinyal selanjutnya (menggantikan live per-signal backtest).
    """
    if not BACKTEST_MODULE:
        send_telegram("❌ Backtest module tidak tersedia.", chat_id)
        return

    try:
        days = int(args.strip()) if args.strip().isdigit() else 30
        days = max(7, min(days, 90))
    except Exception:
        days = 30

    send_telegram(
        f"🧪 <b>Batch Backtest dimulai!</b>\n"
        f"📅 Period: {days} hari | Strategy: COMBINED\n"
        f"⏳ Mengambil top coins... ~3-8 menit\n"
        f"<i>Hasil akan dikirim setelah selesai.</i>",
        chat_id, parse_mode="HTML"
    )

    def _run():
        try:
            from backtest_engine import run_batch_backtest, format_batch_result

            # Ambil top 20 coins dari CoinGecko, sorted by market cap
            all_coins = get_top_coins()
            if not all_coins:
                send_telegram("❌ Gagal fetch coin list dari CoinGecko.", chat_id)
                return

            top20 = sorted(all_coins, key=lambda c: c.get("market_cap", 0), reverse=True)[:20]
            symbols = []
            for c in top20:
                sym = c.get("symbol", "").upper()
                if sym and sym not in ("USDT", "BUSD", "USDC", "DAI"):
                    symbols.append(sym + "USDT")

            sym_names = ", ".join(s.replace("USDT", "") for s in symbols[:10])
            send_telegram(
                f"📋 <b>{len(symbols)} coins:</b> {sym_names}{'...' if len(symbols) > 10 else ''}\n"
                f"⚙️ Running backtest... sabar ya 🙏",
                chat_id, parse_mode="HTML"
            )

            results = run_batch_backtest(symbols, strategy="combined", days=days)
            msg     = format_batch_result(results, "combined", days)
            send_telegram(msg, chat_id, parse_mode="HTML")

            # Update tip
            strong = [r for r in results if r.get("_grade") == "STRONG"]
            if strong:
                syms = ", ".join(r.get("symbol","").replace("USDT","") for r in strong[:5])
                send_telegram(
                    f"💡 <b>Tip:</b> Sinyal selanjutnya untuk {syms} akan divalidasi "
                    f"menggunakan hasil batch backtest ini (cache 7 hari).",
                    chat_id, parse_mode="HTML"
                )
        except Exception as e:
            log.error(f"btall error: {e}", exc_info=True)
            send_telegram(f"❌ Batch backtest error: {e}", chat_id)

    threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────
# TELEGRAM POLLER
# ─────────────────────────────────────────────

last_update_id    = 0
_awaiting_chart   = {}  # chat_id → True (user habis kirim /chart, tunggu foto)
_awaiting_tradeshot = {}  # chat_id → True (user habis kirim /logshot, tunggu screenshot)
_pending_shot     = {}  # chat_id → trade dict hasil baca screenshot, nunggu konfirmasi ya/batal

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

    chat_id   = str(message.get("chat", {}).get("id", ""))
    text      = message.get("text", "").strip()
    photos    = message.get("photo", [])
    # Extract topic thread_id (Telegram Topics / forum supergroup)
    raw_tid   = message.get("message_thread_id")
    topic_tid = str(raw_tid) if raw_tid else None

    if not chat_id:
        return

    # ── SECURITY: whitelist check ────────────
    if not is_allowed(chat_id):
        return  # silent drop — unauthorized user tidak dapat response apapun
    # ─────────────────────────────────────────

    log.info(f"📩 [{chat_id}] tid={topic_tid} text='{text[:60]}' photos={len(photos)}")

    # Inject topic_tid into this thread so inline send_telegram calls in
    # process_update itself also reply to the correct topic.
    _msg_ctx.thread_id = topic_tid

    # Helper: spawn a handler thread that carries the topic thread_id in its
    # thread-local context so all send_telegram() calls reply to the same topic.
    def _thread(fn, *args, tid=topic_tid):
        def _run():
            _msg_ctx.thread_id = tid
            fn(*args)
        return threading.Thread(target=_run, daemon=True)

    # ── Handle foto chart ────────────────────────
    if photos:
        photo   = photos[-1]
        file_id = photo.get("file_id")
        if file_id:
            if JOURNAL_MODULE and is_wizard_expecting_image(chat_id):
                _thread(handle_journal_wizard_image, file_id, chat_id).start()
            elif JOURNAL_MODULE and _awaiting_tradeshot.get(chat_id):
                _thread(handle_trade_screenshot, file_id, chat_id).start()
            else:
                _thread(handle_chart_command, chat_id, file_id).start()
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
            _thread(handle_analyze_command, coin, chat_id).start()
        else:
            send_telegram("❓ Format: `/analyze BTC` atau `/analyze SOLUSDT`", chat_id)

    elif text_lower.startswith("/chart"):
        _awaiting_chart[chat_id] = True
        send_telegram("📸 Siap! Sekarang kirimkan gambar chart kamu dan akan langsung dianalisa AI 🤖", chat_id)

    elif text_lower.startswith("/prepump"):
        _thread(handle_prepump_command, chat_id).start()

    elif text_lower.startswith("/predump"):
        _thread(handle_predump_command, chat_id).start()

    elif text_lower.startswith("/scalp"):
        _thread(handle_scalp_command, chat_id).start()

    # ── v9: News ──────────────────────────────
    elif text_lower.startswith("/newsagent"):
        _thread(handle_newsagent_command, chat_id).start()

    elif text_lower.startswith("/newslessons"):
        _thread(handle_newslessons_command, chat_id).start()

    elif text_lower.startswith("/news"):
        parts = text.split(maxsplit=1)
        coin  = parts[1].strip() if len(parts) > 1 else ""
        _thread(handle_news_command, coin, chat_id).start()

    elif text_lower.startswith("/macro"):
        _thread(handle_macro_command, chat_id).start()

    # ── X Sentiment / DCA ─────────────────────
    elif text_lower.startswith("/dca"):
        parts = text.split(maxsplit=1)
        coin  = parts[1].strip() if len(parts) > 1 else ""
        _thread(handle_dca_command, coin, chat_id).start()

    elif text_lower.startswith("/xsenti"):
        parts = text.split(maxsplit=1)
        coin  = parts[1].strip() if len(parts) > 1 else ""
        _thread(handle_xsentiment_command, coin, chat_id).start()

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
        _thread(handle_logtrade_command, parts[1] if len(parts)>1 else "", chat_id).start()

    elif text_lower.startswith("/logshot") or text_lower.startswith("/logshoot"):
        handle_logshot_command(chat_id)

    elif text_lower.startswith("/trades"):
        _thread(handle_trades_command, chat_id).start()

    elif text_lower.startswith("/weeksummary"):
        _thread(handle_weeksummary_command, chat_id).start()

    elif text_lower.startswith("/refreshdashboard"):
        _thread(handle_refreshdashboard_command, chat_id).start()

    elif text_lower.startswith("/setbalance"):
        parts = text.split(maxsplit=1)
        _thread(handle_setbalance_command, parts[1] if len(parts)>1 else "", chat_id).start()

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
            _thread(lambda: send_telegram(handle_evolve_command(), chat_id, parse_mode="HTML")).start()
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
            _thread(_bt_backtest, args, chat_id, send_telegram).start()
        else:
            send_telegram(
                "❌ Backtest module tidak tersedia.\n"
                "Pastikan `backtest_engine.py` ada di folder yang sama.",
                chat_id
            )

    elif text_lower.startswith("/btall"):
        parts = text.split(maxsplit=1)
        args  = parts[1].strip() if len(parts) > 1 else ""
        handle_btall_command(args, chat_id)

    elif text_lower.startswith("/btresult"):
        if BACKTEST_MODULE:
            handle_btresult_wrapper(chat_id)
        else:
            send_telegram("❌ Backtest module tidak tersedia.", chat_id)

    elif text_lower.startswith("/btcompare"):
        parts = text.split(maxsplit=1)
        args  = parts[1].strip() if len(parts) > 1 else ""
        if BACKTEST_MODULE:
            _thread(_bt_compare, args, chat_id, send_telegram).start()
        else:
            send_telegram("❌ Backtest module tidak tersedia.", chat_id)

    elif text_lower.startswith("/btstats"):
        if BACKTEST_MODULE:
            _bt_stats(chat_id, send_telegram)
        else:
            send_telegram("❌ Backtest module tidak tersedia.", chat_id)

    elif text_lower.startswith("/signalbt"):
        parts = text.split(maxsplit=1)
        args  = parts[1].strip() if len(parts) > 1 else ""
        if BACKTEST_MODULE:
            _thread(_bt_signal, args, chat_id, send_telegram).start()
        else:
            send_telegram(
                "❌ Backtest module tidak tersedia.\n"
                "Pastikan `backtest_engine.py` ada di folder yang sama.",
                chat_id
            )

    # ── Manual Trade Manager ──────────────────────────
    elif text_lower.startswith("/trade ") or text_lower == "/trade":
        parts = text.split(maxsplit=1)
        args  = parts[1].strip() if len(parts) > 1 else ""
        if not TRADE_MANAGER_MODULE:
            send_telegram("❌ Trade Manager module tidak tersedia.", chat_id)
        elif not args:
            send_telegram(
                "📌 <b>Format /trade:</b>\n"
                "<code>/trade SYMBOL DIRECTION ENTRY [SIZE_USD]</code>\n\n"
                "Contoh:\n"
                "<code>/trade BTC LONG 95000 60</code>\n"
                "<code>/trade ETH SHORT 3200 100</code>\n\n"
                "Bot akan otomatis hitung SL, TP1, TP2, BE, dan trailing stop.",
                chat_id
            )
        else:
            def _open_trade():
                parsed = parse_trade_command(args)
                if "error" in parsed:
                    send_telegram(f"❌ {parsed['error']}", chat_id)
                    return
                send_telegram(
                    f"⏳ Menghitung level untuk {parsed['symbol']} (fetch ATR)...", chat_id
                )
                trade = record_trade(
                    parsed["symbol"], parsed["direction"],
                    parsed["entry"], parsed["size"]
                )
                if "error" in trade:
                    send_telegram(f"❌ {trade['error']}", chat_id)
                else:
                    send_telegram(format_trade_opened(trade), chat_id)
            _thread(_open_trade).start()

    elif text_lower.startswith("/close"):
        parts = text.split(maxsplit=2)
        if not TRADE_MANAGER_MODULE:
            send_telegram("❌ Trade Manager module tidak tersedia.", chat_id)
        elif len(parts) < 2:
            send_telegram(
                "📌 Format: <code>/close SYMBOL [EXIT_PRICE]</code>\n"
                "Contoh:\n"
                "<code>/close BTC</code> → close di harga sekarang\n"
                "<code>/close BTC 96000</code> → close di harga spesifik",
                chat_id
            )
        else:
            sym_arg   = parts[1].strip().upper()
            price_arg = None
            if len(parts) >= 3:
                try:
                    price_arg = float(parts[2].replace(",", ""))
                except ValueError:
                    send_telegram(f"❌ Harga tidak valid: '{parts[2]}'", chat_id)
                    return
            def _close_trade(s=sym_arg, p=price_arg):
                if not p:
                    send_telegram(f"⏳ Fetching harga {s}...", chat_id)
                trade = close_trade(s, p, "MANUAL")
                if trade is None:
                    send_telegram(
                        f"❌ Tidak ada posisi aktif untuk <b>{s}</b>.\n"
                        f"Lihat posisi aktif dengan /trades",
                        chat_id
                    )
                else:
                    send_telegram(format_closed_trade(trade), chat_id)
            _thread(_close_trade).start()

    elif text_lower == "/trades":
        if not TRADE_MANAGER_MODULE:
            send_telegram("❌ Trade Manager module tidak tersedia.", chat_id)
        else:
            def _list_trades():
                active = get_active_trades()
                send_telegram(format_trades_list(active), chat_id)
            _thread(_list_trades).start()

    elif text_lower.startswith("/balance"):
        parts = text.split(maxsplit=1)
        if not TRADE_MANAGER_MODULE:
            send_telegram("❌ Trade Manager module tidak tersedia.", chat_id)
        elif len(parts) < 2:
            send_telegram(
                "📌 Format: <code>/balance &lt;jumlah&gt;</code>\n"
                "Contoh: <code>/balance 500</code>\n\n"
                "Gunakan /compound untuk melihat status compound stake.",
                chat_id
            )
        else:
            try:
                bal = float(parts[1].replace(",", "").replace("$", ""))
                if bal <= 0:
                    raise ValueError
                cfg = set_balance(bal)
                from trade_manager import get_auto_stake
                next_stake = get_auto_stake()
                send_telegram(
                    f"✅ Balance diset ke <b>${bal:,.2f}</b>\n"
                    f"📊 Stake per trade ({cfg['stake_pct']*100:.1f}%): <b>${next_stake:,.2f}</b>\n\n"
                    f"Lihat detail: /compound",
                    chat_id
                )
            except ValueError:
                send_telegram(f"❌ Jumlah tidak valid: '{parts[1]}'", chat_id)

    elif text_lower.startswith("/setstake"):
        parts = text.split(maxsplit=1)
        if not TRADE_MANAGER_MODULE:
            send_telegram("❌ Trade Manager module tidak tersedia.", chat_id)
        elif len(parts) < 2:
            send_telegram(
                "📌 Format: <code>/setstake &lt;persen&gt;</code>\n"
                "Contoh: <code>/setstake 10</code> → 10% dari balance per trade\n"
                "Range: 1% – 50%",
                chat_id
            )
        else:
            try:
                pct = float(parts[1].replace("%", ""))
                if pct <= 0:
                    raise ValueError
                cfg = set_stake_pct(pct)
                from trade_manager import get_auto_stake
                next_stake = get_auto_stake()
                stake_str = f"${next_stake:,.2f}" if next_stake else "balance belum diset"
                send_telegram(
                    f"✅ Stake per trade diset ke <b>{cfg['stake_pct']*100:.1f}%</b>\n"
                    f"💰 Next trade size: <b>{stake_str}</b>",
                    chat_id
                )
            except ValueError:
                send_telegram(f"❌ Persentase tidak valid: '{parts[1]}'", chat_id)

    elif text_lower == "/compound":
        if not TRADE_MANAGER_MODULE:
            send_telegram("❌ Trade Manager module tidak tersedia.", chat_id)
        else:
            send_telegram(format_compound_status(), chat_id)

    elif text_lower.startswith("/liqstatus"):
        parts = text.split(maxsplit=1)
        sym_arg = parts[1].strip().upper() if len(parts) > 1 else "BTCUSDT"
        if not sym_arg.endswith("USDT"):
            sym_arg += "USDT"
        if LIQ_TRACKER_MODULE:
            d = get_liq_data(sym_arg)
            surge_icon = ""
            if d.get("liq_surge_long"):
                surge_icon = "⚡ LONG SURGE"
            elif d.get("liq_surge_short"):
                surge_icon = "⚡ SHORT SURGE"
            else:
                surge_icon = "✅ Normal"
            gl_z = get_glassnode_btc_netflow_zscore()
            gl_line = f"\n🔗 Glassnode BTC netflow Z-score: {gl_z:.2f}" if gl_z is not None else ""
            send_telegram(
                f"⚡ <b>Liquidation Status: {sym_arg}</b>\n\n"
                f"Status: {surge_icon}\n"
                f"📉 Long liq (15m): ${d.get('long_liq_usd',0)/1e6:.2f}M\n"
                f"📈 Short liq (15m): ${d.get('short_liq_usd',0)/1e6:.2f}M\n"
                f"Bias: {d.get('net_liq_bias','—')}\n"
                f"Events: {d.get('event_count',0)}"
                + gl_line,
                chat_id
            )
        else:
            send_telegram("⚠️ Liquidation tracker tidak tersedia.", chat_id)

    elif text_lower.startswith("/marketstatus"):
        if MARKET_CONTEXT_MODULE:
            send_telegram("🌐 Fetching market context... ⏳", chat_id)
            try:
                ctx = get_market_context()
                block = format_market_context_block(ctx, compact=False)
                fg    = ctx.get("fear_greed", {})
                ts    = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
                msg   = (
                    "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "🌐 *MARKET REGIME STATUS*\n"
                    f"🕐 {ts}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    + block +
                    "\n\n⚠️ _Not financial advice. DYOR._"
                )
                send_telegram(msg, chat_id)
            except Exception as _me:
                send_telegram(f"❌ Error fetch market context: {_me}", chat_id)
        else:
            send_telegram("⚠️ market_context.py tidak tersedia.", chat_id)

    elif text_lower.startswith("/signals"):
        if TRACKER_MODULE:
            send_telegram(format_tracker_summary(), chat_id)
        else:
            send_telegram("❌ Signal tracker module tidak tersedia.", chat_id)

    # ── v13: Symbol Memory ───────────────────────
    elif text_lower.startswith("/symbolmemory"):
        parts = text.split(maxsplit=1)
        _thread(handle_symbolmemory_command, parts[1] if len(parts)>1 else "", chat_id).start()

    elif text_lower.startswith("/symbolstats"):
        _thread(handle_symbolstats_command, chat_id).start()

    elif text_lower.startswith("/blacklist"):
        parts = text.split(maxsplit=1)
        handle_blacklist_command(parts[1] if len(parts)>1 else "", chat_id)

    elif text_lower.startswith("/unblacklist"):
        parts = text.split(maxsplit=1)
        handle_unblacklist_command(parts[1] if len(parts)>1 else "", chat_id)

    elif text_lower.startswith("/ask"):
        parts    = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""
        _thread(handle_ask_command, question, chat_id).start()

    elif text_lower.startswith("/scan"):
        send_telegram("📡 Manual scan dimulai... ⏳", chat_id)
        _thread(lambda: run_scan(manual=True, chat_id=chat_id)).start()

    elif text_lower.startswith("/status"):
        handle_status_command(chat_id)

    elif text_lower.startswith("/security"):
        send_telegram(get_security_status(), chat_id)

    # ── v15: Signal discussion + trading-style ───
    elif text_lower.startswith("/why"):
        if SIGNAL_CHAT_MODULE:
            parts = text.split(maxsplit=1)
            sym   = parts[1].strip() if len(parts) > 1 else None
            _thread(signal_chat.handle_why, sym, chat_id, _signal_chat_ai, send_telegram).start()
        else:
            send_telegram("⚠️ signal_chat.py tidak tersedia.", chat_id)

    elif text_lower.startswith("/style"):
        if SIGNAL_CHAT_MODULE:
            parts = text.split(maxsplit=1)
            signal_chat.handle_style_command(parts[1] if len(parts) > 1 else "", chat_id, send_telegram)
        else:
            send_telegram("⚠️ signal_chat.py tidak tersedia.", chat_id)

    elif text_lower.startswith("/help") or text_lower.startswith("/start"):
        handle_help_command(chat_id)

    # Konfirmasi ya/batal untuk screenshot trade yang sudah dibaca (prioritas)
    elif JOURNAL_MODULE and chat_id in _pending_shot:
        _thread(handle_shot_confirm, text, chat_id).start()

    # v11: Journal wizard intercept (harus di atas free-form)
    elif JOURNAL_MODULE and is_in_wizard(chat_id):
        _thread(handle_journal_wizard_message, text, chat_id).start()

    # v15: Jawaban ya/skip untuk usulan aturan gaya trading / pelajaran (prioritas)
    elif (SIGNAL_CHAT_MODULE
          and signal_chat.has_pending(chat_id)
          and signal_chat.is_confirm_answer(text)):
        signal_chat.handle_confirm(text, chat_id, send_telegram)

    # v15: /done → tutup diskusi yang sedang aktif
    elif (SIGNAL_CHAT_MODULE and text_lower in ("/done", "selesai", "cukup", "udahan")
          and signal_chat.is_discussion_active(chat_id)):
        signal_chat.end_convo(chat_id)
        send_telegram("👍 Oke, diskusi ditutup. Reply sinyal mana aja kalau mau bahas lagi.", chat_id)

    # v15: Reply ke pesan BOT → diskusi grounded (di atas free-form)
    elif (SIGNAL_CHAT_MODULE
          and message.get("reply_to_message", {}).get("from", {}).get("is_bot")):
        replied = message["reply_to_message"]
        _thread(signal_chat.handle_discussion_reply,
                replied.get("message_id"), text, chat_id, _signal_chat_ai,
                send_telegram, replied.get("text", "")).start()

    # Direct coin name → analyze
    elif len(text.split()) == 1 and text.upper().replace("USDT", "") in TICKER_TO_BINANCE:
        _thread(handle_analyze_command, text.strip(), chat_id).start()

    # Command tak dikenal (diawali "/") → jangan lempar ke AI, kasih saran command terdekat
    elif text_lower.startswith("/"):
        cmd = text_lower.split()[0]
        suggestion = _suggest_command(cmd)
        hint = f"\n\n💡 Maksud kamu <code>{suggestion}</code>?" if suggestion else ""
        send_telegram(
            f"❓ Command <code>{cmd}</code> tidak dikenal.{hint}\n\n"
            "Ketik /help buat lihat daftar command.",
            chat_id,
        )

    # v15: Lanjutan diskusi aktif (tanpa reply, dalam window) → diskusi
    elif SIGNAL_CHAT_MODULE and signal_chat.is_discussion_active(chat_id):
        _thread(signal_chat.handle_followup, text, chat_id, _signal_chat_ai, send_telegram).start()

    # v16: Free-form yang terdengar seperti refleksi hasil sinyal → diskusi reflektif
    # (bisa menghasilkan lesson). Di-thread; kalau ternyata tanpa konteks sinyal,
    # fallback ke /ask di dalam thread yang sama.
    elif (SIGNAL_CHAT_MODULE and len(text) > 3
          and signal_chat.looks_like_reflection(text)):
        def _reflect_or_ask(t=text, c=chat_id):
            if not signal_chat.handle_freeform(t, c, _signal_chat_ai, send_telegram):
                handle_ask_command(t, c)
        _thread(_reflect_or_ask).start()

    # Free-form question → ask
    elif len(text) > 3:
        _thread(handle_ask_command, text, chat_id).start()


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


def _load_active_setups() -> dict:
    """
    Baca pending_signals.json → return {symbol: direction} untuk sinyal yang masih PENDING.
    Dipakai untuk cek apakah setup coin sudah aktif sebelum kirim sinyal baru.
    """
    import os as _os
    try:
        if not _os.path.exists("pending_signals.json"):
            return {}
        with open("pending_signals.json") as _f:
            _pending = _json.load(_f)
        return {
            s["symbol"]: s["direction"]
            for s in _pending
            if isinstance(s, dict) and s.get("status") == "PENDING"
        }
    except Exception:
        return {}


def _gate_cooldown_ok(symbol: str, state: dict) -> bool:
    """
    Phase 1 cooldown check — sebelum fetch data.
    - Kalau symbol punya PENDING signal → izinkan pass (Phase 2 yg putuskan berdasar direction)
    - Kalau tidak ada pending → pakai time-based cooldown seperti biasa
    """
    # Kalau ada active pending setup, bypass cooldown — Phase 2 akan cek direction
    active = _load_active_setups()
    if symbol in active:
        return True

    sent = state.get(_SENT_SIGNALS_KEY, {})
    entry = sent.get(symbol)
    if not entry:
        return True
    # Backwards-compat: entry lama adalah string ISO, entry baru adalah dict
    last_ts = entry["ts"] if isinstance(entry, dict) else entry
    last = datetime.fromisoformat(last_ts)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return elapsed >= GATE_COOLDOWN_HOURS


def _gate_mark_sent(symbol: str, state: dict, direction: str = ""):
    """Tandai symbol sebagai sudah dikirim signal, simpan direction untuk persistence check."""
    state.setdefault(_SENT_SIGNALS_KEY, {})[symbol] = {
        "ts":        datetime.now(timezone.utc).isoformat(),
        "direction": direction,
    }


def _get_persistence_bonus(symbol: str, direction: str, state: dict) -> tuple[int, str]:
    """
    Cek apakah signal sama dikirim di scan sebelumnya.
    Returns (bonus_pts, reason_str):
      +5  jika direction sama (signal konsisten → reward)
      -5  jika direction flip (whipsaw → penalty)
       0  jika tidak ada history
    """
    entry = state.get(_SENT_SIGNALS_KEY, {}).get(symbol)
    if not isinstance(entry, dict):
        return 0, ""
    prev_dir = entry.get("direction", "")
    if not prev_dir:
        return 0, ""
    if prev_dir == direction:
        return 5, f"Persistent {direction} signal +5"
    return -5, f"Direction flip {prev_dir}→{direction} -5"


def _check_money_flow_gate(tf_4h: dict, tf_1h: dict, tf_15m: dict, direction: str) -> tuple[bool, str]:
    """
    Gate: minimal 1 TF harus align dengan direction, dan maksimal 1 TF boleh kontra.
    TF yang NEUTRAL tidak dihitung sebagai aligned maupun kontra.
    PUMP → butuh INFLOW, DUMP → butuh OUTFLOW.
    Returns (passed, reason_str)
    """
    expected  = "INFLOW" if direction in ("LONG", "PUMP") else "OUTFLOW"
    opposite  = "OUTFLOW" if expected == "INFLOW" else "INFLOW"
    tfs       = {"4H": tf_4h, "1H": tf_1h, "15M": tf_15m}
    aligned   = [name for name, tf in tfs.items()
                 if tf.get("money_flow", {}).get("bias") == expected]
    conflict  = [name for name, tf in tfs.items()
                 if tf.get("money_flow", {}).get("bias") == opposite]
    # Butuh ≥1 TF aligned DAN ≤1 TF kontra (TF NEUTRAL tidak dihitung)
    passed    = len(aligned) >= 1 and len(conflict) <= 1
    reason    = f"MoneyFlow {expected}: {len(aligned)}/3 TF aligned ({', '.join(aligned) if aligned else 'none'})"
    if conflict:
        reason += f" ⚠️ kontra di {', '.join(conflict)}"
    return passed, reason


def _check_entry_mode_gate(trade: dict) -> tuple[bool, str]:
    """Gate: entry mode harus MOMENTUM_NOW, SNIPER_ENTRY, atau RETEST_WAIT dengan zona."""
    if not trade:
        return False, "No trade plan"
    em = trade.get("entry_mode", "")
    if em == "MOMENTUM_NOW":
        return True, "MOMENTUM_NOW — entry market sekarang"
    if em == "SNIPER_ENTRY":
        return True, "SNIPER_ENTRY — price di zone + candle confirmed 🎯"
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
    alert_type: str = "SETUP",  # "SETUP" | "ENTRY_NOW"
    tf_4h: dict = None, tf_1h: dict = None, tf_15m: dict = None,
) -> str:
    """
    Build pesan signal yang sudah lolos semua gate.
    alert_type SETUP = pertama kali setup terdeteksi
    alert_type ENTRY_NOW = price masuk retest zone (notif kedua)
    v14: tambah market regime + candle structure section
    """
    tf_4h  = tf_4h  or {}
    tf_1h  = tf_1h  or {}
    tf_15m = tf_15m or {}
    ts  = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
    sym = symbol.replace("USDT", "")
    dir_emoji = "🟢" if direction in ("LONG", "PUMP") else "🔴"
    dir_label = "LONG ▲" if direction in ("LONG", "PUMP") else "SHORT ▼"

    entry_mode = trade.get("entry_mode", "")
    tp1_r = trade.get("tp1_r", 0)
    conf_emoji = "🔥" if confidence == "HIGH" else "✅" if confidence == "MEDIUM" else "🟡"

    def _f(v):
        if v is None: return "N/A"
        if v >= 1000: return f"${v:,.2f}"
        elif v >= 1:  return f"${v:.4f}"
        else:         return f"${v:.6f}"

    if alert_type == "SETUP":
        header_title = "🚦 <b>SETUP TERDETEKSI</b>"
    else:
        header_title = "🚨 <b>ENTRY NOW — PRICE DI ZONA</b>"

    is_long = "LONG" in direction or "PUMP" in direction
    _prof   = trade.get("tp_profile", "")
    rr_val  = trade.get("rr", tp1_r) or tp1_r or 0
    rr_ok   = "✅" if (tp1_r or 0) >= 2.0 else "⚠️"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{conf_emoji} {header_title} — {sym}",
        f"🕐 {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"{dir_emoji} <b>{dir_label}</b>  |  🎯 <b>{master_score}/100</b> ({confidence})",
        f"💰 Harga {_f(price)}  |  📐 R:R {rr_val:.1f}:1 {rr_ok}"
        + (f"  |  🎚️ {_prof}" if _prof else ""),
        "",
    ]

    # ── ACTION: apa yang harus dilakukan (angka entry/SL/TP ada di Market Update) ──
    if entry_mode == "SNIPER_ENTRY":
        lines.append("🎯 <b>SNIPER ENTRY</b> — price di zone + candle confirmed")
        for ctx in trade.get("momentum_context", [])[:1]:
            lines.append(f"   ↳ {ctx}")
    elif entry_mode == "MOMENTUM_NOW":
        lines.append("🚀 <b>ENTRY NOW</b> — momentum confirmed")
        for ctx in trade.get("momentum_context", [])[:1]:
            lines.append(f"   ↳ {ctx}")
    else:
        cz = trade.get("confirmation_zone", {})
        if alert_type == "ENTRY_NOW":
            lines.append("🎯 <b>PRICE DI ZONA</b> — tunggu candle 15M close konfirmasi")
        else:
            lines.append(f"⏳ <b>TUNGGU RETEST</b> — zona {cz.get('source', 'key level')}")
        want_dir = "BULLISH" if is_long else "BEARISH"
        cp15     = tf_15m.get("candle_patterns", {})
        if cp15.get("pattern") not in (None, "NONE") and cp15.get("direction") == want_dir:
            lines.append(f"   ✅ Konfirmasi: {cp15['detail']}")
        else:
            conf_word = "bullish engulfing/pin bar 15M" if is_long else "bearish engulfing/pin bar 15M"
            lines.append(f"   ⏳ Tunggu: {conf_word} + vol ≥1.5x")

    # ── ANALISA: alasan inti saja (regime, candle kunci, money flow, OI, risiko) ──
    analysis = []

    _regime = confluence.get("regime") or tf_4h.get("market_regime", {}).get("regime", "")
    _adx    = tf_4h.get("market_regime", {}).get("adx", 0) or tf_4h.get("adx", 0)
    _regime_emoji = {
        "BULLISH_TREND": "📈", "BEARISH_TREND": "📉",
        "BB_SQUEEZE": "🔵", "RANGING": "↔️",
        "BREAKOUT_UP": "🚀", "BREAKOUT_DOWN": "🔻",
        "VOLATILE": "⚡", "WEAK_TREND": "〰️",
    }.get(_regime, "")
    if _regime and _regime != "UNKNOWN":
        adx_str = f" (ADX {_adx:.0f})" if _adx else ""
        analysis.append(f"  {_regime_emoji} Regime {_regime}{adx_str}")

    # Candle kunci 15M — driver timing utama
    _cp15 = tf_15m.get("candle_patterns", {})
    if _cp15.get("pattern") not in (None, "NONE"):
        pe = ("🟢" if _cp15.get("direction") == "BULLISH" else
              "🔴" if _cp15.get("direction") == "BEARISH" else "⚪")
        analysis.append(f"  {pe} 15M {_cp15['pattern']} — {_cp15['detail']}")

    # Money flow — ringkas (maks 2 baris; konflik antar-TF kelihatan di sini)
    for r in mf_reasons[:2]:
        analysis.append(f"  {r}")

    # OI / funding / L/S — gabung 1 baris
    fr   = oi_data.get("funding_rate")
    oi_c = oi_data.get("oi_change_pct")
    ls   = oi_data.get("ls_ratio")
    oi_bits = []
    if fr   is not None: oi_bits.append(f"Funding {fr:+.3f}%")
    if oi_c is not None: oi_bits.append(f"OI {oi_c:+.1f}%")
    if ls   is not None: oi_bits.append(f"L/S {ls:.2f}")
    if oi_bits:
        analysis.append("  💹 " + " | ".join(oi_bits))

    # Risiko entry: warning kalau harga sudah jauh dari zone
    _ext       = confluence.get("entry_extended", False)
    _near_zone = confluence.get("nearest_zone_pct")
    if _ext and _near_zone is not None:
        analysis.append(f"  ⚠️ Entry extended {_near_zone:.1f}% dari zone — risiko SL lebih tinggi")

    if analysis:
        lines.append("")
        lines.append("─── ANALISA ───")
        lines.extend(analysis)

    # Whale — 1 baris bias saja (align / berlawanan)
    try:
        import whale_tracker as _wt
        _coin_ticker = symbol.replace("USDT", "").replace("UST", "")
        wctx = _wt.get_whale_context_for_coin(_coin_ticker)
        whale_bias = wctx.get("whale_bias", "NEUTRAL")
        has_data = (
            wctx.get("whale_long_vol", 0) + wctx.get("whale_short_vol", 0) > 0 or
            wctx.get("wallet_long_count", 0) + wctx.get("wallet_short_count", 0) > 0
        )
        if has_data and whale_bias != "NEUTRAL":
            against = (is_long and whale_bias == "BEARISH") or (not is_long and whale_bias == "BULLISH")
            be  = "🟢" if whale_bias == "BULLISH" else "🔴"
            tag = "⚠️ berlawanan" if against else "✅ align"
            lines.append(f"  🐋 Whale {be} {whale_bias} {tag}")
    except ImportError:
        pass
    except Exception as _e:
        log.debug(f"Whale inject error: {_e}")

    # Konteks market global (F&G + BTC regime + breadth) — 1 baris compact
    if MARKET_CONTEXT_MODULE:
        try:
            _ctx = get_market_context()
            lines.append("")
            lines.append(format_market_context_block(_ctx, compact=True))
        except Exception:
            pass

    # Personal trade plan (hanya kalau user /setbalance) — position sizing/risk $
    if RISK_MODULE:
        try:
            entry_val = float(trade.get("entry") or price)
            sl_val    = float(trade.get("sl") or 0)
            tp1_val   = float(trade.get("tp1") or 0)
            tp2_val   = float(trade.get("tp2") or 0)
            if entry_val > 0 and sl_val > 0:
                plan_block = format_personal_trade_plan_block(
                    entry=entry_val, sl=sl_val,
                    tp1=tp1_val, tp2=tp2_val,
                    direction=direction
                )
                if plan_block:   # kosong kalau belum /setbalance
                    lines.append("")
                    lines.append(plan_block)
        except Exception as _pe:
            log.debug(f"Personal trade plan error: {_pe}")

    lines.append("")
    lines.append("📊 <i>Entry / SL / TP lengkap → cek Market Update</i>")
    lines.append("<i>⚠️ Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def _build_heartbeat_message(watchlist: dict, last_signal_ts: str) -> str:
    """Build pesan heartbeat tiap 4 jam: status + watchlist."""
    ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
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

    # Market context compact line
    if MARKET_CONTEXT_MODULE:
        try:
            ctx = get_market_context()
            lines.append(format_market_context_block(ctx, compact=True))
            lines.append("")
        except Exception:
            pass

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

    state = _load_gate_state()

    # ── 0. Signal tracker resolve ──────────────────
    if TRACKER_MODULE:
        try:
            resolved = on_scan_start(send_market_update)
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

    signals_sent      = 0
    watchlist_new     = {}
    _scan_sent_syms   = set()   # dedup: satu symbol max 1 signal per scan

    # Load dynamic thresholds dari learning engine (di-update tiap /evolve)
    _dyn = get_dynamic_thresholds()
    _gate_min = int(_dyn.get("PREPUMP_ALERT_THRESHOLD", GATE_MASTER_SCORE_MIN))
    if _gate_min != GATE_MASTER_SCORE_MIN:
        log.info(f"🧬 Dynamic threshold aktif: GATE_MASTER_SCORE_MIN → {_gate_min}")

    # Load active setups SEKALI per scan (efisien, tidak baca file tiap coin)
    _active_setups = _load_active_setups()
    log.info(f"📋 Active pending setups: {len(_active_setups)} coin(s): {list(_active_setups.keys())}")

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

        # v14: fetch real-time momentum sebelum confluence (1m candles, non-blocking)
        realtime_momentum = detect_realtime_momentum(analysis_sym)

        confluence = calculate_confluence_v4(tf_4h, tf_1h, tf_15m, oi, realtime_momentum)
        direction  = confluence.get("direction", "NEUTRAL")

        if direction == "NEUTRAL":
            continue

        # ── Phase 2: Active setup check ───────────────
        # Cegah sinyal duplikat selama setup coin masih PENDING.
        # Izinkan kalau arah berubah (bias baru = setup baru yang valid).
        _signal_dir_now = "LONG" if direction == "PUMP" else "SHORT"
        if analysis_sym in _active_setups:
            _active_dir = _active_setups[analysis_sym]
            if _active_dir == _signal_dir_now:
                log.info(f"⏭️ {analysis_sym}: setup {_signal_dir_now} masih PENDING — skip duplicate")
                continue
            # Arah berubah → kirim peringatan ke Market Update lalu izinkan setup baru
            _cur_price = tf_1h.get("price", 0)
            _flip_emoji_old = "🟢" if _active_dir == "LONG" else "🔴"
            _flip_emoji_new = "🔴" if _active_dir == "LONG" else "🟢"
            _close_action  = "CLOSE LONG / SHORT" if _active_dir == "LONG" else "CLOSE SHORT / BUY BACK"
            # Rekomendasi aksi untuk arah baru (NOW vs WAIT) — heuristik momentum
            # + confluence karena trade plan baru belum dibangun di titik ini.
            _flip_action = _entry_action_reco(_signal_dir_now, confluence, realtime_momentum)
            _bias_flip_msg = (
                f"🔄 <b>BIAS BERUBAH — {analysis_sym.replace('USDT','')}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{_flip_emoji_old} {_active_dir}  →  {_flip_emoji_new} {_signal_dir_now}\n"
                f"💰 Harga sekarang: <code>${_cur_price:.4f}</code>\n\n"
                f"⚠️ Setup <b>{_active_dir}</b> sebelumnya kemungkinan sudah tidak valid.\n"
                f"💡 Pertimbangkan <b>{_close_action}</b> jika posisi masih terbuka.\n\n"
                f"📌 Aksi {_signal_dir_now}: {_flip_action}"
            )
            try:
                send_market_update(_bias_flip_msg)
                log.info(f"📢 Bias flip alert sent: {analysis_sym} {_active_dir}→{_signal_dir_now}")
            except Exception as _e:
                log.warning(f"Bias flip alert error: {_e}")
            log.info(f"🔄 {analysis_sym}: bias berubah {_active_dir}→{_signal_dir_now} — izinkan setup baru")

        # Detectors
        prepump = detect_prepump(analysis_sym, tf_1h, tf_4h, oi, tf_15m)
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

        # v14: conf_score adalah base — detector lain tambah bonus max +40
        # Formula lama salah: conf_score*0.4 → max 40 poin padahal gate 65
        if pump_dir:
            bonus = min(40, (pp_score // 4) + (sc_score // 5) + (sw_score // 6))
            raw_master = min(100, conf_score + bonus)
        else:
            bonus = min(40, (pd_score // 4) + (sc_score // 5) + (sw_score // 6))
            raw_master = min(100, conf_score + bonus)

        # Signal persistence: +5 jika arah sama dengan scan lalu, -5 jika flip
        _signal_dir_str = "LONG" if pump_dir else "SHORT"
        pers_bonus, pers_reason = _get_persistence_bonus(analysis_sym, _signal_dir_str, state)
        if pers_bonus != 0:
            raw_master = max(0, min(100, raw_master + pers_bonus))
            (gate_reasons if pers_bonus > 0 else failed_reasons).append(pers_reason)

        gate_results["master_score"] = raw_master >= _gate_min
        if gate_results["master_score"]:
            gate_reasons.append(f"Master score {raw_master}/100 ≥ {_gate_min}")
        else:
            failed_reasons.append(f"Master score {raw_master} < {_gate_min}")

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
                from confirmed_signal import _quick_backtest_validate
                bt_dir = "LONG" if pump_dir else "SHORT"
                bt_result = _quick_backtest_validate(analysis_sym, bt_dir)
                bt_pf     = bt_result.get("profit_factor", 0)
                # Data belum cukup → lolos (lagi ngumpulin data, jangan blok).
                # Data cukup → baru cek PF harus ≥ minimum.
                if bt_result.get("insufficient_data"):
                    bt_pass   = bt_result.get("valid", True)
                    bt_reason = f"Backtest: {bt_result.get('reason', 'data belum cukup')}"
                else:
                    bt_pass   = bt_result.get("valid", False) and bt_pf >= GATE_BT_PF_MIN
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

        # Sanity check: TP harus di arah yang benar sebelum dikirim
        _trade_tp  = float(trade.get("tp1") or 0)
        _trade_sl  = float(trade.get("sl") or 0)
        _trade_dir = "LONG" if pump_dir else "SHORT"
        _tp_sane   = (_trade_dir == "LONG"  and _trade_tp > price) or \
                     (_trade_dir == "SHORT" and _trade_tp < price)
        if not _tp_sane or not _trade_tp or not _trade_sl:
            log.warning(f"⚠️ TP sanity FAIL {analysis_sym}: dir={_trade_dir} price={price} tp={_trade_tp} — signal dibatalkan")
            all_pass = False

        if all_pass:
            # ── ALL GATES PASSED → DEEPSEEK REVIEW SEBELUM KIRIM ──
            _signal_direction = "LONG" if pump_dir else "SHORT"

            # v15: DeepSeek strategic review — adjust entry/TP/SL + news context
            ai_review = None
            if DEEPSEEK_MODULE and DEEPSEEK_API_KEY:
                try:
                    _news_ctx = None
                    if NEWS_MODULE:
                        try:
                            _news_ctx = get_structured_news_for_ai(analysis_sym)
                        except Exception:
                            pass
                    ai_review = deepseek_signal_review(
                        symbol       = analysis_sym,
                        direction    = _signal_direction,
                        trade        = trade,
                        master_score = raw_master,
                        reasons      = gate_reasons,
                        oi_data      = oi,
                        tf_4h        = tf_4h, tf_1h = tf_1h, tf_15m = tf_15m,
                        news_context = _news_ctx,
                        signal_type  = "GATED_SIGNAL",
                    )
                    # Kalau AI verdict SKIP → batalkan sinyal
                    if ai_review and ai_review.get("ai_verdict") == "SKIP":
                        log.info(f"🤖 DeepSeek SKIP {analysis_sym} — AI tidak konfirmasi sinyal")
                        continue
                    # Apply adjustments kalau ada
                    if ai_review and ai_review.get("was_adjusted"):
                        trade = dict(trade)   # copy agar tidak mutate original
                        trade["entry"] = ai_review["entry"]
                        trade["tp1"]   = ai_review["tp1"]
                        trade["tp2"]   = ai_review["tp2"]
                        trade["sl"]    = ai_review["sl"]
                        log.info(
                            f"🤖 DeepSeek adjusted {analysis_sym}: "
                            f"entry={ai_review['entry']:.4f} tp1={ai_review['tp1']:.4f} sl={ai_review['sl']:.4f}"
                        )
                    # Apply score adjustment
                    if ai_review and ai_review.get("score_adj", 0) != 0:
                        raw_master = max(0, min(100, raw_master + ai_review["score_adj"]))
                except Exception as _ds_e:
                    log.warning(f"DeepSeek review error {analysis_sym}: {_ds_e}")

            # Guard: pastikan TP/SL di sisi benar (mis. setelah override AI)
            # sebelum sinyal dikirim — cegah sinyal malformed sampai ke user.
            trade = _sanitize_trade_levels(trade, _signal_direction)

            confidence_label = ("HIGH" if raw_master >= 85 else
                                "MEDIUM" if raw_master >= 75 else "LOW")
            msg = _build_gated_signal_message(
                symbol=analysis_sym, price=price,
                direction=_signal_direction,
                master_score=raw_master, confidence=confidence_label,
                trade=trade, oi_data=oi, confluence=confluence,
                mf_reasons=mf_all_reasons, gate_reasons=gate_reasons,
                alert_type="SETUP",
                tf_4h=tf_4h, tf_1h=tf_1h, tf_15m=tf_15m,
            )

            # Append DeepSeek insight ke pesan
            if ai_review and ai_review.get("insight"):
                _verdict_emoji = {"CONFIRM": "✅", "CAUTION": "⚠️", "SKIP": "🚫"}.get(
                    ai_review.get("ai_verdict", "CONFIRM"), "🤖")
                msg += (
                    f"\n\n─── 🤖 DeepSeek AI ───\n"
                    f"{_verdict_emoji} <b>{ai_review.get('ai_verdict','CONFIRM')}</b>\n"
                    f"{_sanitize_ai_output(ai_review['insight'])}"
                )
                if ai_review.get("was_adjusted"):
                    msg += "\n🔧 <i>Level harga disesuaikan oleh AI</i>"
            elif GROQ_API_KEY and not DEEPSEEK_MODULE:
                # Fallback ke Groq kalau DeepSeek tidak tersedia
                try:
                    ai_txt = groq_signal_insight(
                        symbol=analysis_sym, direction=_signal_direction,
                        master_score=raw_master, gate_reasons=gate_reasons,
                        trade=trade, tf_4h=tf_4h, tf_1h=tf_1h, tf_15m=tf_15m,
                        oi_data=oi,
                    )
                    if ai_txt:
                        msg += f"\n\n─── AI INSIGHT ───\n🤖 {_sanitize_ai_output(ai_txt)}"
                except Exception as _ai_e:
                    log.debug(f"Groq fallback error: {_ai_e}")

            # Dedup: jangan kirim symbol yang sama dua kali dalam satu scan
            if analysis_sym in _scan_sent_syms:
                log.info(f"⏭️ Dedup skip {analysis_sym} — sudah dikirim di scan ini")
                continue
            _scan_sent_syms.add(analysis_sym)

            send_signal(msg)
            _gate_mark_sent(analysis_sym, state, direction=_signal_direction)
            signals_sent += 1
            log.info(f"🚀 SIGNAL SENT: {analysis_sym} {_signal_direction} score={raw_master}")

            # Notif ringkas entry/TP/SL ke topic Market Update (#1)
            try:
                _mu_emoji  = "🟢" if _signal_direction == "LONG" else "🔴"
                _mu_action = _entry_action_reco(_signal_direction, confluence,
                                                realtime_momentum,
                                                entry_mode=trade.get("entry_mode"))
                _mu_prof   = trade.get("tp_profile", "")
                _mu_entry  = trade.get("entry") or price
                # TP bertahap (ladder)
                _mu_ladder = trade.get("tps") or []
                if _mu_ladder:
                    _mu_n = len(_mu_ladder)
                    _mu_tp_lines = "\n".join(
                        f"🟡 TP{_rg.get('level')}   : {_fmt_price(_rg.get('price'))}  "
                        f"({_rg.get('r', 0)}R, {_rg.get('pct', 0):+.1f}%)"
                        + (" ← runner" if _rg.get('level') == _mu_n else "")
                        for _rg in _mu_ladder
                    )
                else:
                    _mu_tp_lines = (
                        f"🟡 TP1   : {_fmt_price(trade.get('tp1'))}\n"
                        f"🟢 TP2   : {_fmt_price(trade.get('tp2'))}"
                    )
                _mu_msg = (
                    f"📡 <b>SINYAL BARU — {analysis_sym.replace('USDT','')}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{_mu_emoji} {_signal_direction} | Score {raw_master}/100"
                    + (f" | TP plan: {_mu_prof}\n" if _mu_prof else "\n")
                    + f"💰 Harga : <b>{_fmt_price(price)}</b>\n"
                    f"🎯 Entry : {_fmt_price(_mu_entry)}\n"
                    f"{_mu_tp_lines}\n"
                    f"🔴 SL    : {_fmt_price(trade.get('sl'))}\n\n"
                    f"📌 {_mu_action}\n"
                    f"⚠️ <i>Not financial advice. DYOR.</i>"
                )
                send_market_update(_mu_msg)
            except Exception as _mu_e:
                log.debug(f"Market update notif error {analysis_sym}: {_mu_e}")

            # Track ke signal tracker
            if TRACKER_MODULE:
                try:
                    _bt_dir    = _signal_direction
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
                    # Rejection candle check: fetch latest 15M candles to look for
                    # pin bar / engulfing AT the zone before firing ENTRY_NOW.
                    # This prevents entry on first touch without reversal confirmation.
                    rejection_ok = True
                    rejection_note = ""
                    try:
                        fresh_15m = get_binance_klines(entry["symbol"], "15m", limit=6)
                        if fresh_15m and len(fresh_15m) >= 3:
                            rej = detect_candle_rejection(fresh_15m)
                            dir_ = entry.get("direction", "LONG")
                            if dir_ in ("LONG", "PUMP"):
                                # Need BULLISH_REJECTION (lower wick / hammer) at support
                                if rej["type"] == "BULLISH_REJECTION" and rej["strength"] >= 40:
                                    rejection_note = f"✅ Rejection candle: {rej['detail']}"
                                elif rej["type"] == "BEARISH_REJECTION":
                                    # Bearish wick at support = sellers still strong, skip
                                    rejection_ok = False
                                    rejection_note = f"⏳ Bearish wick di zona — tunggu konfirmasi"
                                else:
                                    rejection_note = "  No rejection candle yet — watching"
                            else:
                                # SHORT: need BEARISH_REJECTION at resistance
                                if rej["type"] == "BEARISH_REJECTION" and rej["strength"] >= 40:
                                    rejection_note = f"✅ Rejection candle: {rej['detail']}"
                                elif rej["type"] == "BULLISH_REJECTION":
                                    rejection_ok = False
                                    rejection_note = f"⏳ Bullish wick di zona — tunggu konfirmasi"
                                else:
                                    rejection_note = "  No rejection candle yet — watching"
                    except Exception:
                        pass  # If candle fetch fails, proceed with entry anyway

                    if not rejection_ok:
                        log.info(f"⏳ Retest zone entry but no rejection candle: {entry['symbol']} — {rejection_note}")
                        new_queue.append(entry)
                        continue

                    # PRICE IN ZONE + REJECTION OK → send ENTRY_NOW notification
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
                    gate_reasons_entry = [
                        f"Price {cur_price:.6f} masuk zona {_fmt_zone(entry['zone']['bottom'], entry['zone']['top'])}",
                    ]
                    if rejection_note:
                        gate_reasons_entry.append(rejection_note)
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
                        gate_reasons = gate_reasons_entry,
                        alert_type   = "ENTRY_NOW",
                    )
                    send_signal(msg2)

                    # Kirim notif ringkas ke topic Market Update
                    _dir_emoji = "🟢" if entry["direction"] == "LONG" else "🔴"
                    _mu_msg = (
                        f"🎯 <b>MASUK AREA ENTRY — {entry['symbol'].replace('USDT','')}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{_dir_emoji} {entry['direction']} | Score: {entry['score']}\n"
                        f"💰 Harga   : <b>${cur_price:.4f}</b>\n"
                        f"🎯 Entry   : ${entry['entry']:.4f}\n"
                        f"🟡 TP1     : ${entry['tp1']:.4f}\n"
                        f"🔴 SL      : ${entry['sl']:.4f}\n"
                    )
                    if rejection_note:
                        _mu_msg += f"{rejection_note}\n"
                    _mu_msg += "\n⚠️ <i>Not financial advice. DYOR.</i>"
                    send_market_update(_mu_msg)

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
        send_signal(msg)
        state[_LAST_HB_KEY] = now.isoformat()
        log.info("💓 Heartbeat sent")

def run_scan(manual: bool = False, chat_id: str = None):
    log.info("=" * 50)
    log.info(f"🚀 Starting scan... (manual={manual})")

    # ── v12: Cek outcome sinyal sebelumnya sebelum scan ──
    if TRACKER_MODULE:
        try:
            resolved = on_scan_start(send_market_update)
            if resolved:
                log.info(f"📊 Signal tracker: {len(resolved)} signals resolved")
        except Exception as e:
            log.warning(f"Signal tracker on_scan_start error: {e}")

    # ── Manual Trade Manager: monitor posisi aktif ────
    if TRADE_MANAGER_MODULE:
        try:
            alerts = check_active_trades(send_market_update)
            if alerts:
                log.info(f"📈 Trade manager: {len(alerts)} posisi memicu alert (notify-only)")
        except Exception as e:
            log.warning(f"Trade manager check error: {e}")

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
        _register_fn = signal_chat.register_signal_message if SIGNAL_CHAT_MODULE else None
        _personalize_fn = signal_chat.build_signal_personalization if SIGNAL_CHAT_MODULE else None
        threading.Thread(
            target=run_confirmed_signal_scan,
            args=(enriched_coins, _confirmed_send, tracker_fn, _register_fn, _personalize_fn),
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

    _pp_thr = _eff_prepump_threshold()
    hot = [c for c in candidates if c["total_score"] >= _pp_thr]

    if hot:
        # v15: DeepSeek review sebelum kirim
        hot = _deepseek_enrich_candidates(hot, "PREPUMP")
        if not hot:
            log.info("Pre-pump: semua kandidat di-SKIP oleh DeepSeek AI")
            return
        msg = build_prepump_message(hot)
        send_signal(msg)

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
        log.info(f"🔥 Pre-pump HOT alert: {len(hot)} kandidat (score >= {_pp_thr})")
    else:
        best = candidates[0]["total_score"] if candidates else 0
        log.info(f"Pre-pump scan: no HOT signal (best score={best}, threshold={_pp_thr})")


def run_predump_auto():
    """
    Auto pre-dump scan tiap 5 menit.
    Kirim alert HANYA kalau ada kandidat dengan score >= PREDUMP_ALERT_THRESHOLD (HOT).
    """
    log.info("💀 Auto pre-dump scan triggered")
    candidates = scan_predump_candidates()

    _pd_thr = _eff_predump_threshold()
    hot = [c for c in candidates if c["total_score"] >= _pd_thr]

    if hot:
        # v15: DeepSeek review sebelum kirim
        hot = _deepseek_enrich_candidates(hot, "PREDUMP")
        if not hot:
            log.info("Pre-dump: semua kandidat di-SKIP oleh DeepSeek AI")
            return
        msg = build_predump_message(hot)
        send_signal(msg)

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
        log.info(f"🔴 Pre-dump HOT alert: {len(hot)} kandidat (score >= {_pd_thr})")
    else:
        best = candidates[0]["total_score"] if candidates else 0
        log.info(f"Pre-dump scan: no HOT signal (best score={best}, threshold={_pd_thr})")


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
        send_signal(msg)

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
# v16: REVERSAL ENGINE — V-Shape + Quasimodo (2-stage early/confirm)
# ─────────────────────────────────────────────
_REVERSAL_STAGE_RANK = {"EARLY": 1, "CONFIRM": 2, "IGNITION": 3}


def _load_reversal_state() -> dict:
    return secure_load(REVERSAL_STATE_FILE, default={"sent": {}})


def _save_reversal_state(state: dict):
    if not secure_save(REVERSAL_STATE_FILE, state):
        log.warning("reversal_state save error")


def _reversal_ignition_check(symbol: str, direction: str, entry_ref: float | None) -> dict:
    """Overlay LIVE di TF mikro (5M) — deteksi 'detik-detik mau pump/dump'.
    Ignition = candle terkini punya range & volume jauh di atas rata-rata,
    searah pola, dan harga masih dekat level pola (entry_ref)."""
    out = {"ignited": False, "range_mult": 0.0, "vol_mult": 0.0, "reasons": []}
    if not IGNITION_ENABLED:
        return out
    try:
        kl = get_binance_klines(symbol, IGNITION_TF, limit=30)
    except Exception:
        kl = None
    if not kl or len(kl) < 12:
        return out

    closed = kl[:-1]
    last = kl[-1]   # candle live ("sekarang") — overlay ini memang real-time
    base = closed[-20:] if len(closed) >= 20 else closed
    ranges = [c["high"] - c["low"] for c in base]
    vols = [c.get("volume", 0) or 0 for c in base]
    avg_range = (sum(ranges) / len(ranges)) if ranges else 0.0
    avg_vol = (sum(vols) / len(vols)) if vols else 0.0

    rng = last["high"] - last["low"]
    vol = last.get("volume", 0) or 0
    range_mult = (rng / avg_range) if avg_range > 0 else 0.0
    vol_mult = (vol / avg_vol) if avg_vol > 0 else 0.0
    bullish = last["close"] >= last["open"]
    dir_ok = (direction == "LONG" and bullish) or (direction == "SHORT" and not bullish)
    near = True
    if entry_ref:
        near = abs(last["close"] - entry_ref) / entry_ref * 100 <= IGNITION_NEAR_ZONE_PCT

    ignited = (range_mult >= IGNITION_RANGE_MULT and vol_mult >= IGNITION_VOL_MULT
               and dir_ok and near)
    out.update({"ignited": ignited, "range_mult": round(range_mult, 2), "vol_mult": round(vol_mult, 2)})
    if ignited:
        out["reasons"].append(
            f"🚀 5M ignition: range {range_mult:.1f}x · vol {vol_mult:.1f}x — "
            f"{'pump' if direction == 'LONG' else 'dump'} mulai bergerak"
        )
    return out


def scan_reversal_candidates(symbols: list = None) -> list:
    """Scan V-Shape & QM di TF 1H. Return list kandidat sorted by score DESC."""
    if symbols is None:
        symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
            "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
            "NEARUSDT", "APTUSDT", "INJUSDT", "SUIUSDT", "ARBUSDT",
            "OPUSDT", "TIAUSDT", "RENDERUSDT", "FETUSDT", "PENDLEUSDT",
            "ENAUSDT", "AAVEUSDT", "ONDOUSDT", "JUPUSDT", "HYPEUSDT",
        ]

    candidates = []
    log.info(f"🔄 Reversal scan: {len(symbols)} symbols...")

    for sym in symbols:
        try:
            tf_1h = analyze_timeframe(sym, "1h")
            if tf_1h.get("error"):
                continue

            if SYMBOL_MEMORY_MODULE:
                is_bl, bl_r = is_blacklisted(sym)
                if is_bl:
                    log.info(f"⛔ {sym} blacklisted, skip reversal scan: {bl_r}")
                    continue

            vs = tf_1h.get("v_shape", {}) or {}
            qm = tf_1h.get("qm_pattern", {}) or {}
            picks = [p for p in (vs, qm)
                     if p.get("type", "NONE") != "NONE" and p.get("stage", "NONE") != "NONE"]
            if not picks:
                continue
            best = max(picks, key=lambda p: p.get("score", 0))
            if best.get("score", 0) < REVERSAL_EARLY_MIN_SCORE:
                continue
            direction = best.get("direction", "NONE")
            if direction not in ("LONG", "SHORT"):
                continue

            tf_4h = analyze_timeframe(sym, "4h")
            tf_15m = analyze_timeframe(sym, "15m")
            oi = get_open_interest(sym)
            price = tf_1h.get("price", 0)
            atr_1h = tf_1h.get("atr", 0)
            trade = calculate_trade_plan(
                price, "PUMP" if direction == "LONG" else "DUMP",
                atr_1h, tf_4h, tf_1h, tf_15m, oi,
            )

            cand = {
                "symbol": sym, "price": price,
                "type": best["type"], "direction": direction,
                "stage": best.get("stage", "EARLY"),
                "score": best.get("score", 0), "total_score": best.get("score", 0),
                "label": _REVERSAL_PATTERN_NAME.get(best["type"], best["type"]),
                "reasons": list(best.get("reasons", [])),
                "entry_ref": best.get("entry_ref"),
                "invalidation": best.get("invalidation"),
                "zone": best.get("zone"),
                "trade": trade, "oi_data": oi,
                "ignition": {"ignited": False},
            }

            # Momentum ignition overlay (live 5M) — upgrade ke IGNITION jika meledak
            if IGNITION_ENABLED:
                ig = _reversal_ignition_check(sym, direction, best.get("entry_ref"))
                cand["ignition"] = ig
                if ig.get("ignited"):
                    cand["stage"] = "IGNITION"
                    cand["score"] = min(100, cand["score"] + 6)
                    cand["total_score"] = cand["score"]
                    cand["reasons"] = list(ig.get("reasons", [])) + cand["reasons"]

            candidates.append(cand)
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"Reversal scan error {sym}: {e}")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:10]


def run_reversal_auto():
    """Auto reversal scan tiap REVERSAL_SCAN_INTERVAL menit (2-tahap).
    EARLY heads-up = pure (tanpa AI gate, biar cepat).
    CONFIRM/IGNITION = lewat DeepSeek review + di-track ke signal tracker."""
    if not REVERSAL_SCAN_ENABLED:
        return
    log.info("🔄 Auto reversal (V-Shape/QM) scan triggered")
    candidates = scan_reversal_candidates()
    if not candidates:
        log.info("Reversal scan: no pattern detected")
        return

    state = _load_reversal_state()
    sent = state.get("sent", {})
    now = time.time()

    to_send = []
    for c in candidates:
        rank = _REVERSAL_STAGE_RANK.get(c.get("stage", "EARLY"), 0)
        min_score = REVERSAL_CONFIRM_MIN_SCORE if rank >= 2 else REVERSAL_EARLY_MIN_SCORE
        if c.get("score", 0) < min_score:
            continue
        key = f"{c['symbol']}:{c['type']}:{c['direction']}"
        c["_key"] = key
        prev = sent.get(key)
        if prev:
            prev_rank = _REVERSAL_STAGE_RANK.get(prev.get("stage"), 0)
            age_h = (now - prev.get("ts", 0)) / 3600
            if rank > prev_rank:
                pass  # upgrade stage → tetap kirim
            elif age_h < REVERSAL_COOLDOWN_HOURS:
                continue  # stage sama/turun & masih cooldown → skip
        to_send.append(c)

    if not to_send:
        log.info(f"Reversal scan: {len(candidates)} kandidat, semua di-skip (cooldown/threshold)")
        return

    # EARLY = heads-up murni; CONFIRM/IGNITION = lewat DeepSeek review
    early_only = [c for c in to_send if _REVERSAL_STAGE_RANK.get(c.get("stage"), 0) < 2]
    ai_targets = [c for c in to_send if _REVERSAL_STAGE_RANK.get(c.get("stage"), 0) >= 2]
    longs = [c for c in ai_targets if c["direction"] == "LONG"]
    shorts = [c for c in ai_targets if c["direction"] == "SHORT"]
    enriched_conf = (_deepseek_enrich_candidates(longs, "PREPUMP")
                     + _deepseek_enrich_candidates(shorts, "PREDUMP"))
    final = early_only + enriched_conf
    if not final:
        log.info("Reversal: semua kandidat CONFIRM di-SKIP oleh DeepSeek AI")
        return
    final.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Kirim DETAIL ke Signal thread + SINGKAT ke Market Update thread
    send_signal(build_reversal_message(final))
    brief = build_reversal_mu_brief(final)
    if brief:
        send_market_update(brief)

    # Update state
    for c in final:
        key = c.get("_key") or f"{c['symbol']}:{c['type']}:{c['direction']}"
        sent[key] = {"stage": c.get("stage", "EARLY"), "ts": now, "score": c.get("score", 0)}

    # Track HANYA CONFIRM/IGNITION (entry nyata) ke signal tracker
    if TRACKER_MODULE:
        for c in final:
            if _REVERSAL_STAGE_RANK.get(c.get("stage"), 0) < 2:
                continue
            try:
                trade = c.get("trade", {})
                tp = trade.get("tp1") or trade.get("tp") or 0
                sl = trade.get("sl") or 0
                entry_val = float(trade.get("entry") or c.get("price", 0))
                bt_dir = c["direction"]
                sane = (bt_dir == "LONG" and float(tp) > entry_val) or \
                       (bt_dir == "SHORT" and 0 < float(tp) < entry_val)
                if tp and sl and entry_val and sane:
                    on_signal_sent(
                        symbol=c["symbol"], signal_type="REVERSAL", direction=bt_dir,
                        entry_price=entry_val, tp=float(tp), sl=float(sl),
                        score=c.get("score", 0), confluence_level=c.get("label", ""),
                        reasons=c.get("reasons", [])[:3], strategy="REVERSAL",
                    )
                else:
                    log.warning(f"⚠️ REVERSAL sanity fail {c.get('symbol','')}: entry={entry_val} tp={tp}")
            except Exception as e:
                log.debug(f"Reversal tracker error {c.get('symbol','')}: {e}")

    # Learning log
    if LEARNING_MODULE:
        for c in final:
            try:
                log_decision(actor="REVERSAL", symbol=c["symbol"], decision="ALERT",
                    summary=f"{c.get('type','')} {c.get('stage','')} score={c.get('score',0)}",
                    score=c.get("score", 0), confluence_level=c.get("label", ""),
                    direction=c["direction"], reasons=c.get("reasons", [])[:3])
            except Exception as e:
                log.debug(f"reversal log_decision error: {e}")

    # Prune state > 24 jam
    state["sent"] = {k: v for k, v in sent.items() if (now - v.get("ts", 0)) < 86400}
    _save_reversal_state(state)
    log.info(f"🔄 Reversal alert: {len(final)} kandidat dikirim "
             f"({len(early_only)} EARLY, {len(enriched_conf)} CONFIRM/IGNITION)")


# ─────────────────────────────────────────────
# v16: MARKET PULSE — status semua koin (continuing pump/dump/reversal/ranging)
# ─────────────────────────────────────────────
def _pulse_retest_zone(tf: dict, direction: str):
    """Cari zona retest terdekat searah tren (OB/FVG). Return (price, source)."""
    ob = tf.get("order_blocks", {}) or {}
    fvg = tf.get("fvg", {}) or {}
    cands = []
    if direction == "PUMP":
        if ob.get("bullish_ob"):
            o = ob["bullish_ob"]
            cands.append((o.get("mid") or o.get("bottom"), "OB", abs(o.get("distance_pct", 99))))
        if fvg.get("bullish_fvg"):
            f = fvg["bullish_fvg"]
            cands.append((f.get("mid"), "FVG", abs(f.get("distance_pct", 99))))
    else:
        if ob.get("bearish_ob"):
            o = ob["bearish_ob"]
            cands.append((o.get("mid") or o.get("top"), "OB", abs(o.get("distance_pct", 99))))
        if fvg.get("bearish_fvg"):
            f = fvg["bearish_fvg"]
            cands.append((f.get("mid"), "FVG", abs(f.get("distance_pct", 99))))
    cands = [c for c in cands if c[0]]
    if not cands:
        return None, None
    cands.sort(key=lambda x: x[2])
    return cands[0][0], cands[0][1]


def _classify_market_state(tf_1h: dict, tf_4h: dict) -> dict:
    """Klasifikasi status koin: PUMP | DUMP | REVERSAL | RANGING (basis 1H, bias 4H)."""
    st1 = tf_1h.get("structure", {}) or {}
    reg1 = tf_1h.get("market_regime", {}) or {}
    trend1 = st1.get("trend", "NEUTRAL")
    trend4 = (tf_4h.get("structure", {}) or {}).get("trend", "NEUTRAL")
    adx1 = tf_1h.get("adx", 0) or reg1.get("adx", 0)
    regime = reg1.get("regime", "UNKNOWN")

    vs = tf_1h.get("v_shape", {}) or {}
    qm = tf_1h.get("qm_pattern", {}) or {}
    rev = None
    for p in (qm, vs):
        if p.get("type", "NONE") != "NONE" and p.get("stage", "NONE") != "NONE" \
                and p.get("score", 0) >= REVERSAL_EARLY_MIN_SCORE:
            rev = p
            break

    bull = trend1 == "BULLISH" or regime in ("BREAKOUT_UP", "BULLISH_TREND")
    bear = trend1 == "BEARISH" or regime in ("BREAKOUT_DOWN", "BEARISH_TREND")

    if rev:
        state = "REVERSAL"
    elif bull and adx1 >= MARKET_PULSE_ADX_MIN:
        state = "PUMP"
    elif bear and adx1 >= MARKET_PULSE_ADX_MIN:
        state = "DUMP"
    else:
        state = "RANGING"

    return {
        "state": state, "adx": round(adx1, 0), "regime": regime,
        "trend_1h": trend1, "trend_4h": trend4, "rev": rev,
        "bos": st1.get("bos", False), "choch": st1.get("choch", False),
    }


def scan_market_pulse(symbols: list = None) -> list:
    """Scan status SEMUA koin untuk Market Pulse. Lightweight (1H+4H, no OI)."""
    if symbols is None:
        symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
            "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
            "NEARUSDT", "APTUSDT", "INJUSDT", "SUIUSDT", "ARBUSDT",
            "OPUSDT", "TIAUSDT", "RENDERUSDT", "FETUSDT", "PENDLEUSDT",
            "ENAUSDT", "AAVEUSDT", "ONDOUSDT", "JUPUSDT", "HYPEUSDT",
        ]
    # Whale tracker (opsional) — bias wallet per koin, guarded
    _wt_mod = None
    try:
        import whale_tracker as _wt_mod
    except Exception:
        _wt_mod = None

    results = []
    log.info(f"🌐 Market pulse: {len(symbols)} symbols...")
    for sym in symbols:
        try:
            tf_1h = analyze_timeframe(sym, "1h")
            if tf_1h.get("error"):
                continue
            tf_4h = analyze_timeframe(sym, "4h")
            cl = _classify_market_state(tf_1h, tf_4h)
            price = tf_1h.get("price", 0)

            # Money flow (1H) — CVD + MFI, zero extra API call
            mf = tf_1h.get("money_flow", {}) or {}
            # Whale wallet bias (opsional)
            whale_bias = "NEUTRAL"
            if _wt_mod is not None:
                try:
                    _wctx = _wt_mod.get_whale_context_for_coin(
                        sym.replace("USDT", "").replace("UST", ""))
                    whale_bias = _wctx.get("whale_bias", "NEUTRAL")
                except Exception:
                    whale_bias = "NEUTRAL"

            entry = {
                "symbol": sym, "price": price, "state": cl["state"],
                "adx": cl["adx"], "regime": cl["regime"],
                "trend_4h": cl["trend_4h"], "rev": cl["rev"],
                "retest": None, "retest_src": None,
                "mf_bias": mf.get("bias", "NEUTRAL"),
                "cvd_pct": mf.get("cvd_pct", 0.0),
                "mfi": mf.get("mfi", 50),
                "whale_bias": whale_bias,
            }
            if cl["state"] in ("PUMP", "DUMP"):
                rz, rs = _pulse_retest_zone(tf_1h, cl["state"])
                entry["retest"], entry["retest_src"] = rz, rs
            results.append(entry)
            time.sleep(0.25)
        except Exception as e:
            log.warning(f"Market pulse error {sym}: {e}")
    return results


def _pulse_flow_tag(r: dict) -> str:
    """Baris kecil money-flow + whale untuk satu koin di Market Pulse."""
    mf_emoji = {"INFLOW": "💚", "OUTFLOW": "🔴", "NEUTRAL": "⚪"}.get(r.get("mf_bias", "NEUTRAL"), "⚪")
    mfi = r.get("mfi", 50)
    parts = [f"💧 MF {mf_emoji} CVD {r.get('cvd_pct', 0):+.1f}% · MFI {mfi:.0f}"]
    wb = r.get("whale_bias", "NEUTRAL")
    if wb != "NEUTRAL":
        wh_emoji = {"BULLISH": "🟢", "BEARISH": "🔴"}.get(wb, "⚪")
        parts.append(f"🐳 {wh_emoji} {wb}")
    return "  ·  ".join(parts)


def build_market_pulse_message(results: list) -> str:
    """Digest status semua koin → Market Update thread."""
    ts = datetime.now(_WIB).strftime("%d %b %Y %H:%M WIB")
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "🌐 <b>MARKET PULSE</b> — status semua koin",
             f"🕐 {ts}  ·  <i>TF 1H (bias 4H)</i>", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    if not results:
        lines.append("❄️ Data tidak tersedia saat ini.")
        return "\n".join(lines)

    pumps = [r for r in results if r["state"] == "PUMP"]
    dumps = [r for r in results if r["state"] == "DUMP"]
    revs = [r for r in results if r["state"] == "REVERSAL"]
    rang = [r for r in results if r["state"] == "RANGING"]

    def _sym(r):
        return r["symbol"].replace("USDT", "")

    if pumps:
        lines.append("\n🟢 <b>CONTINUING PUMP</b> — wait for retest:")
        for r in sorted(pumps, key=lambda x: -x["adx"])[:10]:
            rt = f" · retest {fmt_num(r['retest'])} ({r['retest_src']})" if r.get("retest") else ""
            lines.append(f"  ▲ <b>{_sym(r)}</b> {fmt_num(r['price'])} · ADX {r['adx']:.0f}{rt}")
            lines.append(f"      {_pulse_flow_tag(r)}")
    if dumps:
        lines.append("\n🔴 <b>CONTINUING DUMP</b> — wait for retest:")
        for r in sorted(dumps, key=lambda x: -x["adx"])[:10]:
            rt = f" · retest {fmt_num(r['retest'])} ({r['retest_src']})" if r.get("retest") else ""
            lines.append(f"  ▼ <b>{_sym(r)}</b> {fmt_num(r['price'])} · ADX {r['adx']:.0f}{rt}")
            lines.append(f"      {_pulse_flow_tag(r)}")
    if revs:
        lines.append("\n🔄 <b>REVERSAL FORMING</b>:")
        for r in revs[:10]:
            rv = r["rev"] or {}
            ptag = "V-Shape" if rv.get("type", "").startswith("V_SHAPE") else "QM"
            dtag = "🟢 LONG" if rv.get("direction") == "LONG" else "🔴 SHORT"
            lines.append(f"  ⟳ <b>{_sym(r)}</b> {dtag} · {ptag} · {rv.get('stage','')} · {fmt_num(r['price'])}")
            lines.append(f"      {_pulse_flow_tag(r)}")
    if rang:
        lines.append("\n⚪ <b>RANGING / NEUTRAL</b>:")
        lines.append("  " + ", ".join(_sym(r) for r in rang[:25]))

    lines.append("\n⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def run_market_pulse():
    """Broadcast Market Pulse (status semua koin) ke Market Update thread."""
    if not MARKET_PULSE_ENABLED:
        return
    log.info("🌐 Market pulse broadcast triggered")
    results = scan_market_pulse()
    if not results:
        log.info("Market pulse: no data")
        return
    msg = build_market_pulse_message(results)
    send_market_update(msg)
    n_pump = sum(1 for r in results if r["state"] == "PUMP")
    n_dump = sum(1 for r in results if r["state"] == "DUMP")
    n_rev = sum(1 for r in results if r["state"] == "REVERSAL")
    log.info(f"🌐 Market pulse sent: {len(results)} koin "
             f"({n_pump} pump, {n_dump} dump, {n_rev} reversal)")


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
    log.info(f"Reversal  : {'✅ V-Shape+QM scan tiap '+str(REVERSAL_SCAN_INTERVAL)+'m (2-tahap early/confirm)' if (REVERSAL_SCAN_ENABLED and REVERSAL_MODULE) else '⚠️ disabled'}")
    log.info(f"MarketPulse: {'✅ status semua koin tiap '+str(MARKET_PULSE_INTERVAL)+'m → Market Update' if MARKET_PULSE_ENABLED else '⚠️ disabled'}")
    log.info(f"Top coins : {TOP_COINS_COUNT}")
    log.info(f"Gemini    : {'✅ Key set' if GEMINI_API_KEY else '⚠️ No key'} (manual: /analyze /ask /chart /news /macro)")
    log.info(f"NewsAPI   : {'✅ Key set' if NEWSAPI_KEY else '⚠️ No key — /news disabled'}")
    log.info(f"Risk Mgr  : {'✅ Module loaded' if RISK_MODULE else '⚠️ Module missing'}")
    log.info(f"Learning  : {'✅ Module loaded' if LEARNING_MODULE else '⚠️ Module missing — /logoutcome /lessons disabled'}")
    log.info(f"Journal   : {'✅ Module loaded' if JOURNAL_MODULE else '⚠️ Module missing — /logtrade /weeksummary disabled'}")
    log.info(f"Backtest  : {'✅ Module loaded — /backtest /btresult /btcompare /btstats' if BACKTEST_MODULE else '⚠️ Module missing — /backtest disabled'}")
    log.info(f"Tracker   : {'✅ Module loaded — auto signal tracking aktif' if TRACKER_MODULE else '⚠️ Module missing — signal tracking disabled'}")
    log.info(f"Confirmed : {'✅ Module loaded — confirmed entry signal aktif (auto tiap scan)' if CONFIRMED_MODULE else '⚠️ Module missing — confirmed signal disabled'}")
    if X_MODULE:
        _x_src = get_x_source_status()
        log.info(f"X/Twitter : ✅ Module loaded — {_x_src} (/dca /xsenti)")
    else:
        log.warning("⚠️ x_sentiment.py tidak ditemukan — /dca /xsenti disabled")

    # Start Telegram polling
    poll_thread = threading.Thread(target=polling_loop, daemon=True)
    poll_thread.start()
    log.info("📡 Telegram chat handler: RUNNING")

    # ── Startup Telegram notification ────────────
    _regime_status  = "✅" if MARKET_REGIME_MODULE else "⚠️ missing"
    _risk_status    = "✅" if RISK_MODULE else "⚠️"
    _news_status    = "✅" if NEWS_MODULE else "⚠️"
    _liq_status_str = "✅" if LIQ_TRACKER_MODULE else "⚠️"
    _mem_status     = "✅" if SYMBOL_MEMORY_MODULE else "⚠️"
    _startup_msg = (
        "🤖 <b>CRYPTO BOT v14 — ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🆕 <b>Fitur Baru v14:</b>\n"
        "🔵 <b>Market Regime Classifier</b> — Bot sekarang bisa bedain:\n"
        "   RANGING / BULLISH_TREND / BEARISH_TREND\n"
        "   BB_SQUEEZE / BREAKOUT_UP / BREAKOUT_DOWN\n"
        "   → Sinyal di RANGING = score dipotong 30% (filter fakeout)\n\n"
        "🕯️ <b>Full Candle Structure</b> — Bukan cuma pin bar:\n"
        "   Engulfing, Morning/Evening Star, Marubozu,\n"
        "   Three Soldiers/Crows, Inside Bar, Doji\n\n"
        "🌊 <b>Pre-pump Spring Detector</b> — Tangkap pump tiba-tiba:\n"
        "   BB Squeeze + Volume Coil + Sudden Breakout\n"
        "   (ALLO-type pump sekarang ketangkep)\n\n"
        "🎯 <b>Sniper Entry Mode</b> — 3 mode entry:\n"
        "   MOMENTUM_NOW | RETEST_WAIT | SNIPER_ENTRY\n"
        "   → Price extended >2.5% dari zone = warning otomatis\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Modules: Regime {_regime_status} | Risk {_risk_status} | "
        f"News {_news_status} | Liq {_liq_status_str} | Memory {_mem_status}\n"
        f"⏱ Auto scan: tiap {SCAN_INTERVAL_MINUTES}m | Pre-pump/dump: tiap {PREPUMP_SCAN_INTERVAL}m\n\n"
        "Ketik /help untuk list command lengkap."
    )
    try:
        send_telegram(_startup_msg)
    except Exception as _e:
        log.warning(f"Startup Telegram notification failed: {_e}")

    # Liquidation Cascade Tracker
    if LIQ_TRACKER_MODULE:
        start_liq_tracker()
        log.info("⚡ Liquidation Cascade Tracker: RUNNING (wss://fstream.binance.com)")
    else:
        log.warning("⚠️ liquidation_tracker.py tidak ditemukan — liq cascade detection disabled")

    # Whale Tracker (opsional)
    try:
        import whale_tracker
        whale_tracker.init(telegram_fn=send_market_update)
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

    # v16: Reversal engine (V-Shape + QM) — 2-tahap early/confirm
    if REVERSAL_SCAN_ENABLED:
        scheduler.add_job(run_reversal_auto, "interval", minutes=REVERSAL_SCAN_INTERVAL,
                          id="reversal_scan", jitter=30)
        log.info(f"🔄 Reversal engine aktif: V-Shape + QM scan tiap {REVERSAL_SCAN_INTERVAL}m (2-tahap)")

    # v16: Market Pulse — status semua koin → Market Update thread
    if MARKET_PULSE_ENABLED:
        scheduler.add_job(run_market_pulse, "interval", minutes=MARKET_PULSE_INTERVAL,
                          id="market_pulse", jitter=60)
        threading.Thread(target=run_market_pulse, daemon=True, name="market_pulse_startup").start()
        log.info(f"🌐 Market Pulse aktif: status semua koin tiap {MARKET_PULSE_INTERVAL}m (+ startup)")

    # Risk daily reset jam 00:00 UTC
    if RISK_MODULE:
        scheduler.add_job(risk_reset_daily, "cron", hour=0, minute=0, id="risk_daily_reset")

    # Auto-btall: refresh cache backtest harian (jam 01:00 UTC) + sekali saat start.
    # Gate 3 pakai cache ini supaya tidak live-backtest tiap signal.
    if BACKTEST_MODULE:
        scheduler.add_job(run_btall_scheduled, "cron", hour=1, minute=0, id="auto_btall")
        threading.Thread(target=run_btall_scheduled, daemon=True).start()

    # Daily learning: analyze signal outcomes dan call DeepSeek for recommendations (jam 23:00 UTC)
    if LEARNING_MODULE:
        scheduler.add_job(
            lambda: analyze_signal_outcomes_daily(send_telegram_fn=send_trade_report),
            "cron", hour=23, minute=0, id="daily_learning"
        )

    # 12-hour lesson snapshot: unrealized P&L semua pending signals → lesson baru
    if TRACKER_MODULE:
        scheduler.add_job(take_lesson_snapshot, "interval", hours=12, id="lesson_snapshot_12h")

    # News Agent: hourly fetch — update news_intelligence.json setiap jam
    # Kirim Telegram alert otomatis (high-urgency events) ke Market Update room
    if NEWS_AGENT_MODULE and NEWSAPI_KEY:
        scheduler.add_job(
            lambda: run_news_fetch(send_telegram_fn=send_market_update),
            "interval", minutes=60, id="news_agent_hourly",
            jitter=120,   # ± 2 menit random offset agar tidak collision
        )
        # Jalankan sekali saat startup (background agar tidak delay bot)
        threading.Thread(
            target=lambda: run_news_fetch(send_telegram_fn=send_market_update),
            daemon=True, name="news_agent_startup",
        ).start()
        log.info("📰 News Agent: hourly fetch aktif (startup + tiap 60 menit)")

    log.info(
        f"⏱️ Schedulers: Scan={SCAN_INTERVAL_MINUTES}m | "
        f"PrePump/Dump/Scalp={PREPUMP_SCAN_INTERVAL}m | "
        f"Reversal={REVERSAL_SCAN_INTERVAL if REVERSAL_SCAN_ENABLED else 'off'}m | "
        f"MarketPulse={MARKET_PULSE_INTERVAL if MARKET_PULSE_ENABLED else 'off'}m | "
        f"News Agent=60m | "
        f"Risk reset=00:00 UTC | Auto-btall=01:00 UTC | "
        f"Daily-learning=23:00 UTC | Lesson-snapshot=tiap 12j"
    )

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
