# Crypto Screening Bot v13 — Alur Kerja Lengkap

Ini adalah alur kerja bot dari awal sampai akhir untuk menghasilkan sinyal trading.

---

## OVERVIEW SINGKAT

```
DATA FETCH → ANALISIS TEKNIKAL → DETEKSI SINYAL → VALIDASI 7-GATE → SCORING → KIRIM TELEGRAM → TRACKING HASIL → BELAJAR
```

---

## FASE 1 — STARTUP

Bot nyala dan langsung jalanin hal-hal ini secara paralel:

1. Load semua modul (news, learning, backtest, dll.)
2. Start Telegram bot polling (dengerin command dari user)
3. Connect WebSocket ke Binance untuk liquidation tracker
4. Kirim notifikasi ke Telegram bahwa bot sudah online
5. Langsung jalankan scan pertama
6. Set jadwal otomatis:
   - Main screener → setiap **10 menit**
   - Pre-pump scan → setiap **5 menit**
   - Pre-dump scan → setiap **5 menit**
   - Scalp scan → setiap **5 menit**
   - Reset risk harian → jam **00:00 UTC**
   - Refresh backtest → jam **01:00 UTC**

---

## FASE 2 — PENGAMBILAN DATA PASAR

Setiap siklus scan, bot fetch data ini untuk **25+ koin liquid** dari Binance Futures:

| Data | Timeframe | Kegunaan |
|------|-----------|---------|
| OHLCV (candlestick) | 4H, 1H, 15M | Analisis struktur pasar |
| Harga real-time | Tick | Entry/exit kalkulasi |
| Open Interest (OI) | Live | Konfirmasi positioning |
| Funding Rate | Live | Deteksi leverage squeeze |
| BTC context | 4H, 1H | Filter macro |
| Fear & Greed Index | Harian | Market sentiment |

---

## FASE 3 — ANALISIS TEKNIKAL

Setiap koin dianalisis berlapis-lapis:

### A. Smart Money Concepts (SMC)
- **Order Blocks (OB)**: Zona support/resistance dari volume besar (4H, 1H, 15M)
- **Fair Value Gaps (FVG)**: Level harga yang belum terisi, tempat harga sering balik
- **Market Structure Break (BoS)**: Konfirmasi arah trend

### B. Indikator Momentum & Volume
- RSI → deteksi overbought/oversold
- MACD → arah trend + kekuatan momentum
- Money Flow Index → apakah volume mendukung arah
- Volume Anomaly → bandingkan volume sekarang vs rata-rata 20 candle
- ATR → ukur volatilitas untuk sizing posisi

### C. Detektor Lanjutan
- RSI Divergence → harga baru high tapi RSI tidak → sinyal bearish tersembunyi
- Candle Pattern → Engulfing, Pin Bar, Doji, Morning/Evening Star
- Equal Highs/Lows → zona konsolidasi S/R
- Liquidity Sweep → harga menyentuh low lalu balik (stop-hunt)
- Trendline otomatis dari swing points

### D. Market Regime
Bot klasifikasikan kondisi pasar:
- `BULLISH_TREND` → trend naik, prioritaskan LONG
- `BEARISH_TREND` → trend turun, prioritaskan SHORT
- `RANGING` → sideways, pakai scalp setup

---

## FASE 4 — DETEKSI SINYAL (4 Jenis)

### 1. PREPUMP (Sebelum Pompa)
**Tujuan**: Masuk sebelum harga breakout naik

Kondisi yang dicek:
- Funding Rate dalam kondisi squeeze (netral, belum berat ke satu sisi)
- Bollinger Band menyempit (konsolidasi sebelum breakout)
- Volume Coil: kompresi lalu spike
- OI naik tapi harga masih flat (akumulasi diam-diam)

**Skor Komponen:**
```
Funding Squeeze   → 30 poin
Volume Coil       → 25 poin
OI Momentum       → 20 poin
Money Flow        → 15 poin
Konteks BTC/Market→ 10 poin
```
Threshold: **≥ 65 poin** → alert dikirim

---

### 2. PREDUMP (Sebelum Dump)
**Tujuan**: Masuk SHORT sebelum harga breakdown

Kondisi yang dicek:
- Funding Rate ekstrem (long terlalu banyak, ripe for liquidation)
- Long Liquidation Squeeze: konsolidasi di zona high leverage
- Candle pattern bearish di atas support
- Volume menurun (distribution)
- RSI Divergence: harga tinggi tapi RSI lebih rendah

**Skor Komponen:**
```
Extreme Funding     → 35 poin
Long Squeeze + LSF  → 25 poin
Bearish Structure   → 20 poin
Money Flow Diverge  → 15 poin
Risk/Reward         →  5 poin
```
Threshold: **≥ 65 poin** → alert dikirim

