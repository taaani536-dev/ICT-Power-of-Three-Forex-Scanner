# ICT Power of Three — Automated Forex Signal Scanner

Python port of the Pine Script **"ICT Power Of Three | Flux Charts" (v5)**, scanning **28 Forex pairs on the 1-hour
timeframe** with **Twelve Data (Free plan)**, sending **BUY/SELL alerts to Telegram**, running hourly on **GitHub Actions**
(no phone/computer needed). Forex only — no crypto, gold, indices or stocks.

> Status: code complete and unit/integration-tested **with mocks**. It has **not** been run against the live Twelve Data
> API, a live Telegram chat, GitHub Actions, or compared against TradingView (see `docs/TEST_REPORT.md`). Run the
> diagnostic and a dry run first (steps below). This is a signal tool, not financial advice.

## How it works

```
GitHub cron (hourly, may be late)
  └─ prepare 'scanner-state' branch ─► .state/state.json
  └─ main.py scan
       1. load state (corrupt file ⇒ hard stop, exit 3)
       2. for the 28 pairs in batches 8/8/8/4: Twelve Data /time_series (1h, UTC), paced ≤ 8 credits/min
       3. validate candles, drop the still-forming candle (open+1h+30s must be ≤ now)
       4. advance each pair's Pine state machine over every NEW closed candle after its checkpoint
       5. new signals ⇒ ledger as "pending" ; checkpoints + ledger saved atomically BEFORE sending
       6. send pending signals chronologically, 15 s apart; save state after each message
  └─ always: commit/push state (merge on conflict)
```

* **Strategy** — `src/strategy.py` (details and mapping table: `docs/SOURCE_ANALYSIS.md`). Entry/SL/TP in the message are the
  ones the original script computes (entry = signal-candle close, Dynamic ATR SL/TP, RR 0.86). Nothing is invented.
* **Previous-seven-candle recovery** — the strategy is a path-dependent state machine, so it is never "re-run on the last
  bar". Each pair has a persisted checkpoint (`last_processed_ts` + the serialized `lastPO3` object); every run processes all
  closed candles after it in order. A signal found within the last `LOOKBACK_CANDLES` (7) closed candles and not already in the
  ledger is sent (with a "Delayed" note); older ones are recorded as `expired` and never sent. History is requested with
  `outputsize` 1000 (5000 on initialisation) because ATR/range calculations need it; credits are per symbol, not per candle.
* **First run policy** — replays the last 4800 closed candles (the Pine window) to establish the state, **sends no history**;
  only a signal on the latest closed candle is sent (`FIRST_RUN_SEND_LOOKBACK=1`, set `0` for silence).
* **Duplicate protection** — deterministic ID `po3flux-v5|SYMBOL|1H|<candle-open UTC>|BUY/SELL` in a persistent ledger +
  checkpoints. Failed deliveries stay `pending` (retried next run, expire after 24 h / 24 attempts); a message is marked `sent`
  only after Telegram returns `ok`. **Limitation:** if the process dies after Telegram accepted a message but before state is
  saved/pushed, that one message may be re-sent (no simple design gives exactly-once delivery).
* **Persistence choice** — a JSON file on a dedicated git branch `scanner-state` (written through a git worktree). Compared
  with `actions/cache` (can be evicted; entries immutable) and artifacts (need API calls to fetch), it is the simplest durable
  option with history/auditability. A `concurrency` group serialises runs; if a push is still rejected the script fetches the
  remote file, merges (`main.py merge-state`: union of ledger, "sent" beats "pending", newest checkpoint per pair) and retries.
  Trade-off: one small commit per hour on that branch.

## Repository layout
```
.github/workflows/forex_scanner.yml   hourly + manual workflow
scripts/state_branch.sh               prepare / persist the state branch (with merge-on-conflict)
src/config.py symbol_mapping.py market_data.py strategy.py signal_engine.py
    state_store.py telegram_notifier.py scanner.py logging_config.py
main.py                               CLI (scan, diagnose, replay, telegram-test, validate-config, merge-state)
tests/                                pytest suite (all HTTP/Telegram mocked)
docs/SOURCE_ANALYSIS.md  docs/TEST_REPORT.md
requirements.txt requirements-dev.txt .env.example .gitignore pytest.ini
```

