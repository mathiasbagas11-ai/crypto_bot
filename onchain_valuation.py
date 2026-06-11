#!/usr/bin/env python3
"""
ON-CHAIN VALUATION AGENT — Fundamental Layer untuk Crypto
==========================================================
Analog "Fundamentals Analyst" ala hedge fund, tapi versi crypto: bukan
laporan keuangan, melainkan metrik on-chain & supply/valuation.

Tujuan: kasih DIMENSI FUNDAMENTAL yang selama ini hilang, lalu suntik ke
konteks debat AI (Bull vs Bear) supaya keputusan tidak murni teknikal.

Sumber data (GRATIS, tanpa API key):
  - CoinGecko  /coins/markets   → mcap, FDV, volume, supply, ATH
  - DefiLlama  /protocol, /tvl   → TVL & mcap/TVL (khusus token DeFi)

Metrik yang dihitung:
  • FDV ratio  (mcap / FDV)        → overhang unlock / dilusi masa depan
  • Vol/MCap                       → turnover, minat & likuiditas
  • Circulating supply %           → risiko dilusi
  • Jarak dari ATH (drawdown)      → posisi siklus
  • MCap/TVL (DeFi)                → valuasi vs penggunaan nyata
  • Tren TVL                       → protokol tumbuh / surut

Output utama:
  build_valuation_brief_for_ai(symbol, direction) -> str
      Ringkasan teks siap-suntik ke prompt debat. "" kalau data tak ada.

Pure logic (compute_/assess_/build_) bisa di-unit-test tanpa network.
"""

import logging
import math
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("onchain_valuation")

# ── Cache (fundamental bergerak lambat → cache agresif) ──────────────────────
_CACHE: dict[str, dict] = {}          # symbol → {"ts": epoch, "data": dict}
_CACHE_TTL = 3600                      # 1 jam

# CoinGecko symbol (UPPER, tanpa USDT) → coin id (untuk hindari tabrakan simbol)
_CG_ID_OVERRIDE: dict[str, str] = {
    "BTC": "bitcoin",      "ETH": "ethereum",   "SOL": "solana",
    "BNB": "binancecoin",  "XRP": "ripple",     "ADA": "cardano",
    "AVAX": "avalanche-2", "DOGE": "dogecoin",  "LINK": "chainlink",
    "DOT": "polkadot",     "MATIC": "matic-network", "POL": "polygon-ecosystem-token",
    "UNI": "uniswap",      "AAVE": "aave",      "LDO": "lido-dao",
    "ARB": "arbitrum",     "OP": "optimism",    "INJ": "injective-protocol",
    "SUI": "sui",          "SEI": "sei-network","TIA": "celestia",
    "NEAR": "near",        "APT": "aptos",      "FIL": "filecoin",
    "RNDR": "render-token","RENDER": "render-token", "FET": "fetch-ai",
    "TAO": "bittensor",    "WLD": "worldcoin-wld",   "TON": "the-open-network",
    "LTC": "litecoin",     "ATOM": "cosmos",    "ICP": "internet-computer",
}

# Token DeFi → DefiLlama protocol slug (untuk TVL). Best-effort, subset.
_LLAMA_SLUG: dict[str, str] = {
    "UNI": "uniswap",   "AAVE": "aave",      "LDO": "lido",
    "MKR": "makerdao",  "CRV": "curve-dex",  "SNX": "synthetix",
    "COMP": "compound-finance", "SUSHI": "sushi", "GMX": "gmx",
    "DYDX": "dydx",     "PENDLE": "pendle",  "INJ": "injective",
    "JUP": "jupiter",   "RAY": "raydium",    "CAKE": "pancakeswap",
}


# ── Data helpers (network — not unit-tested) ─────────────────────────────────

def _cg_get(path: str, params: dict | None = None):
    """CoinGecko GET → parsed JSON atau None."""
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3{path}",
            params=params,
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 429:
            log.warning(f"CoinGecko 429 on {path}")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"CoinGecko error {path}: {e}")
        return None


def _coin_id(symbol: str) -> str | None:
    """Symbol → CoinGecko coin id. Override map dulu, lalu /search."""
    sym = symbol.upper().replace("USDT", "").strip()
    if sym in _CG_ID_OVERRIDE:
        return _CG_ID_OVERRIDE[sym]
    data = _cg_get("/search", {"query": sym})
    if not data:
        return None
    for coin in (data.get("coins") or []):
        if (coin.get("symbol") or "").upper() == sym:
            return coin.get("id")
    coins = data.get("coins") or []
    return coins[0].get("id") if coins else None


def _fetch_raw_market(symbol: str) -> dict | None:
    """Ambil data market mentah dari CoinGecko untuk satu coin."""
    cid = _coin_id(symbol)
    if not cid:
        return None
    data = _cg_get("/coins/markets", {
        "vs_currency":             "usd",
        "ids":                     cid,
        "price_change_percentage": "24h,7d,30d",
        "sparkline":               False,
    })
    if not data or not isinstance(data, list):
        return None
    return data[0]