---

### 3. SCALP (15M-5M, Intraday Cepat)
**Tujuan**: Entry dan exit dalam 5-30 menit

Trigger:
- Liquidity sweep (stop hunt sudah terjadi, safe to enter)
- Rejection dari Order Block 1H
- FVG terisi lalu balik arah
- Volume spike >2x rata-rata pada candle entry
- RSI < 30 lalu recovery

---

### 4. SWING (4H-1H, Hold 4-24 Jam)
**Tujuan**: Tangkap pergerakan besar intraday

Kondisi:
- 4H structure: break resistance dikonfirmasi
- 1H: pullback + retest + rejection
- Tidak di zona ekstrem pada timeframe daily
- Liquidity cukup (volume rata-rata memadai)

---

## FASE 5 — KALKULASI CONFLUENCE

Semua detektor digabung jadi satu skor confluence:

```
Confluence = Keselarasan timeframe (4H+1H+15M) 
           + Kekuatan structure (OB, FVG)
           + Dukungan momentum (RSI/MACD/MFI)
           + Konfirmasi volume
           + Bias arah (LONG/SHORT/NEUTRAL)
```

| Level | Skor | Makna |
|-------|------|-------|
| EXCELLENT | 80+ | Semua faktor setuju |
| GOOD | 65–79 | Sebagian besar setuju |
| FAIR | 50–64 | Cukup tapi ada noise |
| POOR | <50 | Sinyal lemah, skip |

---

## FASE 6 — VALIDASI 7-GATE

Sebelum sinyal dikirim, harus lolos **7 layer filter**:

```
Gate 1: HTF Trend Alignment (20%)
  → Sinyal harus searah dengan Daily/Weekly structure

Gate 2: BTC Macro Support (20%)
  → BTC bearish ekstrem? → LONG diblok
  → BTC rally kuat? → SHORT kena penalti

Gate 3: Coinbase Premium (20%)
  → Premium > 1% = institutional bullish → boost LONG
  → Premium < -1% = institutional bearish → boost SHORT

Gate 4: Ecosystem Season (12%)
  → Koin dari ekosistem hot (AI, SOL, BTC) → boost
  → Ekosistem sepi → penalti atau skip

Gate 5: OI/Funding Sanity (12%)
  → OI naik tapi harga diam = manipulasi → skip
  → Funding ekstrem (>0.05%) = terlalu crowded → penalti

Gate 6: Multi-TF Confluence Depth (10%)
  → Minimal 2-3 timeframe setuju
  → Hanya 1 TF confirm = lemah → skip

Gate 7: Liquidity & Volatility (3%)
  → ATR terlalu tinggi (>5%) = terlalu risky → skip
  → ATR terlalu rendah (<0.3%) = tidak ada momentum → skip
  → Entry jauh dari zona SMC → skip
```

**Hasil Gate:**
- `PASS` → lanjut ke scoring final
- `SOFT_BLOCK` → skor dikurangi
- `HARD_BLOCK` → sinyal dibatalkan

---

## FASE 7 — MASTER SCORE & KEPUTUSAN FINAL

```
Master Score = Confluence(30%) + Prepump(25%) + Predump(25%) 
             + Scalp(10%) + Swing(10%)
             ± Gate Adjustments
             ± Signal Persistence Bonus
             ± Market Context
```

| Skor | Keputusan |
|------|-----------|
| ≥ 75 | CONFIRMED → langsung kirim Telegram |
| 60–74 | WATCH → simpan, pantau scan berikutnya |
| < 60 | SKIP → diabaikan |

**Signal Persistence Check:**
- Sinyal konsisten dari scan sebelumnya → +5 bonus (lebih terpercaya)
- Sinyal tiba-tiba muncul dari nol → dicurigai noise
- Arah sinyal berubah flip → penalti -5

---

## FASE 8 — VALIDASI BACKTEST

Sebelum dikirim, bot jalankan **mini backtest 7 hari**:

1. Fetch data historis 7 hari terakhir
2. Replay strategi: setiap kondisi match → simulasi entry
3. Hitung: Win Rate, Profit Factor, Sharpe Ratio
4. **Blok sinyal jika:**
   - Win Rate < 40%
   - Profit Factor < 1.0 (lebih banyak rugi)
   - 3+ loss berturut-turut baru-baru ini

---

## FASE 9 — KIRIM TELEGRAM

Format alert yang dikirim:

```
📊 SOLUSDT LONG

🎯 CONFLUENCE: EXCELLENT (85/100)
├─ Reasons: Bullish OB rejection, Volume spike +280%, RSI bullish div
├─ Entry:  $147.50 (limit di OB level)
├─ TP1:   $152.00 (2x risk/reward)
├─ TP2:   $155.50 (3.5x risk/reward)
├─ SL:    $145.00 (-2.00%)
└─ Hold:  2–6 jam (scalp)

📈 BTC: BULLISH | Market: Greed (72) | Season: SOL 🟣 HOT
⚡ Confidence: HIGH (persistent dari scan sebelumnya)
```

