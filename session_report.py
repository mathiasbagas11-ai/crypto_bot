#!/usr/bin/env python3
"""
SESSION REPORT — BTC / ETH / SOL
================================
Bikin update lengkap 3 majors tiap akhir sesi trading (Asia / London / New York)
plus outlook ke sesi berikutnya, dan alert "shifting" saat regime/harga major
berubah signifikan (mis. karena news).

Modul ini PURE LOGIC + formatting: semua data analisa (harga, trend, regime,
CVD, dll.) di-compute di bot utama lalu dioper ke sini sebagai dict sederhana,
supaya gampang dites tanpa narik seluruh bot.

Sesi (UTC):
  Asia    00:00–09:00   (late ~07:00 → next London)
  London  07:00–16:00   (late ~15:00 → next New York)
  NewYork 12:00–21:00   (late ~21:00 → next Asia)
WIB = UTC+7.
"""

from datetime import datetime, timezone, timedelta

# Jam cron (UTC) → sesi yang sedang wrap-up
_SESSION_CLOSE_HOUR = {
    7:  "ASIA",
    15: "LONDON",
    21: "NEW YORK",
}

_NEXT_SESSION = {
    "ASIA":     "LONDON",
    "LONDON":   "NEW YORK",
    "NEW YORK": "ASIA",
}

# Karakter tiap sesi (dipakai di outlook heuristik)
_SESSION_CHAR = {
    "LONDON":   "London sering bawa ekspansi volatilitas & breakout dari range Asia",
    "NEW YORK": "New York biasanya konfirmasi atau justru balikin (reversal) arah London, volume tertinggi",
    "ASIA":     "Asia cenderung lebih kalem/range, sering konsolidasi sebelum London",
}


def session_just_ended(hour_utc: int) -> str | None:
    """Return nama sesi yang baru saja wrap-up untuk jam cron ini, atau None."""
    return _SESSION_CLOSE_HOUR.get(hour_utc)


def next_session_of(session: str) -> str:
    return _NEXT_SESSION.get(session, "ASIA")


def active_session(hour_utc: int) -> str:
    """Sesi yang sedang aktif untuk jam UTC tertentu (fallback non-cron)."""
    if 0 <= hour_utc < 7:   return "ASIA"
    if 7 <= hour_utc < 15:  return "LONDON"
    if 15 <= hour_utc < 21: return "NEW YORK"
    return "ASIA"


def _wib_str(dt_utc: datetime) -> str:
    return (dt_utc + timedelta(hours=7)).strftime("%d %b %Y %H:%M") + " WIB"


def coin_bias(coin: dict) -> tuple[str, float]:
    """
    Hitung bias 1 koin dari kombinasi trend + regime + posisi EMA + RSI + CVD.
    Return (label, score). score >0 bullish, <0 bearish.
    """
    s = 0.0
    trend  = (coin.get("trend") or "").upper()
    regime = (coin.get("regime") or "").upper()
    price  = coin.get("price") or 0
    ema21  = coin.get("ema21") or 0
    ema50  = coin.get("ema50") or 0
    rsi    = coin.get("rsi") or 50
    cvd    = (coin.get("cvd_dir") or "").upper()

    if "BULL" in trend:  s += 1
    elif "BEAR" in trend: s -= 1
    if "BULL" in regime:  s += 1
    elif "BEAR" in regime: s -= 1
    # Posisi vs EMA dengan toleransi 0.1% — harga nempel EMA = netral, bukan bias.
    if price and ema50:
        if price > ema50 * 1.001:   s += 1
        elif price < ema50 * 0.999: s -= 1
    if price and ema21:
        if price > ema21 * 1.001:   s += 0.5
        elif price < ema21 * 0.999: s -= 0.5
    if rsi >= 55:   s += 0.5
    elif rsi <= 45: s -= 0.5
    if cvd == "BUY":  s += 0.5
    elif cvd == "SELL": s -= 0.5

    if s >= 1.5:    label = "BULLISH"
    elif s <= -1.5: label = "BEARISH"
    else:           label = "NETRAL"
    return label, s


def _bias_emoji(label: str) -> str:
    return {"BULLISH": "🟢", "BEARISH": "🔴", "NETRAL": "⚪"}.get(label, "⚪")


