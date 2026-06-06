import streamlit as st
import json, pathlib, time, requests
from datetime import datetime, timezone, timedelta

st.set_page_config(
    page_title="CryptoBot v13 — Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=30_000, key="ar")

ROOT       = pathlib.Path(__file__).parent.parent
HERE       = pathlib.Path(__file__).parent
PORT_FILE  = HERE / "my_portfolio.json"
SHEET_FILE = HERE / "sheet_config.json"
WIB        = timezone(timedelta(hours=7))

# ── Supabase helpers ─────────────────────────────────────
def _sb_cfg():
    url = _secret("SUPABASE_URL", "") or ""
    key = _secret("SUPABASE_ANON_KEY", "") or ""
    return url.rstrip("/"), key

def _sb_headers(key):
    return {"apikey": key, "Authorization": f"Bearer {key}"}

@st.cache_data(ttl=30)
def sb_fetch(table, order="created_at.desc", limit=500):
    url, key = _sb_cfg()
    if not url or not key:
        return [], "SUPABASE_URL / SUPABASE_ANON_KEY belum diset di Secrets"
    try:
        r = requests.get(
            f"{url}/rest/v1/{table}",
            headers=_sb_headers(key),
            params={"select": "*", "order": order, "limit": limit},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json(), None
        return [], f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return [], str(e)

@st.cache_data(ttl=60)
def sb_connected():
    url, key = _sb_cfg()
    if not url or not key:
        return False
    try:
        r = requests.get(f"{url}/rest/v1/trades",
                         headers=_sb_headers(key),
                         params={"select":"id","limit":1}, timeout=5)
        return r.status_code == 200
    except Exception:
        return False

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
    return {"open_positions": []}

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

def pf(v):
    try: return float(v)
    except: return 0.0

# ── Google Sheets / Spreadsheet helpers ───────────────────
import re, io, csv as _csv

def parse_sheet_id(text):
    """Ambil spreadsheet ID + gid dari URL Google Sheets atau ID mentah."""
    text = (text or "").strip()
    if not text:
        return "", ""
    gid = "0"
    m_gid = re.search(r"[#&?]gid=(\d+)", text)
    if m_gid:
        gid = m_gid.group(1)
    m_id = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    if m_id:
        return m_id.group(1), gid
    # mungkin ID mentah (tanpa URL)
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", text):
        return text, gid
    return "", ""

def sheet_csv_url(sheet_id, gid="0"):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def _secret(key, default=""):
    """Akses Secrets dengan aman — tidak crash bila secrets.toml tidak ada."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

def load_sheet_cfg():
    if SHEET_FILE.exists():
        try: return json.loads(SHEET_FILE.read_text())
        except: pass
    # fallback ke Secrets bila ada
    return {"url": _secret("REPORT_SHEET_URL", "")}

def save_sheet_cfg(cfg):
    try: SHEET_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception: pass

@st.cache_data(ttl=60, show_spinner=False)
def fetch_sheet(sheet_id, gid):
    """Baca Google Sheet (yang sudah dipublish/anyone-with-link) sebagai DataFrame."""
    import pandas as pd
    url = sheet_csv_url(sheet_id, gid)
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code} — pastikan sheet di-share 'Anyone with the link'."
        text = r.content.decode("utf-8", errors="replace")
        if text.lstrip().lower().startswith("<!doctype html") or "<html" in text[:200].lower():
            return None, "Sheet belum publik. Buka Share → 'Anyone with the link: Viewer'."
        df = pd.read_csv(io.StringIO(text))
        return df, None
    except Exception as e:
        return None, str(e)

def rows_to_csv(rows, columns=None):
    """List[dict] → bytes CSV (UTF-8 BOM agar rapi di Excel/Sheets)."""
    if not rows:
        return ("﻿" + (",".join(columns) if columns else "")).encode("utf-8")
    if columns is None:
        columns = list({k for r in rows for k in r.keys()})
    buf = io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    return ("﻿" + buf.getvalue()).encode("utf-8")

# ── CSS ──────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap');
html,body,[class*="css"]{ font-family:'Inter',sans-serif!important }
[data-testid="stAppViewContainer"]{ background:#080b10 }
[data-testid="stHeader"]{ background:transparent }
section[data-testid="stSidebar"]{ background:#0d1117;border-right:1px solid #1e2a38 }
.block-container{ padding:1.5rem 2rem 4rem!important;max-width:1500px!important }
.kpi{ background:#0f1621;border:1px solid #1e2a38;border-radius:14px;padding:20px 22px;position:relative;overflow:hidden }
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
.ok-badge{ display:inline-flex;align-items:center;gap:6px;background:#00e67622;border:1px solid #00e67644;color:#00e676;font-size:11px;font-weight:700;padding:3px 12px;border-radius:100px }
.err-badge{ display:inline-flex;align-items:center;gap:6px;background:#ff475722;border:1px solid #ff475744;color:#ff4757;font-size:11px;font-weight:700;padding:3px 12px;border-radius:100px }
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}} .live-dot{display:inline-block;width:8px;height:8px;background:#00e676;border-radius:50%;animation:pulse 1.5s infinite;margin-right:5px}
.dl-note{ font-size:12px;color:#64748b;margin:2px 0 14px }
.step-box{ background:#0f1621;border:1px solid #1e2a38;border-radius:12px;padding:16px 18px;margin-bottom:10px }
.step-box b{ color:#e2e8f0 }
.learn-q{ font-weight:700;color:#00d4ff;font-size:15px;margin-bottom:6px }
.learn-a{ color:#cbd5e1;font-size:14px;line-height:1.7 }
.gloss{ display:grid;grid-template-columns:160px 1fr;gap:10px 16px;font-size:14px }
.gloss .t{ font-weight:700;color:#00d4ff;font-family:'JetBrains Mono',monospace }
.gloss .d{ color:#94a3b8;line-height:1.6 }

/* ── RESPONSIVE: HP / tablet / laptop ─────────────────────── */
/* Streamlit kolom otomatis menumpuk di layar sempit */
@media (max-width: 820px){
  .block-container{ padding:1rem 0.9rem 3rem!important }
  [data-testid="stHorizontalBlock"]{ flex-wrap:wrap!important;gap:8px!important }
  [data-testid="stHorizontalBlock"] > [data-testid="column"]{
    flex:1 1 calc(50% - 8px)!important; min-width:calc(50% - 8px)!important; width:auto!important;
  }
  .kpi-val{ font-size:22px }
  .kpi{ padding:14px 16px }
  .sec-title{ font-size:17px }
  .gloss{ grid-template-columns:1fr; gap:4px 0 }
  .gloss .t{ margin-top:8px }
  /* tabel: izinkan scroll horizontal, jangan dipotong */
  .tbl{ font-size:12px }
  .tbl th,.tbl td{ padding:8px 9px;white-space:nowrap }
}
@media (max-width: 480px){
  [data-testid="stHorizontalBlock"] > [data-testid="column"]{
    flex:1 1 100%!important; min-width:100%!important;
  }
  h1,.sec-title{ letter-spacing:-.5px }
  .kpi-val{ font-size:20px }
}
/* Tab list bisa di-scroll di HP, tidak terpotong */
[data-baseweb="tab-list"]{ overflow-x:auto!important; flex-wrap:nowrap!important; -webkit-overflow-scrolling:touch }
</style>
""", unsafe_allow_html=True)

# ── load data ─────────────────────────────────────────────
outcomes    = load_json("signal_outcomes.json", [])
pending     = load_json("pending_signals.json", [])
lessons_raw = load_json("lessons.json", {})
lessons     = lessons_raw.get("lessons", []) if isinstance(lessons_raw, dict) else lessons_raw
decisions   = load_json("decision_log.json", [])
port        = load_portfolio()
open_pos    = port.get("open_positions", [])
sheet_cfg   = load_sheet_cfg()

# Supabase data
connected          = sb_connected()
sb_trades, sb_err  = sb_fetch("trades", order="created_at.desc")
sb_balance, _      = sb_fetch("balance_log", order="created_at.asc")

# ── Derived stats dari Supabase ───────────────────────────
all_trades  = sb_trades   # list dicts: coin, direction, pnl_usdt, pnl_pct, result, ...
all_balance = sb_balance  # list dicts: balance_after, event, amount, ts

total_trades = len(all_trades)
wins   = sum(1 for t in all_trades if str(t.get("result","")).upper()=="WIN")
losses = sum(1 for t in all_trades if str(t.get("result","")).upper()=="LOSS")
wr     = wins/total_trades*100 if total_trades else 0
pnl_list    = [pf(t.get("pnl_usdt",0)) for t in all_trades]
total_pnl   = sum(pnl_list)
wins_pnl    = [p for p in pnl_list if p > 0]
loss_pnl    = [p for p in pnl_list if p < 0]
profit_factor = abs(sum(wins_pnl)/sum(loss_pnl)) if sum(loss_pnl) != 0 else 0
avg_win  = sum(wins_pnl)/len(wins_pnl) if wins_pnl else 0
avg_loss = sum(loss_pnl)/len(loss_pnl) if loss_pnl else 0
best     = max(pnl_list, default=0)
worst    = min(pnl_list, default=0)

cur_bal  = pf(all_balance[-1]["balance_after"]) if all_balance else 0
init_bal = pf(all_balance[0]["balance_after"])  if all_balance else 0
roi      = (cur_bal - init_bal) / init_bal * 100 if init_bal else 0

unrealized   = sum(p.get("unrealized_pnl", 0) for p in open_pos)
open_margin  = sum(p.get("margin", 0) for p in open_pos)

# bot signal stats
bot_total  = len(outcomes)
bot_tp     = sum(1 for s in outcomes if s["status"]=="TP_HIT")
bot_expw   = sum(1 for s in outcomes if s["status"]=="EXPIRED_WIN")
bot_wr     = (bot_tp+bot_expw)/bot_total*100 if bot_total else 0

# ══════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown('<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px"><div style="width:8px;height:8px;background:#00d4ff;border-radius:50%;box-shadow:0 0 8px #00d4ff"></div><span style="font-size:17px;font-weight:800">CryptoBot <span style="color:#00d4ff">v13</span></span></div>', unsafe_allow_html=True)

    if connected:
        st.markdown(f'<div class="ok-badge">● Supabase · {datetime.now(WIB).strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="err-badge">✕ Supabase offline</div>', unsafe_allow_html=True)

    if not connected:
        with st.expander("⚙️ Setup Supabase", expanded=True):
            st.markdown("""
**3 langkah setup:**

**1. Buat project** di [supabase.com](https://supabase.com) (gratis)

**2. Jalankan SQL ini** di Supabase → SQL Editor:
```sql
create table trades (
  id bigint primary key,
  ts text, coin text, direction text,
  entry_price float, margin_usdt float,
  leverage int, position_size float,
  pnl_usdt float, pnl_pct float,
  result text, note text,
  balance_after float,
  created_at timestamptz default now()
);
create table balance_log (
  id bigserial primary key,
  ts text, event text, amount float,
  balance_after float, note text,
  created_at timestamptz default now()
);
alter table trades enable row level security;
alter table balance_log enable row level security;
create policy "r" on trades for select using (true);
create policy "w" on trades for insert with check (true);
create policy "r2" on balance_log for select using (true);
create policy "w2" on balance_log for insert with check (true);
```

**3. Set Secrets** di Streamlit Cloud & Railway:
```
SUPABASE_URL = https://xxx.supabase.co
SUPABASE_ANON_KEY = eyJxxx...
```
> Anon key **aman** dipublish — bukan private key.
            """)

    st.divider()

    # Open positions mini view
    st.markdown("**📍 Posisi Terbuka**")
    if open_pos:
        for p in open_pos:
            sign = 1 if p["direction"]=="LONG" else -1
            unr  = sign*(p.get("current_price",p["entry"])-p["entry"])/p["entry"]*p["position_size"]
            uc   = "#00e676" if unr>=0 else "#ff4757"
            dc   = "#00e676" if p["direction"]=="LONG" else "#ff4757"
            st.markdown(f"""
            <div style="background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:10px;margin-bottom:6px">
              <div style="display:flex;justify-content:space-between">
                <span style="font-weight:800;font-family:'JetBrains Mono'">{p['coin']}</span>
                <span style="color:{dc};font-weight:700">{'▲' if p['direction']=='LONG' else '▼'} {p['direction']}</span>
              </div>
              <div style="font-size:11px;color:#64748b">${p['entry']:,.4f} · {p['leverage']}x · ${p['margin']:,.0f} margin</div>
              <div style="font-size:14px;color:{uc};font-weight:700;font-family:'JetBrains Mono'">{unr:+,.2f} USDT</div>
            </div>""", unsafe_allow_html=True)
    else:
        st.caption("Tidak ada posisi terbuka")

    st.divider()

    # Quick stats
    bc = "#00e676" if roi>=0 else "#ff4757"
    pc = "#00e676" if total_pnl>=0 else "#ff4757"
    st.markdown(f"""
    <div style="margin-bottom:12px">
      <div style="font-size:11px;color:#64748b;margin-bottom:4px">BALANCE</div>
      <div style="font-size:24px;font-weight:900;font-family:'JetBrains Mono'">${cur_bal:,.2f}</div>
      <div style="font-size:12px;color:{bc}">{roi:+.2f}% ROI dari ${init_bal:,.2f}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div style="background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:10px">
        <div style="font-size:10px;color:#64748b">P&L</div>
        <div style="font-size:15px;font-weight:800;font-family:'JetBrains Mono';color:{pc}">{total_pnl:+,.2f}</div>
      </div>
      <div style="background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:10px">
        <div style="font-size:10px;color:#64748b">WIN RATE</div>
        <div style="font-size:15px;font-weight:800;font-family:'JetBrains Mono';color:{'#00e676' if wr>=50 else '#ff4757'}">{wr:.0f}%</div>
      </div>
      <div style="background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:10px">
        <div style="font-size:10px;color:#64748b">UNREALIZED</div>
        <div style="font-size:15px;font-weight:800;font-family:'JetBrains Mono';color:{'#00e676' if unrealized>=0 else '#ff4757'}">{unrealized:+,.2f}</div>
      </div>
      <div style="background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:10px">
        <div style="font-size:10px;color:#64748b">TRADES</div>
        <div style="font-size:15px;font-weight:800;font-family:'JetBrains Mono';color:#00d4ff">{total_trades}</div>
      </div>
    </div>
    <div style="font-size:11px;color:#64748b;text-align:center;margin-top:16px">
      <span class="live-dot"></span>Auto-refresh 30 detik<br>
      {datetime.now(WIB).strftime('%d %b %Y %H:%M WIB')}
    </div>
    """, unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════
hc1, hc2 = st.columns([3,1])
with hc1:
    st.markdown('<div style="display:flex;align-items:center;gap:12px"><div style="width:10px;height:10px;background:#00d4ff;border-radius:50%;box-shadow:0 0 10px #00d4ff"></div><span style="font-size:22px;font-weight:900;letter-spacing:-.5px">CryptoBot <span style="color:#00d4ff">v13</span> · Dashboard</span></div>', unsafe_allow_html=True)
with hc2:
    badge = f'<div class="ok-badge">● Live · {datetime.now(WIB).strftime("%H:%M:%S")}</div>' if connected else '<div class="err-badge">✕ Supabase offline — setup di sidebar</div>'
    st.markdown(f'<div style="text-align:right;padding-top:6px">{badge}</div>', unsafe_allow_html=True)

st.divider()

# ══════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════
(tab_port, tab_open, tab_hist, tab_coins, tab_pending,
 tab_lessons, tab_log, tab_sheet, tab_learn) = st.tabs([
    "📊 Portfolio",
    f"📍 Open Positions ({len(open_pos)})",
    f"📋 Trade History ({total_trades})",
    "🪙 Coin Stats",
    f"⏳ Bot Pending ({len(pending)})",
    f"🧠 Lessons ({len(lessons)})",
    "📡 Decision Log",
    "📑 Spreadsheet",
    "📚 Belajar",
])

# ══════════════════════════════════════════════════════════
# TAB — PORTFOLIO
# ══════════════════════════════════════════════════════════
with tab_port:
    # KPI
    c1,c2,c3,c4,c5,c6 = st.columns(6)
    for col, label, val, sub, color in [
        (c1,"Balance",f"${cur_bal:,.2f}",f"ROI {roi:+.2f}% · init ${init_bal:,.2f}","#00d4ff"),
        (c2,"Realized P&L",f"${total_pnl:+,.2f}",f"{total_trades} closed trades","#00e676" if total_pnl>=0 else "#ff4757"),
        (c3,"Unrealized",f"${unrealized:+,.2f}",f"{len(open_pos)} open · margin ${open_margin:,.0f}","#00e676" if unrealized>=0 else "#ff4757"),
        (c4,"Win Rate",f"{wr:.1f}%",f"{wins} win · {losses} loss","#00e676" if wr>=50 else "#ff4757"),
        (c5,"Profit Factor",f"{profit_factor:.2f}",f"avg win ${avg_win:+,.2f} / loss ${avg_loss:,.2f}","#00e676" if profit_factor>=1 else "#ff4757"),
        (c6,"Best / Worst",f"${best:+,.2f}",f"worst: ${worst:,.2f}","#ffd32a"),
    ]:
        with col:
            st.markdown(f'<div class="kpi"><div class="kpi-label">{label}</div><div class="kpi-val" style="color:{color}">{val}</div><div class="kpi-sub">{sub}</div></div>', unsafe_allow_html=True)

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    lc, rc = st.columns([1.4, 0.6])

    with lc:
        st.markdown('<div class="sec-label">Equity Curve</div><div class="sec-title">Balance dari Waktu ke Waktu</div>', unsafe_allow_html=True)
        if all_balance:
            import pandas as pd
            df = pd.DataFrame([{"Waktu": r.get("ts",""), "Balance": pf(r.get("balance_after",0))} for r in all_balance])
            st.line_chart(df.set_index("Waktu")["Balance"], height=220, use_container_width=True)
        elif not connected:
            st.info("Hubungkan Supabase untuk melihat equity curve.")
        else:
            st.info("Belum ada data balance. Set balance via /setbalance di Telegram bot.")

        # 10 trade terakhir
        if all_trades:
            st.markdown('<div class="sec-label" style="margin-top:24px">Trade Terbaru</div>', unsafe_allow_html=True)
            rows = ""
            for t in all_trades[:10]:
                p    = pf(t.get("pnl_usdt",0))
                pp   = pf(t.get("pnl_pct",0))
                res  = str(t.get("result","")).upper()
                rc_  = "#00e676" if res=="WIN" else "#ff4757" if res=="LOSS" else "#ffd32a"
                dc_  = "#00e676" if str(t.get("direction","")).upper()=="LONG" else "#ff4757"
                da_  = "▲" if str(t.get("direction","")).upper()=="LONG" else "▼"
                rows += f"""<tr>
                  <td style="color:#64748b;font-size:12px">{fmt_time(t.get('ts',''))}</td>
                  <td class="mono" style="font-weight:700;color:#e2e8f0">{t.get('coin','—')}</td>
                  <td style="color:{dc_};font-weight:700">{da_} {t.get('direction','—')}</td>
                  <td class="mono">${pf(t.get('entry_price',0)):,.4f}</td>
                  <td style="color:#94a3b8;font-size:12px">{t.get('leverage','—')}x · ${pf(t.get('margin_usdt',0)):,.0f}</td>
                  <td><span style="background:{rc_}22;color:{rc_};border:1px solid {rc_}55;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:700">{'✅ WIN' if res=='WIN' else '❌ LOSS' if res=='LOSS' else '➖ BE'}</span></td>
                  <td class="mono" style="color:{rc_};font-weight:700">{p:+,.2f} ({pp:+.2f}%)</td>
                  <td style="color:#64748b;font-size:11px">{str(t.get('note',''))[:30]}</td>
                </tr>"""
            st.markdown(f"""<div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px">
              <table class="tbl"><thead><tr><th>Waktu</th><th>Coin</th><th>Arah</th><th>Entry</th><th>Size</th><th>Hasil</th><th>P&L</th><th>Note</th></tr></thead>
              <tbody>{rows}</tbody></table></div>""", unsafe_allow_html=True)

    with rc:
        st.markdown('<div class="sec-label">Distribusi</div><div class="sec-title">Win / Loss</div>', unsafe_allow_html=True)
        if total_trades:
            n_be   = total_trades - wins - losses
            be_pct = n_be/total_trades*100
            be_html = (
                f'<div style="margin-bottom:14px"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px">'
                f'<span style="color:#ffd32a;font-weight:700">➖ BE</span>'
                f'<span style="color:#ffd32a;font-weight:700">{n_be} ({be_pct:.1f}%)</span></div>'
                f'<div class="prog-wrap"><div class="prog-fill" style="width:{be_pct:.0f}%;background:#ffd32a"></div></div></div>'
            ) if n_be else ""
            st.markdown(f"""<div class="card">
              <div style="margin-bottom:14px"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px">
                <span style="color:#00e676;font-weight:700">✅ Win</span><span style="color:#00e676;font-weight:700">{wins} ({wr:.1f}%)</span></div>
                <div class="prog-wrap"><div class="prog-fill" style="width:{wr:.0f}%;background:#00e676"></div></div></div>
              <div style="margin-bottom:14px"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px">
                <span style="color:#ff4757;font-weight:700">❌ Loss</span><span style="color:#ff4757;font-weight:700">{losses} ({losses/total_trades*100:.1f}%)</span></div>
                <div class="prog-wrap"><div class="prog-fill" style="width:{losses/total_trades*100:.0f}%;background:#ff4757"></div></div></div>
              {be_html}
            </div>""", unsafe_allow_html=True)

        # P&L per coin
        coin_map = {}
        for t in all_trades:
            c = t.get("coin","?")
            if c not in coin_map: coin_map[c] = {"pnl":0,"n":0,"wins":0}
            coin_map[c]["pnl"]  += pf(t.get("pnl_usdt",0))
            coin_map[c]["n"]    += 1
            if str(t.get("result","")).upper()=="WIN": coin_map[c]["wins"] += 1

        st.markdown('<div class="sec-label" style="margin-top:20px">P&L per Coin</div>', unsafe_allow_html=True)
        for coin, cs in sorted(coin_map.items(), key=lambda x:-x[1]["pnl"]):
            wrc_ = cs["wins"]/cs["n"]*100 if cs["n"] else 0
            pc_  = "#00e676" if cs["pnl"]>=0 else "#ff4757"
            st.markdown(f"""<div style="display:flex;justify-content:space-between;padding:9px 14px;background:#0f1621;border:1px solid #1e2a38;border-radius:10px;margin-bottom:5px">
              <div><span style="font-weight:800;font-family:'JetBrains Mono'">{coin}</span>
              <span style="font-size:11px;color:#64748b;margin-left:8px">{cs['n']}t · {wrc_:.0f}% WR</span></div>
              <span style="color:{pc_};font-weight:700;font-family:'JetBrains Mono'">{cs['pnl']:+,.2f}</span>
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB — OPEN POSITIONS
# ══════════════════════════════════════════════════════════
with tab_open:
    st.markdown('<div class="sec-label">Manual Input</div><div class="sec-title">Posisi Terbuka</div>', unsafe_allow_html=True)
    st.caption("Posisi terbuka diinput manual di sini. Setelah tutup posisi via Telegram (/logtrade), data otomatis masuk ke tab Portfolio.")

    with st.expander("➕ Tambah Posisi", expanded=len(open_pos)==0):
        with st.form("add_pos", clear_on_submit=True):
            fc1,fc2 = st.columns(2)
            coin_in  = fc1.text_input("Coin", placeholder="BTC, ETH, SOL...").upper().replace("USDT","")
            dir_in   = fc2.selectbox("Arah", ["LONG","SHORT"])
            fc3,fc4,fc5 = st.columns(3)
            entry_in = fc3.number_input("Entry ($)", min_value=0.0, format="%.4f")
            margin_in= fc4.number_input("Margin (USDT)", min_value=0.0, format="%.2f")
            lev_in   = fc5.number_input("Leverage", min_value=1, max_value=125, value=1)
            fc6,fc7  = st.columns(2)
            tp_in    = fc6.number_input("TP ($)", min_value=0.0, format="%.4f")
            sl_in    = fc7.number_input("SL ($)", min_value=0.0, format="%.4f")
            note_in  = st.text_input("Catatan")
            if st.form_submit_button("🚀 Catat Posisi", use_container_width=True):
                if coin_in and entry_in>0 and margin_in>0:
                    port.setdefault("open_positions",[]).append({
                        "id": int(time.time()*1000), "coin": coin_in, "direction": dir_in,
                        "entry": entry_in, "margin": margin_in, "leverage": lev_in,
                        "position_size": round(margin_in*lev_in,2),
                        "tp": tp_in, "sl": sl_in,
                        "current_price": entry_in, "unrealized_pnl": 0.0,
                        "note": note_in, "opened_at": now_str(),
                    })
                    save_portfolio(port)
                    st.success(f"✅ {coin_in} {dir_in} dicatat"); st.rerun()
                else:
                    st.error("Lengkapi coin, entry, dan margin.")

    for i, pos in enumerate(open_pos):
        sign  = 1 if pos["direction"]=="LONG" else -1
        cur   = pos.get("current_price", pos["entry"])
        unr   = sign*(cur-pos["entry"])/pos["entry"]*pos["position_size"]
        unrp  = sign*(cur-pos["entry"])/pos["entry"]*100
        uc    = "#00e676" if unr>=0 else "#ff4757"
        dc    = "#00e676" if pos["direction"]=="LONG" else "#ff4757"
        tp_d  = (pos["tp"]-pos["entry"])/pos["entry"]*100 if pos.get("tp") else None
        sl_d  = (pos["sl"]-pos["entry"])/pos["entry"]*100 if pos.get("sl") else None

        st.markdown(f"""<div class="card" style="border-left:3px solid {uc}">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <div style="display:flex;align-items:center;gap:10px">
              <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{pos['coin']}USDT</span>
              <span style="color:{dc};font-weight:700">{'▲' if pos['direction']=='LONG' else '▼'} {pos['direction']}</span>
              <span style="background:#1e2a38;color:#94a3b8;font-size:11px;padding:2px 8px;border-radius:6px">{pos['leverage']}x</span>
            </div>
            <span style="font-size:12px;color:#64748b">{fmt_time(pos.get('opened_at',''))}</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:10px">
            <div><div style="font-size:10px;color:#64748b">Entry</div><div class="mono">${pos['entry']:,.4f}</div></div>
            <div><div style="font-size:10px;color:#64748b">Size</div><div class="mono">${pos['position_size']:,.2f}</div></div>
            <div><div style="font-size:10px;color:#64748b">TP</div><div class="mono" style="color:#00e676">${pos.get('tp',0):,.4f}{f' ({tp_d:+.2f}%)' if tp_d else ''}</div></div>
            <div><div style="font-size:10px;color:#64748b">SL</div><div class="mono" style="color:#ff4757">${pos.get('sl',0):,.4f}{f' ({sl_d:.2f}%)' if sl_d else ''}</div></div>
            <div><div style="font-size:10px;color:#64748b">Unrealized</div><div class="mono" style="color:{uc};font-weight:800">{unr:+,.2f} ({unrp:+.2f}%)</div></div>
          </div>
        </div>""", unsafe_allow_html=True)

        cc1,cc2,cc3 = st.columns([2,1,1])
        with cc1:
            new_price = st.number_input(f"Harga {pos['coin']} sekarang", min_value=0.0,
                value=float(pos.get("current_price",pos["entry"])), key=f"pr_{pos['id']}", format="%.4f")
        with cc2:
            if st.button("🔄 Update", key=f"u_{pos['id']}", use_container_width=True):
                port["open_positions"][i]["current_price"] = new_price
                port["open_positions"][i]["unrealized_pnl"] = round(sign*(new_price-pos["entry"])/pos["entry"]*pos["position_size"],2)
                save_portfolio(port); st.rerun()
        with cc3:
            if st.button("✅ Tutup", key=f"c_{pos['id']}", use_container_width=True):
                exit_p = pos.get("current_price",pos["entry"])
                pu = sign*(exit_p-pos["entry"])/pos["entry"]*pos["position_size"]
                port["open_positions"].pop(i)
                save_portfolio(port)
                st.success(f"Ditutup · P&L ${pu:+,.2f}")
                st.rerun()

# ══════════════════════════════════════════════════════════
# TAB — TRADE HISTORY
# ══════════════════════════════════════════════════════════
with tab_hist:
    st.markdown('<div class="sec-label">Supabase</div><div class="sec-title">Semua Trade dari Telegram</div>', unsafe_allow_html=True)

    if not connected:
        st.warning("Hubungkan Supabase untuk melihat trade history dari bot Telegram.")
    elif not all_trades:
        st.info("Belum ada trade. Log trade pertama via /logtrade di Telegram.")
    else:
        f1,f2 = st.columns(2)
        fdir  = f1.multiselect("Filter Arah", ["LONG","SHORT"])
        fres  = f2.multiselect("Filter Hasil", ["WIN","LOSS","BREAKEVEN"])

        src = all_trades[:]
        if fdir: src = [t for t in src if str(t.get("direction","")).upper() in fdir]
        if fres: src = [t for t in src if str(t.get("result","")).upper() in fres]

        rows = ""
        for t in src:
            p    = pf(t.get("pnl_usdt",0))
            pp   = pf(t.get("pnl_pct",0))
            res  = str(t.get("result","")).upper()
            rc_  = "#00e676" if res=="WIN" else "#ff4757" if res=="LOSS" else "#ffd32a"
            dc_  = "#00e676" if str(t.get("direction","")).upper()=="LONG" else "#ff4757"
            da_  = "▲" if str(t.get("direction","")).upper()=="LONG" else "▼"
            rows += f"""<tr>
              <td style="color:#64748b;font-size:12px">{fmt_time(t.get('ts',''))}</td>
              <td class="mono" style="font-weight:700;color:#e2e8f0">{t.get('coin','—')}</td>
              <td style="color:{dc_};font-weight:700">{da_} {t.get('direction','—')}</td>
              <td class="mono">${pf(t.get('entry_price',0)):,.4f}</td>
              <td style="color:#94a3b8;font-size:12px">{t.get('leverage','—')}x</td>
              <td class="mono" style="color:#64748b">${pf(t.get('margin_usdt',0)):,.0f}</td>
              <td><span style="background:{rc_}22;color:{rc_};border:1px solid {rc_}55;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:700">{'✅ WIN' if res=='WIN' else '❌ LOSS' if res=='LOSS' else '➖ BE'}</span></td>
              <td class="mono" style="color:{rc_};font-weight:700">{p:+,.2f} ({pp:+.2f}%)</td>
              <td style="color:#64748b;font-size:11px">{str(t.get('note',''))[:30]}</td>
            </tr>"""

        st.markdown(f"""<div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px;margin-top:8px">
          <table class="tbl"><thead><tr><th>Waktu</th><th>Coin</th><th>Arah</th><th>Entry</th><th>Lev</th><th>Margin</th><th>Hasil</th><th>P&L</th><th>Note</th></tr></thead>
          <tbody>{rows}</tbody></table></div>
        <div style="color:#64748b;font-size:12px;margin-top:8px">{len(src)} / {total_trades} trades</div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB — COIN STATS
# ══════════════════════════════════════════════════════════
with tab_coins:
    st.markdown('<div class="sec-label">Bot Signals</div><div class="sec-title">Performa Per Coin</div>', unsafe_allow_html=True)
    coin_stats = {}
    for s in outcomes:
        sym = s["symbol"]
        if sym not in coin_stats: coin_stats[sym] = {"total":0,"wins":0,"losses":0,"pnl":0.0,"sigs":[]}
        coin_stats[sym]["total"] += 1
        if s["status"] in ("TP_HIT","EXPIRED_WIN"): coin_stats[sym]["wins"] += 1
        elif s["status"] in ("SL_HIT","EXPIRED_LOSS"): coin_stats[sym]["losses"] += 1
        coin_stats[sym]["pnl"] += s.get("pnl_pct",0)
        coin_stats[sym]["sigs"].append(s)

    def dot_c(st_): return "#00e676" if st_ in ("TP_HIT","EXPIRED_WIN") else "#ff4757" if st_=="SL_HIT" else "#ffd32a"

    sb = st.selectbox("Urutkan", ["Win Rate","Total P&L","Total Trades"])
    for sym, stat in sorted(coin_stats.items(),
        key=lambda x:(x[1]["wins"]/x[1]["total"]*100 if x[1]["total"] else 0) if sb=="Win Rate"
        else x[1]["pnl"] if sb=="Total P&L" else x[1]["total"], reverse=True):
        wr_ = stat["wins"]/stat["total"]*100 if stat["total"] else 0
        wc_ = "#00e676" if wr_>=60 else "#ffd32a" if wr_>=40 else "#ff4757"
        nc_ = "#00e676" if stat["pnl"]>=0 else "#ff4757"
        recent = sorted(stat["sigs"], key=lambda x:x.get("created_at",""), reverse=True)[:5]
        dots = "".join(f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{dot_c(s["status"])};margin:1px"></span>' for s in recent)
        st.markdown(f"""<div class="card" style="margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="display:flex;align-items:center;gap:14px">
              <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{sym}</span>
              <span style="font-size:12px;color:#64748b">{stat['total']} sinyal · 5 terakhir: {dots}</span>
            </div>
            <div style="display:flex;gap:20px;text-align:right">
              <div><div style="font-size:10px;color:#64748b">Win Rate</div><div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono';color:{wc_}">{wr_:.0f}%</div></div>
              <div><div style="font-size:10px;color:#64748b">P&L %</div><div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono';color:{nc_}">{stat['pnl']:+.2f}%</div></div>
              <div><div style="font-size:10px;color:#64748b">TP/SL</div><div style="font-size:16px;font-weight:700"><span style="color:#00e676">{stat['wins']}</span>/<span style="color:#ff4757">{stat['losses']}</span></div></div>
            </div>
          </div>
          <div class="prog-wrap" style="margin-top:10px"><div class="prog-fill" style="width:{wr_:.0f}%;background:{wc_}"></div></div>
        </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB — BOT PENDING
# ══════════════════════════════════════════════════════════
with tab_pending:
    st.markdown('<div class="sec-label">Live</div><div class="sec-title">Sinyal Pending Bot</div>', unsafe_allow_html=True)
    if not pending:
        st.info("Tidak ada sinyal pending.")
    else:
        for s in pending:
            created  = datetime.fromisoformat(s["created_at"].replace("Z","+00:00")).astimezone(WIB)
            age_hrs  = (datetime.now(WIB)-created).total_seconds()/3600
            timeout  = s.get("timeout_hours",24)
            pct_done = min(age_hrs/timeout*100,100)
            dist_tp  = (s.get("tp",s["entry_price"])-s["entry_price"])/s["entry_price"]*100
            dist_sl  = (s.get("sl",s["entry_price"])-s["entry_price"])/s["entry_price"]*100
            reasons  = " ".join(f'<span class="chip">{r[:55]}</span>' for r in s.get("reasons",[])[:3])
            dc_      = "#00e676" if s["direction"]=="LONG" else "#ff4757"
            bar_c    = "#ff4757" if pct_done>80 else "#ffd32a" if pct_done>50 else "#00d4ff"
            st.markdown(f"""<div class="card">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div style="display:flex;align-items:center;gap:10px">
                  <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{s['symbol']}</span>
                  <span style="color:{dc_};font-weight:700">{'▲' if s['direction']=='LONG' else '▼'} {s['direction']}</span>
                  <span style="background:#00d4ff22;color:#00d4ff;border:1px solid #00d4ff44;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{s.get('signal_type','—')}</span>
                </div>
                <span style="color:#ffd32a;font-weight:700">{s.get('score','—')}/100</span>
              </div>
              <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:10px">
                <div><div style="font-size:10px;color:#64748b">Entry</div><div class="mono">${s['entry_price']:,.4f}</div></div>
                <div><div style="font-size:10px;color:#64748b">TP</div><div class="mono" style="color:#00e676">${s.get('tp',0):,.4f} ({dist_tp:+.2f}%)</div></div>
                <div><div style="font-size:10px;color:#64748b">SL</div><div class="mono" style="color:#ff4757">${s.get('sl',0):,.4f} ({dist_sl:.2f}%)</div></div>
                <div><div style="font-size:10px;color:#64748b">Confluence</div><div style="font-weight:700">{s.get('confluence_level','—')}</div></div>
              </div>
              <div style="margin-bottom:8px">{reasons}</div>
              <div style="display:flex;justify-content:space-between;font-size:12px;color:#64748b;margin-bottom:5px">
                <span>{age_hrs:.1f}h / {timeout}h</span><span>{fmt_time(s.get('created_at',''))}</span>
              </div>
              <div class="prog-wrap"><div class="prog-fill" style="width:{pct_done:.0f}%;background:{bar_c}"></div></div>
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB — LESSONS
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
            lc_ = "#00e676" if out=="good" else "#ff4757" if out=="poor" else "#ffd32a"
            p   = les.get("pnl_pct")
            ps  = f' · P&L <span style="color:{"#00e676" if p and p>=0 else "#ff4757"}">{p:+.2f}%</span>' if p is not None else ""
            tags= " ".join(f'<span class="chip">{t}</span>' for t in les.get("tags",[]))
            st.markdown(f"""<div class="card" style="margin-bottom:8px;border-left:3px solid {lc_}">
              <div style="display:flex;justify-content:space-between;margin-bottom:8px">
                <div><span style="background:{lc_}22;color:{lc_};border:1px solid {lc_}55;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{out.upper()}</span>
                  <span style="font-size:11px;color:#64748b;margin-left:10px">conf {les.get('confidence',0):.0%}{ps}</span></div>
                <span style="font-size:11px;color:#64748b">{fmt_time(les.get('created_at',''))}</span>
              </div>
              <div style="font-size:14px;color:#cbd5e1;line-height:1.6">{les.get('rule','')}</div>
              <div style="margin-top:8px">{tags}</div>
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB — DECISION LOG
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
            dc_ = "#00e676" if dec in ("ALERT","PASS") else "#ffd32a" if dec=="WATCH" else "#64748b"
            top = " ".join(f'<span class="chip">{r[:50]}</span>' for r in (d.get("top_reasons") or [])[:2])
            rows += f"""<tr>
              <td style="color:#64748b;font-size:12px">{fmt_time(d.get('ts',''))}</td>
              <td class="mono" style="font-weight:700">{d.get('symbol','—')}</td>
              <td style="font-size:12px;color:#94a3b8">{d.get('actor','—')}</td>
              <td><span style="background:{dc_}22;color:{dc_};border:1px solid {dc_}55;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:700">{dec}</span></td>
              <td style="color:#ffd32a;font-family:'JetBrains Mono';font-weight:700">{d.get('score','—')}</td>
              <td>{top}</td>
            </tr>"""
        st.markdown(f"""<div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px">
          <table class="tbl"><thead><tr><th>Waktu</th><th>Symbol</th><th>Actor</th><th>Decision</th><th>Score</th><th>Reasons</th></tr></thead>
          <tbody>{rows}</tbody></table></div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB — SPREADSHEET (koneksi Google Sheets + ekspor CSV)
# ══════════════════════════════════════════════════════════
with tab_sheet:
    import pandas as pd

    st.markdown('<div class="sec-label">Integrasi</div><div class="sec-title">Hubungkan Spreadsheet</div>', unsafe_allow_html=True)
    st.caption("Dua arah: **tarik** laporan dari Google Sheets ke dashboard, atau **ekspor** laporan bot ke CSV untuk dibuka di Excel / Google Sheets.")

    # ── A. Baca dari Google Sheets ────────────────────────────
    st.markdown('<div class="sec-label" style="margin-top:8px">A · Baca dari Google Sheets</div>', unsafe_allow_html=True)
    with st.expander("ℹ️ Cara menyiapkan Google Sheet (sekali saja)", expanded=False):
        st.markdown("""
1. Buka Google Sheet laporanmu.
2. Klik **Share** → ubah ke **Anyone with the link → Viewer**.
3. Salin URL-nya, tempel di kolom bawah, lalu **Simpan**.

> Dashboard membaca lewat ekspor CSV publik Google — **aman**, hanya bisa *melihat*, tidak bisa mengubah sheet-mu.
        """)

    cur_url = sheet_cfg.get("url", "")
    in_url  = st.text_input("URL / ID Google Sheet", value=cur_url,
                            placeholder="https://docs.google.com/spreadsheets/d/XXXX/edit#gid=0")
    bcol1, bcol2 = st.columns([1, 3])
    with bcol1:
        if st.button("💾 Simpan & Muat", use_container_width=True):
            sheet_cfg["url"] = in_url.strip()
            save_sheet_cfg(sheet_cfg)
            st.cache_data.clear()
            st.rerun()
    with bcol2:
        if cur_url:
            st.caption(f"Tersimpan: `{cur_url[:60]}{'…' if len(cur_url) > 60 else ''}`")

    if in_url.strip():
        sid, gid = parse_sheet_id(in_url)
        if not sid:
            st.error("URL/ID tidak dikenali. Tempel URL lengkap Google Sheets atau ID-nya.")
        else:
            df_sheet, err = fetch_sheet(sid, gid)
            if err:
                st.warning(f"⚠️ {err}")
            elif df_sheet is not None:
                st.markdown(f'<div class="ok-badge" style="margin-bottom:10px">● Terhubung · {len(df_sheet)} baris × {len(df_sheet.columns)} kolom</div>', unsafe_allow_html=True)
                st.dataframe(df_sheet, use_container_width=True, hide_index=True)
                st.download_button("⬇️ Unduh sheet ini (CSV)",
                                   data=df_sheet.to_csv(index=False).encode("utf-8"),
                                   file_name="google_sheet.csv", mime="text/csv")
    else:
        st.info("Belum ada sheet terhubung. Tempel URL di atas untuk menampilkan laporan dari Google Sheets.")

    st.divider()

    # ── B. Ekspor laporan bot ke CSV ──────────────────────────
    st.markdown('<div class="sec-label">B · Ekspor laporan bot ke CSV</div><div class="sec-title">Unduh untuk Excel / Google Sheets</div>', unsafe_allow_html=True)
    st.markdown('<div class="dl-note">Setiap file bisa langsung dibuka di Excel atau diimpor ke Google Sheets (File → Import).</div>', unsafe_allow_html=True)

    exports = [
        ("📋 Trade History (Supabase)", all_trades,
         ["ts", "coin", "direction", "entry_price", "leverage", "margin_usdt", "result", "pnl_usdt", "pnl_pct", "note"],
         "trade_history.csv"),
        ("📡 Sinyal Selesai (outcomes)", outcomes,
         ["created_at", "symbol", "direction", "signal_type", "entry_price", "tp", "sl", "score", "status", "pnl_pct", "hold_hours"],
         "signal_outcomes.csv"),
        ("⏳ Sinyal Pending", pending,
         ["created_at", "symbol", "direction", "signal_type", "entry_price", "tp", "sl", "score", "confluence_level", "status"],
         "pending_signals.csv"),
        ("📡 Decision Log", decisions,
         ["ts", "actor", "symbol", "decision", "score", "confluence_level", "direction", "summary"],
         "decision_log.csv"),
        ("🧠 Lessons", lessons,
         ["created_at", "outcome", "confidence", "pnl_pct", "rule", "tags"],
         "lessons.csv"),
    ]
    ec1, ec2 = st.columns(2)
    for i, (label, rows, cols, fname) in enumerate(exports):
        col = ec1 if i % 2 == 0 else ec2
        with col:
            n = len(rows) if rows else 0
            st.download_button(f"{label}  ·  {n} baris",
                               data=rows_to_csv(rows, cols),
                               file_name=fname, mime="text/csv",
                               use_container_width=True, disabled=(n == 0),
                               key=f"dl_{fname}")

    st.divider()

    # ── C. Tarik data bot LANGSUNG ke Google Sheets ───────────
    st.markdown('<div class="sec-label">C · Tarik data bot otomatis ke Google Sheets</div>', unsafe_allow_html=True)
    sb_url, _ = _sb_cfg()
    if sb_url and connected:
        import_formula = f'=IMPORTDATA("{sb_url}/rest/v1/trades?select=*&apikey={_sb_cfg()[1]}", ",")'
        st.markdown("""Kalau bot sudah sync ke **Supabase**, kamu bisa menarik datanya langsung ke Google Sheets
dengan satu rumus (data auto-update). Tempel rumus ini di sel **A1** sheet baru:""")
        st.code(import_formula, language="text")
        st.caption("Google Sheets akan menarik tabel `trades` secara berkala. Ganti `trades` → `balance_log` untuk riwayat saldo.")
    else:
        st.info("Hubungkan Supabase (lihat sidebar) untuk mengaktifkan auto-import ke Google Sheets via rumus `=IMPORTDATA()`. Sementara itu, gunakan ekspor CSV di atas.")

# ══════════════════════════════════════════════════════════
# TAB — BELAJAR (pusat edukasi)
# ══════════════════════════════════════════════════════════
with tab_learn:
    st.markdown('<div class="sec-label">Pusat Edukasi</div><div class="sec-title">Belajar Baca Laporan & Strategi Bot</div>', unsafe_allow_html=True)
    st.caption("Bukan saran finansial. Tujuannya memahami cara bot mengambil keputusan supaya kamu bisa belajar & mengembangkan strateginya sendiri.")

    lc1, lc2 = st.columns(2)

    with lc1:
        st.markdown('<div class="sec-label" style="margin-top:8px">📖 Glosarium Istilah</div>', unsafe_allow_html=True)
        gloss = [
            ("Entry", "Harga rencana masuk posisi."),
            ("TP", "Take Profit — target harga untuk merealisasikan profit."),
            ("SL", "Stop Loss — batas kerugian; posisi ditutup agar rugi tidak membesar."),
            ("LONG / SHORT", "LONG = untung saat harga naik. SHORT = untung saat harga turun."),
            ("Master Score", "Skor 0–100 hasil gabungan banyak detektor. Makin tinggi, makin kuat konfirmasinya."),
            ("Confluence", "Berapa banyak sinyal yang 'sepakat' (struktur, Order Block, FVG, dll)."),
            ("MoneyFlow", "Arah aliran dana (inflow/outflow) di beberapa timeframe."),
            ("R:R", "Risk/Reward — perbandingan potensi rugi vs potensi untung. 1:3 = risiko 1 untuk target 3."),
            ("Win Rate", "Persentase sinyal/trade yang berakhir profit."),
            ("Profit Factor", "Total profit ÷ total loss. >1 berarti sistem menguntungkan."),
            ("Drawdown", "Penurunan saldo dari puncak ke lembah; ukuran 'rasa sakit' terbesar."),
        ]
        rows = "".join(f'<div class="t">{t}</div><div class="d">{d}</div>' for t, d in gloss)
        st.markdown(f'<div class="card"><div class="gloss">{rows}</div></div>', unsafe_allow_html=True)

    with lc2:
        st.markdown('<div class="sec-label" style="margin-top:8px">🔄 Alur Kerja Bot (6 tahap)</div>', unsafe_allow_html=True)
        steps = [
            ("1 · Scan Pasar", "Ambil candle OHLCV semua koin dari Binance. Anti-lookahead: hitung pakai candle yang sudah closed."),
            ("2 · Hitung Indikator", "RSI, MACD, EMA, Bollinger, ATR, volume, Open Interest, funding rate — lintas 15M/1H/4H/1D."),
            ("3 · Fusi & Skor", "Tiap detektor 'voting' bullish/bearish. Digabung jadi Master Score + cek korelasi BTC/ETH."),
            ("4 · Gerbang (Gates)", "News gate, sentimen, whale, blacklist koin. Sinyal lemah/berisiko dijatuhkan diam-diam."),
            ("5 · Rencana Trade", "Tentukan entry mode, TP1–TP3, SL, dan blok risiko. Kirim ke Telegram + catat sebagai pending."),
            ("6 · Lacak & Belajar", "Cek apakah kena TP atau SL. SL → mini-backtest → lesson → learning engine diperbarui."),
        ]
        body = "".join(f'<div class="step-box"><b>{t}</b><div style="color:#94a3b8;font-size:13px;margin-top:4px;line-height:1.6">{d}</div></div>' for t, d in steps)
        st.markdown(body, unsafe_allow_html=True)

    st.markdown('<div class="sec-label" style="margin-top:18px">🧭 Cara Membaca Tiap Tab</div>', unsafe_allow_html=True)
    qa = [
        ("Tab Portfolio", "Ringkasan kesehatan akun: saldo, P&L, win rate, profit factor, equity curve, dan P&L per koin."),
        ("Tab Trade History", "Semua trade yang sudah ditutup (dari Telegram via Supabase). Bisa difilter LONG/SHORT & hasil."),
        ("Tab Bot Pending", "Sinyal yang sedang ditunggu hasilnya. Progress bar = umur sinyal terhadap timeout."),
        ("Tab Coin Stats", "Performa bot per koin: win rate, P&L%, dan 5 hasil terakhir (titik hijau/merah)."),
        ("Tab Lessons", "Pelajaran otomatis yang ditarik bot dari trade yang gagal — inti dari proses 'belajar'."),
        ("Tab Spreadsheet", "Tarik laporan dari Google Sheets, atau ekspor laporan bot ke CSV/Sheets."),
    ]
    cols = st.columns(2)
    for i, (q, a) in enumerate(qa):
        with cols[i % 2]:
            st.markdown(f'<div class="card" style="margin-bottom:10px"><div class="learn-q">{q}</div><div class="learn-a">{a}</div></div>', unsafe_allow_html=True)

    st.markdown('<div class="sec-label" style="margin-top:18px">🛡️ Dasar Manajemen Risiko</div>', unsafe_allow_html=True)
    st.markdown("""
<div class="card">
<div class="learn-a">
• <b>Risiko per trade kecil</b> — banyak trader pakai 1–2% modal per posisi, supaya beberapa kekalahan beruntun tidak menghabiskan akun.<br>
• <b>Selalu pasang SL</b> — tentukan batas rugi <i>sebelum</i> masuk, bukan sesudah harga bergerak melawan.<br>
• <b>Utamakan R:R bagus</b> — cari setup dengan reward minimal 2–3× risiko.<br>
• <b>Batas rugi harian</b> — kalau sudah menyentuh batas, berhenti dulu. Menghindari <i>revenge trading</i>.<br>
• <b>Konsistensi &gt; keberuntungan</b> — satu trade besar tidak membuktikan strategi; ribuan trade yang membuktikannya.
</div>
</div>
    """, unsafe_allow_html=True)
    st.info("⚠️ Semua konten di sini hanya untuk edukasi. Kripto sangat berisiko — selalu lakukan riset sendiri (DYOR).")

st.markdown('<div style="text-align:center;color:#1e2a38;font-size:12px;margin-top:48px;border-top:1px solid #1e2a38;padding-top:24px">CryptoBot v13 · Personal Dashboard · Bukan saran finansial</div>', unsafe_allow_html=True)
