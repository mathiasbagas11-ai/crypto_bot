# Tests

Unit tests for `crypto_bot`. Run from the repo root:

```bash
pip install -r requirements-dev.txt
pytest                  # run everything
pytest --cov=. --cov-report=term-missing   # with coverage
```

CI runs the same suite on every push to `main` and on pull requests
(`.github/workflows/tests.yml`).

## Coverage map

The suite targets the high-value, pure-logic units across three tiers.

### Tier 1 — money, stats & security
| Module | File | What's covered |
|--------|------|----------------|
| `risk_manager.py` | `test_risk_manager.py` | position sizing, 30% cap, leverage, personal trade plan, daily loss-limit halt, risk summary |
| `backtest_engine.py` | `test_backtest_stats.py` | `LocalTrade.close()` P&L (long/short, fees), `_calc_stats()` win rate / profit factor / drawdown / expectancy |
| `security.py` | `test_security.py` | whitelist build/membership, Fernet key derivation, encrypt→decrypt round-trip, plaintext backward-compat & migration |

### Tier 2 — signal correctness & indicators
| Module | File | What's covered |
|--------|------|----------------|
| `auto_validator.py` | `test_indicators.py` | `_ema`, `_rsi`, `_trend_from_candles`, `_choch_bos` |
| `confirmed_signal.py` | `test_master_score.py` | `compute_master_score` weighting / conflict / confidence, `_persistence_adjustment` |
| `crypto_screening_bot_v13.py` | `test_detectors.py` | `detect_market_structure`, `detect_fvg`, `calculate_quality_score` |

### Tier 3 — state, parsing & learning
| Module | File | What's covered |
|--------|------|----------------|
| `symbol_memory.py` | `test_symbol_memory.py` | `_compute_stats`, `_check_blacklist`, `is_blacklisted` |
| `feedback_engine.py` | `test_feedback_parser.py` | `_parse_feedback_rule_based` outcome/direction/condition extraction |
| `exchange_resolver.py` | `test_exchange_resolver.py` | `resolve_symbol` normalization + fallback, `_klines_*` parsing/normalization |
| `signal_tracker.py` | `test_signal_tracker.py` | `check_pending_signals` TP/SL/timeout, conservative SL-first resolution |

### Feature — signal discussion & trading-style learning (v15)
| Module | File | What's covered |
|--------|------|----------------|
| `signal_chat.py` | `test_signal_chat.py` | component classification & `explain_signal`, prompt building, `[STYLE:]` marker parsing, signal↔message mapping, style store add/dedup/remove, discussion-reply orchestration, confirm-to-save flow, **style engine** (parse rules → prefs, deterministic R:R/score/indicator/entry adjustments, personalization block) |

## Conventions

- Tests are offline: network (`requests`, exchange/price fetches) and state
  files are monkeypatched, so the suite runs in well under a second.
- Where exact expected values were hard to hand-derive (e.g. the market
  structure zig-zag), they were captured from the implementation on a clearly
  shaped input and locked in as a regression guard.

## Bugs surfaced & fixed by the suite

- `exchange_resolver.resolve_symbol` mis-normalized `*PERP` input
  (`"XRPPERP"` → `"XRPUSDTT"`, which never resolved). The chained `.replace()`
  was replaced with explicit suffix stripping; covered by
  `test_exchange_resolver.py::test_resolve_symbol_normalization`.
