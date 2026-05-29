"""Tier 3 tests — symbol normalization and OHLCV parsing.

`resolve_symbol` normalises arbitrary user input to a USDT pair and walks the
exchange fallback chain; the `_klines_*` functions normalise each exchange's
quirky candle format (Bybit/OKX newest-first, Gate seconds, nested payloads)
into one uniform schema. Network is mocked so only the pure parsing/routing
logic is exercised.
"""
import pytest

import exchange_resolver as er


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


# ── resolve_symbol normalization + fallback ──────────────────────

@pytest.fixture
def only_bybit(monkeypatch):
    monkeypatch.setattr(er, "_cache_get", lambda s: None)
    monkeypatch.setattr(er, "_cache_set", lambda s, e: None)
    monkeypatch.setattr(er, "_CHECKERS", {
        "binance_futures": lambda s: False,
        "binance_spot": lambda s: False,
        "bybit": lambda s: True,
        "okx": lambda s: False,
        "gate": lambda s: False,
    })


@pytest.mark.parametrize("user_input,expected", [
    ("btc", "BTCUSDT"),
    ("ETH/USDT", "ETHUSDT"),
    ("sol-usdt", "SOLUSDT"),
    ("DOGEUSD", "DOGEUSDT"),
])
def test_resolve_symbol_normalization(only_bybit, user_input, expected):
    r = er.resolve_symbol(user_input)
    assert r["symbol"] == expected
    assert r["exchange"] == "bybit"
    assert r["exchange_label"] == "Bybit"
    assert r["has_futures"] is True


@pytest.mark.xfail(strict=True, reason=(
    "BUG: resolve_symbol chains .replace('PERP','USDT').replace('USD','USDT'); "
    "for PERP input the second replace also matches the 'USD' inside 'USDT', "
    "so 'XRPPERP' -> 'XRPUSDT' -> 'XRPUSDTT' and never resolves."))
def test_resolve_symbol_perp_normalization(only_bybit):
    assert er.resolve_symbol("xrpperp")["symbol"] == "XRPUSDT"


def test_resolve_symbol_not_found(monkeypatch):
    monkeypatch.setattr(er, "_cache_get", lambda s: None)
    monkeypatch.setattr(er, "_cache_set", lambda s, e: None)
    monkeypatch.setattr(er, "_CHECKERS",
                        {k: (lambda s: False) for k in er.EXCHANGE_PRIORITY})
    assert er.resolve_symbol("nonexistentcoin") is None


# ── klines parsing / normalization ───────────────────────────────

def test_klines_binance_maps_array_to_dict(monkeypatch):
    monkeypatch.setattr(er.requests, "get",
                        lambda *a, **k: FakeResp([[1000, "100", "110", "90", "105", "1.5"]]))
    out = er._klines_binance_futures("BTCUSDT", "1h")
    assert out == [{"time": 1000, "open": 100.0, "high": 110.0,
                    "low": 90.0, "close": 105.0, "volume": 1.5}]


def test_klines_bybit_reverses_newest_first(monkeypatch):
    payload = {"result": {"list": [
        ["2000", "2", "2", "2", "2", "2"],   # newest first
        ["1000", "1", "1", "1", "1", "1"],
    ]}}
    monkeypatch.setattr(er.requests, "get", lambda *a, **k: FakeResp(payload))
    out = er._klines_bybit("BTCUSDT", "1h")
    assert [c["time"] for c in out] == [1000, 2000]  # reversed to oldest-first


def test_klines_okx_reverses_newest_first(monkeypatch):
    payload = {"data": [
        ["2000", "2", "2", "2", "2", "2"],
        ["1000", "1", "1", "1", "1", "1"],
    ]}
    monkeypatch.setattr(er.requests, "get", lambda *a, **k: FakeResp(payload))
    out = er._klines_okx("BTCUSDT", "1h")
    assert [c["time"] for c in out] == [1000, 2000]


def test_klines_gate_scales_seconds_to_ms(monkeypatch):
    payload = [{"t": 1, "o": "100", "h": "110", "l": "90", "c": "105", "v": "2"}]
    monkeypatch.setattr(er.requests, "get", lambda *a, **k: FakeResp(payload))
    out = er._klines_gate("BTCUSDT", "1h")
    assert out[0]["time"] == 1000  # seconds * 1000
    assert out[0]["close"] == 105.0


def test_klines_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(er.requests, "get", lambda *a, **k: FakeResp([], status=500))
    assert er._klines_binance_futures("BTCUSDT", "1h") is None
