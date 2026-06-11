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



def _rgb(r, g, b):
    return {"red": r / 255, "green": g / 255, "blue": b / 255}

def _cell_fmt(sid, r1, r2, c1, c2, fmt):
    return {"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "cell": {"userEnteredFormat": fmt},
        "fields": "userEnteredFormat",
    }}

def _merge(sid, r1, r2, c1, c2):
    return {"mergeCells": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "mergeType": "MERGE_ALL",
    }}

def _col_w(sid, c1, c2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": c1, "endIndex": c2},
        "properties": {"pixelSize": px}, "fields": "pixelSize",
    }}

def _row_h(sid, r1, r2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS",
                  "startIndex": r1, "endIndex": r2},
        "properties": {"pixelSize": px}, "fields": "pixelSize",
    }}

def _border(sid, r1, r2, c1, c2, color=None):
    if color is None:
        color = {"red": 0.2, "green": 0.25, "blue": 0.35}
    side = {"style": "SOLID", "colorStyle": {"rgbColor": color}}
    return {"updateBorders": {
        "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "top": side, "bottom": side, "left": side, "right": side,
        "innerHorizontal": side, "innerVertical": side,
    }}

def _cf_text(sid, r1, r2, c1, c2, cond_type, val, fg, bold=True):
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [{"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                    "startColumnIndex": c1, "endColumnIndex": c2}],
        "booleanRule": {
            "condition": {"type": cond_type, "values": [{"userEnteredValue": val}]},
            "format": {"textFormat": {"bold": bold, "foregroundColorStyle": {"rgbColor": fg}}},
        },
    }, "index": 0}}

def _cf_bg(sid, r1, r2, c1, c2, cond_type, val, bg):
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [{"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                    "startColumnIndex": c1, "endColumnIndex": c2}],
        "booleanRule": {
            "condition": {"type": cond_type, "values": [{"userEnteredValue": val}]},
            "format": {"backgroundColor": bg},
        },
    }, "index": 0}}


