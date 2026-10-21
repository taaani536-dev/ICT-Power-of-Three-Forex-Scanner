# Test report (actual results)

Environment: Python 3.12.3, pytest 9.1.1, requests 2.33.1, PyYAML 6.0.3 (Linux sandbox, git 2.43).
Command: `python -m pytest` — **157 discovered, 157 executed, 157 passed, 0 failed, 0 skipped.**

| File | Tests | Result | What it covers |
|---|---|---|---|
| test_strategy.py | 35 | passed | Pine defaults/derived inputs, ATR (RMA/SMA seed/na prefix), highest/lowest, BUY & SELL, same-bar fall-through, delayed manipulation, Close vs Wick, SL-wins-on-same-bar, Fixed TP/SL, 4800-bar window gate, determinism, **checkpoint-resume equals single pass**, no look-ahead, dashboard maths |
| test_market_data.py | 29 | passed | batch request shape, header auth, auth error, 401, unsupported/plan/missing symbols, 429 minute vs daily, 5xx/timeouts/network, malformed/empty/invalid OHLC, duplicates/ordering, forming-candle exclusion, run & daily budget, pacing (≥ 62 s between 8-credit batches), no secrets in logs |
| test_signal_engine.py | 16 | passed | first-run baseline, 7-candle recovery, stale (>7), incremental state carry-over, split-run equivalence, re-init, status TP/SL, deterministic IDs |
| test_state_store.py | 12 | passed | atomic save, corrupt file = hard error, dedupe, status transitions, prune, API usage, merge rules |
| test_telegram_notifier.py | 11 | passed | message format (BUY/SELL, JPY decimals, delayed/resolved notes), 15 s gaps (none after last), failure isolation, 429 retry_after, fatal abort, token never leaked |
| test_scanner.py | 16 | passed | end-to-end with mocked APIs: first run 28 credits & batches 8/8/8/4, delivery order, re-run dedupe, pending retry, crash-safety (state saved before send), dry-run, auth failure, unsupported symbol, quota exhaustion, persisted daily budget, expiry, CLI exit codes 2/3 |
| test_cli.py | 6 | passed | scan --dry-run, diagnose (ok / catalogue gap), replay, symbol whitelist, merge-state |
| test_symbol_mapping.py | 24 | passed | exact 28 pairs & order, excluded instruments rejected, provider mapping, config validation |
| test_workflow_and_scripts.py | 8 | passed | workflow YAML (cron, dispatch, concurrency, permissions, secrets names, files referenced), pinned requirements, no committed secrets, **git state-branch script against a local bare repo including a simulated push race + merge** |

## Which tests used mocks
Everything touching Twelve Data or Telegram uses scripted fake HTTP sessions. The state-branch script test uses real git against a
local bare repository (no network).

## NOT tested (be aware)
* **No live Twelve Data call** (no API key available): endpoint shapes, 429 wording, large-`outputsize` batches and per-pair plan
  access are per documentation only. Use the `diagnose` workflow mode.
* **No live Telegram message** was sent.
* **GitHub Actions was not executed**; only static YAML/file-reference validation and the shell script (locally) were tested.
* **No comparison with TradingView** was performed; expected strategy values in tests were derived by hand from the Pine logic
  (e.g. ATR after one wide bar = (49·1 + TR)/50), which verifies the port's internal consistency with the source logic, not equivalence on real data.

## Unresolved issues / risks
See "Known limitations" in README.md and the limitations list in docs/SOURCE_ANALYSIS.md.