def _fmt(p) -> str:
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "?"
    ap = abs(p)
    if ap >= 100:  return f"{p:,.2f}"
    if ap >= 1:    return f"{p:.3f}"
    if ap >= 0.01: return f"{p:.5f}"
    return f"{p:.8f}"


def build_coin_block(coin: dict) -> str:
    """Blok analisa 1 koin untuk laporan sesi."""
    name   = coin.get("name", "?")
    price  = coin.get("price", 0)
    chg    = coin.get("chg_pct", 0) or 0
    regime = coin.get("regime", "—")
    adx    = coin.get("adx", 0) or 0
    rsi    = coin.get("rsi", 50) or 50
    cvd_d  = (coin.get("cvd_dir") or "FLAT").upper()
    cvd_p  = coin.get("cvd_pct", 0) or 0
    sup    = coin.get("key_sup")
    res    = coin.get("key_res")

    label, _ = coin_bias(coin)
    chg_emoji = "🟢" if chg > 0 else "🔴" if chg < 0 else "⚪"
    cvd_emoji = "💚" if cvd_d == "BUY" else "❤️" if cvd_d == "SELL" else "🤍"

    lines = [
        f"{_bias_emoji(label)} <b>{name}</b>  ${_fmt(price)}  {chg_emoji} {chg:+.2f}%",
        f"   Bias: <b>{label}</b> | Regime: {regime} | ADX {adx:.0f} | RSI {rsi:.0f}",
        f"   {cvd_emoji} CVD {cvd_d.lower()} {cvd_p:+.1f}%",
    ]
    if sup is not None and res is not None:
        lines.append(f"   🎯 Sup {_fmt(sup)} | Res {_fmt(res)}")
    return "\n".join(lines)


def _aggregate_outlook(coins: list[dict], ended: str, nxt: str) -> str:
    """Heuristik outlook sesi berikutnya dari agregat bias 3 majors (BTC bobot 2x)."""
    total = 0.0
    btc_label = "NETRAL"
    for c in coins:
        label, sc = coin_bias(c)
        w = 2.0 if c.get("name") == "BTC" else 1.0
        total += sc * w
        if c.get("name") == "BTC":
            btc_label = label

    if total >= 2.5:
        lean = "condong LANJUT NAIK (bullish continuation)"
    elif total <= -2.5:
        lean = "condong LANJUT TURUN (bearish continuation)"
    elif total > 0:
        lean = "bias bullish tipis — hati-hati fakeout"
    elif total < 0:
        lean = "bias bearish tipis — hati-hati fakeout"
    else:
        lean = "mixed/range — tunggu konfirmasi arah"

    char = _SESSION_CHAR.get(nxt, "")
    return (
        f"📈 <b>Outlook {nxt}:</b> {lean}.\n"
        f"   BTC pegang kendali (bias {btc_label}). {char}."
    )


def build_session_report(ended_session: str, coins: list[dict],
                         now_utc: datetime = None, news_note: str = "") -> str:
    """Rangkai laporan akhir sesi lengkap BTC/ETH/SOL + outlook sesi berikutnya."""
    now_utc = now_utc or datetime.now(timezone.utc)
    nxt = next_session_of(ended_session)

    header = (
        f"🌏 <b>MAJORS — TUTUP SESI {ended_session}</b>\n"
        f"🕐 {_wib_str(now_utc)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    blocks = "\n\n".join(build_coin_block(c) for c in coins)
    outlook = _aggregate_outlook(coins, ended_session, nxt)

    parts = [header, blocks, "─── NEXT SESSION ───", outlook]
    if news_note:
        parts.append(f"📰 <b>Catatan news:</b> {news_note}")
    parts.append("⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n\n".join(parts)


def build_shift_alert(coin: dict, reason: str, now_utc: datetime = None,
                      news_note: str = "") -> str:
    """Alert saat 1 major shift signifikan (regime flip / pergerakan tajam)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    name  = coin.get("name", "?")
    label, _ = coin_bias(coin)
    parts = [
        f"⚡ <b>SHIFTING — {name}</b>",
        f"🕐 {_wib_str(now_utc)}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{_bias_emoji(label)} Bias sekarang: <b>{label}</b>",
        f"🔀 {reason}",
        build_coin_block(coin),
    ]
    if news_note:
        parts.append(f"📰 {news_note}")
    parts.append("⚠️ <i>Not financial advice. DYOR.</i>")
    return "\n".join(parts)
