#!/usr/bin/env python3
"""
TRADE JOURNAL MODULE v1.3
Fix: Dashboard #ERROR - formula ditulis sebagai plain string literal tanpa f-string injection
"""

import os, json, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("trade_journal")

try:
    from supabase_sync import push_trade, push_balance
    SUPABASE_MODULE = True
except ImportError:
    SUPABASE_MODULE = False
    def push_trade(t): return False
    def push_balance(e, a, b, n=""): return False

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_OK = True
except Exception as e:
    GSPREAD_OK = False
    print(f"DEBUG IMPORT ERROR: {e}")

try:
    import google.generativeai as genai
    GEMINI_OK = True
except ImportError:
    GEMINI_OK = False

CREDENTIALS_FILE   = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
CREDENTIALS_JSON   = os.getenv("GOOGLE_CREDENTIALS_JSON", "")   # isi JSON langsung (untuk Railway)
SPREADSHEET_ID     = os.getenv("GOOGLE_SPREADSHEET_ID", "")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
JOURNAL_STATE_FILE = Path("journal_state.json")

SHEET_TRADES  = "Trades"
SHEET_BALANCE = "Balance"
SHEET_SUMMARY = "Weekly Summary"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TRADE_HEADERS = [
    "Timestamp", "Coin", "Direction", "Entry Price",
    "Margin (USDT)", "Leverage", "Position Size (USDT)",
    "PnL (USDT)", "PnL (%)", "Result", "Note", "Image URL"
]
BALANCE_HEADERS = ["Timestamp", "Event", "Amount (USDT)", "Balance After (USDT)", "Note"]

_wizard_sessions: dict = {}


def _get_sheet():
    if not GSPREAD_OK:
        log.error("gspread tidak terinstall"); return None
    if not SPREADSHEET_ID:
        log.error("GOOGLE_SPREADSHEET_ID belum diset di .env"); return None
    try:
        if CREDENTIALS_JSON:
            creds = Credentials.from_service_account_info(json.loads(CREDENTIALS_JSON), scopes=SCOPES)
        elif Path(CREDENTIALS_FILE).exists():
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        else:
            log.error("Google credentials tidak ditemukan (set GOOGLE_CREDENTIALS_JSON di Railway)"); return None
        client = gspread.authorize(creds)
        return client.open_by_key(SPREADSHEET_ID)
    except Exception as e:
        log.error(f"Sheets connection error: {e}"); return None


