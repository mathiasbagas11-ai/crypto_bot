"""Tier 2 tests — confirmed_signal master-score aggregation.

`compute_master_score` is the gate that decides what gets sent as a confirmed
signal: it weights five detectors, penalises conflict and thin conviction, and
classifies confidence. `_persistence_adjustment` rewards signals that persist
across scans. Both are pure logic; the optional ecosystem/coinbase overlays
(which would hit the network) are stubbed out so results are deterministic.
"""
from datetime import datetime, timedelta, timezone

import pytest

import ecosystem_detector
import coinbase_premium
import confirmed_signal as cs


@pytest.fixture(autouse=True)
def no_external_overlays(monkeypatch):
    # Skip the ecosystem-season block entirely (no eco => block is a no-op)
    # and neutralise the coinbase-premium contribution.
    monkeypatch.setattr(ecosystem_detector, "get_coin_ecosystem", lambda *a, **k: None)
    monkeypatch.setattr(coinbase_premium, "get_premium_master_contribution",
                        lambda d: {}, raising=False)


# ── compute_master_score ─────────────────────────────────────────

def test_no_signals_returns_none_direction():
    r = cs.compute_master_score("X", {}, {}, {}, {}, {}, {})
    assert r["direction"] == "NONE"
    assert r["master_score"] == 0
    assert r["signal_count"] == 0
    assert r["confidence"] == "LOW"


def test_coinbase_premium_contributes_to_master_score(monkeypatch):
    # Regression: dulu blok CB premium memakai `direction` sebelum di-assign →
    # UnboundLocalError yang ketelan except → kontribusi institutional tidak
    # pernah masuk. Sekarang harus benar-benar menambah weighted_long.
    monkeypatch.setattr(
        coinbase_premium, "get_premium_master_contribution",
        lambda d: {"weighted_long_add": 8.0, "weighted_short_add": 0.0,
                   "reason": "CB test", "premium_pct": 0.2},
        raising=False)
    r = cs.compute_master_score(
        "BTCUSDT",
        confluence={"direction": "PUMP", "score": 80, "level": "STRONG"},
        prepump={}, predump={}, scalp={}, swing={}, oi_data={},
    )
    # confluence 80 → w=24 long; CB premium +8 → weighted_long = 32.
    assert r["direction"] == "LONG"
    assert r["weighted_long"] == pytest.approx(32.0)


def test_strong_aligned_long_is_high_conviction():
    # confluence 80 (w=24) + prepump 70 (w=17.5) + scalp 65 (w=6.5) = 48 long.
    r = cs.compute_master_score(
        "BTCUSDT",
        confluence={"direction": "PUMP", "score": 80, "level": "STRONG"},
        prepump={"total_score": 70, "label": "x"},
        predump={},
        scalp={"score": 65, "direction": "LONG"},
        swing={},
        oi_data={},
    )
    assert r["direction"] == "LONG"
    assert r["weighted_long"] == pytest.approx(48.0)
    assert r["weighted_short"] == pytest.approx(0.0)
    assert r["master_score"] == 100          # ratio 100% + bonus, capped
    assert r["signal_count"] == 3            # three agreeing votes
    assert r["agreement_count"] == 3
    assert r["conflict_count"] == 0
    assert r["confidence"] == "HIGH"


def test_conflicting_signals_get_penalised():
    # confluence LONG 60 (w=18) vs predump SHORT 60 (w=15): both fire ->
    # conflict ratio 0.83 penalty, plus <2 strong same-direction comps penalty.
    r = cs.compute_master_score(
        "ETHUSDT",
        confluence={"direction": "PUMP", "score": 60, "level": "MODERATE"},
        prepump={},
        predump={"total_score": 60, "label": "y"},
        scalp={},
        swing={},
        oi_data={},
    )
    assert r["direction"] == "LONG"          # long edges out on weight
    assert r["weighted_long"] == pytest.approx(18.0)
    assert r["weighted_short"] == pytest.approx(15.0)
    assert r["master_score"] == 30           # 63 -> *0.75 -> *0.65
    assert r["conflict_count"] == 2
    assert r["confidence"] == "LOW"


def test_funding_extreme_negative_boosts_long():
    r = cs.compute_master_score(
        "BTCUSDT",
        confluence={"direction": "PUMP", "score": 60, "level": "MODERATE"},
        prepump={}, predump={}, scalp={}, swing={},
        oi_data={"funding_rate": -0.05, "ls_bias": "BALANCED"},
    )
    # base confluence weight 18 + 5 funding boost.
    assert r["weighted_long"] == pytest.approx(23.0)
    assert r["direction"] == "LONG"


# ── _persistence_adjustment ──────────────────────────────────────

def _seed(monkeypatch, **prev):
    monkeypatch.setattr(cs, "_prev_scores", {"SYM": prev} if prev else {})


def test_persistence_no_history_is_zero(monkeypatch):
    _seed(monkeypatch)
    assert cs._persistence_adjustment("SYM", "LONG", 70) == 0


def test_persistence_consistent_strong_bonus(monkeypatch):
    _seed(monkeypatch, score=80, direction="LONG", ts=datetime.now(timezone.utc))
    assert cs._persistence_adjustment("SYM", "LONG", 70) == 5


def test_persistence_consistent_moderate_neutral(monkeypatch):
    _seed(monkeypatch, score=55, direction="LONG", ts=datetime.now(timezone.utc))
    assert cs._persistence_adjustment("SYM", "LONG", 70) == 0


def test_persistence_sudden_spike_penalised(monkeypatch):
    _seed(monkeypatch, score=30, direction="LONG", ts=datetime.now(timezone.utc))
    assert cs._persistence_adjustment("SYM", "LONG", 70) == -12


def test_persistence_direction_flip_penalised(monkeypatch):
    _seed(monkeypatch, score=80, direction="SHORT", ts=datetime.now(timezone.utc))
    assert cs._persistence_adjustment("SYM", "LONG", 70) == -5


def test_persistence_stale_cache_ignored(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(minutes=60)
    _seed(monkeypatch, score=80, direction="LONG", ts=old)
    assert cs._persistence_adjustment("SYM", "LONG", 70) == 0
