import streamlit as st
import json, pathlib, time
from datetime import datetime, timezone, timedelta

st.set_page_config(
    page_title="CryptoBot v13 — Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── auto-refresh ──────────────────────────────────────────
from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=30_000, key="autorefresh")   # tiap 30 detik

ROOT      = pathlib.Path(__file__).parent.parent
HERE      = pathlib.Path(__file__).parent
PORT_FILE = HERE / "my_portfolio.json"
WIB       = timezone(timedelta(hours=7))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_TRADES  = "Trades"
SHEET_BALANCE = "Balance"

# ── Google Sheets connection ──────────────────────────────
@st.cache_resource(ttl=60)
def get_gsheet():
    """Buat koneksi ke Google Sheets. Cache 60 detik."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        # Ambil credentials dari st.secrets (Streamlit Cloud) atau env
        if "google" in st.secrets:
            creds_dict = dict(st.secrets["google"])
            # Streamlit secrets menyimpan newline sebagai literal \\n
            if "private_key" in creds_dict:
                creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            spreadsheet_id = st.secrets.get("SPREADSHEET_ID", "")
        else:
            import os
            creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
            spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "")
            if not creds_json or not spreadsheet_id:
                return None, None, "Credentials belum diset"
            creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)

        client = gspread.authorize(creds)
        sheet  = client.open_by_key(spreadsheet_id)
        return sheet, spreadsheet_id, None
    except Exception as e:
        return None, None, str(e)


@st.cache_data(ttl=30)   # cache 30 detik, auto-expired bareng autorefresh
def fetch_trades():
    sheet, _, err = get_gsheet()
    if not sheet:
        return [], err
    try:
        ws   = sheet.worksheet(SHEET_TRADES)
        rows = ws.get_all_records()
        return rows, None
    except Exception as e:
        return [], str(e)


@st.cache_data(ttl=30)
def fetch_balance_history():
    sheet, _, err = get_gsheet()
    if not sheet:
        return [], err
    try:
        ws   = sheet.worksheet(SHEET_BALANCE)
        rows = ws.get_all_records()
        return rows, None
    except Exception as e:
        return [], str(e)


# ── local JSON helpers ────────────────────────────────────
def load_json(fname, default=None):
    p = ROOT / fname
    if p.exists():
        try: return json.loads(p.read_text())
        except: pass
    return default if default is not None else []

def load_portfolio():
    if PORT_FILE.exists():
        try: return json.loads(PORT_FILE.read_text())
        except: pass
    return {"initial_balance":0,"current_balance":0,"balance_set":False,
            "open_positions":[],"closed_trades":[],"balance_history":[]}

def save_portfolio(p):
    PORT_FILE.write_text(json.dumps(p, indent=2, default=str))

def now_str():
    return datetime.now(WIB).isoformat()

def fmt_time(ts):
    if not ts: return "—"
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=WIB)
        else: dt = dt.astimezone(WIB)
        return dt.strftime("%d %b %H:%M")
    except: return str(ts)[:16]

def fmt_usdt(v, sign=True):
    if v is None: return "—"
    color = "#00e676" if v >= 0 else "#ff4757"
    s = f"+${v:,.2f}" if (sign and v > 0) else f"${v:,.2f}"
    return f'<span style="color:{color};font-weight:700">{s}</span>'

# ── CSS ──────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap');
html,body,[class*="css"]{ font-family:'Inter',sans-serif!important }
[data-testid="stAppViewContainer"]{ background:#080b10 }
[data-testid="stHeader"]{ background:transparent }
section[data-testid="stSidebar"]{ background:#0d1117;border-right:1px solid #1e2a38 }
.block-container{ padding:1.5rem 2rem 4rem!important;max-width:1500px!important }

.kpi{ background:#0f1621;border:1px solid #1e2a38;border-radius:14px;padding:20px 22px;position:relative;overflow:hidden;height:100% }
.kpi::before{ content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,#00d4ff,#7c3aed) }
.kpi-label{ font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:6px }
.kpi-val{ font-size:28px;font-weight:900;font-family:'JetBrains Mono',monospace;line-height:1 }
.kpi-sub{ font-size:11px;color:#64748b;margin-top:5px }

.card{ background:#0f1621;border:1px solid #1e2a38;border-radius:14px;padding:20px;margin-bottom:12px }
.tbl{ width:100%;border-collapse:collapse;font-size:13px }
.tbl th{ background:#131920;color:#64748b;text-transform:uppercase;letter-spacing:1px;font-size:11px;font-weight:700;padding:10px 14px;text-align:left;border-bottom:1px solid #1e2a38 }
.tbl td{ padding:11px 14px;border-bottom:1px solid #0f1621;vertical-align:middle }
.tbl tr:hover td{ background:rgba(255,255,255,.02) }
.mono{ font-family:'JetBrains Mono',monospace }

.sec-label{ font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#00d4ff;margin-bottom:4px }
.sec-title{ font-size:20px;font-weight:800;letter-spacing:-.5px;color:#e2e8f0;margin-bottom:16px }
.chip{ display:inline-block;background:#131920;border:1px solid #1e2a38;border-radius:6px;padding:3px 9px;font-size:11px;color:#94a3b8;margin:2px }
.prog-wrap{ background:#1e2a38;border-radius:100px;height:7px;overflow:hidden;margin-top:6px }
.prog-fill{ height:100%;border-radius:100px }
.sync-badge{ display:inline-flex;align-items:center;gap:6px;background:#00e67622;border:1px solid #00e67644;color:#00e676;font-size:11px;font-weight:700;padding:3px 12px;border-radius:100px }
.err-badge{ display:inline-flex;align-items:center;gap:6px;background:#ff475722;border:1px solid #ff475744;color:#ff4757;font-size:11px;font-weight:700;padding:3px 12px;border-radius:100px }
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(1.4)}}
.live-dot{ display:inline-block;width:8px;height:8px;background:#00e676;border-radius:50%;animation:pulse 1.5s infinite;margin-right:5px }
</style>
""", unsafe_allow_html=True)

