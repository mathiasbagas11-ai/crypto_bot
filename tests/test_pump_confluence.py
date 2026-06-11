"""Tests untuk _price_volume_confluence — tulang punggung skor OI-independent.

Memastikan pump/dump alt spot-only (tanpa data funding/OI) tetap bisa skor
tinggi dari price action + volume, dan kondisi kalem tetap rendah (anti-spam).
"""
import crypto_screening_bot_v13 as bot


def _mk(closes, vols, atr, ema=(0, 0, 0), mf=None):
    candles = []
    for i, (cl, v) in enumerate(zip(closes, vols)):
        op = closes[i - 1] if i > 0 else cl
        candles.append({
            "open": op, "close": cl,
            "high": max(op, cl) * 1.004, "low": min(op, cl) * 0.996,
            "volume": v, "time": i,
        })
    return {"candles": candles, "atr": atr,
            "ema9": ema[0], "ema21": ema[1], "ema50": ema[2],
            "money_flow": mf or {}}


def test_spot_only_pump_scores_high_without_oi():
    # Volume spike + akselerasi harga naik + EMA stack + inflow → skor tinggi
    tf = _mk([100] * 18 + [101.5, 104.0],
             [1000] * 18 + [3000, 4200],
             atr=0.8, ema=(103.5, 102, 100.5),
             mf={"bias": "INFLOW", "strength": "STRONG", "cvd_pct": 6.1})
    score, reasons = bot._price_volume_confluence(tf, {}, {}, "UP")
    assert score >= 45, (score, reasons)
    assert any("RVOL" in r for r in reasons)


def test_spot_only_dump_scores_high_without_oi():
    tf = _mk([100] * 18 + [98.5, 95.5],
             [1000] * 18 + [2800, 4000],
             atr=0.8, ema=(96.5, 98, 99.5),
             mf={"bias": "OUTFLOW", "strength": "STRONG", "cvd_pct": -7.2})
    score, reasons = bot._price_volume_confluence(tf, {}, {}, "DOWN")
    assert score >= 45, (score, reasons)
    assert any("RVOL" in r for r in reasons)


def test_flat_market_scores_low():
    tf = _mk([100] * 20, [1000] * 20, atr=0.8, ema=(100, 100, 100))
    score, _ = bot._price_volume_confluence(tf, {}, {}, "UP")
    assert score <= 10, score


def test_wrong_direction_volume_not_rewarded():
    # Volume spike tapi candle bearish saat cek arah UP → RVOL searah tidak penuh
    tf = _mk([100] * 18 + [100, 96.0],          # candle terakhir turun
             [1000] * 18 + [1000, 4200],
             atr=0.8, ema=(101, 100.5, 100))
    up_score, _ = bot._price_volume_confluence(tf, {}, {}, "UP")
    down_score, _ = bot._price_volume_confluence(tf, {}, {}, "DOWN")
    assert down_score > up_score


def test_insufficient_candles_returns_zero():
    tf = _mk([100] * 5, [1000] * 5, atr=0.8)
    score, reasons = bot._price_volume_confluence(tf, {}, {}, "UP")
    assert score == 0 and reasons == []
