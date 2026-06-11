"""Tests untuk session_report — laporan majors BTC/ETH/SOL per sesi + shift alert.

Fokus ke logic murni: deteksi sesi, bias per koin, relative strength,
session quality, trade recommendation, dan rendering pesan.
"""
from datetime import datetime, timezone

import session_report as sr


def _coin(name, **over):
    base = {"name": name, "price": 100.0, "chg_pct": 0.0, "trend": "NEUTRAL",
            "regime": "RANGING", "adx": 20, "rsi": 50, "ema21": 100, "ema50": 100,
            "cvd_dir": "FLAT", "cvd_pct": 0.0, "key_sup": 95, "key_res": 105,
            "squeeze": False}
    base.update(over)
    return base


def test_session_just_ended_maps_cron_hours():
    assert sr.session_just_ended(7) == "ASIA"
    assert sr.session_just_ended(15) == "LONDON"
    assert sr.session_just_ended(21) == "NEW YORK"
    assert sr.session_just_ended(10) is None   # bukan jam tutup sesi


def test_next_session_cycle():
    assert sr.next_session_of("ASIA") == "LONDON"
    assert sr.next_session_of("LONDON") == "NEW YORK"
    assert sr.next_session_of("NEW YORK") == "ASIA"


def test_active_session_ranges():
    assert sr.active_session(3) == "ASIA"
    assert sr.active_session(10) == "LONDON"
    assert sr.active_session(18) == "NEW YORK"
    assert sr.active_session(23) == "ASIA"


def test_coin_bias_bullish_and_bearish():
    bull = _coin("BTC", trend="BULLISH", regime="BULLISH_TREND",
                 price=110, ema21=105, ema50=100, rsi=62, cvd_dir="BUY")
    bear = _coin("BTC", trend="BEARISH", regime="BEARISH_TREND",
                 price=90, ema21=95, ema50=100, rsi=38, cvd_dir="SELL")
    flat = _coin("ETH")
    assert sr.coin_bias(bull)[0] == "BULLISH" and sr.coin_bias(bull)[1] > 0
    assert sr.coin_bias(bear)[0] == "BEARISH" and sr.coin_bias(bear)[1] < 0
    assert sr.coin_bias(flat)[0] == "NETRAL"


# ── Relative Strength ─────────────────────────────────────────────────────────

def test_relative_strength_eth_outperforms():
    btc = _coin("BTC", chg_pct=1.0)
    eth = _coin("ETH", chg_pct=2.2)   # +1.2% vs BTC → LEBIH KUAT
    sol = _coin("SOL", chg_pct=0.8)   # -0.2% vs BTC → SETARA
    rs = sr.relative_strength(btc, eth, sol)
    assert rs["ETH"]["label"] == "LEBIH KUAT"
    assert abs(rs["ETH"]["diff"] - 1.2) < 0.01
    assert rs["SOL"]["label"] == "SETARA"


def test_relative_strength_eth_breakout_vs_btc():
    # ETH di BREAKOUT_UP, BTC tidak → JAUH LEBIH KUAT + breakout=True
    btc = _coin("BTC", regime="BULLISH_TREND", chg_pct=0.5)
    eth = _coin("ETH", regime="BREAKOUT_UP",   chg_pct=1.0)
    sol = _coin("SOL", regime="RANGING",        chg_pct=0.3)
    rs = sr.relative_strength(btc, eth, sol)
    assert rs["ETH"]["breakout"] is True
    assert rs["ETH"]["label"] == "JAUH LEBIH KUAT"
    assert rs["SOL"]["breakout"] is False


def test_relative_strength_eth_weaker():
    btc = _coin("BTC", chg_pct=2.0)
    eth = _coin("ETH", chg_pct=0.3)   # -1.7% vs BTC → JAUH LEBIH LEMAH
    rs = sr.relative_strength(btc, eth, None)
    assert rs["ETH"]["label"] == "JAUH LEBIH LEMAH"
    assert "SOL" not in rs


def test_relative_strength_no_breakout_when_btc_also_breakout():
    # ETH BREAKOUT_UP, BTC juga BREAKOUT_UP → bukan rotasi, breakout=False
    btc = _coin("BTC", regime="BREAKOUT_UP", chg_pct=3.0)
    eth = _coin("ETH", regime="BREAKOUT_UP", chg_pct=2.0)
    rs = sr.relative_strength(btc, eth, None)
    assert rs["ETH"]["breakout"] is False


# ── Session Quality ───────────────────────────────────────────────────────────

def test_session_quality_breakout_is_excellent():
    btc = _coin("BTC", regime="BREAKOUT_UP", adx=30)
    q, _ = sr.session_quality(btc)
    assert q == "EXCELLENT"


def test_session_quality_trending_is_good():
    btc = _coin("BTC", regime="BULLISH_TREND", adx=28)
    q, _ = sr.session_quality(btc)
    assert q == "GOOD"


def test_session_quality_choppy_is_poor():
    btc = _coin("BTC", regime="RANGING", adx=12)
    q, _ = sr.session_quality(btc)
    assert q == "POOR"


def test_session_quality_squeeze_is_waspada():
    btc = _coin("BTC", regime="RANGING", adx=18, squeeze=True)
    q, reason = sr.session_quality(btc)
    assert q == "WASPADA"
    assert "Squeeze" in reason


def test_session_quality_ranging_moderate_is_average():
    btc = _coin("BTC", regime="RANGING", adx=21)
    q, _ = sr.session_quality(btc)
    assert q == "AVERAGE"


# ── Trade Recommendation ──────────────────────────────────────────────────────

