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
    assert any("tidak merespons" in m for m in sends)


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
