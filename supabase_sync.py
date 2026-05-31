#!/usr/bin/env python3
"""
SUPABASE SYNC MODULE
====================
Kirim data trade & balance ke Supabase via REST API.
Tidak butuh private key — cukup SUPABASE_URL + SUPABASE_ANON_KEY.

Setup Supabase:
  1. Buat project di supabase.com (gratis)
  2. Jalankan SQL di bawah di SQL Editor Supabase
  3. Set env vars: SUPABASE_URL dan SUPABASE_ANON_KEY

SQL untuk buat tabel (jalankan sekali di Supabase SQL Editor):
─────────────────────────────────────────────────────────────
create table if not exists trades (
  id          bigint primary key,
  ts          text,
  coin        text,
  direction   text,
  entry_price float,
  margin_usdt float,
  leverage    int,
  position_size float,
  pnl_usdt    float,
  pnl_pct     float,
  result      text,
  note        text,
  balance_after float,
  created_at  timestamptz default now()
);

create table if not exists balance_log (
  id           bigserial primary key,
  ts           text,
  event        text,
  amount       float,
  balance_after float,
  note         text,
  created_at   timestamptz default now()
);

-- Izinkan akses publik (anon key bisa baca & tulis)
alter table trades    enable row level security;
alter table balance_log enable row level security;

create policy "public read trades"    on trades        for select using (true);
create policy "public insert trades"  on trades        for insert with check (true);
create policy "public read balance"   on balance_log   for select using (true);
create policy "public insert balance" on balance_log   for insert with check (true);
─────────────────────────────────────────────────────────────
"""

import os
import logging
import requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("supabase_sync")

SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

def _enabled():
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)

def _headers():
    return {
        "apikey":        SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }

def _post(table: str, data: dict) -> bool:
    if not _enabled():
        return False
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=_headers(),
            json=data,
            timeout=8,
        )
        if r.status_code in (200, 201):
            return True
        log.warning(f"supabase {table} insert {r.status_code}: {r.text[:120]}")
        return False
    except Exception as e:
        log.warning(f"supabase {table} error: {e}")
        return False


def push_signal(signal: dict) -> bool:
    """
    Simpan sinyal ke Supabase agar bisa ditampilkan di website.

    SQL untuk buat tabel (jalankan sekali di Supabase SQL Editor):
    ─────────────────────────────────────────────────────────────
    create table if not exists signals (
      id           bigint primary key,
      ts           text,
      coin         text,
      signal_type  text,
      direction    text,
      entry_price  float,
      tp           float,
      sl           float,
      score        float,
      confidence   text,
      reason       text,
      created_at   timestamptz default now()
    );
    alter table signals enable row level security;
    create policy "public read signals"   on signals for select using (true);
    create policy "public insert signals" on signals for insert with check (true);
    ─────────────────────────────────────────────────────────────
    """
    import time
    WIB = timezone(timedelta(hours=7))
    payload = {
        "id":          int(time.time() * 1000),
        "ts":          signal.get("ts", datetime.now(WIB).isoformat()),
        "coin":        signal.get("coin", ""),
        "signal_type": signal.get("signal_type", "SETUP"),
        "direction":   signal.get("direction", ""),
        "entry_price": signal.get("entry_price", 0),
        "tp":          signal.get("tp", 0),
        "sl":          signal.get("sl", 0),
        "score":       signal.get("score", 0),
        "confidence":  signal.get("confidence", ""),
        "reason":      signal.get("reason", ""),
    }
    ok = _post("signals", payload)
    if ok:
        log.info(f"supabase: signal {signal.get('coin')} {signal.get('direction')} synced")
    return ok


def fetch_signals(limit: int = 50) -> list:
    """Ambil sinyal terbaru dari Supabase (untuk website)."""
    if not _enabled():
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/signals",
            headers={**_headers(), "Prefer": ""},
            params={"select": "*", "order": "created_at.desc", "limit": limit},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        log.warning(f"supabase fetch_signals {r.status_code}: {r.text[:120]}")
        return []
    except Exception as e:
        log.warning(f"supabase fetch_signals error: {e}")
        return []


def push_trade(trade: dict) -> bool:
    """
    Panggil setelah log_trade() berhasil.
    trade = dict yang dikembalikan oleh log_trade()
    """
    import time
    payload = {
        "id":           int(time.time() * 1000),
        "ts":           trade.get("ts", ""),
        "coin":         trade.get("coin", ""),
        "direction":    trade.get("direction", ""),
        "entry_price":  trade.get("entry", 0),
        "margin_usdt":  trade.get("margin", 0),
        "leverage":     trade.get("leverage", 1),
        "position_size":trade.get("position_size", 0),
        "pnl_usdt":     trade.get("pnl_usdt", 0),
        "pnl_pct":      trade.get("pnl_pct", 0),
        "result":       trade.get("result", ""),
        "note":         trade.get("note", ""),
        "balance_after":trade.get("balance_after", 0),
    }
    ok = _post("trades", payload)
    if ok:
        log.info(f"supabase: trade {trade.get('coin')} {trade.get('direction')} synced")
    return ok


def push_balance(event: str, amount: float, balance_after: float, note: str = "") -> bool:
    """
    Panggil setelah set_initial_balance() atau update balance.
    """
    WIB = timezone(timedelta(hours=7))
    payload = {
        "ts":           datetime.now(WIB).isoformat(),
        "event":        event,
        "amount":       amount,
        "balance_after":balance_after,
        "note":         note,
    }
    return _post("balance_log", payload)


def fetch_trades(limit: int = 200) -> list:
    """Ambil semua trade dari Supabase (untuk Streamlit)."""
    if not _enabled():
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            headers={**_headers(), "Prefer": ""},
            params={"select": "*", "order": "created_at.desc", "limit": limit},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        log.warning(f"supabase fetch_trades {r.status_code}: {r.text[:120]}")
        return []
    except Exception as e:
        log.warning(f"supabase fetch_trades error: {e}")
        return []


def fetch_balance_log(limit: int = 500) -> list:
    """Ambil riwayat balance dari Supabase."""
    if not _enabled():
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/balance_log",
            headers={**_headers(), "Prefer": ""},
            params={"select": "*", "order": "created_at.asc", "limit": limit},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        return []
    except Exception as e:
        log.warning(f"supabase fetch_balance error: {e}")
        return []


def is_connected() -> bool:
    """Cek koneksi ke Supabase."""
    if not _enabled():
        return False
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            headers={**_headers(), "Prefer": ""},
            params={"select": "id", "limit": 1},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False
