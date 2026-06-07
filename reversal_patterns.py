"""
reversal_patterns.py — V-Shape & Quasimodo (QM) reversal detectors.

Pure, IO-free functions yang bekerja di atas list candle OHLCV berbentuk:
    {"open","high","low","close","volume","time"[,"taker_buy_vol"]}

Didesain untuk:
  1. Di-plug ke analyze_timeframe() di crypto_screening_bot_v13.py
     (tf["v_shape"], tf["qm_pattern"]) — fokus utama TF 1H.
  2. Memberi makan scanner run_reversal_auto() dengan alur 2-tahap:
        EARLY  → pola sedang forming (heads-up dini, "siap-siap")
        CONFIRM→ entry zone ke-tap / reclaim + volume (entry window)

Semua deteksi anti-lookahead: HANYA closed candles yang dikirim ke sini.
Tidak ada repainting — fungsi murni atas data historis yang sudah close.

Skema return (konsisten untuk kedua detektor):
    {
      "type":        "V_SHAPE_BULLISH" | "V_SHAPE_BEARISH"
                     | "QM_BULLISH" | "QM_BEARISH" | "NONE",
      "direction":   "LONG" | "SHORT" | "NONE",
      "stage":       "EARLY" | "CONFIRM" | "NONE",
      "score":       int 0..100,
      "entry_ref":   float | None,   # harga acuan entry (pivot reclaim / RS level)
      "invalidation":float | None,   # level struktural yang membatalkan tesis (SL)
      "zone":        {"bottom":float,"top":float} | None,
      "reasons":     [str],
      "meta":        {...},          # detail tambahan untuk debugging/score
    }
"""
from __future__ import annotations


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _none_result() -> dict:
    return {
        "type": "NONE", "direction": "NONE", "stage": "NONE", "score": 0,
        "entry_ref": None, "invalidation": None, "zone": None,
        "reasons": [], "meta": {},
    }


def _avg(seq) -> float:
    seq = list(seq)
    return (sum(seq) / len(seq)) if seq else 0.0


def _find_swings(candles: list, lb: int = 3):
    """Swing highs/lows pakai metode fractal (lb bar kiri-kanan), seperti
    detect_market_structure di main bot. Return (swing_highs, swing_lows)
    masing-masing list of (index, price)."""
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    n = len(candles)
    sh = [(i, highs[i]) for i in range(lb, n - lb)
          if highs[i] == max(highs[i - lb:i + lb + 1])]
    sl = [(i, lows[i]) for i in range(lb, n - lb)
          if lows[i] == min(lows[i - lb:i + lb + 1])]
    return sh, sl


def _pick_best(*results: dict) -> dict:
    """Pilih hasil dengan stage valid & score tertinggi."""
    valid = [r for r in results if r and r.get("type") != "NONE"]
    if not valid:
        return _none_result()
    return max(valid, key=lambda r: r.get("score", 0))


# ──────────────────────────────────────────────────────────────────────────
# V-SHAPE  (reversal high-probability)
# ──────────────────────────────────────────────────────────────────────────
def detect_v_shape(
    candles: list,
    lookback: int = 30,
    min_leg_pct: float = 0.025,
    max_leg_bars: int = 10,
    pivot_max_ago: int = 8,
    early_recovery: float = 0.25,
    confirm_recovery: float = 0.50,
    vol_mult: float = 1.3,
) -> dict:
    """Deteksi V-shape (bullish) / inverted-V (bearish).

    V-shape bullish = drop tajam (left leg) → pivot low (sering disertai
    rejection wick / sweep) → recovery cepat (right leg) dengan volume naik.
    Sinyal di-fire saat right leg BARU mulai (EARLY) sehingga tidak telat.
    """
    if not candles or len(candles) < 15:
        return _none_result()

    work = candles[-lookback:] if len(candles) > lookback else list(candles)
    n = len(work)
    highs = [c["high"] for c in work]
    lows = [c["low"] for c in work]
    opens = [c["open"] for c in work]
    closes = [c["close"] for c in work]
    vols = [c.get("volume", 0) or 0 for c in work]
    avg_vol = _avg(vols[:-1]) if len(vols) > 1 else (vols[0] if vols else 0.0)
    cur = closes[-1]

    bull = _v_eval(
        n, highs, lows, opens, closes, vols, avg_vol, cur, work,
        bullish=True, min_leg_pct=min_leg_pct, max_leg_bars=max_leg_bars,
        pivot_max_ago=pivot_max_ago, early_recovery=early_recovery,
        confirm_recovery=confirm_recovery, vol_mult=vol_mult,
    )
    bear = _v_eval(
        n, highs, lows, opens, closes, vols, avg_vol, cur, work,
        bullish=False, min_leg_pct=min_leg_pct, max_leg_bars=max_leg_bars,
        pivot_max_ago=pivot_max_ago, early_recovery=early_recovery,
        confirm_recovery=confirm_recovery, vol_mult=vol_mult,
    )
    return _pick_best(bull, bear)


