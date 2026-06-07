"""Tests untuk import trade dari screenshot order-details (vision AI).

`build_trade_from_screenshot` adalah logika murni yang mengubah field mentah
hasil baca AI vision jadi trade dict siap-simpan. Bagian rawan: derivasi margin
dari |PnL / ROI| dan normalisasi arah ('Close short' -> SHORT). Nilai uji
diambil dari screenshot Bitget asli (HYPE & BTC).
"""
import pytest

from trade_journal import build_trade_from_screenshot, format_shot_preview, _num


def test_hype_screenshot_short_with_derived_margin():
    # HYPEUSDT, Close short 75x, entry 59.903, exit 60.213, PnL -1.12183, ROI -38.82%
    t, err = build_trade_from_screenshot({
        "coin": "HYPEUSDT", "direction": "Close short", "leverage": 75,
        "entry_price": 59.903, "exit_price": 60.213,
        "realized_pnl": -1.12183, "roi_pct": -38.82,
    })
    assert err == ""
    assert t["coin"] == "HYPE"
    assert t["direction"] == "SHORT"
    assert t["entry"] == pytest.approx(59.903)
    assert t["pnl"] == pytest.approx(-1.1218, abs=1e-3)
    # margin = |pnl / roi| = 1.12183 / 0.3882 ≈ 2.89
    assert t["margin"] == pytest.approx(2.89, abs=0.05)


def test_btc_screenshot_parses_messy_strings():
    # Angka dengan koma ribuan, suffix USDT, dan simbol %
    t, err = build_trade_from_screenshot({
        "coin": "BTCUSDT", "direction": "Close short", "leverage": 150,
        "entry_price": "62,499.9", "exit_price": "62,475.5",
        "realized_pnl": "0.17080000 USDT", "roi_pct": "5.85%",
    })
    assert err == ""
    assert t["coin"] == "BTC"
    assert t["direction"] == "SHORT"
    assert t["leverage"] == 150
    assert t["margin"] == pytest.approx(2.92, abs=0.05)


def test_close_long_maps_to_long():
    t, err = build_trade_from_screenshot({
        "coin": "SOL", "direction": "Close long", "leverage": 10,
        "entry_price": 150, "realized_pnl": 5, "roi_pct": 10,
    })
    assert err == ""
    assert t["direction"] == "LONG"
    # margin = 5 / 0.10 = 50
    assert t["margin"] == pytest.approx(50.0, abs=0.1)


def test_explicit_margin_overrides_roi_derivation():
    t, err = build_trade_from_screenshot({
        "coin": "ETH", "direction": "LONG", "leverage": 20,
        "entry_price": 3000, "realized_pnl": 12, "roi_pct": 5, "margin": 100,
    })
    assert err == ""
    assert t["margin"] == pytest.approx(100.0)


def test_missing_direction_is_error():
    t, err = build_trade_from_screenshot({
        "coin": "ETH", "entry_price": 1, "realized_pnl": 1, "roi_pct": 1,
    })
    assert t is None
    assert "Arah" in err


def test_missing_coin_is_error():
    t, err = build_trade_from_screenshot({
        "direction": "LONG", "entry_price": 1, "realized_pnl": 1, "roi_pct": 1,
    })
    assert t is None
    assert "Coin" in err


def test_no_roi_and_no_margin_is_error():
    t, err = build_trade_from_screenshot({
        "coin": "ETH", "direction": "LONG", "entry_price": 100,
        "realized_pnl": 5, "roi_pct": None,
    })
    assert t is None
    assert "Margin" in err


def test_num_parser_handles_variants():
    assert _num("62,499.9") == pytest.approx(62499.9)
    assert _num("-1.12183000 USDT") == pytest.approx(-1.12183)
    assert _num("5.85%") == pytest.approx(5.85)
    assert _num(None) is None
    assert _num("n/a") is None


def test_preview_contains_key_fields():
    t, _ = build_trade_from_screenshot({
        "coin": "HYPEUSDT", "direction": "Close short", "leverage": 75,
        "entry_price": 59.903, "exit_price": 60.213,
        "realized_pnl": -1.12183, "roi_pct": -38.82,
    })
    out = format_shot_preview(t)
    assert "HYPEUSDT" in out
    assert "SHORT" in out
    assert "ya" in out.lower()
    assert "batal" in out.lower()
