"""Tier 3 tests — rule-based feedback parser (the no-AI fallback).

`_parse_feedback_rule_based` turns free-text user feedback (mixed ID/EN) into a
structured outcome/direction/conditions/lesson. It is the deterministic
fallback when Gemini is unavailable, so its keyword and condition matching is
worth pinning down.
"""
import feedback_engine as fe


def test_bad_long_with_condition():
    # NB: avoid words like "turun" (a SHORT keyword) so direction stays LONG.
    r = fe._parse_feedback_rule_based(
        "long BTC gagal gara-gara coinbase premium negatif")
    assert r["outcome"] == "BAD"
    assert r["direction"] == "LONG"
    assert "BTC" in r["coins"]
    assert "coinbase_premium" in r["conditions"]
    assert r["confidence"] == 0.80          # conditions present
    assert r["method"] == "rule_based"
    assert r["main_rule"].startswith("AVOID LONG")


def test_direction_ambiguous_when_long_and_short_words_collide():
    # "long" (LONG kw) + "turun" (SHORT kw) -> conflicting -> UNKNOWN.
    r = fe._parse_feedback_rule_based("BTC long gagal karena btc turun")
    assert r["outcome"] == "BAD"
    assert r["direction"] == "UNKNOWN"
    assert "btc_down" in r["conditions"]


def test_good_short_no_condition():
    r = fe._parse_feedback_rule_based("ETH short profit mantap kena tp")
    assert r["outcome"] == "GOOD"
    assert r["direction"] == "SHORT"
    assert "ETH" in r["coins"]
    assert r["conditions"] == []
    assert r["confidence"] == 0.55          # no conditions


def test_observation_when_no_outcome_keyword():
    r = fe._parse_feedback_rule_based("harga sideways aja belum jelas")
    assert r["outcome"] == "OBSERVATION"
    assert r["direction"] == "UNKNOWN"
    assert r["main_rule"].startswith("OBSERVATION")


def test_mixed_outcome_when_both_signals():
    r = fe._parse_feedback_rule_based("sinyal bagus tapi akhirnya rugi")
    assert r["outcome"] == "MIXED"


def test_multiple_conditions_detected():
    r = fe._parse_feedback_rule_based(
        "long gagal: funding tinggi dan coinbase premium negatif")
    assert r["outcome"] == "BAD"
    assert "funding_high" in r["conditions"]
    assert "coinbase_premium" in r["conditions"]
    assert len(r["sub_rules"]) >= 2