def _setup_dashboard(spreadsheet):
    try:
        # ── Set locale ke en_US supaya formula pakai koma (bukan titik koma) ──
        try:
            spreadsheet.batch_update({"requests": [{
                "updateSpreadsheetProperties": {
                    "properties": {"locale": "en_US"},
                    "fields": "locale",
                }
            }]})
        except Exception as e:
            log.warning(f"Set locale error (lanjut): {e}")

        # ── Hapus & buat ulang Dashboard ────────────────────────
        try:
            spreadsheet.del_worksheet(spreadsheet.worksheet("Dashboard"))
        except Exception:
            pass
        ws = spreadsheet.add_worksheet(title="Dashboard", rows=80, cols=14)
        spreadsheet.reorder_worksheets(
            [ws] + [s for s in spreadsheet.worksheets() if s.title != "Dashboard"]
        )
        sid = ws.id

        # ── Warna palette ────────────────────────────────────────
        C_BG_HEADER   = _rgb(10,  18,  40)    # navy gelap — judul utama
        C_BG_KPI      = _rgb(15,  25,  55)    # navy medium — KPI box
        C_BG_SECTION  = _rgb(20,  35,  70)    # navy — section header
        C_BG_TH       = _rgb(25,  45,  90)    # biru — table header
        C_BG_ROW_ALT  = _rgb(16,  22,  44)    # alternating rows
        C_BG_DARK     = _rgb(12,  18,  36)    # base dark
        C_BG_EQUITY   = _rgb(10,  18,  40)

        C_WHITE       = _rgb(255, 255, 255)
        C_ACCENT      = _rgb(0,   180, 255)   # cyan accent
        C_GREEN       = _rgb(0,   200, 100)
        C_RED         = _rgb(255,  70,  70)
        C_YELLOW      = _rgb(255, 200,  50)
        C_MUTED       = _rgb(140, 160, 200)

        ALIGN_C = {"horizontalAlignment": "CENTER"}
        ALIGN_R = {"horizontalAlignment": "RIGHT"}
        ALIGN_L = {"horizontalAlignment": "LEFT"}

        def txt(bold=False, size=10, fg=None, italic=False):
            t = {"bold": bold, "fontSize": size, "italic": italic}
            if fg:
                t["foregroundColorStyle"] = {"rgbColor": fg}
            return {"textFormat": t}

        def cell(bg=None, text_fmt=None, align=None, wrap=None):
            f = {}
            if bg:   f["backgroundColor"] = bg
            if text_fmt: f["textFormat"]   = text_fmt
            if align:    f.update(align)
            if wrap:     f["wrapStrategy"] = wrap
            return f

        # ── Formulas ────────────────────────────────────────────
        # Saldo = Initial Balance + SUM semua PnL dari Trades
        # Lebih akurat daripada ambil entry terakhir Balance sheet
        F_INIT_BAL     = '=IFERROR(SUMIF(Balance!B2:B10000,"INITIAL BALANCE",Balance!D2:D10000),0)'
        F_SALDO        = '=IFERROR(ROUND(SUMIF(Balance!B2:B10000,"INITIAL BALANCE",Balance!D2:D10000)+SUM(Trades!H2:H10000),2),0)'
        F_TOTAL        = "=COUNTA(Trades!A2:A10000)"
        F_WIN          = '=COUNTIF(Trades!J2:J10000,"WIN")'
        F_LOSS         = '=COUNTIF(Trades!J2:J10000,"LOSS")'
        F_WR           = '=IFERROR(ROUND(COUNTIF(Trades!J2:J10000,"WIN")/COUNTA(Trades!J2:J10000)*100,1)&"%","-")'
        F_PNL          = "=IFERROR(ROUND(SUM(Trades!H2:H10000),2),0)"
        F_PF           = '=IFERROR(ROUND(SUMIF(Trades!J2:J10000,"WIN",Trades!H2:H10000)/ABS(SUMIF(Trades!J2:J10000,"LOSS",Trades!H2:H10000)),2),"-")'
        F_AVG_WIN      = '=IFERROR(ROUND(AVERAGEIF(Trades!J2:J10000,"WIN",Trades!H2:H10000),2),0)'
        F_AVG_LOSS     = '=IFERROR(ROUND(AVERAGEIF(Trades!J2:J10000,"LOSS",Trades!H2:H10000),2),0)'
        F_BEST         = "=IFERROR(ROUND(MAX(Trades!H2:H10000),2),0)"
        F_WORST        = "=IFERROR(ROUND(MIN(Trades!H2:H10000),2),0)"
        F_LS           = '=IFERROR(COUNTIF(Trades!C2:C10000,"LONG")&" L  /  "&COUNTIF(Trades!C2:C10000,"SHORT")&" S","0 / 0")'

        _W  = 'TEXT(TODAY()-7,"yyyy-mm-dd")'
        _TS = "LEFT(Trades!A2:A10000,10)"
        _RES= "Trades!J2:J10000"
        _PNL= "Trades!H2:H10000"
        _DIR= "Trades!C2:C10000"

        F_TW   = f'=IFERROR(SUMPRODUCT((LEN(Trades!A2:A10000)>0)*({_TS}>={_W})),0)'
        F_WW   = f'=IFERROR(SUMPRODUCT(({_RES}="WIN")*({_TS}>={_W})),0)'
        F_LW   = f'=IFERROR(SUMPRODUCT(({_RES}="LOSS")*({_TS}>={_W})),0)'
        F_WRW  = f'=IFERROR(ROUND(SUMPRODUCT(({_RES}="WIN")*({_TS}>={_W}))/SUMPRODUCT((LEN(Trades!A2:A10000)>0)*({_TS}>={_W}))*100,1)&"%","-")'
        F_PNLW = f'=IFERROR(ROUND(SUMPRODUCT(({_TS}>={_W})*N({_PNL})),2),0)'
        F_BW   = f'=IFERROR(ROUND(MAXIFS({_PNL},Trades!A2:A10000,">="&{_W}),2),0)'
        F_WW2  = f'=IFERROR(ROUND(MINIFS({_PNL},Trades!A2:A10000,">="&{_W}),2),0)'
        F_LSW  = f'=IFERROR(SUMPRODUCT(({_DIR}="LONG")*({_TS}>={_W}))&" L  /  "&SUMPRODUCT(({_DIR}="SHORT")*({_TS}>={_W}))&" S","0 / 0")'

        F_QUERY = "=IFERROR(QUERY(Trades!B2:J10000,\"select B, count(B), countif(J, 'WIN'), countif(J, 'LOSS'), round(countif(J, 'WIN')/count(B)*100,1), round(sum(H),2), round(avg(H),2), round(max(H),2), round(min(H),2) where B <> '' group by B order by sum(H) desc\",0),\"Belum ada data\")"
        F_SPARK = '=IFERROR(SPARKLINE(Balance!D2:D10000,{"charttype","line";"color","#00C853";"linewidth",2;"nan policy","skip"})," ")'

        # ── Layout data ──────────────────────────────────────────
        # 14 kolom: A-N (index 0-13)
        # Col layout:
        #   A(0)=spacer  B(1-2)=KPI label  C(3-4)=KPI value  ...repeated x6
        #   Full width cols for section headers, tables

        # Pakai layout sederhana: 9 kolom (A-I)
        # A: label all-time   B: value all-time   C: spacer
        # D: label week       E: value week       F: spacer
        # G-I: equity / extra

        NC = 9  # number of columns used

        data = [
            # Row 1: MAIN TITLE (A1:I1)
            ["🤖  CRYPTO TRADE JOURNAL  v13", "", "", "", "", "", "", "", ""],
            # Row 2: subtitle
            ["Semua angka otomatis update dari bot Telegram", "", "", "", "", "", "", "", ""],
            # Row 3: spacer
            [""] * NC,
            # Row 4: KPI LABELS row
            ["💰 SALDO SEKARANG", "", "🏦 MODAL AWAL", "", "📈 TOTAL P&L", "", "🎯 WIN RATE", "", ""],
            # Row 5: KPI VALUES row
            [F_SALDO, "", F_INIT_BAL, "", F_PNL, "", F_WR, "", ""],
            # Row 6: KPI labels row 2
            ["⚡ PROFIT FACTOR", "", "🏆 BEST TRADE", "", "💔 WORST TRADE", "", "📊 TOTAL TRADES", "", ""],
            # Row 7: KPI values row 2
            [F_PF, "", F_BEST, "", F_WORST, "", F_TOTAL, "", ""],
            # Row 8: spacer
            [""] * NC,
            # Row 9: section headers
            ["📊  ALL TIME", "", "", "📅  MINGGU INI  (7 HARI)", "", "", "📈  EQUITY CURVE", "", ""],
            # Row 10-18: two-column stats + sparkline spanning
            ["Total Trade",    F_TOTAL,   "", "Total Trade",    F_TW,    "", F_SPARK, "", ""],
            ["Win",            F_WIN,     "", "Win",            F_WW,    "", "",       "", ""],
            ["Loss",           F_LOSS,    "", "Loss",           F_LW,    "", "",       "", ""],
            ["Win Rate",       F_WR,      "", "Win Rate",       F_WRW,   "", "",       "", ""],
            ["Total P&L",      F_PNL,     "", "Total P&L",      F_PNLW,  "", "",       "", ""],
            ["Avg Win",        F_AVG_WIN, "", "Best Trade",     F_BW,    "", "",       "", ""],
            ["Avg Loss",       F_AVG_LOSS,"", "Worst Trade",    F_WW2,   "", "",       "", ""],
            ["Best Trade",     F_BEST,    "", "Long vs Short",  F_LSW,   "", "",       "", ""],
            ["Worst Trade",    F_WORST,   "", "",               "",      "", "",       "", ""],
            # Row 19: spacer
            [""] * NC,
            # Row 20: per coin header
            ["🪙  PER COIN BREAKDOWN", "", "", "", "", "", "", "", ""],
            # Row 21: table headers
            ["Coin", "Trades", "Win", "Loss", "Win Rate %", "Total PnL (USDT)", "Avg PnL", "Best Trade", "Worst Trade"],
            # Row 22: QUERY (spans from A22)
            [F_QUERY, "", "", "", "", "", "", "", ""],
        ]

        ws.update("A1", data, value_input_option="USER_ENTERED")

        # ── batchUpdate requests ─────────────────────────────────
        reqs = []

        # Merge cells for title, subtitle, section headers, KPI boxes
        reqs += [
            _merge(sid, 0, 1, 0, NC),   # R1: title full width
            _merge(sid, 1, 2, 0, NC),   # R2: subtitle
            _merge(sid, 3, 4, 0, 2),    # KPI label: Saldo
            _merge(sid, 3, 4, 2, 4),    # KPI label: PnL
            _merge(sid, 3, 4, 4, 6),    # KPI label: Win Rate
            _merge(sid, 3, 4, 6, 9),    # KPI label: Profit Factor
            _merge(sid, 4, 5, 0, 2),    # KPI value: Saldo
            _merge(sid, 4, 5, 2, 4),    # KPI value: PnL
            _merge(sid, 4, 5, 4, 6),    # KPI value: Win Rate
            _merge(sid, 4, 5, 6, 9),    # KPI value: Profit Factor
            _merge(sid, 5, 6, 0, 2),    # KPI2 label: Best
            _merge(sid, 5, 6, 2, 4),    # KPI2 label: Worst
            _merge(sid, 5, 6, 4, 6),    # KPI2 label: L/S
            _merge(sid, 5, 6, 6, 9),    # KPI2 label: Total
            _merge(sid, 6, 7, 0, 2),    # KPI2 val: Best
            _merge(sid, 6, 7, 2, 4),    # KPI2 val: Worst
            _merge(sid, 6, 7, 4, 6),    # KPI2 val: L/S
            _merge(sid, 6, 7, 6, 9),    # KPI2 val: Total
            _merge(sid, 8, 9, 0, 3),    # Section: All Time
            _merge(sid, 8, 9, 3, 6),    # Section: Week
            _merge(sid, 8, 9, 6, 9),    # Section: Equity header
            _merge(sid, 9, 18, 6, 9),   # Sparkline big cell
            _merge(sid, 19, 20, 0, NC), # spacer
            _merge(sid, 20, 21, 0, NC), # Per coin header full width
        ]

        # Row heights
        reqs += [
            _row_h(sid, 0, 1, 52),   # title
            _row_h(sid, 1, 2, 28),   # subtitle
            _row_h(sid, 2, 3, 12),   # spacer
            _row_h(sid, 3, 4, 22),   # KPI label
            _row_h(sid, 4, 5, 44),   # KPI value big
            _row_h(sid, 5, 6, 22),   # KPI2 label
            _row_h(sid, 6, 7, 44),   # KPI2 value big
            _row_h(sid, 7, 8, 16),   # spacer
            _row_h(sid, 8, 9, 30),   # section header
            _row_h(sid, 9, 18, 26),  # stats rows
            _row_h(sid, 18, 19, 12), # spacer
            _row_h(sid, 20, 21, 32), # per coin header
            _row_h(sid, 21, 22, 26), # table header
        ]

        # Column widths
        reqs += [
            _col_w(sid, 0, 1, 170),  # A: all-time label
            _col_w(sid, 1, 2, 110),  # B: all-time value
            _col_w(sid, 2, 3, 24),   # C: spacer
            _col_w(sid, 3, 4, 170),  # D: week label
            _col_w(sid, 4, 5, 110),  # E: week value
            _col_w(sid, 5, 6, 24),   # F: spacer
            _col_w(sid, 6, 7, 100),  # G: equity / coin
            _col_w(sid, 7, 8, 100),  # H
            _col_w(sid, 8, 9, 100),  # I
        ]

        # Freeze no rows (dashboard tidak perlu freeze)
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 0}},
            "fields": "gridProperties.frozenRowCount",
        }})

        # ── Formatting ───────────────────────────────────────────

        # R1: Main title
        reqs.append(_cell_fmt(sid, 0, 1, 0, NC, {
            **cell(bg=C_BG_HEADER),
            **txt(bold=True, size=18, fg=C_ACCENT),
            **ALIGN_C,
        }))

        # R2: subtitle
        reqs.append(_cell_fmt(sid, 1, 2, 0, NC, {
            **cell(bg=C_BG_HEADER),
            **txt(bold=False, size=9, fg=C_MUTED, italic=True),
            **ALIGN_C,
        }))

        # R3 spacer
        reqs.append(_cell_fmt(sid, 2, 3, 0, NC, cell(bg=C_BG_DARK)))

        # KPI label rows (R4, R6)
        for r in [3, 5]:
            reqs.append(_cell_fmt(sid, r, r+1, 0, NC, {
                **cell(bg=C_BG_KPI),
                **txt(bold=True, size=9, fg=C_MUTED),
                **ALIGN_C,
            }))

        # KPI value rows (R5, R7) — big font, centered
        for r in [4, 6]:
            reqs.append(_cell_fmt(sid, r, r+1, 0, NC, {
                **cell(bg=C_BG_KPI),
                **txt(bold=True, size=22, fg=C_WHITE),
                **ALIGN_C,
            }))

        # Borders around KPI boxes
        for c1, c2 in [(0,2),(2,4),(4,6),(6,9)]:
            reqs.append(_border(sid, 3, 7, c1, c2, color={"red":0.1,"green":0.15,"blue":0.3}))

        # R8 spacer
        reqs.append(_cell_fmt(sid, 7, 8, 0, NC, cell(bg=C_BG_DARK)))

        # R9: section headers
        for c1, c2, accent in [(0,3,C_ACCENT),(3,6,C_YELLOW),(6,9,C_GREEN)]:
            reqs.append(_cell_fmt(sid, 8, 9, c1, c2, {
                **cell(bg=C_BG_SECTION),
                **txt(bold=True, size=11, fg=accent),
                **ALIGN_C,
            }))

        # Stats rows R10-R18: alternating, label bold left, value right
        for i in range(9):
            row = 9 + i
            bg  = C_BG_ROW_ALT if i % 2 == 0 else C_BG_DARK
            # label cols A & D
            reqs.append(_cell_fmt(sid, row, row+1, 0, 1, {**cell(bg=bg), **txt(bold=True, size=10, fg=C_MUTED), **ALIGN_L}))
            reqs.append(_cell_fmt(sid, row, row+1, 3, 4, {**cell(bg=bg), **txt(bold=True, size=10, fg=C_MUTED), **ALIGN_L}))
            # value cols B & E
            reqs.append(_cell_fmt(sid, row, row+1, 1, 2, {**cell(bg=bg), **txt(bold=True, size=11, fg=C_WHITE), **ALIGN_R}))
            reqs.append(_cell_fmt(sid, row, row+1, 4, 5, {**cell(bg=bg), **txt(bold=True, size=11, fg=C_WHITE), **ALIGN_R}))
            # spacer cols
            reqs.append(_cell_fmt(sid, row, row+1, 2, 3, cell(bg=bg)))
            reqs.append(_cell_fmt(sid, row, row+1, 5, 6, cell(bg=bg)))

        # Sparkline merged cell
        reqs.append(_cell_fmt(sid, 9, 18, 6, 9, {**cell(bg=_rgb(10,22,38)), **ALIGN_C}))

        # R20 spacer
        reqs.append(_cell_fmt(sid, 19, 20, 0, NC, cell(bg=C_BG_DARK)))

        # R21: Per coin section header
        reqs.append(_cell_fmt(sid, 20, 21, 0, NC, {
            **cell(bg=C_BG_SECTION),
            **txt(bold=True, size=12, fg=C_YELLOW),
            **ALIGN_L,
        }))

        # R22: Table header
        reqs.append(_cell_fmt(sid, 21, 22, 0, NC, {
            **cell(bg=C_BG_TH),
            **txt(bold=True, size=10, fg=C_ACCENT),
            **ALIGN_C,
        }))

        # R23+: QUERY result rows (alternating)
        reqs.append(_cell_fmt(sid, 22, 60, 0, NC, {
            **cell(bg=C_BG_DARK),
            **txt(size=10, fg=C_WHITE),
            **ALIGN_C,
        }))

        spreadsheet.batch_update({"requests": reqs})

        # ── Conditional formatting (separate batch) ──────────────
        cf_reqs = [
            # PnL value (col B = index 1): positive green, negative red
            _cf_text(sid, 4, 5, 0, 2, "NUMBER_GREATER", "0", C_GREEN),
            _cf_text(sid, 4, 5, 0, 2, "NUMBER_LESS",    "0", C_RED),
            _cf_text(sid, 6, 7, 0, 2, "NUMBER_GREATER", "0", C_GREEN),
            _cf_text(sid, 6, 7, 0, 2, "NUMBER_LESS",    "0", C_RED),
            # Stats value col B (all-time)
            _cf_text(sid, 9, 18, 1, 2, "NUMBER_GREATER", "0", C_GREEN),
            _cf_text(sid, 9, 18, 1, 2, "NUMBER_LESS",    "0", C_RED),
            # Stats value col E (week)
            _cf_text(sid, 9, 18, 4, 5, "NUMBER_GREATER", "0", C_GREEN),
            _cf_text(sid, 9, 18, 4, 5, "NUMBER_LESS",    "0", C_RED),
            # Per-coin total P&L col F (index 5): positive=green bg, negative=red bg
            _cf_bg(sid, 22, 60, 5, 6, "NUMBER_GREATER", "0", _rgb(0, 60, 30)),
            _cf_bg(sid, 22, 60, 5, 6, "NUMBER_LESS",    "0", _rgb(60, 15, 15)),
        ]
        spreadsheet.batch_update({"requests": cf_reqs})

        # ── Format Trades sheet juga ────────────────────────────
        _format_trades_sheet(spreadsheet)
        # ── Format Balance sheet ────────────────────────────────
        _format_balance_sheet(spreadsheet)

        log.info("Dashboard berhasil diperbarui")
        return True

    except Exception as e:
        log.error(f"Setup dashboard error: {e}")
        return False