---

## FASE 10 — TRACKING OTOMATIS HASIL SINYAL

Setelah sinyal dikirim:

1. Disimpan ke `pending_signals.json`
2. Setiap scan berikutnya bot cek apakah:
   - **TP1 HIT** → catat win
   - **TP2 HIT** → catat big win
   - **SL HIT** → catat loss, trigger mini backtest
   - **EXPIRED** (>24 jam tanpa gerak) → outcome netral

---

## FASE 11 — SYMBOL MEMORY & AUTO-LEARNING

Bot simpan histori per-koin di `symbol_memory.json`:

```json
{
  "SOLUSDT": {
    "win_rate": 66.7,
    "avg_pnl": 1.8,
    "best_signal_type": "PREPUMP",
    "lessons": [
      "PREPUMP works well in BULLISH markets",
      "Avoid SCALP when BB squeeze < 15%"
    ],
    "blacklisted": false
  }
}
```

**Auto-Blacklist**: Jika SL rate > 75% dalam 10 trade terakhir → koin di-blacklist sementara 6 jam.

**Lesson Derivation**: Bot secara otomatis derive pelajaran dari pola outcome, lalu inject ke prompt AI untuk analisis lebih baik.

---

## FASE 12 — MANAJEMEN TRADE MANUAL

User bisa buka posisi manual via Telegram:

```
/trade BTC LONG 95000 60    → buka posisi BTC long @ $95k, stake $60
/trades                     → lihat posisi aktif + P&L
/close BTC                  → tutup posisi BTC
```

Bot otomatis hitung:
- SL = 1.5x ATR di bawah entry
- TP1 = 2x ATR di atas entry  
- TP2 = 3.5x ATR di atas entry
- Trail Stop aktif setelah TP1

---

## RINGKASAN ALUR (TL;DR)

```
[START]
   │
   ▼
[FETCH DATA] → Binance: OHLCV (4H/1H/15M), OI, Funding, BTC Context
   │
   ▼
[ANALISIS] → SMC (OB, FVG, BoS) + Momentum (RSI, MACD, MFI) + Volume
   │
   ▼
[DETEKSI] → Cek 4 jenis sinyal: Prepump / Predump / Scalp / Swing
   │
   ▼
[SCORING] → Kalkulasi confluence per koin, assign skor 0-100
   │
   ▼
[VALIDASI 7-GATE] → HTF trend, BTC macro, Premium, Ecosystem, OI, Confluence, ATR
   │
   ├─ HARD_BLOCK → ❌ Sinyal dibatalkan
   │
   ├─ SOFT_BLOCK → ⚠️ Skor dikurangi
   │
   └─ PASS → lanjut
   │
   ▼
[MASTER SCORE]
   │
   ├─ < 60  → SKIP
   ├─ 60-74 → WATCH (pantau scan berikut)
   └─ ≥ 75  → CONFIRMED
                  │
                  ▼
            [BACKTEST GATE] → Win rate < 40%? → Blok
                  │
                  ▼
            [KIRIM TELEGRAM] → Alert dengan entry, TP1, TP2, SL
                  │
                  ▼
            [TRACKING] → Monitor harga sampai TP/SL/Expired
                  │
                  ▼
            [CATAT OUTCOME] → Win/Loss masuk ke symbol_memory
                  │
                  ▼
            [AUTO-LEARN] → Derive lessons, update blacklist, inject ke AI

[ULANGI setiap 5-10 menit]
```

---

## FILE UTAMA

| File | Fungsi |
|------|--------|
| `crypto_screening_bot_v13.py` | Engine utama (8500+ baris), semua detektor |
| `confirmed_signal.py` | Master scoring & keputusan final |
| `auto_validator.py` | 7-gate validation |
| `signal_tracker.py` | Tracking outcome sinyal |
| `symbol_memory.py` | Memori & lessons per-koin |
| `backtest_engine.py` | Validasi historis |
| `trade_manager.py` | Manajemen posisi manual |
| `risk_manager.py` | Kalkulasi sizing & daily limit |
| `learning_engine.py` | Logging keputusan & derive lessons |
| `market_regime.py` | Klasifikasi trend/range |
| `ecosystem_detector.py` | Deteksi ekosistem yang lagi season |
| `news_sentiment.py` | Analisis berita & sentiment |
| `market_context.py` | Filter makro (Fear & Greed, BTC) |
| `whale_tracker.py` | Monitor akumulasi institutional |
| `website/streamlit_app.py` | Dashboard web real-time |