def _ws(spreadsheet, title, headers):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
        ws.format(f"A1:{chr(64+len(headers))}1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
        return ws



def _setup_dashboard(spreadsheet):
    try:
        try:
            old = spreadsheet.worksheet("Dashboard")
            spreadsheet.del_worksheet(old)
        except Exception:
            pass

        ws = spreadsheet.add_worksheet(title="Dashboard", rows=100, cols=20)
        spreadsheet.reorder_worksheets(
            [ws] + [s for s in spreadsheet.worksheets() if s.title != "Dashboard"]
        )

        # ── SEMUA FORMULA HARDCODED PLAIN STRING ──────────────────────────────
        # Kutip di dalam formula Sheets = single quote '
        # Ini fix root cause #ERROR: tidak ada f-string atau variable injection
        # ─────────────────────────────────────────────────────────────────────

        F_TOTAL_TRADE   = "=COUNTA(Trades!A2:A10000)"
        F_WIN_ALL       = '=COUNTIF(Trades!J2:J10000,"WIN")'
        F_LOSS_ALL      = '=COUNTIF(Trades!J2:J10000,"LOSS")'
        F_WINRATE_ALL   = '=IFERROR(ROUND(COUNTIF(Trades!J2:J10000,"WIN")/COUNTA(Trades!J2:J10000)*100,1),0)'
        F_PNL_ALL       = "=IFERROR(ROUND(SUM(Trades!H2:H10000),2),0)"
        F_AVG_WIN_ALL   = '=IFERROR(ROUND(AVERAGEIF(Trades!J2:J10000,"WIN",Trades!H2:H10000),2),0)'
        F_AVG_LOSS_ALL  = '=IFERROR(ROUND(AVERAGEIF(Trades!J2:J10000,"LOSS",Trades!H2:H10000),2),0)'
        F_BEST_ALL      = "=IFERROR(ROUND(MAX(Trades!H2:H10000),2),0)"
        F_WORST_ALL     = "=IFERROR(ROUND(MIN(Trades!H2:H10000),2),0)"
        F_PF_ALL        = '=IFERROR(ROUND(SUMIF(Trades!J2:J10000,"WIN",Trades!H2:H10000)/ABS(SUMIF(Trades!J2:J10000,"LOSS",Trades!H2:H10000)),2),0)'
        F_LS_ALL        = '=IFERROR(COUNTIF(Trades!C2:C10000,"LONG")&" : "&COUNTIF(Trades!C2:C10000,"SHORT"),"0 : 0")'
        # INDEX+COUNTA untuk saldo terakhir - tidak pakai MATCH(9^9) yg salah kalau saldo turun
        F_SALDO         = "=IFERROR(INDEX(Balance!D2:D10000,COUNTA(Balance!D2:D10000)),0)"

        # Minggu ini - TEXT(TODAY()-7,"yyyy-mm-dd") dibandingkan LEFT(Timestamp,10)
        F_TRADE_WEEK    = '=IFERROR(SUMPRODUCT((LEN(Trades!A2:A10000)>0)*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd"))),0)'
        F_WIN_WEEK      = '=IFERROR(SUMPRODUCT((Trades!J2:J10000="WIN")*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd"))),0)'
        F_LOSS_WEEK     = '=IFERROR(SUMPRODUCT((Trades!J2:J10000="LOSS")*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd"))),0)'
        F_WINRATE_WEEK  = '=IFERROR(ROUND(SUMPRODUCT((Trades!J2:J10000="WIN")*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd")))/SUMPRODUCT((LEN(Trades!A2:A10000)>0)*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd")))*100,1),0)'
        # N() wrap kolom H untuk handle cell kosong supaya tidak error
        F_PNL_WEEK      = '=IFERROR(ROUND(SUMPRODUCT((LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd"))*N(Trades!H2:H10000)),2),0)'
        F_BEST_WEEK     = '=IFERROR(ROUND(MAXIFS(Trades!H2:H10000,Trades!A2:A10000,">="&TEXT(TODAY()-7,"yyyy-mm-dd")),2),0)'
        F_WORST_WEEK    = '=IFERROR(ROUND(MINIFS(Trades!H2:H10000,Trades!A2:A10000,">="&TEXT(TODAY()-7,"yyyy-mm-dd")),2),0)'
        F_LONG_WEEK     = '=IFERROR(SUMPRODUCT((Trades!C2:C10000="LONG")*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd"))),0)'
        F_SHORT_WEEK    = '=IFERROR(SUMPRODUCT((Trades!C2:C10000="SHORT")*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd"))),0)'
        F_LS_WEEK       = '=IFERROR(SUMPRODUCT((Trades!C2:C10000="LONG")*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd")))&" : "&SUMPRODUCT((Trades!C2:C10000="SHORT")*(LEFT(Trades!A2:A10000,10)>=TEXT(TODAY()-7,"yyyy-mm-dd"))),"0 : 0")'

        # QUERY per coin - TANPA label clause (sumber error di locale non-EN Google Sheets)
        # headers=0 karena kita pakai manual header row di atas QUERY
        F_QUERY_COIN    = "=IFERROR(QUERY(Trades!B2:J10000,\"select B, count(B), countif(J, 'WIN'), countif(J, 'LOSS'), round(countif(J, 'WIN')/count(B)*100,1), round(sum(H),2), round(avg(H),2), round(max(H),2) where B <> '' group by B order by sum(H) desc\",0),\"Belum ada data\")"

        F_SPARKLINE     = '=IFERROR(SPARKLINE(Balance!D2:D10000,{"charttype","line";"color","#00C853";"linewidth",3}),"Belum ada data")'

        data = [
            # R1 Title
            ["🚀 CRYPTO TRADE DASHBOARD", "", "", "", "", "", "", ""],
            # R2 spacer
            ["", "", "", "", "", "", "", ""],
            # R3 section headers
            ["📊 OVERVIEW ALL TIME", "", "", "", "📅 MINGGU INI (7 HARI)", "", "", ""],
            # R4-R15 data rows: Label | blank | Value | blank | Label | blank | Value | blank
            ["Total Trade",           "", F_TOTAL_TRADE,  "", "Trade Minggu Ini",       "", F_TRADE_WEEK,    ""],
            ["Win",                   "", F_WIN_ALL,       "", "Win Minggu Ini",          "", F_WIN_WEEK,      ""],
            ["Loss",                  "", F_LOSS_ALL,      "", "Loss Minggu Ini",         "", F_LOSS_WEEK,     ""],
            ["Win Rate %",            "", F_WINRATE_ALL,   "", "Win Rate Minggu Ini %",   "", F_WINRATE_WEEK,  ""],
            ["Total PnL (USDT)",      "", F_PNL_ALL,       "", "PnL Minggu Ini",          "", F_PNL_WEEK,      ""],
            ["Avg Win (USDT)",        "", F_AVG_WIN_ALL,   "", "Best Trade Minggu Ini",   "", F_BEST_WEEK,     ""],
            ["Avg Loss (USDT)",       "", F_AVG_LOSS_ALL,  "", "Worst Trade Minggu Ini",  "", F_WORST_WEEK,    ""],
            ["Best Trade (USDT)",     "", F_BEST_ALL,      "", "Long Minggu Ini",         "", F_LONG_WEEK,     ""],
            ["Worst Trade (USDT)",    "", F_WORST_ALL,     "", "Short Minggu Ini",        "", F_SHORT_WEEK,    ""],
            ["Profit Factor",         "", F_PF_ALL,        "", "Long vs Short Minggu",    "", F_LS_WEEK,       ""],
            ["Long vs Short",         "", F_LS_ALL,        "", "",                        "", "",               ""],
            ["Saldo Sekarang (USDT)", "", F_SALDO,         "", "",                        "", "",               ""],
            # R16 spacer
            ["", "", "", "", "", "", "", ""],
            # R17 per coin header
            ["🪙 PER COIN BREAKDOWN", "", "", "", "", "", "", ""],
            # R18 manual table headers
            ["Coin", "Total Trade", "Win", "Loss", "Win Rate %", "Total PnL (USDT)", "Avg PnL (USDT)", "Best Trade"],
            # R19 QUERY
            [F_QUERY_COIN, "", "", "", "", "", "", ""],
            # R20-21 spacer
            ["", "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", ""],
            # R22 equity header
            ["📈 EQUITY CURVE (Saldo dari waktu ke waktu)", "", "", "", "", "", "", ""],
            # R23 sparkline
            [F_SPARKLINE, "", "", "", "", "", "", ""],
        ]

        ws.update("A1", data, value_input_option="USER_ENTERED")

        sid = ws.id
        requests = [
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 16}, "backgroundColor": {"red": 0.05, "green": 0.05, "blue": 0.1}}}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 11}, "backgroundColor": {"red": 0.1, "green": 0.15, "blue": 0.25}}}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 16, "endRowIndex": 17, "startColumnIndex": 0, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 11}, "backgroundColor": {"red": 0.1, "green": 0.15, "blue": 0.25}}}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 17, "endRowIndex": 18, "startColumnIndex": 0, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.15, "green": 0.2, "blue": 0.3}}}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 3, "endRowIndex": 15, "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 3, "endRowIndex": 15, "startColumnIndex": 4, "endColumnIndex": 5},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat"}},
            {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3}, "properties": {"pixelSize": 160}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 5}, "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 7}, "properties": {"pixelSize": 160}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 22, "endIndex": 23}, "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
        ]

        cf = [
            {"addConditionalFormatRule": {"rule": {"ranges": [{"sheetId": sid, "startRowIndex": 3, "endRowIndex": 15, "startColumnIndex": 2, "endColumnIndex": 3}],
                "booleanRule": {"condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]}, "format": {"textFormat": {"bold": True, "foregroundColorStyle": {"rgbColor": {"red": 0.0, "green": 0.8, "blue": 0.2}}}}}}, "index": 0}},
            {"addConditionalFormatRule": {"rule": {"ranges": [{"sheetId": sid, "startRowIndex": 3, "endRowIndex": 15, "startColumnIndex": 2, "endColumnIndex": 3}],
                "booleanRule": {"condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]}, "format": {"textFormat": {"bold": True, "foregroundColorStyle": {"rgbColor": {"red": 1.0, "green": 0.2, "blue": 0.2}}}}}}, "index": 1}},
            {"addConditionalFormatRule": {"rule": {"ranges": [{"sheetId": sid, "startRowIndex": 3, "endRowIndex": 15, "startColumnIndex": 6, "endColumnIndex": 7}],
                "booleanRule": {"condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]}, "format": {"textFormat": {"bold": True, "foregroundColorStyle": {"rgbColor": {"red": 0.0, "green": 0.8, "blue": 0.2}}}}}}, "index": 2}},
            {"addConditionalFormatRule": {"rule": {"ranges": [{"sheetId": sid, "startRowIndex": 3, "endRowIndex": 15, "startColumnIndex": 6, "endColumnIndex": 7}],
                "booleanRule": {"condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]}, "format": {"textFormat": {"bold": True, "foregroundColorStyle": {"rgbColor": {"red": 1.0, "green": 0.2, "blue": 0.2}}}}}}, "index": 3}},
        ]

        spreadsheet.batch_update({"requests": requests + cf})
        log.info("Dashboard sheet berhasil dibuat")
        return True

    except Exception as e:
        log.error(f"Setup dashboard error: {e}")
        return False



def _load_state() -> dict:
    if JOURNAL_STATE_FILE.exists():
        try:
            with open(JOURNAL_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"initial_balance": 0.0, "current_balance": 0.0, "total_trades": 0, "balance_set": False}

def _save_state(s: dict):
    with open(JOURNAL_STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def get_current_balance() -> float:
    return _load_state().get("current_balance", 0.0)

def _now() -> str:
    wib = timezone(timedelta(hours=7))
    return datetime.now(wib).strftime("%Y-%m-%d %H:%M:%S")

def set_initial_balance(amount: float) -> str:
    s = _load_state()
    s["initial_balance"] = s["current_balance"] = float(amount)
    s["balance_set"] = True
    _save_state(s)
    sheet = _get_sheet()
    if sheet:
        ws = _ws(sheet, SHEET_BALANCE, BALANCE_HEADERS)
        ws.append_row([_now(), "INITIAL BALANCE", amount, amount, "Set awal"], value_input_option="RAW")
        _setup_dashboard(sheet)
    try:
        push_balance("INITIAL BALANCE", amount, amount, "Set awal")
    except Exception: pass
    return f"✅ Saldo awal diset: <b>${amount:,.2f} USDT</b>"

def _update_balance(pnl: float, note: str = ""):
    s = _load_state()
    s["current_balance"] = round(s["current_balance"] + pnl, 2)
    s["total_trades"]    = s.get("total_trades", 0) + 1
    _save_state(s)
    sheet = _get_sheet()
    if sheet:
        ws = _ws(sheet, SHEET_BALANCE, BALANCE_HEADERS)
        ws.append_row([_now(), "PROFIT" if pnl >= 0 else "LOSS", round(pnl, 2), s["current_balance"], note], value_input_option="RAW")
    try:
        push_balance("PROFIT" if pnl >= 0 else "LOSS", round(pnl, 2), s["current_balance"], note)
    except Exception: pass

def log_trade(coin, direction, entry_price, margin_usdt, leverage, pnl_usdt, note="", image_url="") -> dict:
    pos_size = round(margin_usdt * leverage, 2)
    pnl_pct  = round(pnl_usdt / margin_usdt * 100, 2) if margin_usdt > 0 else 0
    result   = "WIN" if pnl_usdt > 0 else ("LOSS" if pnl_usdt < 0 else "BREAKEVEN")
    ts       = _now()
    row      = [ts, coin.upper().replace("USDT",""), direction.upper(), entry_price,
                margin_usdt, leverage, pos_size, round(pnl_usdt, 2), pnl_pct, result, note, image_url]
    sheets_ok = False
    sheet = _get_sheet()
    if sheet:
        try:
            ws = _ws(sheet, SHEET_TRADES, TRADE_HEADERS)
            ws.append_row(row, value_input_option="RAW")
            sheets_ok = True
        except Exception as e:
            log.error(f"Append error: {e}")
    _update_balance(pnl_usdt, f"{coin} {direction} {result}")
    result_dict = {"ts": ts, "coin": coin.upper().replace("USDT",""), "direction": direction.upper(),
                   "entry": entry_price, "margin": margin_usdt, "leverage": leverage,
                   "position_size": pos_size, "pnl_usdt": round(pnl_usdt, 2), "pnl_pct": pnl_pct,
                   "result": result, "note": note, "sheets_ok": sheets_ok, "balance_after": get_current_balance()}
    # Sync ke Supabase (non-blocking, tidak ganggu flow utama)
    try:
        push_trade(result_dict)
    except Exception as e:
        log.warning(f"supabase push_trade error: {e}")
    return result_dict

def format_trade_logged(t: dict) -> str:
    ed = "🟢" if t["direction"] == "LONG" else "🔴"
    er = "✅" if t["result"] == "WIN" else ("❌" if t["result"] == "LOSS" else "➖")
    s  = "+" if t["pnl_usdt"] >= 0 else ""
    sh = "📊 Tersimpan di Google Sheets" if t["sheets_ok"] else "⚠️ Sheets offline"
    return (
        f"━━━━━━━━━━━━━━━━━━━━\n📝 <b>TRADE DICATAT</b> {er}\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Coin      : <b>{t['coin']}USDT</b>\n{ed} Arah     : <b>{t['direction']}</b>\n"
        f"💵 Entry     : <b>${t['entry']:,.4f}</b>\n💰 Margin    : ${t['margin']:,.2f} USDT\n"
        f"⚡ Leverage  : {t['leverage']}x\n📦 Pos Size  : ${t['position_size']:,.2f} USDT\n"
        f"{'📈' if t['pnl_usdt'] >= 0 else '📉'} PnL      : <b>{s}{t['pnl_usdt']:.2f} USDT ({s}{t['pnl_pct']:.1f}%)</b>\n"
        f"💼 Saldo Now : <b>${t['balance_after']:,.2f} USDT</b>\n🕐 Waktu     : {t['ts']}\n{sh}"
    )

def get_recent_trades(n: int = 5) -> list:
    sheet = _get_sheet()
    if not sheet: return []
    try:
        ws   = sheet.worksheet(SHEET_TRADES)
        rows = ws.get_all_records()
        return rows[-n:] if len(rows) >= n else rows
    except Exception as e:
        log.error(f"get_recent_trades error: {e}"); return []

def format_recent_trades(trades: list) -> str:
    if not trades:
        return "📭 Belum ada trade.\nGunakan /logtrade untuk mulai."
    lines = ["📋 <b>TRADE TERAKHIR</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for i, t in enumerate(reversed(trades), 1):
        pnl = float(t.get("PnL (USDT)", 0))
        s   = "+" if pnl >= 0 else ""
        res = t.get("Result", "")
        em  = "✅" if res == "WIN" else ("❌" if res == "LOSS" else "➖")
        de  = "🟢" if t.get("Direction") == "LONG" else "🔴"
        lines.append(f"{i}. {em} <b>{t.get('Coin','?')}</b> {de}{t.get('Direction','?')} | Entry: ${float(t.get('Entry Price',0)):,.4f} | PnL: <b>{s}{pnl:.2f} USDT</b>")
        lines.append(f"   📅 {t.get('Timestamp','')} | {t.get('Leverage','?')}x")
        if t.get("Note"):
            lines.append(f"   📝 {t['Note']}")
        lines.append("")
    return "\n".join(lines)

def get_week_trades() -> list:
    sheet = _get_sheet()
    if not sheet: return []
    try:
        rows   = sheet.worksheet(SHEET_TRADES).get_all_records()
        cutoff = datetime.now(timezone(timedelta(hours=7))) - timedelta(days=7)
        result = []
        for row in rows:
            try:
                ts = datetime.strptime(row.get("Timestamp",""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=7)))
                if ts >= cutoff: result.append(row)
            except Exception:
                continue
        return result
    except Exception as e:
        log.error(f"get_week_trades error: {e}"); return []

def _compute_stats(trades: list) -> dict:
    if not trades: return {}
    pnls  = [float(t.get("PnL (USDT)", 0)) for t in trades]
    wins  = [p for p in pnls if p > 0]
    loses = [p for p in pnls if p < 0]
    dirs  = [t.get("Direction", "") for t in trades]
    coin_stats: dict = {}
    for t in trades:
        c = t.get("Coin", "?")
        p = float(t.get("PnL (USDT)", 0))
        if c not in coin_stats:
            coin_stats[c] = {"trades": 0, "pnl": 0.0, "wins": 0}
        coin_stats[c]["trades"] += 1
        coin_stats[c]["pnl"]    = round(coin_stats[c]["pnl"] + p, 2)
        if p > 0: coin_stats[c]["wins"] += 1
    return {
        "total_trades": len(trades), "wins": len(wins), "losses": len(loses),
        "win_rate":      round(len(wins)/len(trades)*100, 1),
        "total_pnl":     round(sum(pnls), 2),
        "avg_win":       round(sum(wins)/len(wins), 2) if wins else 0,
        "avg_loss":      round(sum(loses)/len(loses), 2) if loses else 0,
        "profit_factor": round(sum(wins)/abs(sum(loses)), 2) if loses and sum(loses) != 0 else 0,
        "best_trade":    max(pnls), "worst_trade": min(pnls),
        "long_count":    dirs.count("LONG"), "short_count": dirs.count("SHORT"),
        "coin_stats":    coin_stats, "balance_now": get_current_balance(),
    }

def _gemini_analysis(stats: dict, trades: list) -> str:
    if not GEMINI_OK or not GEMINI_API_KEY: return ""
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
        tl = "\n".join([f"- {t.get('Coin')} {t.get('Direction')} | Entry:{t.get('Entry Price')} | Margin:{t.get('Margin (USDT)')} | {t.get('Leverage')}x | PnL:{t.get('PnL (USDT)')} USDT | {t.get('Result')} | Note:{t.get('Note','-')}" for t in trades[-20:]])
        cs_text = "\n".join([f"  {c}: {v['trades']} trades, WR {round(v['wins']/v['trades']*100) if v['trades'] else 0}%, PnL {v['pnl']:+.2f}" for c, v in stats["coin_stats"].items()])
        prompt = f"""Kamu adalah mentor trading crypto profesional.
Analisa trading journal mingguan ini dan beri feedback jujur, konstruktif, actionable.

=== STATISTIK ===
Total Trade   : {stats['total_trades']}
Win Rate      : {stats['win_rate']}%
Total PnL     : {stats['total_pnl']:+.2f} USDT
Profit Factor : {stats['profit_factor']}
Avg Win       : +{stats['avg_win']:.2f} USDT
Avg Loss      : {stats['avg_loss']:.2f} USDT
Best Trade    : +{stats['best_trade']:.2f} USDT
Worst Trade   : {stats['worst_trade']:.2f} USDT
Long : Short  : {stats['long_count']} : {stats['short_count']}
Saldo         : ${stats['balance_now']:,.2f} USDT

=== PER COIN ===
{cs_text}

=== TRADE LOG ===
{tl}

Format jawaban (bahasa Indonesia, singkat padat, pakai emoji):
1. 📊 VERDICT: (1 kalimat overall)
2. ✅ YANG BAGUS: (2-3 poin)
3. ⚠️ YANG PERLU DIPERBAIKI: (2-3 poin spesifik + data)
4. 🎯 POLA TERDETEKSI: (overtrading, revenge trade, dll)
5. 📈 STRATEGI MINGGU DEPAN: (3-5 poin actionable)
6. ⭐ GRADE: (A/B/C/D + alasan singkat)

Jangan basa-basi. Fokus ke data."""
        return model.generate_content(prompt).text.strip()
    except Exception as e:
        log.error(f"Gemini analysis error: {e}"); return ""

def format_weekly_summary() -> str:
    trades = get_week_trades()
    if not trades:
        return "📭 <b>WEEKLY SUMMARY</b>\n\nBelum ada trade di 7 hari terakhir.\nGunakan /logtrade untuk catat trade."
    stats      = _compute_stats(trades)
    now        = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y")
    week_start = (datetime.now(timezone(timedelta(hours=7))) - timedelta(days=7)).strftime("%d %b")
    pe  = "🟢" if stats["total_pnl"] >= 0 else "🔴"
    ps  = "+" if stats["total_pnl"] >= 0 else ""
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━", "📅 <b>WEEKLY SUMMARY</b>", f"   {week_start} – {now}",
        "━━━━━━━━━━━━━━━━━━━━━━━━", "",
        "📊 <b>PERFORMA MINGGU INI</b>",
        f"  Total Trade   : {stats['total_trades']} ({stats['wins']}W / {stats['losses']}L)",
        f"  Win Rate      : <b>{stats['win_rate']}%</b>",
        f"  {pe} Total PnL : <b>{ps}{stats['total_pnl']:.2f} USDT</b>",
        f"  Profit Factor : {stats['profit_factor']}",
        f"  Avg Win       : +{stats['avg_win']:.2f} USDT",
        f"  Avg Loss      : {stats['avg_loss']:.2f} USDT",
        f"  Best Trade    : +{stats['best_trade']:.2f} USDT",
        f"  Worst Trade   : {stats['worst_trade']:.2f} USDT",
        f"  Long : Short  : {stats['long_count']} : {stats['short_count']}",
        f"  💼 Saldo Now  : <b>${stats['balance_now']:,.2f} USDT</b>", "",
        "🪙 <b>BREAKDOWN PER COIN</b>",
    ]
    for coin, cs in sorted(stats["coin_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = round(cs["wins"]/cs["trades"]*100) if cs["trades"] else 0
        s  = "+" if cs["pnl"] >= 0 else ""
        em = "✅" if cs["pnl"] >= 0 else "❌"
        lines.append(f"  {em} {coin}: {cs['trades']}x | WR {wr}% | {s}{cs['pnl']:.2f} USDT")
    ai = _gemini_analysis(stats, trades)
    lines.append("")
    if ai:
        lines += ["🤖 <b>AI MENTOR ANALYSIS</b>", "━━━━━━━━━━━━━━━━━━━━━━━━", ai]
    else:
        lines.append("ℹ️ <i>AI analysis tidak tersedia</i>")
    lines += ["", "📊 <i>Data lengkap tersimpan di Google Sheets</i>"]
    return "\n".join(lines)

STEPS = [
    ("coin",      "🪙 Koin apa? (contoh: BTC, ETH, SOL)"),
    ("direction", "📍 LONG atau SHORT?"),
    ("entry",     "💵 Entry price berapa?"),
    ("margin",    "💰 Margin berapa USDT? (modal yang dipakai di trade ini)"),
    ("leverage",  "⚡ Leverage berapa? (contoh: 10 untuk 10x)"),
    ("pnl",       "📈 Hasil trade dalam USDT?\n   Profit → <code>+25</code>\n   Loss   → <code>-15</code>"),
    ("note",      "📝 Catatan? (setup yang dipakai, kenapa entry, dll)\nKetik <b>skip</b> kalau tidak ada"),
    ("image",     "📸 Kirim screenshot chart sebagai bukti\nKetik <b>skip</b> kalau tidak ada"),
]

def wizard_start(chat_id: str) -> str:
    _wizard_sessions[chat_id] = {"step": 0, "data": {}}
    return f"━━━━━━━━━━━━━━━━━━━━\n📝 <b>LOG TRADE BARU</b>\n━━━━━━━━━━━━━━━━━━━━\n{STEPS[0][1]}"

def is_in_wizard(chat_id: str) -> bool:
    return chat_id in _wizard_sessions

def is_wizard_expecting_image(chat_id: str) -> bool:
    s = _wizard_sessions.get(chat_id)
    return bool(s) and STEPS[s["step"]][0] == "image"

def wizard_process(chat_id: str, text: str = "", image_url: str = "") -> tuple:
    session = _wizard_sessions.get(chat_id)
    if not session:
        return "❓ Tidak ada sesi aktif. Ketik /logtrade untuk mulai.", True
    step_key, _ = STEPS[session["step"]]
    data = session["data"]
    if step_key == "image":
        data["image"] = image_url or ""
        del _wizard_sessions[chat_id]
        t = log_trade(coin=data["coin"], direction=data["direction"],
                      entry_price=float(data["entry"]), margin_usdt=float(data["margin"]),
                      leverage=int(data["leverage"]), pnl_usdt=float(data["pnl"]),
                      note=data.get("note",""), image_url=data.get("image",""))
        return format_trade_logged(t), True
    if step_key == "note":
        data["note"] = "" if text.strip().lower() == "skip" else text.strip()
        session["step"] += 1
        return STEPS[session["step"]][1], False
    val, err = _validate(step_key, text)
    if err:
        return f"❌ {err}\n\nCoba lagi:", False
    data[step_key] = val
    session["step"] += 1
    return STEPS[session["step"]][1], False

def _validate(key: str, text: str):
    text = text.strip()
    if key == "coin":
        c = text.upper().replace("USDT","").replace("/","").strip()
        return (c, None) if (c and len(c) >= 1) else (None, "Nama koin tidak valid. Contoh: BTC")
    if key == "direction":
        d = text.upper()
        if d not in ("LONG","SHORT","L","S"):
            return None, "Harus LONG atau SHORT"
        return ("LONG" if d in ("LONG","L") else "SHORT"), None
    if key in ("entry","margin"):
        try:
            v = float(text.replace(",",""))
            return (v, None) if v > 0 else (None, "Harus angka positif")
        except ValueError:
            return None, f"Harus angka. Contoh: {'65000' if key=='entry' else '50'}"
    if key == "leverage":
        try:
            v = int(float(text.replace("x","")))
            return (v, None) if 1 <= v <= 125 else (None, "Leverage harus 1-125")
        except ValueError:
            return None, "Harus angka. Contoh: 10"
    if key == "pnl":
        try:
            return float(text.replace(",","").replace(" ","")), None
        except ValueError:
            return None, "Harus angka. Contoh: +25 atau -15"
    return text, None

def parse_oneliner(args: str):
    parts = args.strip().split(maxsplit=6)
    if len(parts) < 6:
        return None, "❓ Format kurang lengkap.\nContoh: <code>/logtrade BTC LONG 65000 50 10 +25</code>\nField  : KOIN ARAH ENTRY MARGIN LEVERAGE HASIL [catatan]"
    try:
        coin = parts[0].upper().replace("USDT","")
        d    = parts[1].upper()
        if d not in ("LONG","SHORT","L","S"):
            return None, "Arah harus LONG atau SHORT"
        return {"coin": coin, "direction": "LONG" if d in ("LONG","L") else "SHORT",
                "entry": float(parts[2].replace(",","")), "margin": float(parts[3].replace(",","")),
                "leverage": int(float(parts[4].replace("x",""))), "pnl": float(parts[5].replace(",","")),
                "note": parts[6] if len(parts) > 6 else ""}, ""
    except Exception as e:
        return None, f"❌ Format salah: {e}"