def _format_trades_sheet(spreadsheet):
    """Format sheet Trades: header bold, conditional formatting P&L & Result, col widths."""
    try:
        ws  = spreadsheet.worksheet("Trades")
        sid = ws.id

        reqs = [
            # Header row (R1) — dark navy, cyan bold text
            _cell_fmt(sid, 0, 1, 0, 12, {
                **cell(bg=_rgb(15, 25, 55)),
                **txt(bold=True, size=10, fg=_rgb(0, 180, 255)),
                **{"horizontalAlignment": "CENTER"},
            }),
            # Freeze header
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
            # Column widths: Timestamp(A), Coin(B), Direction(C), Entry(D),
            #   Margin(E), Leverage(F), PositionSize(G), PnL_USDT(H), PnL%(I), Result(J), Note(K), ImageURL(L)
            _col_w(sid, 0,  1, 160),  # Timestamp
            _col_w(sid, 1,  2,  80),  # Coin
            _col_w(sid, 2,  3,  80),  # Direction
            _col_w(sid, 3,  4,  90),  # Entry Price
            _col_w(sid, 4,  5,  90),  # Margin
            _col_w(sid, 5,  6,  70),  # Leverage
            _col_w(sid, 6,  7, 110),  # Position Size
            _col_w(sid, 7,  8, 100),  # PnL USDT
            _col_w(sid, 8,  9,  80),  # PnL %
            _col_w(sid, 9, 10,  80),  # Result
            _col_w(sid, 10,11, 200),  # Note
        ]
        spreadsheet.batch_update({"requests": reqs})

        # Conditional formatting
        GREEN = _rgb(0, 200, 100)
        RED   = _rgb(255, 70, 70)
        cf = [
            # PnL (USDT) col H (index 7): green if >0, red if <0
            _cf_text(sid, 1, 5000, 7, 8, "NUMBER_GREATER", "0", GREEN),
            _cf_text(sid, 1, 5000, 7, 8, "NUMBER_LESS",    "0", RED),
            # PnL % col I (index 8)
            _cf_text(sid, 1, 5000, 8, 9, "NUMBER_GREATER", "0", GREEN),
            _cf_text(sid, 1, 5000, 8, 9, "NUMBER_LESS",    "0", RED),
            # Result col J (index 9): WIN=green bg, LOSS=red bg
            _cf_bg(sid, 1, 5000, 9, 10, "TEXT_EQ", "WIN",  _rgb(0, 50, 25)),
            _cf_bg(sid, 1, 5000, 9, 10, "TEXT_EQ", "LOSS", _rgb(55, 15, 15)),
            # Direction col C (index 2): LONG=cyan text, SHORT=red text
            _cf_text(sid, 1, 5000, 2, 3, "TEXT_EQ", "LONG",  _rgb(0, 180, 255)),
            _cf_text(sid, 1, 5000, 2, 3, "TEXT_EQ", "SHORT", RED),
        ]
        spreadsheet.batch_update({"requests": cf})
        log.info("Trades sheet formatted")
    except Exception as e:
        log.warning(f"Format Trades error: {e}")


