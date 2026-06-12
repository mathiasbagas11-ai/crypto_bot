"""Tests — Hermes final arbiter & exit-liquidity guard.

Hermes adalah agent ke-3 (pengambil keputusan ala-manusia) setelah debat
Bull/Bear/Head-Trader. Mandat intinya: jangan biarkan user jadi EXIT
LIQUIDITY. Tes ini mengunci heuristik deterministik (yang jalan tanpa API
key) dan guardrail verdict.
"""
import hermes_agent as h


# ── _num parser ─────────────────────────────────────────────

def test_num_parses_strings_and_pct():
    assert h._num("0.052%") == 0.052
    assert h._num("+12.3") == 12.3
    assert h._num("N/A") is None
    assert h._num(None) is None
    assert h._num(1.5) == 1.5


# ── _downgrade selalu pilih yang lebih konservatif ──────────

def test_downgrade_picks_more_conservative():
    assert h._downgrade("CONFIRM", "CAUTION") == "CAUTION"
    assert h._downgrade("SKIP", "CONFIRM") == "SKIP"
    assert h._downgrade("CAUTION", "SKIP") == "SKIP"
    assert h._downgrade("CONFIRM", "CONFIRM") == "CONFIRM"


# ── assess_exit_liquidity — LONG crowded = bahaya ───────────

def test_exitliq_long_crowded_is_high():
    risk = {
        "funding": "0.12%",      # long bayar mahal
        "ls_ratio": 3.0,         # retail max long
        "euphoria": True,        # emosi puncak
        "recent_move_pct": 18,   # sudah pump jauh
        "oi_change_pct": 20,
        "extended": True,
    }
    res = h.assess_exit_liquidity(risk, is_long=True)
    assert res["score"] >= h.EXITLIQ_SKIP
    assert res["level"] == "HIGH"
    assert res["flags"]


def test_exitliq_clean_setup_is_none():
    risk = {"funding": "0.005%", "ls_ratio": 1.0, "euphoria": False}
    res = h.assess_exit_liquidity(risk, is_long=True)
    assert res["score"] == 0
    assert res["level"] == "NONE"


def test_exitliq_short_crowded_is_flagged():
    risk = {"funding": "-0.10%", "ls_ratio": 0.35, "recent_move_pct": -16}
    res = h.assess_exit_liquidity(risk, is_long=False)
    assert res["score"] >= h.EXITLIQ_CAUTION
    assert any("short" in f.lower() or "dump" in f.lower() for f in res["flags"])


def test_exitliq_handles_missing_and_na():
    # Tidak boleh meledak walau semua data 'N/A'/kosong.
    res = h.assess_exit_liquidity(
        {"funding": "N/A", "ls_ratio": "N/A", "oi_change_pct": "N/A"},
        is_long=True,
    )
    assert res["score"] == 0


# ── final_arbiter heuristik (tanpa API key) memveto exit-liq ─

def test_final_arbiter_heuristic_vetoes_high_exitliq(monkeypatch):
    # Pastikan tidak ada API key → jalur heuristik.
    monkeypatch.setattr(h, "HERMES_API_KEY", "")
    monkeypatch.setattr(h, "HERMES_EXITLIQ_VETO", True)
    risk = {"funding": "0.12%", "ls_ratio": 3.0, "euphoria": True,
            "recent_move_pct": 18, "oi_change_pct": 20, "extended": True}
    out = h.final_arbiter(
        setup_block="X", bull_case="b", bear_case="r",
        judge_verdict="CONFIRM", judge_reason="ok",
        coin="X", direction="LONG", is_long=True, risk_signals=risk,
    )
    assert out is not None
    assert out["source"] == "heuristic"
    assert out["verdict"] == "SKIP"
    assert out["exit_liquidity"] is True


def test_final_arbiter_heuristic_passes_clean_setup(monkeypatch):
    monkeypatch.setattr(h, "HERMES_API_KEY", "")
    monkeypatch.setattr(h, "HERMES_EXITLIQ_VETO", True)
    risk = {"funding": "0.005%", "ls_ratio": 1.1, "euphoria": False}
    out = h.final_arbiter(
        setup_block="X", bull_case="b", bear_case="r",
        judge_verdict="CONFIRM", judge_reason="ok",
        coin="X", direction="LONG", is_long=True, risk_signals=risk,
    )
    assert out["verdict"] == "CONFIRM"
    assert out["exit_liquidity"] is False


def test_final_arbiter_inactive_returns_none(monkeypatch):
    monkeypatch.setattr(h, "HERMES_API_KEY", "")
    monkeypatch.setattr(h, "HERMES_ENDPOINT_SET", False)
    monkeypatch.setattr(h, "HERMES_ENABLED", False)
    monkeypatch.setattr(h, "HERMES_EXITLIQ_VETO", False)
    out = h.final_arbiter(
        setup_block="X", bull_case="b", bear_case="r",
        judge_verdict="CONFIRM", judge_reason="ok",
        coin="X", direction="LONG", is_long=True, risk_signals={},
    )
    assert out is None


# ── self-host: endpoint lokal tanpa API key tetap "available" ───

def test_available_with_local_endpoint_no_key(monkeypatch):
    # Mode self-host: URL di-override, key kosong → tetap bisa dipanggil.
    monkeypatch.setattr(h, "HERMES_API_KEY", "")
    monkeypatch.setattr(h, "HERMES_ENDPOINT_SET", True)
    monkeypatch.setattr(h, "HERMES_ENABLED", True)
    assert h.is_available() is True
    assert h.is_active() is True


def test_hermes_call_omits_auth_header_when_no_key(monkeypatch):
    # Tanpa key, panggilan tidak boleh kirim header Authorization.
    monkeypatch.setattr(h, "HERMES_API_KEY", "")
    monkeypatch.setattr(h, "HERMES_ENDPOINT_SET", True)
    monkeypatch.setattr(h, "HERMES_API_URL", "http://localhost:8080/v1/chat/completions")

    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(h.requests, "post", _fake_post)
    out = h._hermes_call([{"role": "user", "content": "hi"}])
    assert out == "ok"
    assert "Authorization" not in captured["headers"]
    assert captured["url"].startswith("http://localhost")


def test_hermes_call_returns_empty_when_endpoint_unset(monkeypatch):
    monkeypatch.setattr(h, "HERMES_ENDPOINT_SET", False)
    assert h._hermes_call([{"role": "user", "content": "hi"}]) == ""
