"""Tests — gate aggregation di auto_validator (_aggregate_gate).

Fokus: layer yang TIDAK punya data (no_data) — mis. ecosystem cache stale,
cb_premium untuk koin non-Coinbase — tidak boleh menghukum sinyal. Skor
di-re-normalisasi atas bobot layer yang benar-benar punya data, dengan guard
cakupan supaya sinyal tak lolos dari data tipis. Kualitas tidak diturunkan:
exclusion hanya menaikkan skor sinyal BAGUS yang keseret data absen, dan
tidak menyelamatkan sinyal jelek.
"""
import auto_validator as av


def _layers(scores: dict, no_data=(), blocking=()):
    """Bangun dict results dari skor per-layer. Layer yg tak disebut = 50."""
    out = {}
    for lid in av.LAYER_WEIGHTS:
        out[lid] = {
            "score":    scores.get(lid, 50),
            "blocking": lid in blocking,
            "no_data":  lid in no_data,
        }
    return out


# ── no_data exclusion MENOLONG sinyal bagus yg keseret data absen ──

def test_no_data_excluded_lets_good_signal_pass():
    # 6 layer data nyata @63, cb_premium+ecosystem TANPA data.
    real = {"htf_trend": 63, "btc_alignment": 63, "oi_sanity": 63,
            "mtf_depth": 63, "liquidity": 63, "volatility": 63}
    res = _layers(real, no_data=("cb_premium", "ecosystem"))
    agg = av._aggregate_gate(res)
    # Re-normalisasi atas 72 bobot → 63 → PASS.
    assert agg["total_score"] == 63
    assert agg["gate"] == "PASS"
    # Layer no_data tidak masuk failed.
    assert "cb_premium" not in agg["failed"]
    assert "ecosystem" not in agg["failed"]


def test_old_behavior_would_have_soft_blocked():
    # Tanpa exclusion (hitung manual): 63*72 + 50*28 = 59.36 → SOFT_BLOCK.
    # Ini mengunci bahwa exclusion-lah yang mengubah hasil jadi PASS.
    real = {"htf_trend": 63, "btc_alignment": 63, "oi_sanity": 63,
            "mtf_depth": 63, "liquidity": 63, "volatility": 63}
    full = _layers(real)  # cb_premium+ecosystem default 50, TANPA no_data
    agg = av._aggregate_gate(full)
    assert agg["total_score"] == 59
    assert agg["gate"] == "SOFT_BLOCK"


# ── no_data exclusion TIDAK menyelamatkan sinyal jelek ──

def test_no_data_does_not_rescue_bad_signal():
    real = {"htf_trend": 35, "btc_alignment": 35, "oi_sanity": 35,
            "mtf_depth": 35, "liquidity": 35, "volatility": 35}
    res = _layers(real, no_data=("cb_premium", "ecosystem"))
    agg = av._aggregate_gate(res)
    # Real layers rata-rata 35 → tetap HARD_BLOCK (malah lebih ketat, krn
    # angka 50 dari layer no_data tak lagi mengangkat rata-rata).
    assert agg["total_score"] == 35
    assert agg["gate"] == "HARD_BLOCK"


# ── guard cakupan: data tipis → denominator penuh (konservatif) ──

def test_coverage_guard_thin_data_uses_full_denominator():
    # Cuma mtf_depth(10)+volatility(2)=12 bobot punya data → < GATE_MIN_COVERAGE.
    real = {"mtf_depth": 90, "volatility": 90}
    no_data = tuple(l for l in av.LAYER_WEIGHTS if l not in real)
    res = _layers(real, no_data=no_data)
    agg = av._aggregate_gate(res)
    # denom=100 → (90*12)/100 ≈ 11 → tidak bisa lolos dari data tipis.
    assert agg["total_score"] == 11
    assert agg["gate"] == "HARD_BLOCK"


# ── blocking layer tetap HARD_BLOCK walau skor tinggi ──

def test_blocking_layer_forces_hard_block():
    real = {lid: 90 for lid in av.LAYER_WEIGHTS}
    res = _layers(real, blocking=("htf_trend",))
    agg = av._aggregate_gate(res)
    assert agg["gate"] == "HARD_BLOCK"
    assert "htf_trend" in agg["hard_blocked"]


# ── semua layer punya data & bagus → PASS + boost ──

def test_all_good_passes_with_boost():
    res = _layers({lid: 85 for lid in av.LAYER_WEIGHTS})
    agg = av._aggregate_gate(res)
    assert agg["total_score"] == 85
    assert agg["gate"] == "PASS"
    assert agg["adjustment"] == 10