def _v_eval(n, highs, lows, opens, closes, vols, avg_vol, cur, work, *,
            bullish, min_leg_pct, max_leg_bars, pivot_max_ago,
            early_recovery, confirm_recovery, vol_mult) -> dict:
    # Pivot = extreme global di window. Bullish→min low, bearish→max high.
    if bullish:
        piv_i = min(range(n), key=lambda i: lows[i])
        piv = lows[piv_i]
    else:
        piv_i = max(range(n), key=lambda i: highs[i])
        piv = highs[piv_i]

    # Butuh ruang: minimal 2 bar di kiri (descent) & 1 bar di kanan (ascent).
    if piv_i < 2 or piv_i > n - 2:
        return _none_result()

    bars_after = (n - 1) - piv_i
    if bars_after > pivot_max_ago:   # pivot kelamaan → momentum mungkin habis
        return _none_result()

    left_start = max(0, piv_i - max_leg_bars)
    if bullish:
        # left leg high (titik awal turun)
        seg = highs[left_start:piv_i + 1]
        left_ext = max(seg)
        left_ext_i = left_start + seg.index(left_ext)
        leg_pct = (left_ext - piv) / left_ext if left_ext > 0 else 0.0
        denom = (left_ext - piv)
        recovery = (cur - piv) / denom if denom > 0 else 0.0
        pv = work[piv_i]
        rng = pv["high"] - pv["low"]
        wick = (min(pv["open"], pv["close"]) - pv["low"]) / rng if rng > 0 else 0.0
        last_dir_ok = closes[-1] > opens[-1]
        ptype, direction = "V_SHAPE_BULLISH", "LONG"
    else:
        seg = lows[left_start:piv_i + 1]
        left_ext = min(seg)
        left_ext_i = left_start + seg.index(left_ext)
        leg_pct = (piv - left_ext) / piv if piv > 0 else 0.0
        denom = (piv - left_ext)
        recovery = (piv - cur) / denom if denom > 0 else 0.0
        pv = work[piv_i]
        rng = pv["high"] - pv["low"]
        wick = (pv["high"] - max(pv["open"], pv["close"])) / rng if rng > 0 else 0.0
        last_dir_ok = closes[-1] < opens[-1]
        ptype, direction = "V_SHAPE_BEARISH", "SHORT"

    leg_bars = piv_i - left_ext_i
    if leg_pct < min_leg_pct or leg_bars <= 0 or leg_bars > max_leg_bars:
        return _none_result()
    if recovery < early_recovery:
        return _none_result()

    # Volume reversal: ada bar pasca-pivot dengan volume di atas rata-rata.
    rev_vol = max(vols[piv_i:]) if piv_i < n else 0.0
    vol_ok = avg_vol > 0 and rev_vol >= avg_vol * vol_mult

    stage = "CONFIRM" if (recovery >= confirm_recovery and last_dir_ok and vol_ok) else "EARLY"

    # Scoring 0..100
    score = 0.0
    score += _clamp(leg_pct / 0.10, 0, 1) * 30      # kedalaman leg (≤10% → 30)
    score += _clamp(recovery / 1.0, 0, 1) * 30      # kekuatan recovery
    score += 15 if vol_ok else 0                    # konfirmasi volume
    score += _clamp(wick / 0.5, 0, 1) * 15          # rejection wick di pivot
    score += _clamp((max_leg_bars - leg_bars) / max_leg_bars, 0, 1) * 10  # ketajaman
    score = int(round(_clamp(score, 0, 100)))

    reasons = []
    arrow = "drop" if bullish else "rally"
    reasons.append(
        f"{'🟢' if bullish else '🔴'} V-Shape {'BULLISH' if bullish else 'BEARISH'}: "
        f"{arrow} {leg_pct*100:.1f}% dalam {leg_bars} bar, recovery {recovery*100:.0f}%"
    )
    if vol_ok:
        reasons.append(f"📊 Volume reversal {rev_vol/avg_vol:.1f}x — momentum konfirmasi")
    if wick >= 0.3:
        reasons.append(f"🪝 Rejection wick {wick*100:.0f}% di pivot — absorpsi")

    if bullish:
        entry_ref = piv
        zone = {"bottom": round(piv * 0.998, 8), "top": round(piv * 1.01, 8)}
        invalidation = round(piv * 0.995, 8)
    else:
        entry_ref = piv
        zone = {"bottom": round(piv * 0.99, 8), "top": round(piv * 1.002, 8)}
        invalidation = round(piv * 1.005, 8)

    return {
        "type": ptype, "direction": direction, "stage": stage, "score": score,
        "entry_ref": round(entry_ref, 8), "invalidation": invalidation, "zone": zone,
        "reasons": reasons,
        "meta": {
            "pivot_idx": piv_i, "pivot_price": round(piv, 8),
            "leg_pct": round(leg_pct, 4), "leg_bars": leg_bars,
            "recovery": round(recovery, 3), "bars_after": bars_after,
            "vol_ok": vol_ok,
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# QUASIMODO (QM) — shift bear↔bull via sweep + CHoCH
# ──────────────────────────────────────────────────────────────────────────
def detect_qm_pattern(
    candles: list,
    lookback: int = 40,
    swing_lb: int = 3,
    rs_buffer_pct: float = 0.006,
    min_sweep_pct: float = 0.002,
) -> dict:
    """Deteksi pola Quasimodo (Over-and-Under).

    Bullish QM (shift bear→bull):
        Left Shoulder low (LS) → rally ke high (LS-high) → Head: lower-low
        yang SWEEP di bawah LS (grab likuiditas) → rally BREAK di atas LS-high
        (CHoCH bullish) → Right Shoulder: retrace ke level LS = entry zone.
    Bearish QM = mirror.

    Reuse fondasi yang sudah ada di bot: swing detection + konsep CHoCH +
    liquidity sweep. RS tap = entry low-risk yang dini terhadap leg berikutnya.
    """
    if not candles or len(candles) < (swing_lb * 2 + 5):
        return _none_result()

    work = candles[-lookback:] if len(candles) > lookback else list(candles)
    sh, sl = _find_swings(work, swing_lb)
    cur = work[-1]["close"]

    bull = _qm_eval(work, sh, sl, cur, rs_buffer_pct, min_sweep_pct, bullish=True)
    bear = _qm_eval(work, sh, sl, cur, rs_buffer_pct, min_sweep_pct, bullish=False)
    return _pick_best(bull, bear)


def _qm_eval(work, sh, sl, cur, rs_buffer_pct, min_sweep_pct, *, bullish) -> dict:
    n = len(work)

    if bullish:
        # Head = swing low terbaru. Butuh ≥1 swing high & ≥2 swing low.
        if len(sl) < 2 or len(sh) < 1:
            return _none_result()
        head_i, head_p = sl[-1]
        prior_highs = [h for h in sh if h[0] < head_i]
        if not prior_highs:
            return _none_result()
        ls_high = max(prior_highs, key=lambda x: x[1])          # peak (LS-high)
        prior_lows = [l for l in sl if l[0] < ls_high[0]]
        if not prior_lows:
            return _none_result()
        ls = min(prior_lows, key=lambda x: ls_high[0] - x[0])    # LS terdekat sebelum peak
        ls_i, ls_p = ls

        # Core QM: head SWEEP di bawah LS (grab likuiditas sell-side)
        if ls_p <= 0:
            return _none_result()
        sweep_pct = (ls_p - head_p) / ls_p
        if head_p >= ls_p or sweep_pct < min_sweep_pct:
            return _none_result()

        # CHoCH: harga break di atas LS-high setelah head
        post = work[head_i + 1:]
        post_ext = max((c["high"] for c in post), default=cur)
        choch = cur > ls_high[1] or post_ext > ls_high[1]

        rs_top = ls_p * (1 + rs_buffer_pct)
        rs_bottom = ls_p * (1 - rs_buffer_pct)

        if choch:
            stage = "CONFIRM" if rs_bottom <= cur <= rs_top else "EARLY"
        else:
            if cur <= head_p:        # belum reclaim → belum forming valid
                return _none_result()
            stage = "EARLY"

        struct_pct = (ls_high[1] - ls_p) / ls_p if ls_p > 0 else 0.0
        ptype, direction = "QM_BULLISH", "LONG"
        entry_ref = ls_p
        invalidation = round(head_p * 0.997, 8)
        zone = {"bottom": round(rs_bottom, 8), "top": round(rs_top, 8)}
        prox = abs(cur - ls_p) / ls_p if ls_p > 0 else 1.0
    else:
        if len(sh) < 2 or len(sl) < 1:
            return _none_result()
        head_i, head_p = sh[-1]
        prior_lows = [l for l in sl if l[0] < head_i]
        if not prior_lows:
            return _none_result()
        ls_low = min(prior_lows, key=lambda x: x[1])             # trough (LS-low)
        prior_highs = [h for h in sh if h[0] < ls_low[0]]
        if not prior_highs:
            return _none_result()
        ls = min(prior_highs, key=lambda x: ls_low[0] - x[0])    # LS terdekat sebelum trough
        ls_i, ls_p = ls

        if ls_p <= 0:
            return _none_result()
        sweep_pct = (head_p - ls_p) / ls_p
        if head_p <= ls_p or sweep_pct < min_sweep_pct:
            return _none_result()

        post = work[head_i + 1:]
        post_ext = min((c["low"] for c in post), default=cur)
        choch = cur < ls_low[1] or post_ext < ls_low[1]

        rs_top = ls_p * (1 + rs_buffer_pct)
        rs_bottom = ls_p * (1 - rs_buffer_pct)

        if choch:
            stage = "CONFIRM" if rs_bottom <= cur <= rs_top else "EARLY"
        else:
            if cur >= head_p:
                return _none_result()
            stage = "EARLY"

        struct_pct = (ls_p - ls_low[1]) / ls_p if ls_p > 0 else 0.0
        ptype, direction = "QM_BEARISH", "SHORT"
        entry_ref = ls_p
        invalidation = round(head_p * 1.003, 8)
        zone = {"bottom": round(rs_bottom, 8), "top": round(rs_top, 8)}
        prox = abs(cur - ls_p) / ls_p if ls_p > 0 else 1.0

    head_ago = (n - 1) - head_i

    # Scoring 0..100
    score = 0.0
    score += 25 if choch else 10
    score += _clamp(sweep_pct / 0.02, 0, 1) * 20
    score += _clamp(struct_pct / 0.10, 0, 1) * 20
    score += _clamp((n - head_ago) / n, 0, 1) * 15
    score += _clamp((0.05 - prox) / 0.05, 0, 1) * 20
    score = int(round(_clamp(score, 0, 100)))

    reasons = [
        f"{'🟢' if bullish else '🔴'} QM {'BULLISH' if bullish else 'BEARISH'}: "
        f"sweep {sweep_pct*100:.2f}% {'di bawah' if bullish else 'di atas'} left shoulder "
        f"{'+ CHoCH' if choch else '(CHoCH pending)'}"
    ]
    if choch:
        reasons.append(
            f"🎯 RS zone {zone['bottom']:.6g}–{zone['top']:.6g} — "
            f"{'tap entry' if stage == 'CONFIRM' else 'tunggu retrace'}"
        )

    return {
        "type": ptype, "direction": direction, "stage": stage, "score": score,
        "entry_ref": round(entry_ref, 8), "invalidation": invalidation, "zone": zone,
        "reasons": reasons,
        "meta": {
            "head_idx": head_i, "head_price": round(head_p, 8),
            "ls_price": round(ls_p, 8), "ls_high_low": round(
                (ls_high[1] if bullish else ls_low[1]), 8),
            "choch": choch, "sweep_pct": round(sweep_pct, 4),
            "head_ago": head_ago, "prox": round(prox, 4),
        },
    }
