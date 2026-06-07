# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A crypto futures **screening & signal bot** (v13/v16). It scans ~25+ liquid Binance Futures coins on a schedule, runs a layered technical analysis + signal-fusion pipeline, validates candidates through a 7-gate filter and a mini backtest, then pushes alerts to Telegram and tracks their outcomes to feed an auto-learning loop. The bot is operated entirely through Telegram commands and runs as a long-lived worker (Railway / Procfile).

Code comments, docstrings, and `WORKFLOW.md` are written in Indonesian — match that language when editing them.

## Commands

```bash
# Install (runtime + test deps)
pip install -r requirements-dev.txt

# Run the whole test suite (offline, runs in well under a second)
pytest

# With coverage (same as CI)
pytest -ra --cov=. --cov-report=term-missing

# Single file / single test
pytest tests/test_master_score.py
pytest tests/test_master_score.py::test_compute_master_score_weighting

# Run the bot (long-lived worker; needs env vars set, see below)
python crypto_screening_bot_v13.py

# Run the Streamlit dashboard
streamlit run website/streamlit_app.py
```

Python 3.11. CI (`.github/workflows/tests.yml`) runs `pytest` on every push to `main` and on all PRs.

## Architecture

The system is a **scan → analyze → detect → score → gate → backtest → send → track → learn** pipeline. `WORKFLOW.md` documents the full 12-phase flow (in Indonesian) and is the canonical reference for scoring weights, gate percentages, and thresholds — read it before touching scoring/gate logic.

**`crypto_screening_bot_v13.py`** is the monolithic engine (~8500 lines, 450KB). It owns: data fetching, all the raw technical detectors (SMC order blocks, FVG, market-structure break, RSI/MACD/MFI, volume anomaly, liquidity sweeps), the APScheduler jobs, and the Telegram command router. Most other modules are imported by it. **Every module import is wrapped in `try/except ImportError` with fallback stubs** — the bot degrades gracefully when an optional module or dependency is missing, so new modules should follow the same optional-import pattern and never be assumed present.

The signal pipeline is split across these collaborators:

- **`auto_validator.py`** — the **7-gate validation** (HTF trend, BTC macro, Coinbase premium, ecosystem season, OI/funding sanity, multi-TF confluence, liquidity/volatility). Returns `PASS` / `SOFT_BLOCK` (score penalty) / `HARD_BLOCK` (cancel). Also hosts pure indicator helpers (`_ema`, `_rsi`, `_trend_from_candles`, `_choch_bos`).
- **`confirmed_signal.py`** — `compute_master_score`: fuses confluence + the four signal types (prepump/predump/scalp/swing) into a final 0–100 score with persistence adjustment, deciding CONFIRMED (≥75) / WATCH (60–74) / SKIP.
- **`backtest_engine.py`** — `LocalTrade.close()` P&L and `_calc_stats()` (win rate, profit factor, drawdown, expectancy); runs the mini 7-day backtest gate that can block a confirmed signal.
- **`signal_tracker.py`** — monitors `pending_signals.json`, resolves TP/SL/timeout (SL-first / conservative), records outcomes.
- **`symbol_memory.py`** — per-coin win/SL stats, auto-derived lessons, and **auto-blacklist** (SL rate >75% over last 10 trades → 6h blacklist). Injected as historical context into AI prompts.
- **`learning_engine.py` / `feedback_engine.py`** — decision logging and turning trade outcomes (and user feedback) into lessons that feed back into prompts.

Supporting analysis modules: `market_regime.py`, `market_context.py` (Fear & Greed, BTC macro), `ecosystem_detector.py`, `coinbase_premium.py`, `whale_tracker.py`, `liquidation_tracker.py` (Binance WebSocket), `news_sentiment.py` / `news_agent.py` / `x_sentiment.py`, `reversal_patterns.py`. Manual trading: `trade_manager.py`, `trade_journal.py`, `risk_manager.py`. User-facing chat: `signal_chat.py` (per-signal discussion + trading-style learning).

**AI providers are pluggable**: the bot can use Anthropic/Claude, Gemini, DeepSeek, or Groq (selected via env vars). Auto-scan and the scoring/gate/backtest paths are **rule-based with no AI calls** — AI is only invoked on explicit Telegram commands (`/analyze`, `/ask`, `/chart`, `/news`, `/macro`, `/weeksummary`). Keep this separation; do not introduce AI into the automated scan path.

### Security model

`security.py` is wired through the whole bot: `is_allowed(chat_id)` enforces a Telegram chat-ID whitelist (silent drop of unauthorized users), and `secure_load` / `secure_save` transparently encrypt sensitive state with Fernet (AES) using `ENCRYPTION_KEY`. Encrypted state files use the `.enc` extension (e.g. `gate_state.json.enc`, `risk_state.json.enc`) and are gitignored. There is backward-compat migration from plaintext state. Use `secure_load`/`secure_save` (not raw `json`) for any new sensitive state file.

### State files

Runtime state lives in JSON files at the repo root: `pending_signals.json`, `signal_outcomes.json`, `confirmed_signals_history.json`, `symbol_memory.json`, `decision_log.json`, `validation_log.json`, `backtest_results.json`, `lessons.json`. Tests treat these as monkeypatched, never written for real.

## Testing conventions

- Tests are **fully offline**: network (`requests`, exchange/price fetches) and state-file I/O are monkeypatched. New tests must keep the suite network-free and fast.
- `pytest.ini` sets `pythonpath = .` so tests import top-level modules directly (no package install). `testpaths = tests`.
- For hard-to-hand-derive expected values (e.g. zig-zag market structure), values were captured from the implementation on a clearly shaped input and locked in as regression guards. `tests/README.md` has the full module→test coverage map and is worth updating when adding tests.
- `test_ai_insight.py` lives at the repo root (not under `tests/`) and is not part of the default `pytest` run.

## Environment variables

Loaded via `python-dotenv` from `.env`. Core: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ENCRYPTION_KEY`, `ALLOWED_CHAT_IDS`. AI providers: `ANTHROPIC_API_KEY`/`CLAUDE_MODEL`, `GEMINI_API_KEY`/`GEMINI_MODEL`, `DEEPSEEK_API_KEY`/`DEEPSEEK_MODEL`, `GROQ_API_KEY`/`GROQ_MODEL`. Telegram topic routing: `SIGNAL_THREAD_ID`, `MARKET_UPDATE_THREAD_ID`, `TRADE_REPORT_THREAD_ID`. Feature flags: `MARKET_PULSE_ENABLED`/`MARKET_PULSE_INTERVAL`, `REVERSAL_SCAN_ENABLED`/`REVERSAL_IGNITION`. Integrations: `SUPABASE_URL`/`SUPABASE_ANON_KEY`, `GOOGLE_CREDENTIALS_JSON`/`GOOGLE_CREDENTIALS_FILE`/`GOOGLE_SPREADSHEET_ID`, `NEWSAPI_KEY`, `TWITTER_BEARER_TOKEN`.

## Deployment

Runs as a worker process (`Procfile`: `worker: python crypto_screening_bot_v13.py`). Railway via nixpacks (`railway.toml`, Python 3.11, restart-always). The `.devcontainer` is set up to auto-launch the Streamlit dashboard on port 8501.
