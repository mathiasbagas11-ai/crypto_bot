import streamlit as st
import json, os, pathlib
from datetime import datetime, timezone, timedelta
import pandas as pd

st.set_page_config(
    page_title="CryptoBot v13 — Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

ROOT = pathlib.Path(__file__).parent.parent

# ── helpers ──────────────────────────────────────────────
def load(fname, default=None):
    p = ROOT / fname
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return default or []

def fmt_pct(v):
    if v is None: return "—"
    color = "#00e676" if v >= 0 else "#ff4757"
    sign  = "+" if v > 0 else ""
    return f'<span style="color:{color};font-weight:700">{sign}{v:.2f}%</span>'

def status_badge(s):
    m = {
        "TP_HIT":      ("#00e676", "✅ TP HIT"),
        "SL_HIT":      ("#ff4757", "❌ SL HIT"),
        "EXPIRED_WIN": ("#ffd32a", "⏱ EXPIRED W"),
        "EXPIRED_LOSS":("#ff9800", "⏱ EXPIRED L"),
        "PENDING":     ("#00d4ff", "⏳ PENDING"),
    }
    color, label = m.get(s, ("#64748b", s))
    return f'<span style="background:{color}22;color:{color};border:1px solid {color}55;padding:2px 10px;border-radius:100px;font-size:12px;font-weight:700">{label}</span>'

def dir_badge(d):
    if d == "LONG":
        return '<span style="color:#00e676;font-weight:700">▲ LONG</span>'
    return '<span style="color:#ff4757;font-weight:700">▼ SHORT</span>'

WIB = timezone(timedelta(hours=7))
def fmt_time(ts):
    if not ts: return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(WIB)
        return dt.strftime("%d %b %H:%M WIB")
    except Exception:
        return ts

# ── CSS ──────────────────────────────────────────────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap');

  html, body, [class*="css"] { font-family: 'Inter', sans-serif !important; }
  [data-testid="stAppViewContainer"] { background: #080b10; }
  [data-testid="stHeader"]           { background: transparent; }
  section[data-testid="stSidebar"]   { background: #0d1117; }
  .block-container { padding: 2rem 2.5rem 4rem !important; max-width: 1400px !important; }

  /* metric cards */
  .kpi {
    background: #0f1621; border: 1px solid #1e2a38; border-radius: 14px;
    padding: 22px 24px; position: relative; overflow: hidden;
  }
  .kpi::before {
    content:''; position:absolute; top:0; left:0; right:0; height:2px;
    background: linear-gradient(90deg, #00d4ff, #7c3aed);
  }
  .kpi-label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; margin-bottom: 8px; }
  .kpi-val   { font-size: 32px; font-weight: 900; font-family: 'JetBrains Mono', monospace; line-height: 1; }
  .kpi-sub   { font-size: 12px; color: #64748b; margin-top: 6px; }

  /* tables */
  .tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
  .tbl th { background: #131920; color: #64748b; text-transform: uppercase; letter-spacing: 1px; font-size: 11px; font-weight: 700; padding: 10px 14px; text-align: left; border-bottom: 1px solid #1e2a38; }
  .tbl td { padding: 12px 14px; border-bottom: 1px solid #0f1621; vertical-align: middle; }
  .tbl tr:hover td { background: rgba(255,255,255,0.02); }
  .tbl .mono { font-family: 'JetBrains Mono', monospace; }

  /* cards */
  .card { background: #0f1621; border: 1px solid #1e2a38; border-radius: 14px; padding: 22px; margin-bottom: 16px; }
  .card-title { font-size: 14px; font-weight: 700; margin-bottom: 16px; color: #e2e8f0; }

  /* section header */
  .sec-header { margin: 32px 0 18px; }
  .sec-label  { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 2px; color: #00d4ff; margin-bottom: 4px; }
  .sec-title  { font-size: 22px; font-weight: 800; letter-spacing: -0.5px; color: #e2e8f0; }

  /* live dot */
  @keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(1.4)} }
  .live-dot { display:inline-block; width:8px; height:8px; background:#00e676; border-radius:50%; animation:pulse 1.5s infinite; margin-right:6px; }

  /* progress */
  .prog-wrap { background:#1e2a38; border-radius:100px; height:7px; overflow:hidden; margin-top:6px; }
  .prog-fill  { height:100%; border-radius:100px; }

  /* reason chip */
  .chip { display:inline-block; background:#131920; border:1px solid #1e2a38; border-radius:6px; padding:3px 9px; font-size:11px; color:#94a3b8; margin:2px; }

  div[data-testid="stHorizontalBlock"] > div { gap: 0 !important; }
</style>
""", unsafe_allow_html=True)

# ── load data ─────────────────────────────────────────────
outcomes  = load("signal_outcomes.json", [])
pending   = load("pending_signals.json", [])
lessons   = load("lessons.json", {})
if isinstance(lessons, dict):
    lessons = lessons.get("lessons", [])
decisions = load("decision_log.json", [])

# ── compute stats ─────────────────────────────────────────
total    = len(outcomes)
tp_hit   = sum(1 for s in outcomes if s["status"] == "TP_HIT")
sl_hit   = sum(1 for s in outcomes if s["status"] == "SL_HIT")
exp_w    = sum(1 for s in outcomes if s["status"] == "EXPIRED_WIN")
exp_l    = sum(1 for s in outcomes if s["status"] == "EXPIRED_LOSS")
win_rate = (tp_hit + exp_w) / total * 100 if total else 0
pnls     = [s.get("pnl_pct", 0) for s in outcomes if s.get("pnl_pct") is not None]
total_pnl = sum(pnls)
avg_score = sum(s.get("score", 0) for s in outcomes) / total if total else 0

wins_pnl  = [p for p in pnls if p > 0]
loss_pnl  = [p for p in pnls if p < 0]
avg_win   = sum(wins_pnl)/len(wins_pnl) if wins_pnl else 0
avg_loss  = sum(loss_pnl)/len(loss_pnl) if loss_pnl else 0
pf        = abs(sum(wins_pnl)/sum(loss_pnl)) if sum(loss_pnl) != 0 else 0

# per-coin stats
coin_stats = {}
for s in outcomes:
    sym = s["symbol"]
    if sym not in coin_stats:
        coin_stats[sym] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0, "signals": []}
    coin_stats[sym]["total"] += 1
    if s["status"] in ("TP_HIT", "EXPIRED_WIN"):
        coin_stats[sym]["wins"] += 1
    elif s["status"] in ("SL_HIT", "EXPIRED_LOSS"):
        coin_stats[sym]["losses"] += 1
    coin_stats[sym]["pnl"] += s.get("pnl_pct", 0)
    coin_stats[sym]["signals"].append(s)

# ── HEADER ────────────────────────────────────────────────
col_logo, col_live = st.columns([3, 1])
with col_logo:
    st.markdown("""
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">
      <div style="width:10px;height:10px;background:#00d4ff;border-radius:50%;box-shadow:0 0 10px #00d4ff"></div>
      <span style="font-size:24px;font-weight:900;letter-spacing:-0.5px">CryptoBot <span style="color:#00d4ff">v13</span></span>
    </div>
    <div style="color:#64748b;font-size:14px;margin-left:22px">Personal Trading Dashboard</div>
    """, unsafe_allow_html=True)
with col_live:
    st.markdown(f"""
    <div style="text-align:right;padding-top:8px">
      <span class="live-dot"></span>
      <span style="font-size:13px;color:#00e676;font-weight:600">LIVE</span>
      <div style="color:#64748b;font-size:12px;margin-top:4px">{datetime.now(WIB).strftime('%d %b %Y %H:%M WIB')}</div>
      <div style="color:#64748b;font-size:12px">{len(pending)} pending · {total} tracked</div>
    </div>
    """, unsafe_allow_html=True)

st.divider()

# ── KPI ROW ───────────────────────────────────────────────
k1, k2, k3, k4, k5, k6 = st.columns(6)
kpis = [
    (k1, "Total Signals", str(total), f"{tp_hit} TP · {sl_hit} SL"),
    (k2, "Win Rate", f"{win_rate:.1f}%", f"{tp_hit+exp_w} wins / {sl_hit+exp_l} losses",
     "#00e676" if win_rate >= 50 else "#ff4757"),
    (k3, "Total P&L", f"{'+' if total_pnl>=0 else ''}{total_pnl:.2f}%",
     f"Avg/trade {total_pnl/total:.2f}%" if total else "—",
     "#00e676" if total_pnl >= 0 else "#ff4757"),
    (k4, "Profit Factor", f"{pf:.2f}", f"Avg win {avg_win:.2f}% / loss {avg_loss:.2f}%",
     "#00e676" if pf >= 1 else "#ff4757"),
    (k5, "Avg Score", f"{avg_score:.0f}/100", f"{len(coin_stats)} coins tracked", "#ffd32a"),
    (k6, "Pending Now", str(len(pending)), "Waiting TP/SL", "#00d4ff"),
]
for item in kpis:
    col = item[0]; label = item[1]; val = item[2]; sub = item[3]
    color = item[4] if len(item) > 4 else "#e2e8f0"
    with col:
        st.markdown(f"""
        <div class="kpi">
          <div class="kpi-label">{label}</div>
          <div class="kpi-val" style="color:{color}">{val}</div>
          <div class="kpi-sub">{sub}</div>
        </div>
        """, unsafe_allow_html=True)

st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

# ── TABS ─────────────────────────────────────────────────
tab_pending, tab_history, tab_coins, tab_lessons, tab_log = st.tabs([
    f"⏳ Pending ({len(pending)})",
    f"📋 Signal History ({total})",
    f"🪙 Coin Performance ({len(coin_stats)})",
    f"🧠 Lessons ({len(lessons)})",
    f"📡 Decision Log ({len(decisions)})",
])

# ═══════════════════════════════════════════════
# TAB 1 — PENDING SIGNALS
# ═══════════════════════════════════════════════
with tab_pending:
    st.markdown('<div class="sec-header"><div class="sec-label">Live Watchlist</div><div class="sec-title">Pending Signals</div></div>', unsafe_allow_html=True)

    if not pending:
        st.info("Tidak ada sinyal pending saat ini.")
    else:
        for s in pending:
            created = datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")).astimezone(WIB)
            age_hrs  = (datetime.now(WIB) - created).total_seconds() / 3600
            timeout  = s.get("timeout_hours", 24)
            pct_done = min(age_hrs / timeout * 100, 100)
            dist_tp  = ((s.get("tp", s["entry_price"]) - s["entry_price"]) / s["entry_price"] * 100)
            dist_sl  = ((s.get("sl", s["entry_price"]) - s["entry_price"]) / s["entry_price"] * 100)

            reasons_html = " ".join(f'<span class="chip">{r[:60]}</span>' for r in s.get("reasons", [])[:3])

            st.markdown(f"""
            <div class="card">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
                <div style="display:flex;align-items:center;gap:12px">
                  <span style="font-size:18px;font-weight:800;font-family:'JetBrains Mono'">{s['symbol']}</span>
                  {dir_badge(s['direction'])}
                  <span style="background:#00d4ff22;color:#00d4ff;border:1px solid #00d4ff55;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{s.get('signal_type','—')}</span>
                </div>
                <div style="text-align:right;font-size:12px;color:#64748b">
                  Score: <span style="color:#ffd32a;font-weight:700">{s.get('score','—')}/100</span> ·
                  {fmt_time(s['created_at'])}
                </div>
              </div>
              <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:14px">
                <div><div style="font-size:11px;color:#64748b">Entry</div><div style="font-family:'JetBrains Mono';font-weight:700">${s['entry_price']:,.4f}</div></div>
                <div><div style="font-size:11px;color:#64748b">TP</div><div style="font-family:'JetBrains Mono';color:#00e676;font-weight:700">${s.get('tp', 0):,.4f} <span style="font-size:11px;color:#00e676">({'+' if dist_tp>=0 else ''}{dist_tp:.2f}%)</span></div></div>
                <div><div style="font-size:11px;color:#64748b">SL</div><div style="font-family:'JetBrains Mono';color:#ff4757;font-weight:700">${s.get('sl', 0):,.4f} <span style="font-size:11px;color:#ff4757">({dist_sl:.2f}%)</span></div></div>
                <div><div style="font-size:11px;color:#64748b">Confluence</div><div style="font-weight:700;font-size:13px">{s.get('confluence_level','—')}</div></div>
              </div>
              <div style="margin-bottom:10px">{reasons_html}</div>
              <div style="display:flex;justify-content:space-between;font-size:12px;color:#64748b;margin-bottom:6px">
                <span>Waktu berjalan: <b style="color:#e2e8f0">{age_hrs:.1f}h / {timeout}h</b></span>
                <span>Timeout: {fmt_time(s.get('created_at',''))}</span>
              </div>
              <div class="prog-wrap"><div class="prog-fill" style="width:{pct_done:.0f}%;background:{'#ff4757' if pct_done>80 else '#ffd32a' if pct_done>50 else '#00d4ff'}"></div></div>
            </div>
            """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════
# TAB 2 — SIGNAL HISTORY
# ═══════════════════════════════════════════════
with tab_history:
    st.markdown('<div class="sec-header"><div class="sec-label">Riwayat</div><div class="sec-title">Semua Signal</div></div>', unsafe_allow_html=True)

    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filter_status = st.multiselect("Filter Status", ["TP_HIT","SL_HIT","EXPIRED_WIN","EXPIRED_LOSS"], default=[])
    with col_f2:
        filter_dir = st.multiselect("Filter Arah", ["LONG","SHORT"], default=[])
    with col_f3:
        filter_type = st.multiselect("Filter Type", list({s["signal_type"] for s in outcomes}), default=[])

    filtered = outcomes[:]
    if filter_status: filtered = [s for s in filtered if s["status"] in filter_status]
    if filter_dir:    filtered = [s for s in filtered if s["direction"] in filter_dir]
    if filter_type:   filtered = [s for s in filtered if s["signal_type"] in filter_type]

    filtered = sorted(filtered, key=lambda x: x.get("created_at",""), reverse=True)

    rows_html = ""
    for s in filtered:
        pnl = s.get("pnl_pct")
        rows_html += f"""
        <tr>
          <td class="mono" style="color:#e2e8f0;font-weight:700">{s['symbol']}</td>
          <td>{dir_badge(s['direction'])}</td>
          <td><span style="font-size:12px;color:#94a3b8">{s.get('signal_type','—')}</span></td>
          <td class="mono">${s.get('entry_price',0):,.4f}</td>
          <td class="mono" style="color:#00e676">${s.get('tp',0):,.4f}</td>
          <td class="mono" style="color:#ff4757">${s.get('sl',0):,.4f}</td>
          <td>{status_badge(s['status'])}</td>
          <td>{fmt_pct(pnl)}</td>
          <td class="mono" style="color:#64748b">{s.get('hold_hours',0):.1f}h</td>
          <td><span style="color:#ffd32a;font-weight:700">{s.get('score','—')}</span></td>
          <td style="color:#64748b;font-size:12px">{fmt_time(s.get('created_at',''))}</td>
        </tr>"""

    st.markdown(f"""
    <div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px;margin-top:8px">
      <table class="tbl">
        <thead><tr>
          <th>Symbol</th><th>Arah</th><th>Type</th>
          <th>Entry</th><th>TP</th><th>SL</th>
          <th>Status</th><th>P&L</th><th>Hold</th><th>Score</th><th>Waktu</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    <div style="color:#64748b;font-size:12px;margin-top:8px">Menampilkan {len(filtered)} dari {total} sinyal</div>
    """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════
# TAB 3 — COIN PERFORMANCE
# ═══════════════════════════════════════════════
with tab_coins:
    st.markdown('<div class="sec-header"><div class="sec-label">Per Koin</div><div class="sec-title">Performa Tiap Coin</div></div>', unsafe_allow_html=True)

    sort_by = st.selectbox("Urutkan berdasarkan", ["Win Rate", "Total P&L", "Total Trades"], index=0)

    sorted_coins = sorted(
        coin_stats.items(),
        key=lambda x: (
            (x[1]["wins"]/x[1]["total"]*100 if x[1]["total"] else 0) if sort_by=="Win Rate"
            else x[1]["pnl"] if sort_by=="Total P&L"
            else x[1]["total"]
        ),
        reverse=True
    )

    for sym, stat in sorted_coins:
        wr   = stat["wins"] / stat["total"] * 100 if stat["total"] else 0
        pnl  = stat["pnl"]
        prog_color = "#00e676" if wr >= 60 else "#ffd32a" if wr >= 40 else "#ff4757"
        pnl_color  = "#00e676" if pnl >= 0 else "#ff4757"

        recent = sorted(stat["signals"], key=lambda x: x.get("created_at",""), reverse=True)[:5]
        def dot_color(st_):
            if st_ in ("TP_HIT","EXPIRED_WIN"): return "#00e676"
            if st_ == "SL_HIT": return "#ff4757"
            return "#ffd32a"
        dots = "".join(
            f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{dot_color(s["status"])};margin:1px" title="{s["status"]}"></span>'
            for s in recent
        )

        st.markdown(f"""
        <div class="card" style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="display:flex;align-items:center;gap:16px">
              <span style="font-size:18px;font-weight:800;font-family:'JetBrains Mono'">{sym}</span>
              <span style="font-size:13px;color:#64748b">{stat['total']} sinyal</span>
              <span style="font-size:12px;color:#64748b">5 terakhir: {dots}</span>
            </div>
            <div style="display:flex;gap:24px;text-align:right">
              <div>
                <div style="font-size:11px;color:#64748b">Win Rate</div>
                <div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono';color:{prog_color}">{wr:.0f}%</div>
              </div>
              <div>
                <div style="font-size:11px;color:#64748b">Total P&L</div>
                <div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono';color:{pnl_color}">{'+' if pnl>=0 else ''}{pnl:.2f}%</div>
              </div>
              <div>
                <div style="font-size:11px;color:#64748b">TP / SL</div>
                <div style="font-size:16px;font-weight:700"><span style="color:#00e676">{stat['wins']}</span> / <span style="color:#ff4757">{stat['losses']}</span></div>
              </div>
            </div>
          </div>
          <div class="prog-wrap" style="margin-top:12px"><div class="prog-fill" style="width:{wr:.0f}%;background:{prog_color}"></div></div>
        </div>
        """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════
# TAB 4 — LESSONS
# ═══════════════════════════════════════════════
with tab_lessons:
    st.markdown('<div class="sec-header"><div class="sec-label">Learning Engine</div><div class="sec-title">Lessons yang Dipelajari Bot</div></div>', unsafe_allow_html=True)

    if not lessons:
        st.info("Belum ada lessons.")
    else:
        outcome_filter = st.selectbox("Filter outcome", ["Semua", "good", "poor", "neutral"], index=0)
        shown = lessons if outcome_filter == "Semua" else [l for l in lessons if l.get("outcome") == outcome_filter]
        shown = sorted(shown, key=lambda x: x.get("created_at",""), reverse=True)

        for les in shown[:40]:
            out   = les.get("outcome","—")
            conf  = les.get("confidence", 0)
            color = "#00e676" if out=="good" else "#ff4757" if out=="poor" else "#ffd32a"
            tags  = " ".join(f'<span class="chip">{t}</span>' for t in les.get("tags",[]))
            pnl   = les.get("pnl_pct")
            pnl_str = (f' · P&L: <span style="color:{"#00e676" if pnl>=0 else "#ff4757"}">{pnl:+.2f}%</span>' if pnl is not None else "")

            st.markdown(f"""
            <div class="card" style="margin-bottom:8px;border-left:3px solid {color}">
              <div style="display:flex;justify-content:space-between;margin-bottom:8px">
                <div>
                  <span style="background:{color}22;color:{color};border:1px solid {color}55;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{out.upper()}</span>
                  <span style="font-size:11px;color:#64748b;margin-left:10px">confidence: {conf:.0%}{pnl_str}</span>
                </div>
                <span style="font-size:11px;color:#64748b">{fmt_time(les.get('created_at',''))}</span>
              </div>
              <div style="font-size:14px;color:#cbd5e1;line-height:1.6">{les.get('rule','')}</div>
              <div style="margin-top:8px">{tags}</div>
            </div>
            """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════
# TAB 5 — DECISION LOG
# ═══════════════════════════════════════════════
with tab_log:
    st.markdown('<div class="sec-header"><div class="sec-label">Bot Activity</div><div class="sec-title">Decision Log</div></div>', unsafe_allow_html=True)

    if not decisions:
        st.info("Belum ada decision log.")
    else:
        recent_decisions = sorted(decisions, key=lambda x: x.get("ts",""), reverse=True)[:50]
        rows = ""
        for d in recent_decisions:
            dec   = d.get("decision","—")
            color = "#00e676" if dec in ("ALERT","PASS") else "#ffd32a" if dec=="WATCH" else "#64748b"
            top_r = (d.get("top_reasons") or [])[:2]
            rhtml = " ".join(f'<span class="chip">{r[:55]}</span>' for r in top_r)
            rows += f"""
            <tr>
              <td style="color:#64748b;font-size:12px">{fmt_time(d.get('ts',''))}</td>
              <td class="mono" style="font-weight:700;color:#e2e8f0">{d.get('symbol','—')}</td>
              <td><span style="color:#94a3b8;font-size:12px">{d.get('actor','—')}</span></td>
              <td><span style="background:{color}22;color:{color};border:1px solid {color}55;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{dec}</span></td>
              <td style="color:#ffd32a;font-family:'JetBrains Mono';font-weight:700">{d.get('score','—')}</td>
              <td>{rhtml}</td>
            </tr>"""

        st.markdown(f"""
        <div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px">
          <table class="tbl">
            <thead><tr><th>Waktu</th><th>Symbol</th><th>Actor</th><th>Decision</th><th>Score</th><th>Reasons</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """, unsafe_allow_html=True)

# ── Footer ──────────────────────────────────────
st.markdown("""
<div style="text-align:center;color:#1e2a38;font-size:12px;margin-top:48px;border-top:1px solid #1e2a38;padding-top:24px">
  CryptoBot v13 · Personal Dashboard · Bukan saran finansial
</div>
""", unsafe_allow_html=True)
