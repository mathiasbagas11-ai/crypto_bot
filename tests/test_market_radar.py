"""Tests for market_radar — pure logic only (no API calls).

Covers: sector_score, filter_and_rank_sectors, build_radar_message,
        _to_binance_symbol.
"""
import math
import sys
import os
from datetime import datetime, timezone, timedelta

# Ensure the project root is on the path so `import market_radar` works
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_radar as mr


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cat(cat_id, name=None, chg=5.0, vol=100_000_000):
    """Build a minimal CoinGecko categories dict."""
    return {
        "id":                    cat_id,
        "name":                  name or cat_id.replace("-", " ").title(),
        "market_cap_change_24h": chg,
        "volume_24h":            vol,
    }


def _sector(name="DeFi", chg=5.0, vol=200_000_000, coins=None):
    """Build a minimal ranked sector dict."""
    return {
        "id":     name.lower(),
        "name":   name,
        "chg24h": chg,
        "vol24h": vol,
        "score":  mr.sector_score(chg, vol),
        "coins":  coins or [],
    }


# ── sector_score ─────────────────────────────────────────────────────────────

def test_sector_score_positive_chg():
    score = mr.sector_score(5.0, 100_000_000)
    assert score > 0, "Positive chg + nonzero vol should give positive score"
    assert math.isclose(score, 5.0 * math.log10(100_000_000), rel_tol=1e-9)


def test_sector_score_negative_chg():
    score = mr.sector_score(-3.0, 50_000_000)
    assert score < 0, "Negative chg should give negative score"


def test_sector_score_zero_vol():
    score = mr.sector_score(10.0, 0)
    assert score == 0.0, "vol=0 should return 0.0"


# ── filter_and_rank_sectors ───────────────────────────────────────────────────

def test_filter_excludes_noise_categories():
    cats = [
        _cat("stablecoins",    chg=1.0, vol=500_000_000),
        _cat("wrapped-tokens", chg=2.0, vol=300_000_000),
        _cat("defi",           chg=8.0, vol=200_000_000),
    ]
    result = mr.filter_and_rank_sectors(cats)
    ids = [r["id"] for r in result]
    assert "stablecoins"    not in ids, "stablecoins should be excluded"
    assert "wrapped-tokens" not in ids, "wrapped-tokens should be excluded"
    assert "defi"           in ids,     "defi should be included"


def test_filter_excludes_low_volume():
    cats = [
        _cat("defi",       chg=8.0, vol=200_000_000),
        _cat("gaming",     chg=5.0, vol=10_000_000),   # below min_vol_usd
    ]
    result = mr.filter_and_rank_sectors(cats, min_vol_usd=50_000_000)
    ids = [r["id"] for r in result]
    assert "gaming" not in ids, "Low-volume category should be filtered out"
    assert "defi"   in ids


def test_filter_ranks_by_score():
    # DeFi: chg=10, vol=200M → score = 10 * log10(200M)
    # L2:   chg=2,  vol=500M → score = 2  * log10(500M) (smaller)
    cats = [
        _cat("layer-2", chg=2.0,  vol=500_000_000),
        _cat("defi",    chg=10.0, vol=200_000_000),
    ]
    result = mr.filter_and_rank_sectors(cats)
    assert result[0]["id"] == "defi", "Higher score should rank first"


def test_filter_returns_top_n():
    cats = [_cat(f"cat-{i}", chg=float(i), vol=100_000_000) for i in range(1, 11)]
    result = mr.filter_and_rank_sectors(cats, top_n=3)
    assert len(result) == 3, "Should respect top_n limit"


# ── build_radar_message ───────────────────────────────────────────────────────

_FIXED_UTC = datetime(2025, 6, 10, 8, 30, tzinfo=timezone.utc)
# WIB = UTC+7 → 15:30 WIB


def test_build_radar_empty_returns_warning():
    msg = mr.build_radar_message([], now_utc=_FIXED_UTC)
    assert "Data sektor tidak tersedia" in msg


def test_build_radar_contains_sector_name():
    sectors = [_sector("DeFi", chg=7.5, vol=300_000_000)]
    msg = mr.build_radar_message(sectors, now_utc=_FIXED_UTC)
    assert "DeFi" in msg


def test_build_radar_shows_rank_icons():
    sectors = [_sector("DeFi", chg=7.5, vol=300_000_000)]
    msg = mr.build_radar_message(sectors, now_utc=_FIXED_UTC)
    assert "🥇" in msg, "First-place icon should appear for rank-1 sector"


def test_build_radar_shows_coins():
    coins = [
        {"name": "ETH",  "symbol": "eth",  "pct": 3.2, "price": 3500.0},
        {"name": "AAVE", "symbol": "aave", "pct": 2.1, "price":  90.0},
    ]
    sectors = [_sector("DeFi", coins=coins)]
    msg = mr.build_radar_message(sectors, now_utc=_FIXED_UTC)
    assert "ETH"  in msg, "Coin names should appear in message"
    assert "AAVE" in msg


def test_build_radar_shows_tesis():
    sectors = [_sector("DeFi", chg=7.5, vol=300_000_000)]
    msg = mr.build_radar_message(sectors, now_utc=_FIXED_UTC)
    assert "Tesis:" in msg, "'Tesis:' block should be present"


def test_build_radar_wib_time():
    msg = mr.build_radar_message([_sector("DeFi")], now_utc=_FIXED_UTC)
    # _FIXED_UTC 08:30 UTC → 15:30 WIB
    assert "WIB" in msg, "WIB timestamp should appear in message"
    assert "15:30" in msg, "Correct WIB hour should appear"


# ── _to_binance_symbol ────────────────────────────────────────────────────────

def test_to_binance_symbol_basic():
    assert mr._to_binance_symbol("eth") == "ETHUSDT"
    assert mr._to_binance_symbol("sol") == "SOLUSDT"
    assert mr._to_binance_symbol("btc") == "BTCUSDT"


def test_to_binance_symbol_override():
    assert mr._to_binance_symbol("icp") == "ICPUSDT"
    assert mr._to_binance_symbol("gmt") == "GMTUSDT"
    assert mr._to_binance_symbol("ton") == "TONUSDT"
    assert mr._to_binance_symbol("wld") == "WLDUSDT"
