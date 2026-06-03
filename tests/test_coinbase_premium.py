"""Tier 2 tests — coinbase_premium directional scoring.

`get_premium_score` turns the Coinbase spot-vs-perp premium into a 0-100
directional score that feeds the confirmed-signal gate. The OVEREXTENDED case
must NOT be scored as a full STRONG conviction (mean-reversion risk), and
overextension must reduce the master-score contribution too.
"""
import pytest

import coinbase_premium as cb


def _pd(signal, premium_pct=0.25, strength=60, momentum="STABLE"):
    return {"available": True, "premium_pct": premium_pct,
            "signal": signal, "strength": strength, "momentum": momentum}


def test_strong_long_is_high_score():
    r = cb.get_premium_score("LONG", _pd("STRONG_LONG", strength=90))
    assert r["score"] >= 90


def test_overextended_long_is_not_strong_score():
    # Regression: dulu "STRONG_LONG" in "OVEREXTENDED_STRONG_LONG" → score 90+.
    # Sekarang harus jatuh ke cabang mean-reversion (55), bukan konviksi penuh.
    r = cb.get_premium_score("LONG", _pd("OVEREXTENDED_STRONG_LONG"))
    assert r["score"] == 55


def test_overextended_short_is_not_strong_score():
    r = cb.get_premium_score("SHORT", _pd("OVEREXTENDED_STRONG_SHORT", premium_pct=-0.25))
    assert r["score"] == 55


def test_master_contribution_skips_overextended():
    # get_premium_master_contribution sudah meng-guard OVEREXTENDED — pastikan
    # tidak ada penambahan bobot directional saat overextended.
    import unittest.mock as mock
    with mock.patch.object(cb, "fetch_premium",
                           return_value=_pd("OVEREXTENDED_STRONG_LONG")):
        c = cb.get_premium_master_contribution("LONG")
    assert c["weighted_long_add"] == 0.0
