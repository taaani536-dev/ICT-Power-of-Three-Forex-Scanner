# Source analysis — "ICT Power Of Three | Flux Charts" (Pine Script v5)

All statements below were derived from the supplied script. Nothing was assumed from the strategy's name:
the script contains **no** liquidity sweeps, fair-value gaps, order blocks, market-structure shifts, EMA/RSI
filters or sessions. It is an ATR-scaled *range → breakout → bigger breakout* state machine.

## A–C. Version, inputs, defaults
`//@version=5`, `indicator(..., overlay=true)`. Signal-relevant inputs and the values that are actually used:

| Pine input / constant | Default | Effective values (derived in code) |
|---|---|---|
| `algorithmMode` | "Small Manipulation" | accumulationLength 40, accumulation ATR mult 5, manipulation ATR mult 0.6 |
| (other modes) | – | "Short Accumulation": len 11, acc 2, manip 1 · "Big Manipulation": len 11, acc 5, manip 1 |
| `breakoutMethod` | "Wick" | high/low (Wick) or close/close (Close) used as breakout source |
| `tpslMethod` | "Dynamic" | ATR based; "Fixed" uses % |
| `riskAmount` | "Normal" | SL = 5 × ATR (Highest 9.5, High 6, Low 4, Lowest 1.5) |
| `RR` | 0.86 (debug-only input, constant otherwise) | TP distance = 0.86 × SL distance |
| `tpPercent` / `slPercent` | 0.3 / 0.4 | Fixed method only |
| `atrLen` | 50 | `ta.atr(50)` |
| `accumulationExpandMult` | 0.5 | box expanded by 0.5 ATR |
| `maxDistanceToLastBar` | 4800 | state machine only runs on the last 4800 bars |

Visual-only inputs (colors, zones, TP/SL layout, dashboard position, label size) and alert on/off toggles are not signal logic.

## D/H. Exact signal conditions (all on a **confirmed** bar, evaluated top-to-bottom, *not* `else`-chained)
1. **New cycle**: create a new `PO3` ("Waiting For Accumulation") unless the last PO3 has a trade with no exit yet (`exitPrice` na).
2. **Accumulation**: `highest(len) − lowest(len) ≤ ATR × accMult` → box top/bottom = highest/lowest ± 0.5·ATR; state "Waiting For Accumulation End".
3. **Accumulation end**: wick (or close) beyond the box → record the breakout bar's high/low, state "Waiting For Manipulation"; otherwise extend `accumulationEndIndex` to the current bar.
4. **Manipulation** (checked the **same bar** as the breakout because `accumulationEndIndex` still holds the previous bar):
   * `highBreakout > boxTop + 0.6·ATR` → **Bullish** manipulation → later **SELL**
   * else `lowBreakout < boxBottom − 0.6·ATR` → **Bearish** manipulation → later **BUY**
   * if neither, the PO3 keeps waiting on every following bar (no invalidation).
5. **Distribution / entry** (same bar): state "Entry Taken"; `entryPrice = close`; Bearish → **Long/BUY**, Bullish → **Short/SELL**.
6. **TP/SL** (Dynamic): `SL = entry ∓ ATR·slMult`, `TP = entry ± |entry−SL|·0.86`. Exits are checked from the **next** bar on; TP block runs first, SL block second, so **SL wins when both are touched on one bar**. After an exit the state becomes "Done" and a new PO3 starts on the next bar.

## E/F/K. Stateful variables, candle use, look-ahead
* `var PO3 lastPO3` (+ `PO3List`) is the only strategy state; it is **path dependent** (a trade in progress blocks new cycles).
* Uses `high`, `low`, `close`, `time`, `bar_index`; `open` is not used. ATR = RMA(true range) seeded by an SMA (na for the first 49 bars). `ta.highest/lowest(len)` include the current bar and are na until the window is full.
* No `request.security`, no `lookahead`, no future bars (`time("", -15)` appears only in drawing code).

## G. Session / time / volatility
No sessions or time filters. Volatility enters only through ATR(50).

## I. Intrabar vs closed-bar
The whole logic block is wrapped in `if bar_index > last_bar_index − 4800 and barstate.isconfirmed`, so **signals occur only on bar close**. No intrabar policy change is needed; the scanner only uses closed candles.

## J. Entry/SL/TP
Calculated by the original script (entry = signal-bar close; SL/TP as above) and therefore shown in Telegram. No confidence/probability exists in the source and none is invented.

## L. TradingView-specific features → Python
| Pine | Python (`src/strategy.py`) |
|---|---|
| `ta.atr(50)` / `ta.rma` / `ta.tr(true)` | `atr_series`, `rma`, `true_range` (SMA seed, α = 1/50, first TR = high−low) |
| `ta.highest(len)`, `ta.lowest(len)` | `rolling_extreme` (None until full, includes current bar) |
| `barstate.isconfirmed` | only closed candles are passed in (`closed_candles`) |
| `bar_index > last_bar_index − 4800` | `window_start()` on first run; afterwards the persisted checkpoint |
| `bar_index > accumulationEndIndex` | strict timestamp comparison (identical for strictly increasing bars) |
| `var lastPO3` | `PO3Machine` + JSON checkpoint per symbol (`PO3.to_dict/from_dict`) |
| `time` (bar open) | `Candle.ts` (UTC bar-open time) = signal candle timestamp |
| na semantics (comparison with na = false) | explicit `None` checks |
| `createNewPO3 / sequential ifs` | `PO3Machine.step` preserves order and same-bar fall-through |
| Backtesting table maths | `dashboard_stats` (quirks kept: unresolved trade counts as a loss; % uses exit price as denominator) |
| Not ported | drawing (boxes/lines/labels/plotshape), alert plumbing, table rendering, DEBUG inputs |

## Behavioural differences / limitations (honest list)
1. **Window start is not reproducible even inside TradingView.** The state machine starts at `last_bar_index − 4800` of whatever history is loaded, so a TradingView chart reload can start the machine in a different phase. The scanner mimics a TradingView *live alert*: it starts once from the last 4800 closed candles and then carries the state forward by checkpoint. Its phase therefore depends on the first-run time and cannot be guaranteed to equal any particular TradingView chart.
2. **Data source differences** (Twelve Data vs the broker feed used on the chart) change OHLC and therefore ATR/boxes/signals. Weekend/Sunday-open candles can also differ.
3. **ATR warm-up**: TradingView seeds ATR at the first bar of its full history; the scanner seeds at the first fetched candle (≥ 1000 candles, 5000 on init). The seed influence decays as 0.98ⁿ (≈10⁻⁹ after 1000 bars) — negligible but not bit-identical.
4. **Alerts**: the Pine script also alerts on TP/SL; per the project brief only BUY/SELL are sent. TP/SL status is shown inside delayed BUY/SELL messages when already resolved.
5. Alert toggles (`buyAlertEnabled`, …) are not inputs of the scanner: BUY and SELL are always sent.
6. A signal recovered late (up to 7 candles) is sent with its **original** candle-close entry price, which may be far from the current price — the message states the delay and whether TP/SL was already hit.