## Setup (GitHub)
1. **Create a new private repository** and upload the contents of this folder (keep the folder structure).
2. **Revoke any Telegram bot token that was ever pasted anywhere public** (BotFather → `/revoke`) and use the new one.
3. *Settings → Secrets and variables → Actions → New repository secret*: `TWELVE_DATA_API_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`. (Get the chat id by messaging your bot, then opening `getUpdates` for the bot in a browser.)
4. *Settings → Actions → General → Workflow permissions* → **Read and write permissions** (needed to push the state branch).
5. *Actions* tab → enable workflows if prompted.
6. **Diagnostic (≈29 credits, no Telegram):** Actions → *Forex Scanner* → *Run workflow* → mode `diagnose`. Every pair must
   show `OK` and the catalogue list must report none missing. Any symbol not available on your plan is reported (never substituted).
7. **Dry run:** mode `dry-run` → fetches + evaluates, sends nothing, does not change checkpoints/signals.
8. **Telegram check:** mode `telegram-test` sends one test message to your chat.
9. **Go live:** mode `scan` once (establishes the baseline). From then on the hourly schedule (`7 * * * *`, UTC) runs by itself.

### Monitoring API usage
* Each run's **Job Summary** shows credits used in the run and today (`x/800`); logs show the `api-credits-used/left` headers.
* The ledger of today's own usage is `api_usage` in `state.json` (branch `scanner-state`). Your Twelve Data dashboard is authoritative.
* Budget: 28 pairs × 24 runs = **672 credits/day** (1 credit per symbol, batch or not). The scanner stops at `800 − 40 = 760`,
  i.e. about three extra full manual runs per day; `diagnose` costs 29, `replay` 1.

### Disabling safely
*Actions → Forex Scanner → ⋯ → Disable workflow* (or delete the `schedule:` block). State is kept, so re-enabling resumes
cleanly (missed candles beyond the 7-candle window are skipped, not back-filled). Note: GitHub can disable scheduled
workflows on inactive public repositories after ~60 days, and scheduled runs can start late or be skipped — check *Actions*
if alerts stop.

## Local use
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest                      # all tests, no credentials needed
export TWELVE_DATA_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python main.py diagnose               # live; consumes ~29 credits
python main.py scan --dry-run
python main.py replay EURUSD          # 1 credit; prints dashboard stats + every trade
```
Exit codes: `0` ok (warnings are annotated), `1` unrecoverable (bad API key, all symbols failed, Telegram auth failure),
`2` configuration/secrets, `3` corrupt state file.

## Configuration (environment variables, all optional except the three secrets)
`LOOKBACK_CANDLES` (7) · `FIRST_RUN_SEND_LOOKBACK` (1) · `BATCH_SIZE` (8) · `DAILY_SAFETY_MARGIN` (40) ·
`MAX_CREDITS_PER_RUN` (40) · `INCREMENTAL_OUTPUTSIZE` (1000) · `INIT_OUTPUTSIZE` (5000) · `TELEGRAM_INTERVAL_SECONDS` (15) ·
`STATE_PATH` · `LOG_LEVEL` · strategy inputs `PO3_ALGORITHM_MODE`, `PO3_BREAKOUT_METHOD`, `PO3_TPSL_METHOD`, `PO3_RISK`,
`PO3_TP_PERCENT`, `PO3_SL_PERCENT` (defaults = Pine defaults; **change them only knowingly**).

## Validating against TradingView (manual — no automated comparison exists)
1. TradingView: same pair, **1h**, indicator defaults (Small Manipulation, Wick, Dynamic, Normal), same data feed if possible.
2. `python main.py replay EURUSD` and compare the printed **Total Entries / Wins / Losses / Winrate / Average / Total Profit**
   (ported from the indicator's dashboard) and, more importantly, each trade's **candle time (UTC), direction, entry, SL, TP**.
3. Expect differences caused by data provider, the 4800-bar window start (see limitation 1 in `docs/SOURCE_ANALYSIS.md`) and
   weekend candles. Compare several pairs; investigate mismatches rather than ignoring them. Do not treat the scanner as
   "identical to TradingView" unless you have verified it this way.

## Known limitations
* Live behaviour of Twelve Data (large `outputsize` in batches, exact 429 texts, forming-candle behaviour, per-pair plan
  access) was taken from its documentation and is **unverified live**. If an init batch fails, set `BATCH_SIZE=2`.
* Daily/minute 429 detection relies on message wording ("for the day"); unknown wording is treated as per-minute and retried
  a bounded number of times, never indefinitely.
* The pipeline cannot know a candle is final if the provider revises it after the grace period (30 s).
* Telegram duplicates are minimised, not impossible (see above).
