"""Tests untuk session_report — laporan majors BTC/ETH/SOL per sesi + shift alert.

Fokus ke logic murni: deteksi sesi, bias per koin, dan rendering pesan
(struktur, bukan I/O / scheduling).
"""
from datetime import datetime, timezone

import session_report as sr


def _coin(name, **over):
    base = {"name": name, "price": 100.0, "chg_pct": 0.0, "trend": "NEUTRAL",
            "regime": "RANGING", "adx": 20, "rsi": 50, "ema21": 100, "ema50": 100,
            "cvd_dir": "FLAT", "cvd_pct": 0.0, "key_sup": 95, "key_res": 105}
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
