import streamlit as st
import json, pathlib
from datetime import datetime, timezone, timedelta

st.set_page_config(
    page_title="CryptoBot v13 — Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

ROOT      = pathlib.Path(__file__).parent.parent
HERE      = pathlib.Path(__file__).parent
PORT_FILE = HERE / "my_portfolio.json"

WIB = timezone(timedelta(hours=7))

# ── I/O helpers ──────────────────────────────────────────
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

@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(1.4)}}
.live-dot{ display:inline-block;width:8px;height:8px;background:#00e676;border-radius:50%;animation:pulse 1.5s infinite;margin-right:5px }

/* streamlit widget overrides */
div[data-testid="stNumberInput"] input,
div[data-testid="stTextInput"] input,
div[data-testid="stSelectbox"] select,
textarea{ background:#0f1621!important;border:1px solid #1e2a38!important;color:#e2e8f0!important;border-radius:8px!important }
div[data-testid="stForm"]{ background:#0f1621;border:1px solid #1e2a38;border-radius:14px;padding:20px }
</style>
""", unsafe_allow_html=True)

# ── load data ─────────────────────────────────────────────
outcomes  = load_json("signal_outcomes.json", [])
pending   = load_json("pending_signals.json", [])
lessons_raw = load_json("lessons.json", {})
lessons   = lessons_raw.get("lessons", []) if isinstance(lessons_raw, dict) else lessons_raw
decisions = load_json("decision_log.json", [])
port      = load_portfolio()

# ── derived stats ─────────────────────────────────────────
total    = len(outcomes)
tp_hit   = sum(1 for s in outcomes if s["status"] == "TP_HIT")
sl_hit   = sum(1 for s in outcomes if s["status"] == "SL_HIT")
exp_w    = sum(1 for s in outcomes if s["status"] == "EXPIRED_WIN")
exp_l    = sum(1 for s in outcomes if s["status"] == "EXPIRED_LOSS")
win_rate = (tp_hit + exp_w) / total * 100 if total else 0
pnls     = [s.get("pnl_pct", 0) for s in outcomes if s.get("pnl_pct") is not None]
total_pnl_pct = sum(pnls)
avg_score = sum(s.get("score", 0) for s in outcomes) / total if total else 0
wins_pnl  = [p for p in pnls if p > 0]
loss_pnl  = [p for p in pnls if p < 0]
pf        = abs(sum(wins_pnl)/sum(loss_pnl)) if sum(loss_pnl) != 0 else 0

# portfolio stats
closed    = port.get("closed_trades", [])
open_pos  = port.get("open_positions", [])
total_pnl_usdt   = sum(t.get("pnl_usdt", 0) for t in closed)
total_wins_usdt  = sum(t.get("pnl_usdt", 0) for t in closed if t.get("pnl_usdt", 0) > 0)
total_loss_usdt  = sum(t.get("pnl_usdt", 0) for t in closed if t.get("pnl_usdt", 0) < 0)
n_wins    = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
n_losses  = sum(1 for t in closed if t.get("pnl_usdt", 0) < 0)
port_wr   = n_wins / len(closed) * 100 if closed else 0
cur_bal   = port.get("current_balance", 0)
init_bal  = port.get("initial_balance", 0)
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

    # ── Set / Update Balance
    st.markdown("### 💰 Saldo Trading")
    with st.form("set_balance"):
        new_bal = st.number_input("Balance (USDT)", min_value=0.0, value=float(cur_bal) if cur_bal else 0.0, step=10.0, format="%.2f")
        bal_note = st.text_input("Catatan (opsional)", placeholder="Deposit, profit withdraw...")
        if st.form_submit_button("💾 Simpan Saldo", use_container_width=True):
            old = port.get("current_balance", 0)
            if not port.get("balance_set"):
                port["initial_balance"] = new_bal
                port["balance_set"] = True
            port["current_balance"] = new_bal
            port.setdefault("balance_history", []).append({
                "ts": now_str(), "event": "UPDATE",
                "old_balance": old, "new_balance": new_bal, "note": bal_note
            })
            save_portfolio(port)
            st.success(f"Saldo diupdate: ${new_bal:,.2f}")
            st.rerun()

    st.divider()

    # ── Quick stats sidebar
    if port.get("balance_set"):
        pnl_color = "#00e676" if total_pnl_usdt >= 0 else "#ff4757"
        roi = (cur_bal - init_bal) / init_bal * 100 if init_bal else 0
        roi_color = "#00e676" if roi >= 0 else "#ff4757"
        st.markdown(f"""
        <div style='margin-bottom:8px'>
          <div style='font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px'>Balance</div>
          <div style='font-size:24px;font-weight:900;font-family:"JetBrains Mono";color:#e2e8f0'>${cur_bal:,.2f}</div>
          <div style='font-size:12px;color:{roi_color};margin-top:2px'>{'+' if roi>=0 else ''}{roi:.2f}% ROI</div>
        </div>
        <div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px'>
          <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px'>
            <div style='font-size:10px;color:#64748b;margin-bottom:4px'>REALIZED P&L</div>
            <div style='font-size:16px;font-weight:800;font-family:"JetBrains Mono";color:{pnl_color}'>{'+' if total_pnl_usdt>=0 else ''}{total_pnl_usdt:,.2f}</div>
          </div>
          <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px'>
            <div style='font-size:10px;color:#64748b;margin-bottom:4px'>UNREALIZED</div>
            <div style='font-size:16px;font-weight:800;font-family:"JetBrains Mono";color:{"#00e676" if unrealized>=0 else "#ff4757"}'>{'+' if unrealized>=0 else ''}{unrealized:,.2f}</div>
          </div>
          <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px'>
            <div style='font-size:10px;color:#64748b;margin-bottom:4px'>WIN RATE</div>
            <div style='font-size:16px;font-weight:800;font-family:"JetBrains Mono";color:{"#00e676" if port_wr>=50 else "#ff4757"}'>{port_wr:.0f}%</div>
          </div>
          <div style='background:#0f1621;border:1px solid #1e2a38;border-radius:10px;padding:12px'>
            <div style='font-size:10px;color:#64748b;margin-bottom:4px'>OPEN POS</div>
            <div style='font-size:16px;font-weight:800;font-family:"JetBrains Mono";color:#00d4ff'>{len(open_pos)}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

    st.divider()
    st.markdown(f"""
    <div style='font-size:12px;color:#64748b;text-align:center'>
      <span class='live-dot'></span>
      {datetime.now(WIB).strftime('%d %b %Y %H:%M WIB')}<br>
      {len(pending)} pending bot · {total} sinyal tracked
    </div>
    """, unsafe_allow_html=True)

# ── HEADER ────────────────────────────────────────────────
st.markdown("""
<div style='display:flex;align-items:center;gap:12px;margin-bottom:4px'>
  <div style='width:10px;height:10px;background:#00d4ff;border-radius:50%;box-shadow:0 0 10px #00d4ff'></div>
  <span style='font-size:22px;font-weight:900;letter-spacing:-.5px'>CryptoBot <span style="color:#00d4ff">v13</span> · Personal Dashboard</span>
</div>
""", unsafe_allow_html=True)
st.divider()

# ── TABS ──────────────────────────────────────────────────
tab_port, tab_pending, tab_history, tab_coins, tab_lessons, tab_log = st.tabs([
    "💼 Portfolio",
    f"⏳ Pending ({len(pending)})",
    f"📋 Signal History ({total})",
    f"🪙 Coin Stats ({len(set(s['symbol'] for s in outcomes))})",
    f"🧠 Lessons ({len(lessons)})",
    f"📡 Decision Log",
])

# ══════════════════════════════════════════════════════════
# TAB 0 — PORTFOLIO / PERSONAL TRACKING
# ══════════════════════════════════════════════════════════
with tab_port:
    if not port.get("balance_set"):
        st.warning("⬅️ Set balance dulu di sidebar kiri sebelum mulai tracking.")

    # ── KPI row
    c1,c2,c3,c4,c5 = st.columns(5)
    kpi_data = [
        (c1, "Balance Sekarang", f"${cur_bal:,.2f}", f"Initial ${init_bal:,.2f}",
         "#00d4ff"),
        (c2, "Realized P&L", f"${total_pnl_usdt:+,.2f}",
         f"{'+' if (cur_bal-init_bal)/init_bal*100>=0 else ''}{(cur_bal-init_bal)/init_bal*100:.2f}% ROI" if init_bal else "Set balance dulu",
         "#00e676" if total_pnl_usdt>=0 else "#ff4757"),
        (c3, "Unrealized P&L", f"${unrealized:+,.2f}",
         f"{len(open_pos)} posisi terbuka · margin ${open_margin:,.2f}",
         "#00e676" if unrealized>=0 else "#ff4757"),
        (c4, "Win / Loss", f"{n_wins} / {n_losses}",
         f"Win rate {port_wr:.1f}% · {len(closed)} trades",
         "#00e676" if port_wr>=50 else "#ff4757"),
        (c5, "Best / Worst Trade",
         f"${max((t.get('pnl_usdt',0) for t in closed), default=0):+,.2f}",
         f"Worst: ${min((t.get('pnl_usdt',0) for t in closed), default=0):+,.2f}",
         "#ffd32a"),
    ]
    for col, label, val, sub, color in kpi_data:
        with col:
            st.markdown(f"""
            <div class="kpi">
              <div class="kpi-label">{label}</div>
              <div class="kpi-val" style="color:{color}">{val}</div>
              <div class="kpi-sub">{sub}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    left, right = st.columns([1.1, 0.9])

    # ── Open Positions
    with left:
        st.markdown('<div class="sec-label">Live Positions</div><div class="sec-title">Posisi Terbuka</div>', unsafe_allow_html=True)

        with st.expander("➕ Tambah Posisi Baru", expanded=len(open_pos)==0):
            with st.form("add_position", clear_on_submit=True):
                fc1,fc2 = st.columns(2)
                coin      = fc1.text_input("Coin", placeholder="BTC, ETH, SOL...").upper().replace("USDT","")
                direction = fc2.selectbox("Arah", ["LONG","SHORT"])
                fc3,fc4,fc5 = st.columns(3)
                entry     = fc3.number_input("Entry Price ($)", min_value=0.0, format="%.4f")
                margin    = fc4.number_input("Margin (USDT)", min_value=0.0, format="%.2f")
                leverage  = fc5.number_input("Leverage (x)", min_value=1, max_value=125, value=1)
                fc6,fc7 = st.columns(2)
                tp_price  = fc6.number_input("Take Profit ($)", min_value=0.0, format="%.4f")
                sl_price  = fc7.number_input("Stop Loss ($)", min_value=0.0, format="%.4f")
                note      = st.text_input("Catatan", placeholder="Setup reason, confluence level...")
                if st.form_submit_button("🚀 Buka Posisi", use_container_width=True):
                    if coin and entry > 0 and margin > 0:
                        pos = {
                            "id": int(datetime.now().timestamp()*1000),
                            "coin": coin, "direction": direction,
                            "entry": entry, "margin": margin, "leverage": leverage,
                            "position_size": round(margin * leverage, 2),
                            "tp": tp_price, "sl": sl_price,
                            "current_price": entry, "unrealized_pnl": 0.0,
                            "note": note, "opened_at": now_str(),
                        }
                        port.setdefault("open_positions", []).append(pos)
                        save_portfolio(port)
                        st.success(f"✅ Posisi {coin} {direction} dibuka!")
                        st.rerun()
                    else:
                        st.error("Lengkapi coin, entry price, dan margin dulu.")

        if not open_pos:
            st.info("Belum ada posisi terbuka.")
        else:
            for i, pos in enumerate(open_pos):
                cur  = pos.get("current_price", pos["entry"])
                sign = 1 if pos["direction"] == "LONG" else -1
                unr  = sign * (cur - pos["entry"]) / pos["entry"] * pos["position_size"]
                unr_pct = sign * (cur - pos["entry"]) / pos["entry"] * 100
                pnl_color = "#00e676" if unr >= 0 else "#ff4757"
                dir_color = "#00e676" if pos["direction"]=="LONG" else "#ff4757"
                dir_arrow = "▲" if pos["direction"]=="LONG" else "▼"

                tp_dist = ((pos["tp"] - pos["entry"]) / pos["entry"] * 100) if pos["tp"] else None
                sl_dist = ((pos["sl"] - pos["entry"]) / pos["entry"] * 100) if pos["sl"] else None

                with st.container():
                    st.markdown(f"""
                    <div class="card" style="border-left:3px solid {pnl_color}">
                      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                        <div style="display:flex;align-items:center;gap:10px">
                          <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{pos['coin']}USDT</span>
                          <span style="color:{dir_color};font-weight:700">{dir_arrow} {pos['direction']}</span>
                          <span style="background:#1e2a38;color:#94a3b8;font-size:11px;padding:2px 8px;border-radius:6px">{pos['leverage']}x</span>
                        </div>
                        <span style="font-size:12px;color:#64748b">{fmt_time(pos.get('opened_at',''))}</span>
                      </div>
                      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:12px">
                        <div><div style="font-size:10px;color:#64748b">Entry</div><div class="mono" style="font-weight:700">${pos['entry']:,.4f}</div></div>
                        <div><div style="font-size:10px;color:#64748b">Pos Size</div><div class="mono" style="font-weight:700">${pos['position_size']:,.2f}</div></div>
                        <div><div style="font-size:10px;color:#64748b">TP</div><div class="mono" style="color:#00e676">${pos['tp']:,.4f}{f' ({tp_dist:+.2f}%)' if tp_dist else ''}</div></div>
                        <div><div style="font-size:10px;color:#64748b">SL</div><div class="mono" style="color:#ff4757">${pos['sl']:,.4f}{f' ({sl_dist:+.2f}%)' if sl_dist else ''}</div></div>
                        <div><div style="font-size:10px;color:#64748b">Unrealized P&L</div><div class="mono" style="color:{pnl_color};font-weight:800">{unr:+,.2f} ({unr_pct:+.2f}%)</div></div>
                      </div>
                      {f'<div style="font-size:12px;color:#64748b;margin-bottom:10px">📝 {pos["note"]}</div>' if pos.get("note") else ""}
                    </div>
                    """, unsafe_allow_html=True)

                    cc1,cc2,cc3 = st.columns([2,1,1])
                    with cc1:
                        new_price = st.number_input(f"Update harga {pos['coin']}", min_value=0.0,
                                                    value=float(pos.get("current_price", pos["entry"])),
                                                    key=f"price_{pos['id']}", format="%.4f", label_visibility="visible")
                    with cc2:
                        if st.button("🔄 Update Harga", key=f"upd_{pos['id']}", use_container_width=True):
                            port["open_positions"][i]["current_price"] = new_price
                            unr_new = sign * (new_price - pos["entry"]) / pos["entry"] * pos["position_size"]
                            port["open_positions"][i]["unrealized_pnl"] = round(unr_new, 2)
                            save_portfolio(port)
                            st.rerun()
                    with cc3:
                        if st.button("✅ Tutup Posisi", key=f"close_{pos['id']}", use_container_width=True):
                            exit_price = pos.get("current_price", pos["entry"])
                            pnl_usdt   = sign * (exit_price - pos["entry"]) / pos["entry"] * pos["position_size"]
                            pnl_pct    = sign * (exit_price - pos["entry"]) / pos["entry"] * 100
                            trade = {
                                "id": pos["id"], "coin": pos["coin"], "direction": pos["direction"],
                                "entry": pos["entry"], "exit": exit_price,
                                "margin": pos["margin"], "leverage": pos["leverage"],
                                "position_size": pos["position_size"],
                                "pnl_usdt": round(pnl_usdt, 2), "pnl_pct": round(pnl_pct, 2),
                                "result": "WIN" if pnl_usdt > 0 else ("LOSS" if pnl_usdt < 0 else "BREAKEVEN"),
                                "note": pos.get("note",""),
                                "opened_at": pos.get("opened_at",""), "closed_at": now_str(),
                            }
                            port["closed_trades"].append(trade)
                            port["open_positions"].pop(i)
                            port["current_balance"] = round(port.get("current_balance", 0) + pnl_usdt, 2)
                            port.setdefault("balance_history", []).append({
                                "ts": now_str(), "event": "WIN" if pnl_usdt>0 else "LOSS",
                                "pnl": round(pnl_usdt,2), "balance": port["current_balance"],
                                "coin": pos["coin"]
                            })
                            save_portfolio(port)
                            st.success(f"Posisi {pos['coin']} ditutup · P&L: ${pnl_usdt:+,.2f}")
                            st.rerun()

    # ── Closed Trades
    with right:
        st.markdown('<div class="sec-label">Trade History</div><div class="sec-title">Closed Trades</div>', unsafe_allow_html=True)

        if not closed:
            st.info("Belum ada trade yang ditutup.")
        else:
            # equity mini-chart dengan st.bar_chart sederhana
            cum = 0
            eq_labels, eq_vals = [], []
            for t in closed:
                cum += t.get("pnl_usdt", 0)
                eq_labels.append(t.get("coin","?"))
                eq_vals.append(round(cum, 2))

            import pandas as pd
            df_eq = pd.DataFrame({"Cumulative P&L": eq_vals})
            st.line_chart(df_eq, height=140, use_container_width=True)

            rows = ""
            for t in sorted(closed, key=lambda x: x.get("closed_at",""), reverse=True):
                p = t.get("pnl_usdt", 0)
                pct = t.get("pnl_pct", 0)
                rc = "#00e676" if p>0 else "#ff4757"
                dir_c = "#00e676" if t["direction"]=="LONG" else "#ff4757"
                res_label = "✅ WIN" if t["result"]=="WIN" else ("❌ LOSS" if t["result"]=="LOSS" else "➖ BE")
                rows += f"""<tr>
                  <td class="mono" style="font-weight:700;color:#e2e8f0">{t['coin']}</td>
                  <td style="color:{dir_c};font-weight:700">{'▲' if t['direction']=='LONG' else '▼'} {t['direction']}</td>
                  <td class="mono" style="font-size:12px">${t['entry']:,.4f}<br><span style="color:#64748b">→${t['exit']:,.4f}</span></td>
                  <td><span style="color:{rc};font-weight:700">{res_label}</span></td>
                  <td class="mono" style="color:{rc};font-weight:700">{p:+,.2f}<br><span style="font-size:11px">{pct:+.2f}%</span></td>
                  <td style="color:#64748b;font-size:11px">{fmt_time(t.get('closed_at',''))}</td>
                </tr>"""

            st.markdown(f"""
            <div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px">
              <table class="tbl">
                <thead><tr><th>Coin</th><th>Arah</th><th>Entry/Exit</th><th>Hasil</th><th>P&L</th><th>Waktu</th></tr></thead>
                <tbody>{rows}</tbody>
              </table>
            </div>""", unsafe_allow_html=True)

        # Manual add closed trade
        with st.expander("➕ Input Trade Manual"):
            with st.form("add_closed", clear_on_submit=True):
                mc1,mc2 = st.columns(2)
                m_coin  = mc1.text_input("Coin").upper().replace("USDT","")
                m_dir   = mc2.selectbox("Arah", ["LONG","SHORT"], key="m_dir")
                mc3,mc4,mc5 = st.columns(3)
                m_entry  = mc3.number_input("Entry ($)", min_value=0.0, format="%.4f", key="m_entry")
                m_exit   = mc4.number_input("Exit ($)", min_value=0.0, format="%.4f", key="m_exit")
                m_margin = mc5.number_input("Margin (USDT)", min_value=0.0, format="%.2f", key="m_margin")
                mc6,mc7 = st.columns(2)
                m_lev    = mc6.number_input("Leverage", min_value=1, max_value=125, value=1, key="m_lev")
                m_note   = mc7.text_input("Catatan", key="m_note")
                m_date   = st.date_input("Tanggal trade")
                if st.form_submit_button("💾 Simpan Trade", use_container_width=True):
                    if m_coin and m_entry>0 and m_exit>0 and m_margin>0:
                        sign   = 1 if m_dir=="LONG" else -1
                        pos_sz = m_margin * m_lev
                        pnl_u  = sign * (m_exit - m_entry) / m_entry * pos_sz
                        pnl_p  = sign * (m_exit - m_entry) / m_entry * 100
                        trade  = {
                            "id": int(datetime.now().timestamp()*1000),
                            "coin": m_coin, "direction": m_dir,
                            "entry": m_entry, "exit": m_exit,
                            "margin": m_margin, "leverage": m_lev,
                            "position_size": round(pos_sz,2),
                            "pnl_usdt": round(pnl_u,2), "pnl_pct": round(pnl_p,2),
                            "result": "WIN" if pnl_u>0 else ("LOSS" if pnl_u<0 else "BREAKEVEN"),
                            "note": m_note,
                            "opened_at": str(m_date), "closed_at": str(m_date),
                        }
                        port["closed_trades"].append(trade)
                        port["current_balance"] = round(port.get("current_balance",0)+pnl_u,2)
                        save_portfolio(port)
                        st.success(f"Trade {m_coin} disimpan · P&L ${pnl_u:+,.2f}")
                        st.rerun()
                    else:
                        st.error("Lengkapi semua field.")

# ══════════════════════════════════════════════════════════
# TAB 1 — PENDING
# ══════════════════════════════════════════════════════════
with tab_pending:
    st.markdown('<div class="sec-label">Live Watchlist</div><div class="sec-title">Sinyal Pending Bot</div>', unsafe_allow_html=True)
    if not pending:
        st.info("Tidak ada sinyal pending saat ini.")
    else:
        for s in pending:
            created = datetime.fromisoformat(s["created_at"].replace("Z","+00:00")).astimezone(WIB)
            age_hrs = (datetime.now(WIB)-created).total_seconds()/3600
            timeout = s.get("timeout_hours",24)
            pct_done = min(age_hrs/timeout*100,100)
            dist_tp  = (s.get("tp",s["entry_price"])-s["entry_price"])/s["entry_price"]*100
            dist_sl  = (s.get("sl",s["entry_price"])-s["entry_price"])/s["entry_price"]*100
            reasons  = " ".join(f'<span class="chip">{r[:55]}</span>' for r in s.get("reasons",[])[:3])
            dir_c    = "#00e676" if s["direction"]=="LONG" else "#ff4757"
            dir_a    = "▲" if s["direction"]=="LONG" else "▼"
            bar_c    = "#ff4757" if pct_done>80 else "#ffd32a" if pct_done>50 else "#00d4ff"

            st.markdown(f"""
            <div class="card">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div style="display:flex;align-items:center;gap:10px">
                  <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{s['symbol']}</span>
                  <span style="color:{dir_c};font-weight:700">{dir_a} {s['direction']}</span>
                  <span style="background:#00d4ff22;color:#00d4ff;border:1px solid #00d4ff44;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{s.get('signal_type','—')}</span>
                </div>
                <span style="color:#ffd32a;font-weight:700;font-family:'JetBrains Mono'">{s.get('score','—')}/100</span>
              </div>
              <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px">
                <div><div style="font-size:10px;color:#64748b">Entry</div><div class="mono" style="font-weight:700">${s['entry_price']:,.4f}</div></div>
                <div><div style="font-size:10px;color:#64748b">TP</div><div class="mono" style="color:#00e676;font-weight:700">${s.get('tp',0):,.4f} ({dist_tp:+.2f}%)</div></div>
                <div><div style="font-size:10px;color:#64748b">SL</div><div class="mono" style="color:#ff4757;font-weight:700">${s.get('sl',0):,.4f} ({dist_sl:.2f}%)</div></div>
                <div><div style="font-size:10px;color:#64748b">Confluence</div><div style="font-weight:700;font-size:13px">{s.get('confluence_level','—')}</div></div>
              </div>
              <div style="margin-bottom:10px">{reasons}</div>
              <div style="display:flex;justify-content:space-between;font-size:12px;color:#64748b;margin-bottom:5px">
                <span>Berjalan: <b style="color:#e2e8f0">{age_hrs:.1f}h / {timeout}h</b></span>
                <span>{fmt_time(s.get('created_at',''))}</span>
              </div>
              <div class="prog-wrap"><div class="prog-fill" style="width:{pct_done:.0f}%;background:{bar_c}"></div></div>
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB 2 — SIGNAL HISTORY
# ══════════════════════════════════════════════════════════
with tab_history:
    st.markdown('<div class="sec-label">Riwayat Bot</div><div class="sec-title">Semua Signal</div>', unsafe_allow_html=True)

    def status_badge(s):
        m = {"TP_HIT":("#00e676","✅ TP"),"SL_HIT":("#ff4757","❌ SL"),
             "EXPIRED_WIN":("#ffd32a","⏱ EXP W"),"EXPIRED_LOSS":("#ff9800","⏱ EXP L")}
        c,l = m.get(s,("#64748b",s))
        return f'<span style="background:{c}22;color:{c};border:1px solid {c}55;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:700">{l}</span>'

    f1,f2,f3 = st.columns(3)
    fs  = f1.multiselect("Status", ["TP_HIT","SL_HIT","EXPIRED_WIN","EXPIRED_LOSS"])
    fd  = f2.multiselect("Arah", ["LONG","SHORT"])
    ft  = f3.multiselect("Type", list({s["signal_type"] for s in outcomes}))

    filt = outcomes[:]
    if fs: filt = [s for s in filt if s["status"] in fs]
    if fd: filt = [s for s in filt if s["direction"] in fd]
    if ft: filt = [s for s in filt if s["signal_type"] in ft]
    filt = sorted(filt, key=lambda x: x.get("created_at",""), reverse=True)

    rows = ""
    for s in filt:
        p = s.get("pnl_pct")
        pc = "#00e676" if p and p>=0 else "#ff4757"
        dc = "#00e676" if s["direction"]=="LONG" else "#ff4757"
        da = "▲" if s["direction"]=="LONG" else "▼"
        rows += f"""<tr>
          <td class="mono" style="font-weight:700;color:#e2e8f0">{s['symbol']}</td>
          <td style="color:{dc};font-weight:700">{da} {s['direction']}</td>
          <td style="font-size:12px;color:#94a3b8">{s.get('signal_type','—')}</td>
          <td class="mono">${s.get('entry_price',0):,.4f}</td>
          <td class="mono" style="color:#00e676">${s.get('tp',0):,.4f}</td>
          <td class="mono" style="color:#ff4757">${s.get('sl',0):,.4f}</td>
          <td>{status_badge(s['status'])}</td>
          <td class="mono" style="color:{pc};font-weight:700">{f'{p:+.2f}%' if p is not None else '—'}</td>
          <td style="color:#ffd32a;font-weight:700">{s.get('score','—')}</td>
          <td style="color:#64748b;font-size:12px">{fmt_time(s.get('created_at',''))}</td>
        </tr>"""

    st.markdown(f"""
    <div style="overflow-x:auto;background:#0f1621;border:1px solid #1e2a38;border-radius:14px;margin-top:8px">
      <table class="tbl">
        <thead><tr><th>Symbol</th><th>Arah</th><th>Type</th><th>Entry</th><th>TP</th><th>SL</th>
          <th>Status</th><th>P&L %</th><th>Score</th><th>Waktu</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    <div style="color:#64748b;font-size:12px;margin-top:8px">{len(filt)} / {total} sinyal</div>
    """, unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB 3 — COIN STATS
# ══════════════════════════════════════════════════════════
with tab_coins:
    st.markdown('<div class="sec-label">Per Koin</div><div class="sec-title">Performa Tiap Coin</div>', unsafe_allow_html=True)

    coin_stats = {}
    for s in outcomes:
        sym = s["symbol"]
        if sym not in coin_stats:
            coin_stats[sym] = {"total":0,"wins":0,"losses":0,"pnl":0.0,"signals":[]}
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
        pc  = "#00e676" if wr>=60 else "#ffd32a" if wr>=40 else "#ff4757"
        nc  = "#00e676" if pnl>=0 else "#ff4757"
        recent = sorted(stat["signals"], key=lambda x: x.get("created_at",""), reverse=True)[:5]
        dots = "".join(f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{dot_color(s["status"])};margin:1px" title="{s["status"]}"></span>' for s in recent)

        st.markdown(f"""
        <div class="card" style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="display:flex;align-items:center;gap:14px">
              <span style="font-size:17px;font-weight:800;font-family:'JetBrains Mono'">{sym}</span>
              <span style="font-size:12px;color:#64748b">{stat['total']} sinyal</span>
              <span>5 terakhir: {dots}</span>
            </div>
            <div style="display:flex;gap:20px;text-align:right">
              <div><div style="font-size:10px;color:#64748b">Win Rate</div><div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono';color:{pc}">{wr:.0f}%</div></div>
              <div><div style="font-size:10px;color:#64748b">Total P&L</div><div style="font-size:20px;font-weight:800;font-family:'JetBrains Mono';color:{nc}">{'+' if pnl>=0 else ''}{pnl:.2f}%</div></div>
              <div><div style="font-size:10px;color:#64748b">TP/SL</div><div style="font-size:16px;font-weight:700"><span style="color:#00e676">{stat['wins']}</span>/<span style="color:#ff4757">{stat['losses']}</span></div></div>
            </div>
          </div>
          <div class="prog-wrap" style="margin-top:10px"><div class="prog-fill" style="width:{wr:.0f}%;background:{pc}"></div></div>
        </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB 4 — LESSONS
# ══════════════════════════════════════════════════════════
with tab_lessons:
    st.markdown('<div class="sec-label">Learning Engine</div><div class="sec-title">Lessons yang Dipelajari Bot</div>', unsafe_allow_html=True)
    if not lessons:
        st.info("Belum ada lessons.")
    else:
        of = st.selectbox("Filter outcome", ["Semua","good","poor","neutral"])
        shown = lessons if of=="Semua" else [l for l in lessons if l.get("outcome")==of]
        shown = sorted(shown, key=lambda x: x.get("created_at",""), reverse=True)
        for les in shown[:40]:
            out = les.get("outcome","—")
            c   = "#00e676" if out=="good" else "#ff4757" if out=="poor" else "#ffd32a"
            conf= les.get("confidence",0)
            tags= " ".join(f'<span class="chip">{t}</span>' for t in les.get("tags",[]))
            p   = les.get("pnl_pct")
            ps  = f' · P&L: <span style="color:{"#00e676" if p and p>=0 else "#ff4757"}">{p:+.2f}%</span>' if p is not None else ""
            st.markdown(f"""
            <div class="card" style="margin-bottom:8px;border-left:3px solid {c}">
              <div style="display:flex;justify-content:space-between;margin-bottom:8px">
                <div><span style="background:{c}22;color:{c};border:1px solid {c}55;padding:2px 10px;border-radius:100px;font-size:11px;font-weight:700">{out.upper()}</span>
                  <span style="font-size:11px;color:#64748b;margin-left:10px">confidence {conf:.0%}{ps}</span></div>
                <span style="font-size:11px;color:#64748b">{fmt_time(les.get('created_at',''))}</span>
              </div>
              <div style="font-size:14px;color:#cbd5e1;line-height:1.6">{les.get('rule','')}</div>
              <div style="margin-top:8px">{tags}</div>
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# TAB 5 — DECISION LOG
# ══════════════════════════════════════════════════════════
with tab_log:
    st.markdown('<div class="sec-label">Bot Activity</div><div class="sec-title">Decision Log</div>', unsafe_allow_html=True)
    if not decisions:
        st.info("Belum ada decision log.")
    else:
        recent = sorted(decisions, key=lambda x: x.get("ts",""), reverse=True)[:60]
        rows = ""
        for d in recent:
            dec = d.get("decision","—")
            c   = "#00e676" if dec in ("ALERT","PASS") else "#ffd32a" if dec=="WATCH" else "#64748b"
            top = " ".join(f'<span class="chip">{r[:50]}</span>' for r in (d.get("top_reasons") or [])[:2])
            rows += f"""<tr>
              <td style="color:#64748b;font-size:12px">{fmt_time(d.get('ts',''))}</td>
              <td class="mono" style="font-weight:700;color:#e2e8f0">{d.get('symbol','—')}</td>
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
