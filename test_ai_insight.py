#!/usr/bin/env python3
"""
Tes cepat output AI Insight (groq_signal_insight) tanpa nunggu sinyal live.
Jalanin di tempat yang ada GROQ_API_KEY:

    python test_ai_insight.py

Skenario: SHORT ZEC dgn KONFLIK sengaja (candle 15M bullish vs trade short)
biar keliatan apakah prompt baru berhasil nangkep konflik + kasih level
invalidasi konkret.
"""
import os

# load .env kalau ada (opsional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import crypto_screening_bot_v13 as b

if not b.GROQ_API_KEY:
    raise SystemExit("⚠️  GROQ_API_KEY belum di-set. Set dulu di .env atau env var.")

# Skenario realistis dgn konflik antar-timeframe yang disengaja
out = b.groq_signal_insight(
    symbol       = "ZECUSDT",
    direction    = "SHORT",
    master_score = 79,
    gate_reasons = [
        "Master score 79/100",
        "MoneyFlow OUTFLOW 2/3 TF aligned (4H, 15M)",
        "4H BEARISH trend confirmed",
    ],
    trade = {
        "entry_mode": "RETEST_WAIT",
        "entry": 553.40, "tp1": 533.80, "sl": 558.55,
    },
    tf_4h = {
        "structure": {"trend": "BEARISH"},
        "money_flow": {"bias": "OUTFLOW", "strength": "STRONG", "cvd_pct": -11.2},
    },
    tf_1h = {
        "structure": {"trend": "NEUTRAL"},
        "money_flow": {"bias": "INFLOW", "cvd_pct": 2.3},
        "candle_patterns": {"pattern": "BEARISH_ENGULFING", "direction": "BEARISH"},
    },
    tf_15m = {
        "money_flow": {"bias": "OUTFLOW"},
        # KONFLIK sengaja: candle bullish di setup short
        "candle_patterns": {"pattern": "BULLISH_ENGULFING", "direction": "BULLISH"},
    },
    oi_data = {
        "funding_rate": 0.011, "oi_change_pct": 2.5,
        "ls_ratio": 1.12, "ls_bias": "LONG",
        "top_ls_ratio": 0.88, "top_ls_bias": "SHORT",
        "perp_spot_basis": 0.04,
    },
)

print("=" * 60)
print("AI INSIGHT OUTPUT (Groq):")
print("=" * 60)
print(out)
print("=" * 60)
print("\nCek: apakah dia nyebut KONFLIK candle 15M bullish vs short,")
print("dan kasih level INVALIDASI konkret (acuan SL 558.55 / struktur)?")