def _format_balance_sheet(spreadsheet):
    """Format sheet Balance: header, col widths, conditional P&L."""
    try:
        ws  = spreadsheet.worksheet("Balance")
        sid = ws.id
        reqs = [
            _cell_fmt(sid, 0, 1, 0, 5, {
                **cell(bg=_rgb(15, 25, 55)),
                **txt(bold=True, size=10, fg=_rgb(0, 180, 255)),
                **{"horizontalAlignment": "CENTER"},
            }),
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
            _col_w(sid, 0, 1, 160),  # Timestamp
            _col_w(sid, 1, 2, 110),  # Event
            _col_w(sid, 2, 3, 110),  # Amount
            _col_w(sid, 3, 4, 150),  # Balance After
            _col_w(sid, 4, 5, 200),  # Note
        ]
        spreadsheet.batch_update({"requests": reqs})

        cf = [
            _cf_text(sid, 1, 5000, 2, 3, "NUMBER_GREATER", "0", _rgb(0, 200, 100)),
            _cf_text(sid, 1, 5000, 2, 3, "NUMBER_LESS",    "0", _rgb(255, 70, 70)),
            _cf_bg(sid, 1, 5000, 1, 2, "TEXT_EQ", "PROFIT", _rgb(0, 50, 25)),
            _cf_bg(sid, 1, 5000, 1, 2, "TEXT_EQ", "LOSS",   _rgb(55, 15, 15)),
        ]
        spreadsheet.batch_update({"requests": cf})
        log.info("Balance sheet formatted")
    except Exception as e:
        log.warning(f"Format Balance error: {e}")



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
    """
    Saldo sekarang. SOURCE OF TRUTH = Google Sheet (tahan restart/redeploy),
    karena state lokal hilang tiap container Railway di-reclaim.
    Saldo = Σ(INITIAL BALANCE) + Σ(semua PnL di Trades) — sama dgn formula dashboard.
    Fallback ke state lokal kalau sheet tidak tersedia.
    """
    sheet_bal = _compute_balance_from_sheet()
    if sheet_bal is not None:
        # Self-heal state lokal biar konsisten dgn sheet
        s = _load_state()
        if s.get("current_balance") != sheet_bal:
            s["current_balance"] = sheet_bal
            _save_state(s)
        return sheet_bal
    return _load_state().get("current_balance", 0.0)


