"""
chart_generator.py — Auto Chart Generator for Crypto Bot v11
=============================================================
Generates candlestick chart + indicators as PNG, kirim ke Telegram.

Dependencies:
    pip install mplfinance matplotlib pandas

Features:
  - Candlestick + Volume bar
  - EMA 20/50
  - Entry / SL / TP1 / TP2 lines (jika ada trade plan)
  - Pattern label overlay
  - Score badge di sudut kiri atas
  - Dark theme (mirip TradingView)
"""

import io
import logging
import requests
import numpy as np

log = logging.getLogger(__name__)

# ── Optional imports ──────────────────────────────────────────────
try:
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")   # non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyBboxPatch
    CHART_AVAILABLE = True
except ImportError:
    CHART_AVAILABLE = False
    log.warning("chart_generator: matplotlib/pandas tidak terinstall. pip install matplotlib pandas")


# ─────────────────────────────────────────────
# COLOR SCHEME (TradingView Dark)
# ─────────────────────────────────────────────
BG_COLOR     = "#131722"
PANEL_COLOR  = "#1E222D"
GRID_COLOR   = "#2A2E39"
TEXT_COLOR   = "#D1D4DC"
GREEN        = "#26A69A"
RED          = "#EF5350"
EMA20_COLOR  = "#FF9800"
EMA50_COLOR  = "#2196F3"
ENTRY_COLOR  = "#FFD700"
SL_COLOR     = "#FF4444"
TP1_COLOR    = "#00E676"
TP2_COLOR    = "#69F0AE"
VOL_GREEN    = "#1B5E2088"
VOL_RED      = "#B71C1C88"


# ─────────────────────────────────────────────
# PATTERN DETECTION
# ─────────────────────────────────────────────