def _fetch_tvl(symbol: str) -> dict | None:
    """Best-effort TVL dari DefiLlama untuk token DeFi. None kalau bukan DeFi."""
    sym = symbol.upper().replace("USDT", "").strip()
    slug = _LLAMA_SLUG.get(sym)
    if not slug:
        return None
    try:
        r = requests.get(f"https://api.llama.fi/protocol/{slug}", timeout=15)
        if r.status_code != 200:
            return None
        d = r.json()
        tvl_now = d.get("currentChainTvls", {})
        total_tvl = float(d.get("tvl", [{}])[-1].get("totalLiquidityUSD", 0)) \
            if isinstance(d.get("tvl"), list) and d.get("tvl") else 0.0
        # Tren TVL 30 hari (approx): bandingkan titik awal/akhir series harian
        tvl_series = d.get("tvl", [])
        tvl_chg_30d = None
        if isinstance(tvl_series, list) and len(tvl_series) >= 30:
            old = float(tvl_series[-30].get("totalLiquidityUSD", 0) or 0)
            new = float(tvl_series[-1].get("totalLiquidityUSD", 0) or 0)
            if old > 0:
                tvl_chg_30d = (new - old) / old * 100
        return {
            "tvl":         total_tvl or sum(float(v) for v in tvl_now.values() if isinstance(v, (int, float))),
            "tvl_chg_30d": tvl_chg_30d,
        }
    except Exception as e:
        log.warning(f"DefiLlama error {symbol}: {e}")
        return None


# ── Pure logic (unit-tested) ─────────────────────────────────────────────────

def compute_valuation_metrics(raw: dict, tvl_data: dict | None = None) -> dict:
    """Hitung metrik valuasi dari data mentah CoinGecko (+ TVL opsional).

    Semua field defensif terhadap None/0. Return dict metrik numerik.
    """
    raw = raw or {}
    mcap = float(raw.get("market_cap") or 0)
    fdv  = float(raw.get("fully_diluted_valuation") or 0)
    vol  = float(raw.get("total_volume") or 0)
    circ = float(raw.get("circulating_supply") or 0)
    tot  = float(raw.get("total_supply") or 0)

    fdv_ratio   = (mcap / fdv) if fdv > 0 else None          # 1.0 = no dilution
    vol_mcap    = (vol / mcap) if mcap > 0 else None
    circ_pct    = (circ / tot * 100) if tot > 0 else None
    ath_chg     = raw.get("ath_change_percentage")           # negatif = di bawah ATH
    chg_7d      = raw.get("price_change_percentage_7d_in_currency")
    chg_30d     = raw.get("price_change_percentage_30d_in_currency")

    mcap_tvl = None
    tvl = None
    tvl_chg_30d = None
    if tvl_data and tvl_data.get("tvl"):
        tvl = float(tvl_data["tvl"])
        if tvl > 0 and mcap > 0:
            mcap_tvl = mcap / tvl
        tvl_chg_30d = tvl_data.get("tvl_chg_30d")

    return {
        "mcap":        mcap,
        "fdv":         fdv,
        "fdv_ratio":   fdv_ratio,
        "vol_mcap":    vol_mcap,
        "circ_pct":    circ_pct,
        "ath_chg_pct": float(ath_chg) if ath_chg is not None else None,
        "chg_7d":      float(chg_7d) if chg_7d is not None else None,
        "chg_30d":     float(chg_30d) if chg_30d is not None else None,
        "tvl":         tvl,
        "mcap_tvl":    mcap_tvl,
        "tvl_chg_30d": tvl_chg_30d,
    }