def _compute_balance_from_sheet(sheet=None):
    """
    Hitung saldo dari sheet: Σ(Balance After utk row INITIAL BALANCE) + Σ(PnL Trades).
    Return float, atau None kalau sheet tidak bisa dibaca.
    """
    sheet = sheet or _get_sheet()
    if not sheet:
        return None
    try:
        brows = sheet.worksheet(SHEET_BALANCE).get_all_records()
    except Exception as e:
        log.warning(f"compute balance: baca Balance sheet gagal: {e}")
        return None
    init = 0.0
    for r in brows:
        if r.get("Event") == "INITIAL BALANCE":
            try:
                init += float(r.get("Balance After (USDT)", 0) or 0)
            except (TypeError, ValueError):
                pass
    pnl_sum = 0.0
    try:
        for r in sheet.worksheet(SHEET_TRADES).get_all_records():
            try:
                pnl_sum += float(r.get("PnL (USDT)", 0) or 0)
            except (TypeError, ValueError):
                pass
    except Exception:
        pass  # belum ada sheet Trades / kosong
    return round(init + pnl_sum, 2)

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

def _update_balance(pnl: float, note: str = "", _spreadsheet=None):
    sheet = _spreadsheet or _get_sheet()
    # Saldo baru dari sheet (sudah termasuk trade yg baru di-append di log_trade),
    # bukan dari state lokal yg bisa reset ke 0 setelah redeploy.
    new_bal = _compute_balance_from_sheet(sheet)
    if new_bal is None:
        new_bal = round(_load_state().get("current_balance", 0.0) + pnl, 2)
    s = _load_state()
    s["current_balance"] = new_bal
    s["total_trades"]    = s.get("total_trades", 0) + 1
    _save_state(s)
    if sheet:
        ws = _ws(sheet, SHEET_BALANCE, BALANCE_HEADERS)
        ws.append_row([_now(), "PROFIT" if pnl >= 0 else "LOSS", round(pnl, 2), new_bal, note], value_input_option="RAW")
    try:
        push_balance("PROFIT" if pnl >= 0 else "LOSS", round(pnl, 2), new_bal, note)
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
    _update_balance(pnl_usdt, f"{coin} {direction} {result}", _spreadsheet=sheet)
    result_dict = {"ts": ts, "coin": coin.upper().replace("USDT",""), "direction": direction.upper(),
                   "entry": entry_price, "margin": margin_usdt, "leverage": leverage,
                   "position_size": pos_size, "pnl_usdt": round(pnl_usdt, 2), "pnl_pct": pnl_pct,
                   "result": result, "note": note, "sheets_ok": sheets_ok, "balance_after": get_current_balance()}
    # Sync ke Supabase (non-blocking, tidak ganggu flow utama)
    try:
        push_trade(result_dict)
    except Exception as e:
        log.warning(f"supabase push_trade error: {e}")
    # Auto-lesson ke learning engine tiap trade (best-effort, jangan ganggu flow)
    try:
        import learning_engine
        learning_engine.record_trade_journal_lesson(result_dict)
    except Exception as e:
        log.warning(f"auto-lesson dari trade gagal: {e}")
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


