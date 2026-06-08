"""Test saldo journal dibaca dari sheet (source of truth, tahan restart).

Bug: saldo disimpan di state lokal yang hilang tiap container Railway di-reclaim,
jadi trade pertama setelah redeploy mulai dari 0 — mengabaikan INITIAL BALANCE
yang sudah ada di sheet. Fix: hitung saldo dari sheet (Σ INITIAL BALANCE + Σ PnL).
"""
import trade_journal as tj


class _FakeWS:
    def __init__(self, records):
        self._records = records

    def get_all_records(self):
        return self._records


class _FakeSheet:
    def __init__(self, balance, trades=None):
        self._ws = {"Balance": _FakeWS(balance)}
        if trades is not None:
            self._ws["Trades"] = _FakeWS(trades)

    def worksheet(self, name):
        if name in self._ws:
            return self._ws[name]
        raise Exception(f"worksheet {name} tidak ada")


def test_balance_initial_plus_all_trades(monkeypatch):
    sheet = _FakeSheet(
        balance=[{"Event": "INITIAL BALANCE", "Balance After (USDT)": 13.37}],
        trades=[{"PnL (USDT)": 2.97}, {"PnL (USDT)": -0.67},
                {"PnL (USDT)": -0.46}, {"PnL (USDT)": 8.75}],
    )
    monkeypatch.setattr(tj, "_get_sheet", lambda: sheet)
    assert tj._compute_balance_from_sheet() == 23.96  # 13.37 + 2.97 - 0.67 - 0.46 + 8.75


def test_first_trade_does_not_ignore_set_balance(monkeypatch):
    # Replikasi bug aslinya: INITIAL 13.37, satu WIN +2.97 → harus 16.34 (bukan 2.97)
    sheet = _FakeSheet(
        balance=[{"Event": "INITIAL BALANCE", "Balance After (USDT)": 13.37}],
        trades=[{"PnL (USDT)": 2.97}],
    )
    monkeypatch.setattr(tj, "_get_sheet", lambda: sheet)
    monkeypatch.setattr(tj, "_load_state",
                        lambda: {"current_balance": 0.0, "total_trades": 0})
    monkeypatch.setattr(tj, "_save_state", lambda s: None)
    assert tj.get_current_balance() == 16.34


def test_falls_back_to_local_state_without_sheet(monkeypatch):
    monkeypatch.setattr(tj, "_get_sheet", lambda: None)
    monkeypatch.setattr(tj, "_load_state", lambda: {"current_balance": 99.0})
    assert tj.get_current_balance() == 99.0


def test_only_trades_no_initial(monkeypatch):
    sheet = _FakeSheet(balance=[], trades=[{"PnL (USDT)": 5.0}, {"PnL (USDT)": -1.5}])
    monkeypatch.setattr(tj, "_get_sheet", lambda: sheet)
    assert tj._compute_balance_from_sheet() == 3.5


def test_missing_trades_sheet_uses_initial_only(monkeypatch):
    sheet = _FakeSheet(balance=[{"Event": "INITIAL BALANCE", "Balance After (USDT)": 10.0}])
    monkeypatch.setattr(tj, "_get_sheet", lambda: sheet)
    assert tj._compute_balance_from_sheet() == 10.0


def test_ignores_non_numeric_pnl(monkeypatch):
    sheet = _FakeSheet(
        balance=[{"Event": "INITIAL BALANCE", "Balance After (USDT)": 10.0}],
        trades=[{"PnL (USDT)": 2.0}, {"PnL (USDT)": ""}, {"PnL (USDT)": None}],
    )
    monkeypatch.setattr(tj, "_get_sheet", lambda: sheet)
    assert tj._compute_balance_from_sheet() == 12.0
