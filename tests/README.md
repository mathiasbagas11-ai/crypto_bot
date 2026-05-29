# Tests

Unit tests for `crypto_bot`. Run from the repo root:

```bash
pip install -r requirements-dev.txt
pytest                  # run everything
pytest --cov=. --cov-report=term-missing   # with coverage
```

## Coverage status

This is the **Tier 1** test suite — the highest-risk, pure-logic units:

| Area | Module | What's covered |
|------|--------|----------------|
| Money math | `risk_manager.py` | position sizing, 30% cap, leverage, personal trade plan, daily loss-limit halt, risk summary |
| Backtest stats | `backtest_engine.py` | `LocalTrade.close()` P&L (long/short, fees), `_calc_stats()` win rate / profit factor / drawdown / expectancy |
| Auth & crypto | `security.py` | whitelist build/membership, Fernet key derivation, encrypt→decrypt round-trip, plaintext backward-compat & migration |

These functions are pure (no live network) and are exercised by monkeypatching
state I/O, so the suite runs fast and offline.

Later tiers (signal scoring, indicator primitives, exchange parsing, state
trackers) are not yet covered — see the analysis in the PR/issue thread.