def test_trade_rec_eth_breakout_prioritized():
    btc = _coin("BTC", trend="BULLISH", regime="BULLISH_TREND", adx=27)
    eth = _coin("ETH", regime="BREAKOUT_UP", chg_pct=3.0)
    sol = _coin("SOL", regime="RANGING",     chg_pct=1.0)
    coins = [btc, eth, sol]
    rs = sr.relative_strength(btc, eth, sol)
    q, _ = sr.session_quality(btc)
    rec = sr.build_trade_rec(coins, rs, q, "NEW YORK")
    assert "ETH" in rec and "breakout" in rec.lower()


def test_trade_rec_both_alts_breakout():
    btc = _coin("BTC", trend="BULLISH", regime="BULLISH_TREND", adx=26)
    eth = _coin("ETH", regime="BREAKOUT_UP", chg_pct=3.5)
    sol = _coin("SOL", regime="BREAKOUT_UP", chg_pct=4.0)
    coins = [btc, eth, sol]
    rs = sr.relative_strength(btc, eth, sol)
    q, _ = sr.session_quality(btc)
    rec = sr.build_trade_rec(coins, rs, q, "NEW YORK")
    assert "ETH" in rec and "SOL" in rec
    assert "altcoin season" in rec.lower() or "altcoin" in rec.lower()


def test_trade_rec_poor_quality_sidelines():
    btc = _coin("BTC", adx=12, regime="RANGING")
    coins = [btc, _coin("ETH"), _coin("SOL")]
    rs = sr.relative_strength(btc, _coin("ETH"), _coin("SOL"))
    rec = sr.build_trade_rec(coins, rs, "POOR", "ASIA")
    assert "Sidelines" in rec or "sidelines" in rec.lower()
    assert "Worth trading: TIDAK" in rec


def test_trade_rec_btc_when_alts_not_stronger():
    btc = _coin("BTC", trend="BULLISH", regime="BULLISH_TREND",
                price=110, ema21=105, ema50=100, rsi=62, cvd_dir="BUY", adx=27)
    eth = _coin("ETH", chg_pct=0.0)
    sol = _coin("SOL", chg_pct=0.2)
    coins = [btc, eth, sol]
    rs = sr.relative_strength(btc, eth, sol)
    q, _ = sr.session_quality(btc)
    rec = sr.build_trade_rec(coins, rs, q, "NEW YORK")
    assert "BTC" in rec


# ── Build functions ───────────────────────────────────────────────────────────

def test_coin_block_shows_squeeze_warning():
    coin = _coin("BTC", squeeze=True, regime="RANGING", adx=19)
    block = sr.build_coin_block(coin)
    assert "Squeeze" in block or "squeeze" in block.lower()


def test_coin_block_no_squeeze_line_when_false():
    coin = _coin("BTC", squeeze=False)
    block = sr.build_coin_block(coin)
    assert "Squeeze" not in block


def test_session_report_contains_all_majors_and_outlook():
    coins = [_coin("BTC", trend="BULLISH", regime="BULLISH_TREND", price=110,
                   ema21=105, ema50=100, rsi=60, cvd_dir="BUY"),
             _coin("ETH"), _coin("SOL", chg_pct=2.5)]
    now = datetime(2026, 6, 11, 7, 0, tzinfo=timezone.utc)
    msg = sr.build_session_report("ASIA", coins, now_utc=now, news_note="CPI rilis")
    assert "TUTUP SESI ASIA" in msg
    assert "BTC" in msg and "ETH" in msg and "SOL" in msg
    assert "Outlook LONDON" in msg          # outlook ke sesi berikutnya
    assert "CPI rilis" in msg               # news note ikut
    assert "14:00 WIB" in msg               # 07:00 UTC → 14:00 WIB


def test_session_report_has_rs_and_trade_rec():
    coins = [_coin("BTC", trend="BULLISH", regime="BULLISH_TREND", adx=27,
                   price=110, ema21=105, ema50=100, rsi=62, cvd_dir="BUY",
                   chg_pct=1.0),
             _coin("ETH", regime="BREAKOUT_UP", chg_pct=3.0),
             _coin("SOL", chg_pct=0.5)]
    msg = sr.build_session_report("LONDON", coins)
    assert "Relative Strength" in msg
    assert "Trading Outlook" in msg
    assert "Worth trading" in msg
    assert "ETH" in msg


def test_bullish_majors_outlook_leans_up():
    coins = [_coin("BTC", trend="BULLISH", regime="BULLISH_TREND", price=110,
                   ema21=105, ema50=100, rsi=65, cvd_dir="BUY"),
             _coin("ETH", trend="BULLISH", regime="BULLISH_TREND", price=110,
                   ema21=105, ema50=100, rsi=60, cvd_dir="BUY"),
             _coin("SOL", trend="BULLISH", regime="BREAKOUT_UP", price=110,
                   ema21=105, ema50=100, rsi=68, cvd_dir="BUY")]
    msg = sr.build_session_report("LONDON", coins)
    assert "LANJUT NAIK" in msg


def test_shift_alert_renders_reason():
    coin = _coin("SOL", trend="BULLISH", regime="BREAKOUT_UP", price=180,
                 ema21=170, ema50=160, rsi=70, cvd_dir="BUY", chg_pct=3.1)
    msg = sr.build_shift_alert(coin, "bias flip NETRAL→BULLISH", news_note="ETF inflow")
    assert "SHIFTING — SOL" in msg
    assert "bias flip NETRAL→BULLISH" in msg
    assert "ETF inflow" in msg