# ── Screenshot import (vision AI → trade) ───────────────────────────

def _num(v):
    """Parse angka dari string/number, buang koma/persen/USDT/spasi."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    s = s.replace("USDT", "").replace("usdt", "").strip()
    # ambil token angka pertama (boleh minus / desimal)
    import re as _re
    m = _re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def build_trade_from_screenshot(raw: dict) -> tuple:
    """
    Ubah field mentah hasil baca AI vision dari screenshot order-details
    jadi trade dict siap-preview untuk log_trade().

    raw diharapkan punya (sebagian boleh kosong):
      coin, direction, leverage, entry_price, exit_price,
      realized_pnl, roi_pct

    Margin dihitung dari |pnl / roi| kalau tidak diberikan langsung.
    Return (trade_dict, error_str). Kalau error_str != "" → gagal.
    """
    if not isinstance(raw, dict):
        return None, "Data screenshot tidak terbaca."

    coin = str(raw.get("coin", "")).upper().replace("USDT", "").replace("/", "").strip()
    if not coin:
        return None, "Coin tidak terbaca dari screenshot."

    d = str(raw.get("direction", "")).upper().strip()
    if d in ("LONG", "L", "BUY", "CLOSE LONG"):
        direction = "LONG"
    elif d in ("SHORT", "S", "SELL", "CLOSE SHORT"):
        direction = "SHORT"
    else:
        return None, f"Arah trade tidak jelas (terbaca: '{raw.get('direction','')}')."

    entry = _num(raw.get("entry_price"))
    if entry is None or entry <= 0:
        return None, "Entry price tidak terbaca."

    pnl = _num(raw.get("realized_pnl"))
    if pnl is None:
        return None, "Realized PnL tidak terbaca."

    leverage = _num(raw.get("leverage"))
    leverage = int(leverage) if leverage and leverage >= 1 else 1

    roi = _num(raw.get("roi_pct"))
    margin = _num(raw.get("margin"))
    if (margin is None or margin <= 0) and roi not in (None, 0):
        margin = abs(pnl / (roi / 100.0))
    if margin is None or margin <= 0:
        return None, "Margin tidak bisa dihitung (ROI/margin tidak terbaca)."
    margin = round(margin, 2)

    exit_price = _num(raw.get("exit_price"))
    note_bits = ["dari screenshot"]
    if exit_price:
        note_bits.append(f"exit {exit_price:g}")
    if roi is not None:
        note_bits.append(f"ROI {roi:+.2f}%")
    note = " | ".join(note_bits)

    return {
        "coin": coin, "direction": direction, "entry": entry,
        "margin": margin, "leverage": leverage, "pnl": round(pnl, 4),
        "roi": roi, "exit": exit_price, "note": note,
    }, ""


def format_shot_preview(t: dict) -> str:
    """Preview hasil baca screenshot sebelum user konfirmasi simpan."""
    ed = "🟢" if t["direction"] == "LONG" else "🔴"
    s  = "+" if t["pnl"] >= 0 else ""
    roi_line = f"  📊 ROI       : <b>{t['roi']:+.2f}%</b>\n" if t.get("roi") is not None else ""
    exit_line = f"  🚪 Exit      : ${t['exit']:g}\n" if t.get("exit") else ""
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📸 <b>BACA SCREENSHOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"  🪙 Coin      : <b>{t['coin']}USDT</b>\n"
        f"  {ed} Arah     : <b>{t['direction']}</b>\n"
        f"  💵 Entry     : ${t['entry']:g}\n"
        f"{exit_line}"
        f"  ⚡ Leverage  : {t['leverage']}x\n"
        f"  💰 Margin    : ${t['margin']:,.2f} USDT  <i>(auto)</i>\n"
        f"  {'📈' if t['pnl'] >= 0 else '📉'} PnL      : <b>{s}{t['pnl']:.2f} USDT</b>\n"
        f"{roi_line}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Ketik <b>ya</b> untuk simpan  •  ❌ <b>batal</b> untuk batal"
    )
