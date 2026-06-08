"""Test auto-lesson dari trade journal (record_trade_journal_lesson).

Tiap trade yang masuk journal otomatis jadi lesson yang ngefeed konteks sinyal,
jadi hasil trade ASLI ikut dipelajari — bukan cuma sinyal bot sendiri.
"""
import learning_engine as le


def _isolate(tmp_path, monkeypatch):
    """Arahkan lessons.json ke tmp dir biar tidak menyentuh data asli."""
    monkeypatch.setattr(le, "LESSONS_FILE", str(tmp_path / "lessons.json"))


def test_win_trade_creates_good_lesson(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    l = le.record_trade_journal_lesson({
        "coin": "HYPE", "direction": "SHORT", "result": "WIN",
        "pnl_usdt": 5.2, "pnl_pct": 5.2, "note": "exit 35.2",
    })
    assert l["outcome"] == "good"
    assert l["source_type"] == "trade_journal"
    assert "HYPE" in l["tags"] and "SHORT" in l["tags"]
    assert "WIN HYPE SHORT" in l["rule"]
    assert "exit 35.2" in l["rule"]


def test_loss_trade_creates_bad_lesson(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    l = le.record_trade_journal_lesson({
        "coin": "SOL", "direction": "LONG", "result": "LOSS",
        "pnl_usdt": -3.1, "pnl_pct": -3.1, "note": "",
    })
    assert l["outcome"] == "bad"
    assert "LOSS SOL LONG" in l["rule"]


def test_breakeven_is_neutral(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    l = le.record_trade_journal_lesson({
        "coin": "BTC", "direction": "LONG", "result": "BREAKEVEN",
        "pnl_usdt": 0, "pnl_pct": 0,
    })
    assert l["outcome"] == "neutral"


def test_missing_coin_or_direction_skipped(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert le.record_trade_journal_lesson({"coin": "", "direction": "LONG"}) == {}
    assert le.record_trade_journal_lesson({"coin": "BTC", "direction": "??"}) == {}


def test_lesson_feeds_signal_prompt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    le.record_trade_journal_lesson({
        "coin": "SOL", "direction": "LONG", "result": "LOSS",
        "pnl_usdt": -3.1, "pnl_pct": -3.1, "note": "kena SL",
    })
    prompt = le.get_lessons_for_prompt(agent_type="GENERAL")
    assert prompt is not None
    assert "SOL LONG" in prompt


def test_usdt_suffix_stripped_from_coin(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    l = le.record_trade_journal_lesson({
        "coin": "HYPEUSDT", "direction": "SHORT", "result": "WIN",
        "pnl_usdt": 1.0, "pnl_pct": 1.0,
    })
    assert "HYPE" in l["tags"]
    assert "HYPEUSDT" not in l["tags"]
