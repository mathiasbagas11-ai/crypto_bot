import streamlit as st
import requests
from datetime import datetime, timezone, timedelta

st.set_page_config(
    page_title="CryptoBot — Signal Feed",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=30_000, key="ar")

WIB = timezone(timedelta(hours=7))

# ── Supabase ──────────────────────────────────────────────────────────────────
def _sb_cfg():
    url = st.secrets.get("SUPABASE_URL", "") or ""
    key = st.secrets.get("SUPABASE_ANON_KEY", "") or ""
    return url.rstrip("/"), key

def _sb_headers(key):
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

@st.cache_data(ttl=30)
def fetch(table, order="created_at.desc", limit=100):
    url, key = _sb_cfg()
    if not url or not key:
        return [], "Supabase belum dikonfigurasi (set SUPABASE_URL & SUPABASE_ANON_KEY di Secrets)"
    try:
        r = requests.get(
            f"{url}/rest/v1/{table}",
            headers=_sb_headers(key),
            params={"select": "*", "order": order, "limit": limit},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json(), None
        return [], f"HTTP {r.status_code}"
    except Exception as e:
        return [], str(e)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* Dark navy background */
  .stApp { background: #0d1117; color: #e6edf3; }
  [data-testid="stHeader"] { background: #0d1117; }

  /* Signal card */
  .sig-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 14px;
    position: relative;
  }
  .sig-card.long  { border-left: 4px solid #00e5a0; }
  .sig-card.short { border-left: 4px solid #ff4c6a; }

  .sig-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .coin-badge {
    font-size: 1.1rem; font-weight: 700; color: #e6edf3;
    background: #21262d; padding: 4px 10px; border-radius: 6px;
  }
  .dir-badge {
    font-size: .8rem; font-weight: 700; padding: 3px 9px;
    border-radius: 5px; letter-spacing: .05em;
  }
  .dir-long  { background: #003d25; color: #00e5a0; }
  .dir-short { background: #3d0012; color: #ff4c6a; }
  .type-badge {
    font-size: .75rem; color: #8b949e;
    background: #21262d; padding: 3px 8px; border-radius: 5px;
  }
  .sig-time { font-size: .75rem; color: #8b949e; margin-left: auto; }

  .sig-levels { display: flex; gap: 24px; flex-wrap: wrap; margin: 8px 0; }
  .level-item { display: flex; flex-direction: column; }
  .level-label { font-size: .7rem; color: #8b949e; text-transform: uppercase; letter-spacing: .05em; }
  .level-value { font-size: .95rem; font-weight: 600; color: #e6edf3; }
  .level-value.tp  { color: #00e5a0; }
  .level-value.sl  { color: #ff4c6a; }

  .sig-score { font-size: .8rem; color: #8b949e; margin-top: 6px; }
  .sig-reason { font-size: .82rem; color: #adbac7; margin-top: 4px; font-style: italic; }

  /* Score pill */
  .score-pill {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: .78rem; font-weight: 600;
  }
  .score-a  { background: #003d25; color: #00e5a0; }
  .score-b  { background: #1a2a00; color: #7ee787; }
  .score-c  { background: #2a1800; color: #e3b341; }
  .score-d  { background: #3d0012; color: #ff4c6a; }

  /* KPI boxes */
  .kpi-box {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 14px 18px; text-align: center;
  }
  .kpi-label { font-size: .75rem; color: #8b949e; text-transform: uppercase; letter-spacing: .06em; }
  .kpi-value { font-size: 1.6rem; font-weight: 700; color: #e6edf3; margin-top: 2px; }
  .kpi-value.green { color: #00e5a0; }
  .kpi-value.red   { color: #ff4c6a; }

  /* Section title */
  .section-title {
    font-size: 1rem; font-weight: 700; color: #8b949e;
    text-transform: uppercase; letter-spacing: .1em;
    margin: 24px 0 12px; border-bottom: 1px solid #21262d; padding-bottom: 6px;
  }

  h1 { color: #e6edf3 !important; }
  div[data-testid="stMarkdownContainer"] p { color: #adbac7; }
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────
now_wib = datetime.now(WIB).strftime("%d %b %Y, %H:%M WIB")
st.markdown(f"""
<div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
  <div>
    <h1 style="margin:0; font-size:1.8rem;">⚡ CryptoBot Signal Feed</h1>
    <p style="margin:0; color:#8b949e; font-size:.85rem;">
      Live signals · auto-refresh setiap 30 detik · {now_wib}
    </p>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Fetch data ────────────────────────────────────────────────────────────────
signals, sig_err = fetch("signals", order="created_at.desc", limit=50)
trades,  tr_err  = fetch("trades",  order="created_at.desc", limit=200)

# ── KPI row ───────────────────────────────────────────────────────────────────
total_sig   = len(signals)
long_count  = sum(1 for s in signals if s.get("direction","").upper() == "LONG")
short_count = sum(1 for s in signals if s.get("direction","").upper() == "SHORT")

total_tr  = len(trades)
wins      = sum(1 for t in trades if t.get("result","").upper() == "WIN")
losses    = sum(1 for t in trades if t.get("result","").upper() == "LOSS")
total_pnl = sum(t.get("pnl_usdt", 0) or 0 for t in trades)
wr_txt    = f"{wins/total_tr*100:.0f}%" if total_tr else "—"
pnl_cls   = "green" if total_pnl >= 0 else "red"
pnl_sign  = "+" if total_pnl >= 0 else ""

c1, c2, c3, c4, c5 = st.columns(5)
for col, label, val, cls in [
    (c1, "Sinyal Terkirim", total_sig, ""),
    (c2, "LONG", long_count, "green"),
    (c3, "SHORT", short_count, "red"),
    (c4, "Win Rate", wr_txt, "green" if wins > losses else "red"),
    (c5, "Total P&L", f"{pnl_sign}${total_pnl:,.2f}", pnl_cls),
]:
    col.markdown(f"""
    <div class="kpi-box">
      <div class="kpi-label">{label}</div>
      <div class="kpi-value {cls}">{val}</div>
    </div>""", unsafe_allow_html=True)

st.markdown("")

# ── Signal feed & Trade log tabs ──────────────────────────────────────────────
tab_sig, tab_trades = st.tabs(["📡 Signal Feed", "📋 Trade Log"])

# helpers
def _fmt_price(v):
    if not v: return "—"
    v = float(v)
    if v >= 1000: return f"{v:,.0f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def _score_class(score):
    s = float(score or 0)
    if s >= 75: return "score-a"
    if s >= 60: return "score-b"
    if s >= 45: return "score-c"
    return "score-d"

def _rel_time(ts_str):
    if not ts_str:
        return ""
    try:
        # Parse ISO string
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        diff = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
        m = int(diff.total_seconds() / 60)
        if m < 1:   return "baru saja"
        if m < 60:  return f"{m}m lalu"
        h = m // 60
        if h < 24:  return f"{h}j lalu"
        return f"{h//24}h lalu"
    except Exception:
        return ts_str[:16] if ts_str else ""

with tab_sig:
    if sig_err:
        st.warning(sig_err)
    elif not signals:
        st.info("Belum ada sinyal. Sinyal akan muncul di sini setelah bot mengirimnya.")
    else:
        # Filter controls
        col_f1, col_f2, col_f3, _ = st.columns([1, 1, 1, 3])
        dir_filter  = col_f1.selectbox("Direction", ["Semua", "LONG", "SHORT"], key="df")
        type_filter = col_f2.selectbox("Tipe", ["Semua"] + sorted({s.get("signal_type","SETUP") for s in signals}), key="tf")
        min_score   = col_f3.slider("Min Score", 0, 100, 0, key="ms")

        shown = 0
        for sig in signals:
            d = (sig.get("direction") or "").upper()
            t = sig.get("signal_type", "SETUP")
            sc = float(sig.get("score") or 0)
            if dir_filter != "Semua" and d != dir_filter: continue
            if type_filter != "Semua" and t != type_filter: continue
            if sc < min_score: continue

            dir_cls  = "long" if d == "LONG" else "short"
            badge_cls = "dir-long" if d == "LONG" else "dir-short"
            sc_cls   = _score_class(sc)
            reason   = sig.get("reason") or ""
            conf     = sig.get("confidence") or ""
            rel      = _rel_time(sig.get("created_at") or sig.get("ts") or "")

            st.markdown(f"""
<div class="sig-card {dir_cls}">
  <div class="sig-header">
    <span class="coin-badge">{sig.get('coin','?')}</span>
    <span class="dir-badge {badge_cls}">{d}</span>
    <span class="type-badge">{t}</span>
    <span class="sig-time">{rel}</span>
  </div>
  <div class="sig-levels">
    <div class="level-item">
      <span class="level-label">Entry</span>
      <span class="level-value">{_fmt_price(sig.get('entry_price'))}</span>
    </div>
    <div class="level-item">
      <span class="level-label">TP</span>
      <span class="level-value tp">{_fmt_price(sig.get('tp'))}</span>
    </div>
    <div class="level-item">
      <span class="level-label">SL</span>
      <span class="level-value sl">{_fmt_price(sig.get('sl'))}</span>
    </div>
  </div>
  <div class="sig-score">
    Score: <span class="score-pill {sc_cls}">{sc:.0f}</span>
    {"&nbsp;·&nbsp;" + conf if conf else ""}
  </div>
  {"<div class='sig-reason'>💬 " + reason + "</div>" if reason else ""}
</div>
""", unsafe_allow_html=True)
            shown += 1

        if shown == 0:
            st.info("Tidak ada sinyal yang cocok dengan filter.")
        else:
            st.caption(f"Menampilkan {shown} dari {len(signals)} sinyal")

with tab_trades:
    if tr_err:
        st.warning(tr_err)
    elif not trades:
        st.info("Belum ada trade yang tercatat.")
    else:
        import pandas as pd
        df = pd.DataFrame(trades)
        show_cols = ["ts","coin","direction","entry_price","pnl_usdt","pnl_pct","result","leverage","note"]
        show_cols = [c for c in show_cols if c in df.columns]
        df = df[show_cols].rename(columns={
            "ts": "Waktu", "coin": "Coin", "direction": "Dir",
            "entry_price": "Entry", "pnl_usdt": "P&L ($)",
            "pnl_pct": "P&L (%)", "result": "Result",
            "leverage": "Lev", "note": "Note",
        })

        def color_result(val):
            if str(val).upper() == "WIN":  return "color: #00e5a0; font-weight:700"
            if str(val).upper() == "LOSS": return "color: #ff4c6a; font-weight:700"
            return ""
        def color_pnl(val):
            try:
                return "color: #00e5a0" if float(val) >= 0 else "color: #ff4c6a"
            except Exception:
                return ""

        styled = df.style \
            .applymap(color_result, subset=["Result"]) \
            .applymap(color_pnl, subset=["P&L ($)", "P&L (%)"]) \
            .format({"P&L ($)": "{:+.2f}", "P&L (%)": "{:+.2f}%", "Entry": "{:,.4f}"}, na_rep="—")

        st.dataframe(styled, use_container_width=True, height=500)