# ── load all data ─────────────────────────────────────────
outcomes    = load_json("signal_outcomes.json", [])
pending     = load_json("pending_signals.json", [])
lessons_raw = load_json("lessons.json", {})
lessons     = lessons_raw.get("lessons", []) if isinstance(lessons_raw, dict) else lessons_raw
decisions   = load_json("decision_log.json", [])
port        = load_portfolio()

# Google Sheets data
gs_trades, gs_trades_err   = fetch_trades()
gs_balance, gs_balance_err = fetch_balance_history()
gs_ok = gs_trades_err is None and gs_balance_err is None

# ── Derived stats dari Sheets ─────────────────────────────
def parse_float(v):
    try: return float(str(v).replace(",","").replace("$","").strip())
    except: return 0.0

if gs_trades:
    all_closed = gs_trades  # list of dicts dari get_all_records()
    gs_pnl_usdt   = [parse_float(t.get("PnL (USDT)", 0)) for t in all_closed]
    gs_total_pnl  = sum(gs_pnl_usdt)
    gs_wins       = sum(1 for t in all_closed if str(t.get("Result","")).upper()=="WIN")
    gs_losses     = sum(1 for t in all_closed if str(t.get("Result","")).upper()=="LOSS")
    gs_wr         = gs_wins / len(all_closed) * 100 if all_closed else 0
    gs_best       = max(gs_pnl_usdt, default=0)
    gs_worst      = min(gs_pnl_usdt, default=0)
    gs_wins_vals  = [p for p in gs_pnl_usdt if p > 0]
    gs_loss_vals  = [p for p in gs_pnl_usdt if p < 0]
    gs_pf         = abs(sum(gs_wins_vals)/sum(gs_loss_vals)) if sum(gs_loss_vals) != 0 else 0
    gs_avg_win    = sum(gs_wins_vals)/len(gs_wins_vals) if gs_wins_vals else 0
    gs_avg_loss   = sum(gs_loss_vals)/len(gs_loss_vals) if gs_loss_vals else 0
else:
    all_closed    = []
    gs_total_pnl = gs_wins = gs_losses = gs_wr = gs_best = gs_worst = gs_pf = 0
    gs_avg_win = gs_avg_loss = 0

if gs_balance:
    cur_bal  = parse_float(gs_balance[-1].get("Balance After (USDT)", 0))
    init_bal = parse_float(gs_balance[0].get("Balance After (USDT)", 0))
else:
    cur_bal  = port.get("current_balance", 0)
    init_bal = port.get("initial_balance", 0)

roi = (cur_bal - init_bal) / init_bal * 100 if init_bal else 0

# bot signal stats
total    = len(outcomes)
tp_hit   = sum(1 for s in outcomes if s["status"] == "TP_HIT")
sl_hit   = sum(1 for s in outcomes if s["status"] == "SL_HIT")
exp_w    = sum(1 for s in outcomes if s["status"] == "EXPIRED_WIN")
win_rate = (tp_hit + exp_w) / total * 100 if total else 0
pnls     = [s.get("pnl_pct", 0) for s in outcomes if s.get("pnl_pct") is not None]

# open positions dari portfolio lokal
open_pos    = port.get("open_positions", [])
open_margin = sum(p.get("margin", 0) for p in open_pos)
unrealized  = sum(p.get("unrealized_pnl", 0) for p in open_pos)

