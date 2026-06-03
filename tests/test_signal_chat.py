"""Tests — signal discussion / trading-style learning (signal_chat).

Covers the pure explainability + prompt + parsing logic and the stateful
discussion/confirm/style flow. All file I/O is redirected to tmp_path and the
learning_engine side-write is stubbed so the real lessons.json is never touched.
Network/AI is injected as a fake callable.
"""
import json

import pytest

import signal_chat as sc


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect every signal_chat JSON store into tmp_path and stub the
    learning_engine push so tests stay hermetic."""
    monkeypatch.setattr(sc, "SIGNAL_MAP_FILE", str(tmp_path / "map.json"))
    monkeypatch.setattr(sc, "CONVO_FILE", str(tmp_path / "convos.json"))
    monkeypatch.setattr(sc, "STYLE_FILE", str(tmp_path / "style.json"))
    monkeypatch.setattr(sc, "HISTORY_FILE", str(tmp_path / "history.json"))
    import learning_engine
    monkeypatch.setattr(learning_engine, "add_manual_lesson",
                        lambda *a, **k: None, raising=False)
    return tmp_path


def _signal(direction="LONG", **over):
    sig = {
        "symbol": "BNBUSDT", "direction": direction,
        "master_score": 99, "confidence": "MEDIUM",
        "reasons": ["Confluence LONG (EXCELLENT, 76/100)", "L/S long-heavy"],
        "conflict_reasons": [],
        "component_scores": {
            "confluence": {"direction": "LONG", "score": 76, "weight": 22.8},
            "prepump": {"direction": "NONE", "score": 11, "weight": 0},
            "predump": {"direction": "NONE", "score": 32, "weight": 0},
            "scalp": {"direction": "NONE", "score": 37, "weight": 0},
            "swing": {"direction": "NONE", "score": 43, "weight": 0},
        },
        "trade": {"entry": 651.9, "sl": 647.8, "tp1": 663.4, "tp2": 670.9, "rr": 2.81},
    }
    sig.update(over)
    return sig


# ── classify_components / explain_signal ─────────────────────────

def test_classify_single_driver():
    cls = sc.classify_components(_signal())
    assert [d["key"] for d in cls["drivers"]] == ["confluence"]
    assert [d["key"] for d in cls["agree"]] == ["confluence"]
    assert cls["conflict"] == []
    assert {d["key"] for d in cls["neutral"]} == {"prepump", "predump", "scalp", "swing"}


def test_classify_detects_conflict():
    sig = _signal(direction="LONG", component_scores={
        "confluence": {"direction": "LONG", "score": 70, "weight": 21.0},
        "predump": {"direction": "SHORT", "score": 60, "weight": 15.0},
        "prepump": {"direction": "NONE", "score": 0, "weight": 0},
        "scalp": {"direction": "NONE", "score": 0, "weight": 0},
        "swing": {"direction": "NONE", "score": 0, "weight": 0},
    })
    cls = sc.classify_components(sig)
    assert [c["key"] for c in cls["conflict"]] == ["predump"]
    assert [c["key"] for c in cls["agree"]] == ["confluence"]
    assert {d["key"] for d in cls["drivers"]} == {"confluence", "predump"}


def test_explain_single_indicator_warns():
    text = sc.explain_signal(_signal())
    assert "BNBUSDT LONG" in text
    assert "1 indikator" in text  # warns about weak confirmation


def test_explain_multi_indicator_confident():
    sig = _signal(component_scores={
        "confluence": {"direction": "LONG", "score": 76, "weight": 22.8},
        "prepump": {"direction": "LONG", "score": 70, "weight": 17.5},
        "scalp": {"direction": "LONG", "score": 65, "weight": 6.5},
        "predump": {"direction": "NONE", "score": 0, "weight": 0},
        "swing": {"direction": "NONE", "score": 0, "weight": 0},
    })
    text = sc.explain_signal(sig)
    assert "3 indikator" in text


# ── parse_style_suggestion ───────────────────────────────────────

def test_parse_style_marker_extracted():
    clean, rule = sc.parse_style_suggestion("Oke masuk akal. [STYLE: suka entry retest]")
    assert clean == "Oke masuk akal."
    assert rule == "suka entry retest"


def test_parse_no_marker():
    clean, rule = sc.parse_style_suggestion("Cuma penjelasan biasa.")
    assert clean == "Cuma penjelasan biasa."
    assert rule is None


def test_build_discussion_prompt_grounds_on_signal():
    p = sc.build_discussion_prompt(_signal(), [], "kenapa long?", ["R:R min 2.5"])
    assert "BNBUSDT" in p
    assert "R:R min 2.5" in p
    assert "kenapa long?" in p
    assert "[STYLE:" in p  # instruction to emit marker present


# ── signal ↔ message mapping ─────────────────────────────────────

def test_register_and_get_signal(store):
    sc.register_signal_message("555", _signal())
    got = sc.get_signal_for_message("555")
    assert got["symbol"] == "BNBUSDT"
    assert sc.get_signal_for_message("does-not-exist") is None


def test_find_latest_signal(store):
    hist = [_signal(symbol="ETHUSDT"), _signal(symbol="SOLUSDT")]
    with open(sc.HISTORY_FILE, "w") as f:
        json.dump(hist, f)
    assert sc.find_latest_signal()["symbol"] == "SOLUSDT"
    assert sc.find_latest_signal("ETH")["symbol"] == "ETHUSDT"
    assert sc.find_latest_signal("DOGE") is None


# ── style store ──────────────────────────────────────────────────

def test_style_add_dedup_remove(store):
    assert sc.add_style_rule("suka entry retest") is True
    assert sc.add_style_rule("Suka Entry Retest") is False  # case-insensitive dup
    assert sc.add_style_rule("mau R:R minimal 2.5") is True
    assert sc.get_style_rules() == ["suka entry retest", "mau R:R minimal 2.5"]

    removed = sc.remove_style_rule(1)
    assert removed == "suka entry retest"
    assert sc.get_style_rules() == ["mau R:R minimal 2.5"]
    assert sc.remove_style_rule(99) is None


def test_add_empty_rule_rejected(store):
    assert sc.add_style_rule("   ") is False


# ── confirm answer helpers ───────────────────────────────────────

@pytest.mark.parametrize("word,expected", [
    ("ya", True), ("simpan", True), ("OK", True),
    ("skip", False), ("gak", False), ("batal", False),
])
def test_confirm_answer_classification(word, expected):
    assert sc.is_confirm_answer(word) is True
    assert sc._is_yes(word) is expected


def test_random_text_not_confirm():
    assert sc.is_confirm_answer("kenapa begitu?") is False


# ── orchestration: discussion reply ──────────────────────────────

def test_discussion_reply_unknown_message_not_handled(store):
    sends = []
    handled = sc.handle_discussion_reply(
        "nope", "halo", "chatA",
        ai_fn=lambda p: "x", send_fn=lambda m, c=None: sends.append(m))
    assert handled is False
    assert sends == []


def test_discussion_reply_full_flow(store):
    sc.register_signal_message("777", _signal())
    sends = []

    def ai_fn(prompt):
        return "Sinyal ini cuma ditopang confluence. [STYLE: mau R:R minimal 2.5]"

    handled = sc.handle_discussion_reply(
        "777", "kenapa long ini?", "chatA",
        ai_fn=ai_fn, send_fn=lambda m, c=None: sends.append(m))

    assert handled is True
    joined = "\n".join(sends)
    assert "BNBUSDT LONG" in joined          # deterministic explanation sent
    assert "cuma ditopang confluence" in joined  # AI reply sent
    assert "mau R:R minimal 2.5" in joined   # rule proposed for confirmation
    assert sc.get_pending_rule("chatA") == "mau R:R minimal 2.5"
    # The proposed rule is NOT saved until confirmed.
    assert sc.get_style_rules() == []


def test_discussion_reply_ai_silent(store):
    sc.register_signal_message("888", _signal())
    sends = []
    sc.handle_discussion_reply("888", "halo", "chatB",
                               ai_fn=lambda p: "", send_fn=lambda m, c=None: sends.append(m))
    assert any("error" in m.lower() for m in sends)


def test_discussion_reply_grounds_on_replied_text_when_unregistered(store):
    # No registered signal & no coin in history, but the bot's replied message
    # text is available -> should still ground the discussion on it.
    captured = {}
    sends = []

    def ai_fn(prompt):
        captured["prompt"] = prompt
        return "Gue suruh short Allo karena udah overextended di resistance."

    handled = sc.handle_discussion_reply(
        "unreg", "kenapa short padahal naik?", "chatC",
        ai_fn=ai_fn, send_fn=lambda m, c=None: sends.append(m),
        replied_text="ALLO SHORT — entry 1.2, overextended")

    assert handled is True
    assert "ALLO SHORT — entry 1.2" in captured["prompt"]   # replied text grounds prompt
    assert any("overextended" in m for m in sends)


def test_discussion_reply_finds_signal_by_coin_mention(store):
    import json
    with open(sc.HISTORY_FILE, "w") as f:
        json.dump([_signal()], f)  # BNBUSDT in history
    captured = {}
    sc.handle_discussion_reply(
        "unreg", "kenapa BNB long?", "chatD",
        ai_fn=lambda p: captured.setdefault("p", p) or "jawab",
        send_fn=lambda m, c=None: None,
        replied_text="")
    # Grounded on the BNB signal pulled from history (full component context).
    assert "BNBUSDT" in captured["p"]
    assert "Indikator pendorong" in captured["p"]


def test_persona_is_casual_and_grounded():
    p = sc.build_discussion_prompt(_signal(), [], "kenapa?", [], replied_text="ALLO short call")
    assert "gue/lo" in p.lower()           # casual persona
    assert "ALLO short call" in p          # replied text included
    assert "[STYLE:" in p


def test_followup_continues_active_discussion(store):
    sc.start_convo("chatE", None, _signal())  # sets context + last_active=now
    assert sc.is_discussion_active("chatE") is True
    sends = []
    handled = sc.handle_followup("terus gimana entrynya?", "chatE",
                                 ai_fn=lambda p: "masuk pas retest aja.",
                                 send_fn=lambda m, c=None: sends.append(m))
    assert handled is True
    assert any("retest" in m for m in sends)


def test_followup_not_active_returns_false(store):
    assert sc.handle_followup("halo", "chatNobody",
                              ai_fn=lambda p: "x", send_fn=lambda m, c=None: None) is False


# ── orchestration: confirm ───────────────────────────────────────

def test_confirm_yes_saves_rule(store):
    sc.set_pending_rule("chatA", "mau R:R minimal 2.5")
    sends = []
    handled = sc.handle_confirm("ya", "chatA", lambda m, c=None: sends.append(m))
    assert handled is True
    assert sc.get_style_rules() == ["mau R:R minimal 2.5"]
    assert sc.get_pending_rule("chatA") is None
    assert any("Tersimpan" in m for m in sends)


def test_confirm_skip_discards_rule(store):
    sc.set_pending_rule("chatA", "mau R:R minimal 2.5")
    sends = []
    handled = sc.handle_confirm("skip", "chatA", lambda m, c=None: sends.append(m))
    assert handled is True
    assert sc.get_style_rules() == []
    assert sc.get_pending_rule("chatA") is None


def test_confirm_without_pending_not_handled(store):
    handled = sc.handle_confirm("ya", "chatZ", lambda m, c=None: None)
    assert handled is False


# ── reflection → lesson memory ───────────────────────────────────

def test_parse_lesson_marker_extracted():
    clean, lesson = sc.parse_lesson_suggestion(
        "Iya bener, ini kena SL. [LESSON: jangan entry LONG pas funding ekstrem negatif]")
    assert "[LESSON" not in clean
    assert lesson == "jangan entry LONG pas funding ekstrem negatif"


def test_parse_lesson_no_marker():
    clean, lesson = sc.parse_lesson_suggestion("Cuma ngobrol biasa.")
    assert clean == "Cuma ngobrol biasa."
    assert lesson is None


def test_style_and_lesson_markers_both_stripped():
    raw = "Oke. [STYLE: suka retest] juga [LESSON: hindari TP kejauhan pas ranging]"
    clean, rule = sc.parse_style_suggestion(raw)
    clean, lesson = sc.parse_lesson_suggestion(clean)
    assert rule == "suka retest"
    assert lesson == "hindari TP kejauhan pas ranging"
    assert "[STYLE" not in clean and "[LESSON" not in clean


def test_looks_like_reflection_detects_outcome_talk():
    assert sc.looks_like_reflection("sinyal BNB tadi kena SL") is True
    assert sc.looks_like_reflection("harusnya TP nya jangan kejauhan") is True
    assert sc.looks_like_reflection("apa itu order block?") is False


def test_pending_lesson_state(store):
    assert sc.has_pending("chatA") is False
    sc.set_pending_lesson("chatA", "next time jangan begini")
    assert sc.get_pending_lesson("chatA") == "next time jangan begini"
    assert sc.has_pending_lesson("chatA") is True
    assert sc.has_pending("chatA") is True


def test_add_reflection_lesson_pushes_to_learning_engine(store, monkeypatch):
    captured = {}
    import learning_engine
    monkeypatch.setattr(learning_engine, "add_manual_lesson",
                        lambda rule, **k: captured.update({"rule": rule, **k}),
                        raising=False)
    ok = sc.add_reflection_lesson("jangan entry pas funding ekstrem",
                                  symbol="BNBUSDT", direction="LONG")
    assert ok is True
    assert captured["rule"] == "jangan entry pas funding ekstrem"
    assert captured["pinned"] is True          # feedback user → prioritas
    assert captured["role"] is None            # ikut ke semua prompt
    assert "reflection" in captured["tags"]
    assert "bnbusdt" in captured["tags"] and "long" in captured["tags"]


def test_confirm_yes_saves_lesson(store, monkeypatch):
    captured = {}
    import learning_engine
    monkeypatch.setattr(learning_engine, "add_manual_lesson",
                        lambda rule, **k: captured.update({"rule": rule}),
                        raising=False)
    sc.start_convo("chatA", None, {"symbol": "BNBUSDT", "direction": "LONG"})
    sc.set_pending_lesson("chatA", "jangan entry pas funding ekstrem")
    sends = []
    handled = sc.handle_confirm("ya", "chatA", lambda m, c=None: sends.append(m))
    assert handled is True
    assert sc.get_pending_lesson("chatA") is None
    assert captured["rule"] == "jangan entry pas funding ekstrem"
    assert any("memori" in m for m in sends)


def test_confirm_skip_discards_lesson(store):
    sc.set_pending_lesson("chatA", "jangan begini")
    sends = []
    handled = sc.handle_confirm("skip", "chatA", lambda m, c=None: sends.append(m))
    assert handled is True
    assert sc.get_pending_lesson("chatA") is None


# ── style engine: parse_style_to_prefs ───────────────────────────

@pytest.mark.parametrize("rule,key,expected", [
    ("mau R:R minimal 2.5", "min_rr", 2.5),
    ("rr minimal 3", "min_rr", 3.0),
    ("risk reward minimal 2", "min_rr", 2.0),
    ("cuma mau sinyal dengan score minimal 85", "min_score", 85),
    ("minimal 2 konfirmasi indikator", "min_indicators", 2),
    ("jangan entry kalau cuma 1 indikator", "min_indicators", 2),
    ("gue suka entry retest bukan market", "entry_style", "RETEST"),
    ("suka masuk market langsung", "entry_style", "MARKET"),
])
def test_parse_style_to_prefs(rule, key, expected):
    assert sc.parse_style_to_prefs([rule])[key] == expected


def test_parse_keeps_notes():
    rules = ["skip kalau btc lagi turun", "suka TP bertahap"]
    assert sc.parse_style_to_prefs(rules)["notes"] == rules


# ── style engine: apply_style_to_signal ──────────────────────────

def _trade_signal(direction="LONG", **trade):
    base = {"direction": direction, "entry": 100.0, "sl": 95.0,
            "tp1": 110.0, "tp2": 130.0}
    base.update(trade)
    return _signal(direction=direction, trade=base, master_score=72)


def test_apply_min_rr_extends_tp_long():
    sig = _trade_signal("LONG")  # risk 5, tp1 rr=2.0
    res = sc.apply_style_to_signal(sig, sc.parse_style_to_prefs(["R:R minimal 2.5"]))
    # 100 + 2.5*5 = 112.5; tp2 (rr 6) left alone.
    assert res["adjusted_trade"] == {"tp1": 112.5}


def test_apply_min_rr_extends_tp_short():
    sig = _trade_signal("SHORT", entry=100.0, sl=105.0, tp1=92.0, tp2=70.0)  # risk 5, tp1 rr=1.6
    res = sc.apply_style_to_signal(sig, sc.parse_style_to_prefs(["R:R minimal 2.5"]))
    # 100 - 2.5*5 = 87.5
    assert res["adjusted_trade"]["tp1"] == 87.5


def test_apply_min_rr_no_change_when_already_met():
    sig = _trade_signal("LONG", tp1=120.0)  # rr 4.0 already > 2.5
    res = sc.apply_style_to_signal(sig, sc.parse_style_to_prefs(["R:R minimal 2.5"]))
    assert "tp1" not in res["adjusted_trade"]


def test_apply_min_score_flags_skip():
    sig = _trade_signal("LONG")  # master_score 72
    res = sc.apply_style_to_signal(sig, sc.parse_style_to_prefs(["score minimal 80"]))
    assert res["suppress"] is True
    assert any("di bawah ambang" in w for w in res["warnings"])


def test_apply_min_indicators_flags_skip():
    sig = _trade_signal("LONG")  # single driver (confluence)
    res = sc.apply_style_to_signal(sig, sc.parse_style_to_prefs(["minimal 2 konfirmasi indikator"]))
    assert res["suppress"] is True


def test_apply_entry_style_retest_note():
    sig = _trade_signal("LONG")
    res = sc.apply_style_to_signal(sig, sc.parse_style_to_prefs(["suka entry retest"]))
    assert res["entry_note"] and "retest" in res["entry_note"].lower()


# ── style engine: build_signal_personalization (integration) ─────

def test_build_personalization_empty_without_rules(store):
    assert sc.build_signal_personalization(_trade_signal()) == ""


def test_build_personalization_with_rules(store):
    sc.add_style_rule("R:R minimal 2.5")
    block = sc.build_signal_personalization(_trade_signal("LONG"))
    assert "Disesuaikan gaya trading kamu" in block
    assert "112.5" in block  # adjusted TP shown
    assert "angka mekanis bot tetap di atas" in block