def assess_valuation(metrics: dict, direction: str) -> dict:
    """Terjemahkan metrik → bias fundamental + catatan untuk Bull/Bear.

    Return:
      {"bias": "SUPPORTS_LONG"|"SUPPORTS_SHORT"|"NEUTRAL"|"CAUTION",
       "bull_notes": [str], "bear_notes": [str], "headline": str}
    """
    is_long = direction in ("LONG", "PUMP", "PREPUMP")
    bull, bear = [], []

    fdv_ratio = metrics.get("fdv_ratio")
    if fdv_ratio is not None:
        if fdv_ratio < 0.30:
            bear.append(f"Hanya {fdv_ratio*100:.0f}% supply beredar — overhang unlock besar (dilusi menekan harga).")
        elif fdv_ratio > 0.80:
            bull.append(f"{fdv_ratio*100:.0f}% supply sudah beredar — risiko dilusi unlock kecil.")

    vol_mcap = metrics.get("vol_mcap")
    if vol_mcap is not None:
        if vol_mcap > 0.15:
            bull.append(f"Turnover tinggi (vol/mcap {vol_mcap:.2f}) — minat & likuiditas kuat.")
        elif vol_mcap < 0.02:
            bear.append(f"Turnover rendah (vol/mcap {vol_mcap:.3f}) — minat tipis, rawan ilikuid.")

    ath = metrics.get("ath_chg_pct")
    if ath is not None:
        if ath > -8:
            bear.append(f"Hanya {abs(ath):.0f}% dari ATH — zona price discovery/euforia, risk reward LONG memburuk.")
        elif ath < -85:
            bull.append(f"{abs(ath):.0f}% di bawah ATH — valuasi tertekan dalam, ruang pemulihan besar.")

    mcap_tvl = metrics.get("mcap_tvl")
    if mcap_tvl is not None:
        if mcap_tvl < 1.0:
            bull.append(f"MCap/TVL {mcap_tvl:.2f} (<1) — dihargai murah relatif terhadap TVL nyata.")
        elif mcap_tvl > 8.0:
            bear.append(f"MCap/TVL {mcap_tvl:.1f} — premium spekulatif tinggi vs penggunaan riil.")

    tvl_chg = metrics.get("tvl_chg_30d")
    if tvl_chg is not None:
        if tvl_chg > 15:
            bull.append(f"TVL naik {tvl_chg:+.0f}% (30h) — protokol bertumbuh, modal masuk.")
        elif tvl_chg < -15:
            bear.append(f"TVL turun {tvl_chg:+.0f}% (30h) — modal keluar, fundamental melemah.")

    # Tentukan bias agregat relatif terhadap arah trade
    nb, nr = len(bull), len(bear)
    if nb == 0 and nr == 0:
        bias = "NEUTRAL"
    elif is_long:
        bias = "SUPPORTS_LONG" if nb > nr else ("CAUTION" if nr > nb else "NEUTRAL")
    else:
        # SHORT: argumen bear fundamental justru MENDUKUNG arah trade
        bias = "SUPPORTS_SHORT" if nr > nb else ("CAUTION" if nb > nr else "NEUTRAL")

    headline = {
        "SUPPORTS_LONG":  "Fundamental mendukung LONG",
        "SUPPORTS_SHORT": "Fundamental mendukung SHORT",
        "CAUTION":        "Fundamental berlawanan dengan arah trade",
        "NEUTRAL":        "Fundamental netral",
    }[bias]

    return {"bias": bias, "bull_notes": bull, "bear_notes": bear, "headline": headline}


def build_valuation_brief(metrics: dict, assessment: dict, coin: str) -> str:
    """Format ringkasan valuasi untuk disuntik ke prompt debat AI."""
    if not assessment:
        return ""
    lines = [f"VALUASI ON-CHAIN {coin} — {assessment.get('headline','')}:"]

    facts = []
    if metrics.get("mcap"):
        facts.append(f"MCap ${metrics['mcap']/1e6:,.0f}M")
    if metrics.get("fdv_ratio") is not None:
        facts.append(f"FDV ratio {metrics['fdv_ratio']:.2f}")
    if metrics.get("vol_mcap") is not None:
        facts.append(f"Vol/MCap {metrics['vol_mcap']:.3f}")
    if metrics.get("ath_chg_pct") is not None:
        facts.append(f"{metrics['ath_chg_pct']:+.0f}% dari ATH")
    if metrics.get("mcap_tvl") is not None:
        facts.append(f"MCap/TVL {metrics['mcap_tvl']:.2f}")
    if facts:
        lines.append("  " + " | ".join(facts))

    for n in assessment.get("bull_notes", [])[:3]:
        lines.append(f"  🐂 {n}")
    for n in assessment.get("bear_notes", [])[:3]:
        lines.append(f"  🐻 {n}")

    return "\n".join(lines)


# ── Orchestration (cached) ───────────────────────────────────────────────────

def get_valuation(symbol: str, direction: str = "LONG") -> dict | None:
    """Full pipeline ber-cache: fetch → compute → assess → brief.

    Return dict {metrics, assessment, brief} atau None kalau data tak ada.
    """
    sym = symbol.upper().replace("USDT", "").strip()
    now = time.time()

    cached = _CACHE.get(sym)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        raw_bundle = cached["data"]
    else:
        raw = _fetch_raw_market(symbol)
        if not raw:
            _CACHE[sym] = {"ts": now, "data": None}   # cache miss juga, hindari spam
            return None
        tvl_data = _fetch_tvl(symbol)
        raw_bundle = {"raw": raw, "tvl": tvl_data}
        _CACHE[sym] = {"ts": now, "data": raw_bundle}

    if not raw_bundle:
        return None

    metrics    = compute_valuation_metrics(raw_bundle["raw"], raw_bundle.get("tvl"))
    assessment = assess_valuation(metrics, direction)
    brief      = build_valuation_brief(metrics, assessment, symbol.replace("USDT", ""))
    return {"metrics": metrics, "assessment": assessment, "brief": brief}


def build_valuation_brief_for_ai(symbol: str, direction: str = "LONG") -> str:
    """Entry-point integrasi: ringkasan valuasi siap-suntik ke debat. '' kalau N/A."""
    try:
        v = get_valuation(symbol, direction)
        return v["brief"] if v else ""
    except Exception as e:
        log.warning(f"valuation brief error {symbol}: {e}")
        return ""


def is_available() -> bool:
    """Modul keyless — selalu siap selama jaringan ada."""
    return True