# ── SIDEBAR ───────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style='display:flex;align-items:center;gap:10px;margin-bottom:20px'>
      <div style='width:8px;height:8px;background:#00d4ff;border-radius:50%;box-shadow:0 0 8px #00d4ff'></div>
      <span style='font-size:17px;font-weight:800'>CryptoBot <span style="color:#00d4ff">v13</span></span>
    </div>
    """, unsafe_allow_html=True)

    # Status koneksi Sheets
    if gs_ok:
        st.markdown(f'<div class="sync-badge">● Sheets terhubung · {datetime.now(WIB).strftime("%H:%M")}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="err-badge">✕ Sheets offline</div>', unsafe_allow_html=True)
        if gs_trades_err:
            st.caption(f"Error: {gs_trades_err[:80]}")

    st.divider()

    # ── Setup Sheets credentials (kalau belum terhubung)
    if not gs_ok:
        with st.expander("⚙️ Setup Google Sheets", expanded=True):
            st.markdown("""
            **Cara setup:**
            1. Buka [Streamlit Cloud App Settings](https://share.streamlit.io)
            2. Masuk ke app → **Settings → Secrets**
            3. Paste format berikut:

            ```toml
            SPREADSHEET_ID = "id_spreadsheet_kamu"

            [google]
            type = "service_account"
            project_id = "..."
            private_key_id = "..."
            private_key = "-----BEGIN RSA PRIVATE KEY-----\\n..."
            client_email = "...@....iam.gserviceaccount.com"
            client_id = "..."
            token_uri = "https://oauth2.googleapis.com/token"
            ```
            """)

    st.markdown("### 💼 Open Positions")
    if open_pos:
        for p in open_pos:
            sign  = 1 if p["direction"]=="LONG" else -1
            cur   = p.get("current_price", p["entry"])
            unr   = sign*(cur-p["entry"])/p["entry"]*p["position_size"]
            unr_c = "#00e676" if unr>=0 else "#ff4757"
            st.markdown(f"""
            <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px;margin-bottom:8px'>
              <div style='display:flex;justify-content:space-between'>
                <span style='font-weight:800;font-family:"JetBrains Mono"'>{p['coin']}</span>
                <span style='color:{"#00e676" if p["direction"]=="LONG" else "#ff4757"};font-weight:700'>{'▲' if p['direction']=='LONG' else '▼'} {p['direction']}</span>
              </div>
              <div style='font-size:12px;color:#64748b;margin-top:4px'>Entry: ${p['entry']:,.4f} · {p['leverage']}x</div>
              <div style='font-size:14px;color:{unr_c};font-weight:700;font-family:"JetBrains Mono";margin-top:4px'>{unr:+,.2f} USDT</div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.caption("Tidak ada posisi terbuka")

    st.divider()

    # Balance summary sidebar
    bal_c = "#00e676" if roi >= 0 else "#ff4757"
    st.markdown(f"""
    <div style='margin-bottom:8px'>
      <div style='font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px'>
        Balance {'(dari Sheets)' if gs_ok else '(lokal)'}
      </div>
      <div style='font-size:26px;font-weight:900;font-family:"JetBrains Mono";color:#e2e8f0'>${cur_bal:,.2f}</div>
      <div style='font-size:12px;color:{bal_c};margin-top:2px'>{'+' if roi>=0 else ''}{roi:.2f}% ROI dari ${init_bal:,.2f}</div>
    </div>
    <div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px'>
      <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px'>
        <div style='font-size:10px;color:#64748b;margin-bottom:4px'>REALIZED P&L</div>
        <div style='font-size:16px;font-weight:800;font-family:"JetBrains Mono";color:{"#00e676" if gs_total_pnl>=0 else "#ff4757"}'>{'+$' if gs_total_pnl>=0 else '-$'}{abs(gs_total_pnl):,.2f}</div>
      </div>
      <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px'>
        <div style='font-size:10px;color:#64748b;margin-bottom:4px'>UNREALIZED</div>
        <div style='font-size:16px;font-weight:800;font-family:"JetBrains Mono";color:{"#00e676" if unrealized>=0 else "#ff4757"}'>{unrealized:+,.2f}</div>
      </div>
      <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px'>
        <div style='font-size:10px;color:#64748b;margin-bottom:4px'>WIN RATE</div>
        <div style='font-size:16px;font-weight:800;font-family:"JetBrains Mono";color:{"#00e676" if gs_wr>=50 else "#ff4757"}'>{gs_wr:.0f}%</div>
      </div>
      <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px'>
        <div style='font-size:10px;color:#64748b;margin-bottom:4px'>TRADES</div>
        <div style='font-size:16px;font-weight:800;font-family:"JetBrains Mono";color:#00d4ff'>{len(all_closed)}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    st.divider()
    st.markdown(f"""
    <div style='font-size:12px;color:#64748b;text-align:center'>
      <span class='live-dot'></span>Auto-refresh 30 detik<br>
      {datetime.now(WIB).strftime('%d %b %Y %H:%M WIB')}<br>
      {len(pending)} pending bot · {total} sinyal tracked
    </div>
    """, unsafe_allow_html=True)

# ── HEADER ────────────────────────────────────────────────
hc1, hc2 = st.columns([3,1])
with hc1:
    st.markdown("""
    <div style='display:flex;align-items:center;gap:12px;margin-bottom:4px'>
      <div style='width:10px;height:10px;background:#00d4ff;border-radius:50%;box-shadow:0 0 10px #00d4ff'></div>
      <span style='font-size:22px;font-weight:900;letter-spacing:-.5px'>CryptoBot <span style="color:#00d4ff">v13</span> · Personal Dashboard</span>
    </div>
    """, unsafe_allow_html=True)
with hc2:
    if gs_ok:
        st.markdown(f'<div style="text-align:right;padding-top:6px"><span class="sync-badge">● Live dari Sheets · {datetime.now(WIB).strftime("%H:%M:%S")}</span></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="text-align:right;padding-top:6px"><span class="err-badge">✕ Sheets offline — setup di sidebar</span></div>', unsafe_allow_html=True)

st.divider()

# ── TABS ──────────────────────────────────────────────────
tab_port, tab_open, tab_pending, tab_history, tab_coins, tab_lessons, tab_log = st.tabs([
    "📊 Portfolio",
    f"📍 Open Positions ({len(open_pos)})",
    f"⏳ Bot Pending ({len(pending)})",
    f"📋 Trade History ({len(all_closed)})",
    f"🪙 Coin Stats",
    f"🧠 Lessons ({len(lessons)})",
    f"📡 Decision Log",
])

# ══════════════════════════════════════════════════════════
# TAB 0 — PORTFOLIO OVERVIEW
# ══════════════════════════════════════════════════════════
with tab_port:
    # KPI row
    c1,c2,c3,c4,c5,c6 = st.columns(6)
    kpis = [
        (c1, "Balance", f"${cur_bal:,.2f}", f"ROI {roi:+.2f}%", "#00d4ff"),
        (c2, "Realized P&L", f"${gs_total_pnl:+,.2f}", f"{len(all_closed)} trades ditutup",
         "#00e676" if gs_total_pnl>=0 else "#ff4757"),
        (c3, "Unrealized", f"${unrealized:+,.2f}", f"{len(open_pos)} posisi terbuka · margin ${open_margin:,.2f}",
         "#00e676" if unrealized>=0 else "#ff4757"),
        (c4, "Win Rate", f"{gs_wr:.1f}%", f"{gs_wins} win · {gs_losses} loss",
         "#00e676" if gs_wr>=50 else "#ff4757"),
        (c5, "Profit Factor", f"{gs_pf:.2f}", f"Avg win ${gs_avg_win:+,.2f} / loss ${gs_avg_loss:,.2f}",
         "#00e676" if gs_pf>=1 else "#ff4757"),
        (c6, "Best / Worst", f"${gs_best:+,.2f}", f"Worst: ${gs_worst:,.2f}", "#ffd32a"),
    ]
    for col,label,val,sub,color in kpis:
        with col:
            st.markdown(f"""
            <div class="kpi">
              <div class="kpi-label">{label}</div>
              <div class="kpi-val" style="color:{color}">{val}</div>
              <div class="kpi-sub">{sub}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    left, right = st.columns([1.4, 0.6])

    with left:
        st.markdown('<div class="sec-label">Equity Curve</div><div class="sec-title">Pertumbuhan Balance</div>', unsafe_allow_html=True)

        if gs_balance:
            import pandas as pd
            bal_rows = [{"Waktu": r.get("Timestamp",""), "Balance": parse_float(r.get("Balance After (USDT)",0))} for r in gs_balance]
            df_bal = pd.DataFrame(bal_rows)
            if not df_bal.empty:
                st.line_chart(df_bal.set_index("Waktu")["Balance"], height=240, use_container_width=True)
        elif all_closed:
            import pandas as pd
            cum, vals = 0, []
            for t in all_closed:
                cum += parse_float(t.get("PnL (USDT)", 0))
                vals.append({"Trade": t.get("Coin","?"), "Cumulative P&L": round(cum,2)})
            df_eq = pd.DataFrame(vals)
            st.line_chart(df_eq.set_index("Trade"), height=240, use_container_width=True)
        else:
            st.info("Belum ada data balance dari Sheets.")

        # Tabel 10 trade terakhir
        if all_closed:
            st.markdown('<div class="sec-label" style="margin-top:24px">Trade Terbaru</div>', unsafe_allow_html=True)
            recent_trades = list(reversed(all_closed))[:10]
            rows = ""
            for t in recent_trades:
                pnl   = parse_float(t.get("PnL (USDT)",0))
                pnlp  = parse_float(t.get("PnL (%)",0))
                res   = str(t.get("Result","")).upper()
                rc    = "#00e676" if res=="WIN" else "#ff4757" if res=="LOSS" else "#ffd32a"
                dc    = "#00e676" if str(t.get("Direction","")).upper()=="LONG" else "#ff4757"
                da    = "▲" if str(t.get("Direction","")).upper()=="LONG" else "▼"
                rows += f"""<tr>
                  <td style="color:#64748b;font-size:12px">{fmt_time(t.get('Timestamp',''))}</td>
                  <td class="mono" style="font-weight:700;color:#e2e8f0">{t.get('Coin','—')}</td>
                  <td style="color:{dc};font-weight:700">{da} {t.get('Direction','—')}</td>
                  <td class="mono">${parse_float(t.get('Entry Price',0)):,.4f}</td>
                  <td style="color:#94a3b8;font-size:12px">{t.get('Leverage','—')}x · ${parse_float(t.get('Margin (USDT)',0)):,.2f}</td>
                  <td><span style="background:{rc}22;color:{rc};border:1px solid {rc}55;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:700">{'✅ WIN' if res=='WIN' else '❌ LOSS' if res=='LOSS' else '➖ BE'}</span></td>
                  <td class="mono" style="color:{rc};font-weight:700">{pnl:+,.2f} ({pnlp:+.2f}%)</td>
                  <td style="color:#64748b;font-size:12px">{t.get('Note','')[:30]}</td>
                </tr>"""
            st.markdown(f"""
            <div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px">
              <table class="tbl">
                <thead><tr><th>Waktu</th><th>Coin</th><th>Arah</th><th>Entry</th><th>Size</th><th>Hasil</th><th>P&L</th><th>Note</th></tr></thead>
                <tbody>{rows}</tbody>
              </table>
            </div>""", unsafe_allow_html=True)

    with right:
        st.markdown('<div class="sec-label">Distribusi</div><div class="sec-title">Win vs Loss</div>', unsafe_allow_html=True)

        # Pie-like stats
        total_trades = len(all_closed)
        if total_trades:
            n_be = total_trades - gs_wins - gs_losses
            be_pct = n_be/total_trades*100 if total_trades else 0
            be_html = (
                f'<div style="margin-bottom:16px"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px">'
                f'<span style="color:#ffd32a;font-weight:700">➖ BE</span>'
                f'<span style="font-weight:700;color:#ffd32a">{n_be} ({be_pct:.1f}%)</span></div>'
                f'<div class="prog-wrap"><div class="prog-fill" style="width:{be_pct:.0f}%;background:#ffd32a"></div></div></div>'
            ) if n_be else ""
            st.markdown(f"""
            <div class="card">
              <div style="margin-bottom:16px">
                <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px">
                  <span style="color:#00e676;font-weight:700">✅ Win</span>
                  <span style="font-family:'JetBrains Mono';font-weight:700;color:#00e676">{gs_wins} ({gs_wr:.1f}%)</span>
                </div>
                <div class="prog-wrap"><div class="prog-fill" style="width:{gs_wr:.0f}%;background:#00e676"></div></div>
              </div>
              <div style="margin-bottom:16px">
                <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px">
                  <span style="color:#ff4757;font-weight:700">❌ Loss</span>
                  <span style="font-family:'JetBrains Mono';font-weight:700;color:#ff4757">{gs_losses} ({gs_losses/total_trades*100:.1f}%)</span>
                </div>
                <div class="prog-wrap"><div class="prog-fill" style="width:{gs_losses/total_trades*100:.0f}%;background:#ff4757"></div></div>
              </div>
              {be_html}
            </div>
            """, unsafe_allow_html=True)

            # Per-coin P&L dari Sheets
            coin_pnl = {}
            for t in all_closed:
                c = t.get("Coin","?")
                if c not in coin_pnl: coin_pnl[c] = {"pnl":0,"trades":0,"wins":0}
                coin_pnl[c]["pnl"]    += parse_float(t.get("PnL (USDT)",0))
                coin_pnl[c]["trades"] += 1
                if str(t.get("Result","")).upper()=="WIN": coin_pnl[c]["wins"] += 1

            st.markdown('<div class="sec-label" style="margin-top:20px">P&L per Coin</div>', unsafe_allow_html=True)
            for coin, cs in sorted(coin_pnl.items(), key=lambda x:-x[1]["pnl"]):
                wr_c = cs["wins"]/cs["trades"]*100 if cs["trades"] else 0
                pc   = "#00e676" if cs["pnl"]>=0 else "#ff4757"
                st.markdown(f"""
                <div style="display:flex;justify-content:space-between;padding:10px 14px;background:#0f1621;border:1px solid #1e2a38;border-radius:10px;margin-bottom:6px">
                  <div>
                    <span style="font-weight:800;font-family:'JetBrains Mono'">{coin}</span>
                    <span style="font-size:11px;color:#64748b;margin-left:8px">{cs['trades']} trade · {wr_c:.0f}% WR</span>
                  </div>
                  <span style="color:{pc};font-weight:700;font-family:'JetBrains Mono'">{cs['pnl']:+,.2f}</span>
                </div>""", unsafe_allow_html=True)
        else:
            st.info("Belum ada trade di Sheets.")

# ══════════════════════════════════════════════════════════
# TAB 1 — OPEN POSITIONS (manual input lokal)
# ══════════════════════════════════════════════════════════
with tab_open:
    st.markdown('<div class="sec-label">Live</div><div class="sec-title">Posisi Terbuka</div>', unsafe_allow_html=True)

    with st.expander("➕ Tambah Posisi Baru", expanded=len(open_pos)==0):
        with st.form("add_position", clear_on_submit=True):
            fc1,fc2 = st.columns(2)
            coin      = fc1.text_input("Coin", placeholder="BTC, ETH, SOL...").upper().replace("USDT","")
            direction = fc2.selectbox("Arah", ["LONG","SHORT"])
            fc3,fc4,fc5 = st.columns(3)
            entry    = fc3.number_input("Entry Price ($)", min_value=0.0, format="%.4f")
            margin   = fc4.number_input("Margin (USDT)", min_value=0.0, format="%.2f")
            leverage = fc5.number_input("Leverage (x)", min_value=1, max_value=125, value=1)
            fc6,fc7 = st.columns(2)
            tp_price = fc6.number_input("Take Profit ($)", min_value=0.0, format="%.4f")
            sl_price = fc7.number_input("Stop Loss ($)", min_value=0.0, format="%.4f")
            note     = st.text_input("Catatan", placeholder="Setup, confluence level...")
            if st.form_submit_button("🚀 Buka Posisi", use_container_width=True):
                if coin and entry > 0 and margin > 0:
                    pos = {"id":int(time.time()*1000),"coin":coin,"direction":direction,
                           "entry":entry,"margin":margin,"leverage":leverage,
                           "position_size":round(margin*leverage,2),
                           "tp":tp_price,"sl":sl_price,"current_price":entry,
                           "unrealized_pnl":0.0,"note":note,"opened_at":now_str()}
                    port.setdefault("open_positions",[]).append(pos)
                    save_portfolio(port)
                    st.success(f"✅ Posisi {coin} {direction} dibuka!")
                    st.rerun()
                else:
                    st.error("Lengkapi coin, entry price, dan margin.")

    if not open_pos:
        st.info("Tidak ada posisi terbuka. Tambah di atas atau masuk trade via bot Telegram.")
    else:
        for i, pos in enumerate(open_pos):
            sign  = 1 if pos["direction"]=="LONG" else -1
            cur   = pos.get("current_price", pos["entry"])
            unr   = sign*(cur-pos["entry"])/pos["entry"]*pos["position_size"]
            unrp  = sign*(cur-pos["entry"])/pos["entry"]*100
            pc    = "#00e676" if unr>=0 else "#ff4757"
            dc    = "#00e676" if pos["direction"]=="LONG" else "#ff4757"
            tp_d  = (pos["tp"]-pos["entry"])/pos["entry"]*100 if pos.get("tp") else None
            sl_d  = (pos["sl"]-pos["entry"])/pos["entry"]*100 if pos.get("sl") else None

            st.markdown(f"""
            <div class="card" style="border-left:3px solid {pc}">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div style="display:flex;align-items:center;gap:10px">
                  <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{pos['coin']}USDT</span>
                  <span style="color:{dc};font-weight:700">{'▲' if pos['direction']=='LONG' else '▼'} {pos['direction']}</span>
                  <span style="background:#1e2a38;color:#94a3b8;font-size:11px;padding:2px 8px;border-radius:6px">{pos['leverage']}x</span>
                </div>
                <span style="font-size:12px;color:#64748b">{fmt_time(pos.get('opened_at',''))}</span>
              </div>
              <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:12px">
                <div><div style="font-size:10px;color:#64748b">Entry</div><div class="mono" style="font-weight:700">${pos['entry']:,.4f}</div></div>
                <div><div style="font-size:10px;color:#64748b">Pos Size</div><div class="mono">${pos['position_size']:,.2f}</div></div>
                <div><div style="font-size:10px;color:#64748b">TP</div><div class="mono" style="color:#00e676">${pos.get('tp',0):,.4f}{f' ({tp_d:+.2f}%)' if tp_d else ''}</div></div>
                <div><div style="font-size:10px;color:#64748b">SL</div><div class="mono" style="color:#ff4757">${pos.get('sl',0):,.4f}{f' ({sl_d:.2f}%)' if sl_d else ''}</div></div>
                <div><div style="font-size:10px;color:#64748b">Unrealized P&L</div><div class="mono" style="color:{pc};font-weight:800">{unr:+,.2f} ({unrp:+.2f}%)</div></div>
              </div>
              {f'<div style="font-size:12px;color:#64748b;margin-bottom:8px">📝 {pos["note"]}</div>' if pos.get("note") else ""}
            </div>""", unsafe_allow_html=True)

            cc1,cc2,cc3 = st.columns([2,1,1])
            with cc1:
                new_price = st.number_input(f"Harga terkini {pos['coin']}", min_value=0.0,
                    value=float(pos.get("current_price",pos["entry"])),
                    key=f"price_{pos['id']}", format="%.4f")
            with cc2:
                if st.button("🔄 Update", key=f"upd_{pos['id']}", use_container_width=True):
                    port["open_positions"][i]["current_price"] = new_price
                    unr_new = sign*(new_price-pos["entry"])/pos["entry"]*pos["position_size"]
                    port["open_positions"][i]["unrealized_pnl"] = round(unr_new,2)
                    save_portfolio(port)
                    st.rerun()
            with cc3:
                if st.button("✅ Tutup", key=f"close_{pos['id']}", use_container_width=True):
                    exit_price = pos.get("current_price",pos["entry"])
                    pnl_u  = sign*(exit_price-pos["entry"])/pos["entry"]*pos["position_size"]
                    pnl_p  = sign*(exit_price-pos["entry"])/pos["entry"]*100
                    trade  = {"id":pos["id"],"coin":pos["coin"],"direction":pos["direction"],
                              "entry":pos["entry"],"exit":exit_price,"margin":pos["margin"],
                              "leverage":pos["leverage"],"position_size":pos["position_size"],
                              "pnl_usdt":round(pnl_u,2),"pnl_pct":round(pnl_p,2),
                              "result":"WIN" if pnl_u>0 else ("LOSS" if pnl_u<0 else "BREAKEVEN"),
                              "note":pos.get("note",""),"opened_at":pos.get("opened_at",""),"closed_at":now_str()}
                    port["closed_trades"].append(trade)
                    port["open_positions"].pop(i)
                    port["current_balance"] = round(port.get("current_balance",0)+pnl_u,2)
                    save_portfolio(port)
                    st.success(f"Ditutup · P&L ${pnl_u:+,.2f}")
                    st.rerun()

# ══════════════════════════════════════════════════════════
# TAB 2 — BOT PENDING
# ══════════════════════════════════════════════════════════
with tab_pending:
    st.markdown('<div class="sec-label">Live Watchlist Bot</div><div class="sec-title">Sinyal Pending</div>', unsafe_allow_html=True)
    if not pending:
        st.info("Tidak ada sinyal pending saat ini.")
    else:
        for s in pending:
            created  = datetime.fromisoformat(s["created_at"].replace("Z","+00:00")).astimezone(WIB)
            age_hrs  = (datetime.now(WIB)-created).total_seconds()/3600
            timeout  = s.get("timeout_hours",24)
            pct_done = min(age_hrs/timeout*100,100)
            dist_tp  = (s.get("tp",s["entry_price"])-s["entry_price"])/s["entry_price"]*100
            dist_sl  = (s.get("sl",s["entry_price"])-s["entry_price"])/s["entry_price"]*100
            reasons  = " ".join(f'<span class="chip">{r[:55]}</span>' for r in s.get("reasons",[])[:3])
            dc       = "#00e676" if s["direction"]=="LONG" else "#ff4757"
            bar_c    = "#ff4757" if pct_done>80 else "#ffd32a" if pct_done>50 else "#00d4ff"

            st.markdown(f"""
            <div class="card">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div style="display:flex;align-items:center;gap:10px">
                  <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{s['symbol']}</span>
                  <span style="color:{dc};font-weight:700">{'▲' if s['direction']=='LONG' else '▼'} {s['direction']}</span>
                  <span style="background:#00d4ff22;color:#00d4ff;border:1px solid #00d4ff44;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{s.get('signal_type','—')}</span>
                </div>
                <span style="color:#ffd32a;font-weight:700;font-family:'JetBrains Mono'">{s.get('score','—')}/100</span>
              </div>
              <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px">
                <div><div style="font-size:10px;color:#64748b">Entry</div><div class="mono" style="font-weight:700">${s['entry_price']:,.4f}</div></div>
                <div><div style="font-size:10px;color:#64748b">TP</div><div class="mono" style="color:#00e676">${s.get('tp',0):,.4f} ({dist_tp:+.2f}%)</div></div>
                <div><div style="font-size:10px;color:#64748b">SL</div><div class="mono" style="color:#ff4757">${s.get('sl',0):,.4f} ({dist_sl:.2f}%)</div></div>
                <div><div style="font-size:10px;color:#64748b">Confluence</div><div style="font-weight:700">{s.get('confluence_level','—')}</div></div>
              </div>
              <div style="margin-bottom:10px">{reasons}</div>
              <div style="display:flex;justify-content:space-between;font-size:12px;color:#64748b;margin-bottom:5px">
                <span>Berjalan: <b style="color:#e2e8f0">{age_hrs:.1f}h / {timeout}h</b></span>
                <span>{fmt_time(s.get('created_at',''))}</span>
              </div>
              <div class="prog-wrap"><div class="prog-fill" style="width:{pct_done:.0f}%;background:{bar_c}"></div></div>
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB 3 — FULL TRADE HISTORY dari Sheets
# ══════════════════════════════════════════════════════════
with tab_history:
    st.markdown('<div class="sec-label">Google Sheets</div><div class="sec-title">Semua Trade</div>', unsafe_allow_html=True)

    if not gs_ok:
        st.warning("Sambungkan Google Sheets dulu untuk melihat full history.")
        # fallback: tampilkan signal outcomes bot
        st.markdown("**Signal outcomes bot (fallback):**")

    f1,f2 = st.columns(2)
    fdir  = f1.multiselect("Filter Arah", ["LONG","SHORT"])
    fres  = f2.multiselect("Filter Hasil", ["WIN","LOSS","BREAKEVEN"])

    source = all_closed if gs_ok else []
    if fdir: source = [t for t in source if str(t.get("Direction","")).upper() in fdir]
    if fres: source = [t for t in source if str(t.get("Result","")).upper() in fres]

    source = list(reversed(source))
    rows = ""
    for t in source:
        pnl  = parse_float(t.get("PnL (USDT)",0))
        pnlp = parse_float(t.get("PnL (%)",0))
        res  = str(t.get("Result","")).upper()
        rc   = "#00e676" if res=="WIN" else "#ff4757" if res=="LOSS" else "#ffd32a"
        dc   = "#00e676" if str(t.get("Direction","")).upper()=="LONG" else "#ff4757"
        da   = "▲" if str(t.get("Direction","")).upper()=="LONG" else "▼"
        rows += f"""<tr>
          <td style="color:#64748b;font-size:12px">{fmt_time(t.get('Timestamp',''))}</td>
          <td class="mono" style="font-weight:700;color:#e2e8f0">{t.get('Coin','—')}</td>
          <td style="color:{dc};font-weight:700">{da} {t.get('Direction','—')}</td>
          <td class="mono">${parse_float(t.get('Entry Price',0)):,.4f}</td>
          <td style="color:#94a3b8;font-size:12px">{t.get('Leverage','—')}x</td>
          <td class="mono" style="color:#64748b">${parse_float(t.get('Margin (USDT)',0)):,.2f}</td>
          <td><span style="background:{rc}22;color:{rc};border:1px solid {rc}55;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:700">{'✅ WIN' if res=='WIN' else '❌ LOSS' if res=='LOSS' else '➖ BE'}</span></td>
          <td class="mono" style="color:{rc};font-weight:700">{pnl:+,.2f}<br><span style="font-size:11px">{pnlp:+.2f}%</span></td>
          <td style="color:#64748b;font-size:11px">{str(t.get('Note',''))[:35]}</td>
        </tr>"""

    if rows:
        st.markdown(f"""
        <div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px;margin-top:8px">
          <table class="tbl">
            <thead><tr><th>Waktu</th><th>Coin</th><th>Arah</th><th>Entry</th><th>Lev</th><th>Margin</th><th>Hasil</th><th>P&L</th><th>Note</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        <div style="color:#64748b;font-size:12px;margin-top:8px">{len(source)} trades</div>""", unsafe_allow_html=True)
    elif gs_ok:
        st.info("Belum ada trade di spreadsheet.")

# ══════════════════════════════════════════════════════════
# TAB 4 — COIN STATS (dari bot signals)
# ══════════════════════════════════════════════════════════
with tab_coins:
    st.markdown('<div class="sec-label">Bot Signals</div><div class="sec-title">Performa Per Coin</div>', unsafe_allow_html=True)
    coin_stats = {}
    for s in outcomes:
        sym = s["symbol"]
        if sym not in coin_stats: coin_stats[sym] = {"total":0,"wins":0,"losses":0,"pnl":0.0,"signals":[]}
        coin_stats[sym]["total"] += 1
        if s["status"] in ("TP_HIT","EXPIRED_WIN"): coin_stats[sym]["wins"] += 1
        elif s["status"] in ("SL_HIT","EXPIRED_LOSS"): coin_stats[sym]["losses"] += 1
        coin_stats[sym]["pnl"] += s.get("pnl_pct",0)
        coin_stats[sym]["signals"].append(s)

    sort_by = st.selectbox("Urutkan", ["Win Rate","Total P&L","Total Trades"])
    sorted_coins = sorted(coin_stats.items(),
        key=lambda x:(x[1]["wins"]/x[1]["total"]*100 if x[1]["total"] else 0) if sort_by=="Win Rate"
        else x[1]["pnl"] if sort_by=="Total P&L" else x[1]["total"], reverse=True)

    def dot_color(st_):
        if st_ in ("TP_HIT","EXPIRED_WIN"): return "#00e676"
        if st_ == "SL_HIT": return "#ff4757"
        return "#ffd32a"

    for sym, stat in sorted_coins:
        wr  = stat["wins"]/stat["total"]*100 if stat["total"] else 0
        pnl = stat["pnl"]
        wc  = "#00e676" if wr>=60 else "#ffd32a" if wr>=40 else "#ff4757"
        nc  = "#00e676" if pnl>=0 else "#ff4757"
        recent = sorted(stat["signals"], key=lambda x: x.get("created_at",""), reverse=True)[:5]
        dots = "".join(f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{dot_color(s["status"])};margin:1px" title="{s["status"]}"></span>' for s in recent)
        st.markdown(f"""
        <div class="card" style="margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="display:flex;align-items:center;gap:14px">
              <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{sym}</span>
              <span style="font-size:12px;color:#64748b">{stat['total']} sinyal</span>
              <span>5 terakhir: {dots}</span>
            </div>
            <div style="display:flex;gap:20px;text-align:right">
              <div><div style="font-size:10px;color:#64748b">Win Rate</div><div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono';color:{wc}">{wr:.0f}%</div></div>
              <div><div style="font-size:10px;color:#64748b">Total P&L</div><div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono';color:{nc}">{'+' if pnl>=0 else ''}{pnl:.2f}%</div></div>
              <div><div style="font-size:10px;color:#64748b">TP/SL</div><div style="font-size:16px;font-weight:700"><span style="color:#00e676">{stat['wins']}</span>/<span style="color:#ff4757">{stat['losses']}</span></div></div>
            </div>
          </div>
          <div class="prog-wrap" style="margin-top:10px"><div class="prog-fill" style="width:{wr:.0f}%;background:{wc}"></div></div>
        </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB 5 — LESSONS
# ══════════════════════════════════════════════════════════
with tab_lessons:
    st.markdown('<div class="sec-label">Learning Engine</div><div class="sec-title">Lessons Bot</div>', unsafe_allow_html=True)
    if not lessons:
        st.info("Belum ada lessons.")
    else:
        of = st.selectbox("Filter", ["Semua","good","poor","neutral"])
        shown = lessons if of=="Semua" else [l for l in lessons if l.get("outcome")==of]
        for les in sorted(shown, key=lambda x:x.get("created_at",""), reverse=True)[:40]:
            out = les.get("outcome","—")
            c   = "#00e676" if out=="good" else "#ff4757" if out=="poor" else "#ffd32a"
            p   = les.get("pnl_pct")
            ps  = f' · P&L: <span style="color:{"#00e676" if p and p>=0 else "#ff4757"}">{p:+.2f}%</span>' if p is not None else ""
            tags= " ".join(f'<span class="chip">{t}</span>' for t in les.get("tags",[]))
            st.markdown(f"""
            <div class="card" style="margin-bottom:8px;border-left:3px solid {c}">
              <div style="display:flex;justify-content:space-between;margin-bottom:8px">
                <div><span style="background:{c}22;color:{c};border:1px solid {c}55;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{out.upper()}</span>
                  <span style="font-size:11px;color:#64748b;margin-left:10px">conf {les.get('confidence',0):.0%}{ps}</span></div>
                <span style="font-size:11px;color:#64748b">{fmt_time(les.get('created_at',''))}</span>
              </div>
              <div style="font-size:14px;color:#cbd5e1;line-height:1.6">{les.get('rule','')}</div>
              <div style="margin-top:8px">{tags}</div>
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB 6 — DECISION LOG
# ══════════════════════════════════════════════════════════
with tab_log:
    st.markdown('<div class="sec-label">Bot Activity</div><div class="sec-title">Decision Log</div>', unsafe_allow_html=True)
    if not decisions:
        st.info("Belum ada decision log.")
    else:
        recent = sorted(decisions, key=lambda x:x.get("ts",""), reverse=True)[:60]
        rows = ""
        for d in recent:
            dec = d.get("decision","—")
            c   = "#00e676" if dec in ("ALERT","PASS") else "#ffd32a" if dec=="WATCH" else "#64748b"
            top = " ".join(f'<span class="chip">{r[:50]}</span>' for r in (d.get("top_reasons") or [])[:2])
            rows += f"""<tr>
              <td style="color:#64748b;font-size:12px">{fmt_time(d.get('ts',''))}</td>
              <td class="mono" style="font-weight:700">{d.get('symbol','—')}</td>
              <td style="font-size:12px;color:#94a3b8">{d.get('actor','—')}</td>
              <td><span style="background:{c}22;color:{c};border:1px solid {c}55;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:700">{dec}</span></td>
              <td style="color:#ffd32a;font-family:'JetBrains Mono';font-weight:700">{d.get('score','—')}</td>
              <td>{top}</td>
            </tr>"""
        st.markdown(f"""
        <div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px">
          <table class="tbl">
            <thead><tr><th>Waktu</th><th>Symbol</th><th>Actor</th><th>Decision</th><th>Score</th><th>Reasons</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>""", unsafe_allow_html=True)

st.markdown("""
<div style="text-align:center;color:#1e2a38;font-size:12px;margin-top:48px;border-top:1px solid #1e2a38;padding-top:24px">
  CryptoBot v13 · Personal Dashboard · Bukan saran finansial
</div>""", unsafe_allow_html=True)