def detect_patterns(candles: list) -> list:
    """
    Deteksi chart patterns dari candle data.
    Returns list of pattern dicts: {name, bullish, strength}
    """
    patterns = []
    if not candles or len(candles) < 20:
        return patterns

    closes  = [c["close"] for c in candles]
    highs   = [c["high"]  for c in candles]
    lows    = [c["low"]   for c in candles]
    volumes = [c["volume"] for c in candles]

    # ── Double Bottom ─────────────────────────
    # Dua lembah dengan jarak mirip, dipisah puncak
    try:
        window = 20
        c_slice = closes[-window:]
        l_slice = lows[-window:]
        min1_idx = int(np.argmin(l_slice[:window//2]))
        min2_idx = int(np.argmin(l_slice[window//2:])) + window//2
        min1_val = l_slice[min1_idx]
        min2_val = l_slice[min2_idx]
        mid_high = max(c_slice[min1_idx:min2_idx]) if min2_idx > min1_idx else 0

        if (abs(min1_val - min2_val) / max(min1_val, min2_val) < 0.03 and
                mid_high > min(min1_val, min2_val) * 1.02 and
                closes[-1] > mid_high * 0.98):
            patterns.append({"name": "Double Bottom", "bullish": True, "strength": "STRONG"})
    except Exception:
        pass

    # ── Double Top ────────────────────────────
    try:
        window = 20
        c_slice = closes[-window:]
        h_slice = highs[-window:]
        max1_idx = int(np.argmax(h_slice[:window//2]))
        max2_idx = int(np.argmax(h_slice[window//2:])) + window//2
        max1_val = h_slice[max1_idx]
        max2_val = h_slice[max2_idx]
        mid_low  = min(c_slice[max1_idx:max2_idx]) if max2_idx > max1_idx else 9e9

        if (abs(max1_val - max2_val) / max(max1_val, max2_val) < 0.03 and
                mid_low < max(max1_val, max2_val) * 0.98 and
                closes[-1] < mid_low * 1.02):
            patterns.append({"name": "Double Top", "bullish": False, "strength": "STRONG"})
    except Exception:
        pass

    # ── Bull Flag ─────────────────────────────
    # Strong move up, diikuti konsolidasi sideways/slight down
    try:
        pole_candles = 5
        flag_candles = 8
        if len(closes) >= pole_candles + flag_candles:
            pole   = closes[-(pole_candles + flag_candles):-flag_candles]
            flag   = closes[-flag_candles:]
            pole_move = (pole[-1] - pole[0]) / pole[0] * 100
            flag_range = (max(flag) - min(flag)) / max(flag) * 100
            flag_slope = (flag[-1] - flag[0]) / flag[0] * 100

            if pole_move > 5 and flag_range < 4 and -3 < flag_slope < 1:
                # Volume: pole lebih tinggi dari flag
                pole_vol = np.mean(volumes[-(pole_candles + flag_candles):-flag_candles])
                flag_vol = np.mean(volumes[-flag_candles:])
                if pole_vol > flag_vol * 1.2:
                    patterns.append({"name": "Bull Flag", "bullish": True, "strength": "MODERATE"})
    except Exception:
        pass

    # ── Bear Flag ─────────────────────────────
    try:
        if len(closes) >= 13:
            pole   = closes[-13:-8]
            flag   = closes[-8:]
            pole_move = (pole[-1] - pole[0]) / pole[0] * 100
            flag_slope = (flag[-1] - flag[0]) / flag[0] * 100

            if pole_move < -5 and 0 < flag_slope < 3:
                patterns.append({"name": "Bear Flag", "bullish": False, "strength": "MODERATE"})
    except Exception:
        pass

    # ── Ascending Triangle ────────────────────
    try:
        h_slice = highs[-15:]
        l_slice = lows[-15:]
        high_range = (max(h_slice) - min(h_slice)) / max(h_slice) * 100
        low_trend  = (l_slice[-1] - l_slice[0]) / l_slice[0] * 100

        if high_range < 2.5 and low_trend > 2:
            patterns.append({"name": "Ascending Triangle", "bullish": True, "strength": "MODERATE"})
    except Exception:
        pass

    # ── Descending Triangle ───────────────────
    try:
        h_slice = highs[-15:]
        l_slice = lows[-15:]
        low_range  = (max(l_slice) - min(l_slice)) / max(l_slice) * 100
        high_trend = (h_slice[-1] - h_slice[0]) / h_slice[0] * 100

        if low_range < 2.5 and high_trend < -2:
            patterns.append({"name": "Descending Triangle", "bullish": False, "strength": "MODERATE"})
    except Exception:
        pass

    return patterns[:2]   # max 2 patterns


# ─────────────────────────────────────────────
# EMA CALCULATOR
# ─────────────────────────────────────────────

def calc_ema(prices: list, period: int) -> list:
    if len(prices) < period:
        return [None] * len(prices)
    k = 2 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(float(np.mean(prices[:period])))
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema


# ─────────────────────────────────────────────
# MAIN: GENERATE CHART
# ─────────────────────────────────────────────

def generate_chart(
    symbol: str,
    candles: list,          # list of {open, high, low, close, volume, time}
    trade_plan: dict = None,  # {entry, sl, tp1, tp2, direction}
    patterns: list = None,   # dari detect_patterns()
    score: int = None,       # confluence/setup score
    label: str = "",         # e.g. "LONG | EXCELLENT" atau "Pre-Pump"
    timeframe: str = "1H"
) -> bytes | None:
    """
    Generate chart PNG, return bytes.
    Returns None jika gagal (matplotlib tidak tersedia, candles kosong, dll).
    """
    if not CHART_AVAILABLE:
        log.warning("chart_generator: matplotlib tidak tersedia")
        return None

    if not candles or len(candles) < 20:
        log.warning(f"chart_generator: candles tidak cukup untuk {symbol}")
        return None

    try:
        # Pakai 60 candle terakhir buat clarity
        candles = candles[-60:]
        n = len(candles)

        opens   = [c["open"]   for c in candles]
        highs   = [c["high"]   for c in candles]
        lows    = [c["low"]    for c in candles]
        closes  = [c["close"]  for c in candles]
        volumes = [c["volume"] for c in candles]
        x = list(range(n))

        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)

        # ── Figure setup ─────────────────────
        fig, (ax_price, ax_vol) = plt.subplots(
            2, 1, figsize=(10, 6),
            gridspec_kw={"height_ratios": [3, 1]},
            facecolor=BG_COLOR
        )
        fig.subplots_adjust(hspace=0.05)

        for ax in [ax_price, ax_vol]:
            ax.set_facecolor(PANEL_COLOR)
            ax.tick_params(colors=TEXT_COLOR, labelsize=7)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["bottom"].set_color(GRID_COLOR)
            ax.spines["left"].set_color(GRID_COLOR)
            ax.yaxis.grid(True, color=GRID_COLOR, linewidth=0.4, alpha=0.6)
            ax.xaxis.grid(False)

        # ── Candlesticks ─────────────────────
        for i in x:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            color = GREEN if c >= o else RED
            # Body
            ax_price.bar(i, abs(c - o), bottom=min(o, c), width=0.7,
                         color=color, alpha=0.9, zorder=2)
            # Wick
            ax_price.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=1)

        # ── EMAs ─────────────────────────────
        ema20_x = [i for i, v in enumerate(ema20) if v is not None]
        ema20_y = [v for v in ema20 if v is not None]
        ema50_x = [i for i, v in enumerate(ema50) if v is not None]
        ema50_y = [v for v in ema50 if v is not None]

        if ema20_x:
            ax_price.plot(ema20_x, ema20_y, color=EMA20_COLOR,
                          linewidth=1.2, alpha=0.85, label="EMA20")
        if ema50_x:
            ax_price.plot(ema50_x, ema50_y, color=EMA50_COLOR,
                          linewidth=1.2, alpha=0.85, label="EMA50")

        # ── Trade plan lines ─────────────────
        if trade_plan:
            entry = trade_plan.get("entry")
            sl    = trade_plan.get("sl")
            tp1   = trade_plan.get("tp1")
            tp2   = trade_plan.get("tp2")

            if entry:
                ax_price.axhline(entry, color=ENTRY_COLOR, linewidth=1.2,
                                 linestyle="--", alpha=0.9, zorder=3)
                ax_price.text(n - 0.5, entry, f"  Entry {_fmt(entry)}",
                              color=ENTRY_COLOR, fontsize=7, va="center")
            if sl:
                ax_price.axhline(sl, color=SL_COLOR, linewidth=1.0,
                                 linestyle=":", alpha=0.85, zorder=3)
                ax_price.text(n - 0.5, sl, f"  SL {_fmt(sl)}",
                              color=SL_COLOR, fontsize=7, va="center")
            if tp1:
                ax_price.axhline(tp1, color=TP1_COLOR, linewidth=1.0,
                                 linestyle="--", alpha=0.85, zorder=3)
                ax_price.text(n - 0.5, tp1, f"  TP1 {_fmt(tp1)}",
                              color=TP1_COLOR, fontsize=7, va="center")
            if tp2:
                ax_price.axhline(tp2, color=TP2_COLOR, linewidth=1.0,
                                 linestyle="--", alpha=0.7, zorder=3)
                ax_price.text(n - 0.5, tp2, f"  TP2 {_fmt(tp2)}",
                              color=TP2_COLOR, fontsize=7, va="center")

        # ── Volume bars ──────────────────────
        for i in x:
            color = VOL_GREEN if closes[i] >= opens[i] else VOL_RED
            ax_vol.bar(i, volumes[i], width=0.7, color=color)

        # ── Pattern label ────────────────────
        if patterns:
            p_text = "  ".join([
                f"{'▲' if p['bullish'] else '▼'} {p['name']}"
                for p in patterns
            ])
            ax_price.text(
                0.01, 0.97, p_text,
                transform=ax_price.transAxes,
                color="#FFD700", fontsize=8, va="top",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="#00000080", edgecolor="none")
            )

        # ── Score badge ──────────────────────
        if score is not None:
            badge_color = (
                "#E53935" if score < 40 else
                "#FB8C00" if score < 60 else
                "#43A047" if score < 80 else
                "#00ACC1"
            )
            ax_price.text(
                0.99, 0.97,
                f"Score: {score}/100",
                transform=ax_price.transAxes,
                color="white", fontsize=8, va="top", ha="right", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", facecolor=badge_color, edgecolor="none")
            )

        # ── Title ────────────────────────────
        title_str = f"{symbol} · {timeframe}"
        if label:
            title_str += f"  |  {label}"
        ax_price.set_title(title_str, color=TEXT_COLOR, fontsize=9,
                           fontweight="bold", pad=6, loc="left")

        # ── Legend ───────────────────────────
        handles = []
        if ema20_x:
            handles.append(mpatches.Patch(color=EMA20_COLOR, label="EMA20"))
        if ema50_x:
            handles.append(mpatches.Patch(color=EMA50_COLOR, label="EMA50"))
        if handles:
            ax_price.legend(handles=handles, loc="upper right",
                            fontsize=7, framealpha=0.3,
                            labelcolor=TEXT_COLOR, facecolor=PANEL_COLOR,
                            edgecolor=GRID_COLOR)

        # ── X-axis cleanup ───────────────────
        ax_price.set_xlim(-0.5, n + 3)    # +3 biar label TP/SL ga kepotong
        ax_vol.set_xlim(-0.5, n + 3)
        ax_price.set_xticklabels([])
        ax_price.tick_params(bottom=False)

        # Label candle index di bawah
        tick_step = max(1, n // 6)
        ax_vol.set_xticks(range(0, n, tick_step))
        ax_vol.set_xticklabels([str(i) for i in range(0, n, tick_step)],
                               color=TEXT_COLOR, fontsize=6)
        ax_vol.set_ylabel("Vol", color=TEXT_COLOR, fontsize=7)
        ax_price.set_ylabel("Price", color=TEXT_COLOR, fontsize=7)

        # ── Export ke bytes ──────────────────
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=BG_COLOR, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        log.error(f"chart_generator error untuk {symbol}: {e}", exc_info=True)
        try:
            plt.close("all")
        except Exception:
            pass
        return None


# ─────────────────────────────────────────────
# SEND CHART TO TELEGRAM
# ─────────────────────────────────────────────

def send_chart_telegram(
    bot_token: str,
    chat_id: str,
    chart_bytes: bytes,
    caption: str = "",
    parse_mode: str = "HTML"
) -> bool:
    """Kirim chart PNG ke Telegram sebagai foto."""
    if not chart_bytes:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption,
                  "parse_mode": parse_mode},
            files={"photo": ("chart.png", chart_bytes, "image/png")},
            timeout=20
        )
        if r.status_code == 200:
            log.info(f"✅ Chart sent to {chat_id}")
            return True
        else:
            log.warning(f"Chart send failed: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        log.error(f"send_chart_telegram error: {e}")
        return False


# ─────────────────────────────────────────────
# HELPER: SHOULD SEND CHART?
# ─────────────────────────────────────────────

def should_send_chart(coin_data: dict) -> bool:
    """
    Decide apakah chart layak dikirim untuk coin ini.
    Chart hanya dikirim kalau setup JELAS:
      - confluence score >= 65, DAN
      - minimal ada 1 dari: OB fresh, FVG valid, sweep confirmed, pattern detected
    """
    confluence = coin_data.get("confluence", {})
    score      = confluence.get("score", 0)

    if score < 65:
        return False

    tf_15m = coin_data.get("tf_15m", {})
    tf_1h  = coin_data.get("tf_1h", {})
    tf_4h  = coin_data.get("tf_4h", {})

    # OB fresh
    ob1 = tf_1h.get("order_blocks", {})
    ob4 = tf_4h.get("order_blocks", {})
    has_ob = (ob1.get("bullish_ob") or ob1.get("bearish_ob") or
              ob4.get("bullish_ob") or ob4.get("bearish_ob"))

    # FVG valid
    fvg = tf_15m.get("fvg", {})
    has_fvg = fvg.get("fvg_type", "NONE") != "NONE"

    # Sweep confirmed
    sweep = tf_1h.get("sweep", {}) or tf_15m.get("sweep", {})
    has_sweep = sweep.get("swept", False)

    # Pattern detected
    has_pattern = bool(coin_data.get("patterns"))

    return bool(has_ob or has_fvg or has_sweep or has_pattern)


# ─────────────────────────────────────────────
# HELPER: FORMAT PRICE
# ─────────────────────────────────────────────

def _fmt(v: float) -> str:
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v:,.0f}"
    elif v >= 1:
        return f"{v:.3f}"
    elif v >= 0.01:
        return f"{v:.4f}"
    else:
        return f"{v:.6f}"
