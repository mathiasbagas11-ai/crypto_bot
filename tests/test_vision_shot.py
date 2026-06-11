"""Test pembacaan screenshot trade (/logshot) — error surfacing + downscale.

Konteks: screenshot order-details gagal dibaca tanpa keterangan jelas.
Sekarang:
- _extract_shot_json mengembalikan (data, error) sehingga bot bisa kasih tau
  alasan gagal (mis. "API 400", "rate limit").
- _prepare_image_for_vision mengecilkan gambar yang melebihi limit base64 Groq.
"""
import io
import base64

import pytest
from PIL import Image

import crypto_screening_bot_v13 as bot


def _png_bytes(w, h, noise=False):
    if noise:
        import os
        im = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    else:
        im = Image.new("RGB", (w, h), (10, 20, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ── _prepare_image_for_vision ──────────────────────────────

def test_small_image_passed_through():
    raw = _png_bytes(200, 200)
    b64, mime = bot._prepare_image_for_vision(raw, "image/png")
    assert mime == "image/png"
    assert b64 == base64.b64encode(raw).decode("ascii")


def test_oversized_image_downscaled_under_limit():
    raw = _png_bytes(4000, 4000, noise=True)  # b64 jauh di atas limit
    assert len(base64.b64encode(raw)) > bot._VISION_MAX_B64_BYTES
    b64, mime = bot._prepare_image_for_vision(raw, "image/png")
    assert mime == "image/jpeg"
    assert len(b64) <= bot._VISION_MAX_B64_BYTES


# ── _extract_shot_json error surfacing ─────────────────────

def test_extract_returns_error_when_vision_fails(monkeypatch):
    monkeypatch.setattr(bot, "_groq_vision_request",
                        lambda *a, **k: ("", "API 400: bad model"))
    # matikan fallback supaya error utama yang muncul
    monkeypatch.setattr(bot, "GROQ_VISION_MODEL_FB", bot.GROQ_VISION_MODEL)
    data, err = bot._extract_shot_json("x", "image/png")
    assert data == {}
    assert "400" in err


def test_extract_parses_valid_json(monkeypatch):
    payload = '{"coin":"HYPE","direction":"SHORT","entry_price":35.2,"realized_pnl":-4.1}'
    monkeypatch.setattr(bot, "_groq_vision_request", lambda *a, **k: (payload, ""))
    data, err = bot._extract_shot_json("x", "image/png")
    assert err == ""
    assert data["coin"] == "HYPE"
    assert data["direction"] == "SHORT"


def test_extract_strips_code_fence(monkeypatch):
    payload = '```json\n{"coin":"BTC","direction":"LONG","entry_price":95000}\n```'
    monkeypatch.setattr(bot, "_groq_vision_request", lambda *a, **k: (payload, ""))
    data, err = bot._extract_shot_json("x", "image/png")
    assert err == ""
    assert data["coin"] == "BTC"


def test_extract_tries_fallback_model(monkeypatch):
    calls = []

    def fake(image_b64, prompt, mime="image/jpeg", max_tokens=700,
             temperature=0.0, model=None):
        calls.append(model or bot.GROQ_VISION_MODEL)
        # model utama gagal, fallback sukses
        if len(calls) == 1:
            return "", "API 500"
        return '{"coin":"ETH","direction":"LONG","entry_price":3200}', ""

    monkeypatch.setattr(bot, "_groq_vision_request", fake)
    monkeypatch.setattr(bot, "GROQ_VISION_MODEL_FB", "fallback-model-xyz")
    data, err = bot._extract_shot_json("x", "image/png")
    assert err == ""
    assert data["coin"] == "ETH"
    assert len(calls) == 2  # utama + fallback
