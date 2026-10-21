# Repair and Review Report

## Changes made

1. **Incomplete scans now fail visibly.** Previously, if the Twelve Data daily quota was exhausted after one or more batches had succeeded, the scan could return exit code `0` even though some symbols were never requested. `RunReport.exit_code` now returns `1` when `deferred` symbols remain. This prevents GitHub Actions from presenting a partial scan as a successful complete scan.
2. **Regression test updated.** The daily-quota test now verifies that an incomplete scan exits non-zero.
3. **README updated.** The API usage section explains that quota/budget exhaustion makes the workflow visibly incomplete.

## Verification performed in this environment

- Python test suite: **157 passed, 0 failed**.
- Python compile check: passed.
- The project uses mocked Twelve Data and Telegram responses in automated tests; no live API key or Telegram credentials were available in this review environment.

## Important checks still required after upload to GitHub

1. Add the three GitHub Actions secrets: `TWELVE_DATA_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`.
2. Run workflow mode `diagnose`; confirm all 28 currency pairs return `OK` and none are missing from your Twelve Data plan.
3. Run `dry-run`; inspect the Actions log and summary for failed symbols or stale candles.
4. Run `telegram-test`; confirm the test message reaches your Telegram chat.
5. Run `scan` once to establish the baseline. Then verify subsequent hourly runs.
6. Compare at least one symbol with the original TradingView indicator using the same candles and settings. Exact matching is not guaranteed because Twelve Data candles may differ from your TradingView broker feed.

## Known limitations / risks

- A successful mock-based test suite does **not** prove that the live API plan supports all 28 pairs or 5,000 hourly candles for each pair.
- Telegram delivery can still duplicate a message in the narrow case where Telegram accepts it but the runner crashes before saving the sent status.
- Signals are generated from the Python port of the supplied Pine strategy. A live TradingView parity check has not been performed.
- API credit usage recorded by the scanner is an estimate of its own calls; the Twelve Data dashboard is authoritative.
- This is a signal scanner, not a guarantee of profitable trades. Test signals and risk controls before relying on them.
