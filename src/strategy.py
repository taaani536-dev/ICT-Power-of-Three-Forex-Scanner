"""Faithful Python port of the Pine Script "ICT Power Of Three | Flux Charts" (v5).

Only the signal / state-machine logic is ported.  Drawing code (boxes, lines,
labels, plotshape), alert plumbing and the backtesting-table *rendering* are
not signal logic and are not ported (the table's statistics are ported in
``dashboard_stats`` for validation purposes).

Pine behaviours reproduced (see docs/SOURCE_ANALYSIS.md for the mapping):
  * ``ta.atr(50)``  -> RMA of true range, seeded with an SMA, na for 49 bars.
  * ``ta.highest/lowest(len)`` -> rolling extreme including current bar, na until full.
  * The main block runs only on confirmed bars within the last 4800 bars.
  * Sequential (non ``else``) ``if`` statements: several state transitions can
    happen on the SAME bar (accumulation -> breakout -> manipulation -> entry).
  * ``bar_index > accumulationEndIndex`` is ported as a strict timestamp
    comparison (identical for strictly increasing candle times).
  * When TP and SL are both touched on one bar, the SL block runs last and wins.
  * na semantics: any comparison with na is false.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from .config import StrategyParams

# States (identical strings to the Pine source)
S_WAIT_ACC = "Waiting For Accumulation"
S_WAIT_ACC_END = "Waiting For Accumulation End"
S_WAIT_MANIP = "Waiting For Manipulation"
S_WAIT_DIST = "Waiting For Distribution"
S_ENTRY = "Entry Taken"
S_TP = "Take Profit"
S_SL = "Stop Loss"
S_DONE = "Done"


@dataclass(frozen=True)
class Candle:
    ts: datetime   # bar OPEN time, tz-aware UTC (Pine ``time``)
    open: float
    high: float
    low: float
    close: float


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------- indicators
def true_range(candles: list[Candle]) -> list[float]:
    """ta.tr(true): first bar = high-low, afterwards the classic 3-way max."""
    out: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def rma(values: list[float], length: int) -> list[Optional[float]]:
    """ta.rma: na until `length` values, seeded with SMA, then alpha=1/length."""
    out: list[Optional[float]] = [None] * len(values)
    alpha = 1.0 / length
    for i in range(len(values)):
        if i < length - 1:
            continue
        if i == length - 1:
            out[i] = sum(values[:length]) / length
        else:
            out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def atr_series(candles: list[Candle], length: int) -> list[Optional[float]]:
    return rma(true_range(candles), length)


def rolling_extreme(values: list[float], length: int, fn) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    for i in range(length - 1, len(values)):
        out[i] = fn(values[i - length + 1:i + 1])
    return out


# ------------------------------------------------------------------ state
@dataclass
class PO3:
    state: str
    start_ts: Optional[datetime] = None
    acc_top: Optional[float] = None
    acc_bottom: Optional[float] = None
    acc_end_top: Optional[float] = None
    acc_end_bottom: Optional[float] = None
    acc_start_ts: Optional[datetime] = None
    acc_end_ts: Optional[datetime] = None
    manip_top: Optional[float] = None
    manip_bottom: Optional[float] = None
    manip_start_ts: Optional[datetime] = None
    manip_end_ts: Optional[datetime] = None
    manip_direction: Optional[str] = None
    sl_target: Optional[float] = None
    tp_target: Optional[float] = None
    entry_type: Optional[str] = None
    entry_ts: Optional[datetime] = None
    exit_ts: Optional[datetime] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    entry_atr: Optional[float] = None       # supplementary (atr at entry bar)
    outcome: Optional[str] = None           # supplementary: "Take Profit"/"Stop Loss"

    _TS = ("start_ts", "acc_start_ts", "acc_end_ts", "manip_start_ts", "manip_end_ts", "entry_ts", "exit_ts")

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in self._TS:
            d[k] = _iso(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PO3":
        d = dict(d)
        for k in cls._TS:
            d[k] = _parse(d.get(k))
        return cls(**d)


class PO3Machine:
    """Holds `lastPO3` and advances it one confirmed bar at a time."""

    def __init__(self, params: StrategyParams, po3: Optional[PO3] = None):
        self.p = params
        self.po3 = po3

    def to_dict(self) -> Optional[dict]:
        return self.po3.to_dict() if self.po3 else None

    @classmethod
    def from_dict(cls, params: StrategyParams, d: Optional[dict]) -> "PO3Machine":
        return cls(params, PO3.from_dict(d) if d else None)

    def step(self, bar: Candle, atr: Optional[float], hi: Optional[float], lo: Optional[float],
             acc_start_ts: Optional[datetime] = None) -> bool:
        """Process one confirmed bar. Returns True if a new entry (signal) occurred on this bar."""
        P = self.p
        wick = P.breakout_method == "Wick"
        high_bo = bar.high if wick else bar.close
        low_bo = bar.low if wick else bar.close
        entered = False

        # --- create new PO3 unless a trade is still open (exitPrice na) ---
        create = True
        if self.po3 is not None and self.po3.exit_price is None:
            create = False
        if create:
            self.po3 = PO3(state=S_WAIT_ACC, start_ts=bar.ts)
        po3 = self.po3

        # --- Find accumulation ---
        if po3.state == S_WAIT_ACC:
            if hi is not None and lo is not None and atr is not None and \
                    (hi - lo) <= atr * P.accumulation_atr_mult:
                po3.state = S_WAIT_ACC_END
                po3.acc_start_ts = acc_start_ts
                po3.acc_end_ts = bar.ts
                po3.acc_top = hi + atr * P.accumulation_expand_mult
                po3.acc_bottom = lo - atr * P.accumulation_expand_mult
        if po3.state == S_WAIT_ACC_END:
            if high_bo > po3.acc_top or low_bo < po3.acc_bottom:
                po3.state = S_WAIT_MANIP
                po3.acc_end_top = high_bo
                po3.acc_end_bottom = low_bo
            else:
                po3.acc_end_ts = bar.ts

        # --- Find manipulation (note: runs on the same bar as the breakout) ---
        if po3.state == S_WAIT_MANIP:
            if bar.ts > po3.acc_end_ts and atr is not None:
                if high_bo > po3.acc_top + atr * P.manipulation_atr_mult:
                    po3.state = S_WAIT_DIST
                    po3.manip_direction = "Bullish"
                    po3.manip_start_ts = po3.acc_end_ts
                    po3.manip_end_ts = bar.ts
                    po3.manip_top = high_bo
                    po3.manip_bottom = po3.acc_end_bottom
                elif low_bo < po3.acc_bottom - atr * P.manipulation_atr_mult:
                    po3.state = S_WAIT_DIST
                    po3.manip_direction = "Bearish"
                    po3.manip_start_ts = po3.acc_end_ts
                    po3.manip_end_ts = bar.ts
                    po3.manip_top = po3.acc_end_top
                    po3.manip_bottom = low_bo

        # --- Distribution => entry at close of the same bar ---
        if po3.state == S_WAIT_DIST:
            po3.state = S_ENTRY
            po3.entry_ts = bar.ts
            po3.entry_price = bar.close
            po3.entry_atr = atr
            entered = True
            if po3.manip_direction == "Bearish":
                po3.entry_type = "Long"
                if P.tpsl_method == "Fixed":
                    po3.sl_target = po3.entry_price * (1 - P.sl_percent / 100.0)
                    po3.tp_target = po3.entry_price * (1 + P.tp_percent / 100.0)
                else:
                    po3.sl_target = po3.entry_price - atr * P.sl_atr_mult
                    po3.tp_target = po3.entry_price + abs(po3.entry_price - po3.sl_target) * P.rr
            else:
                po3.entry_type = "Short"
                if P.tpsl_method == "Fixed":
                    po3.sl_target = po3.entry_price * (1 + P.sl_percent / 100.0)
                    po3.tp_target = po3.entry_price * (1 - P.tp_percent / 100.0)
                else:
                    po3.sl_target = po3.entry_price + atr * P.sl_atr_mult
                    po3.tp_target = po3.entry_price - abs(po3.entry_price - po3.sl_target) * P.rr

        # --- Entry taken: evaluate exits on later bars (TP first, SL second => SL wins) ---
        if po3.state == S_ENTRY and bar.ts > po3.entry_ts:
            e = po3.entry_price
            if P.tpsl_method == "Fixed":
                if po3.entry_type == "Long" and ((bar.high / e) - 1) * 100 >= P.tp_percent:
                    self._exit(po3, bar, e * (1 + P.tp_percent / 100.0), S_TP)
                if po3.entry_type == "Short" and ((bar.low / e) - 1) * 100 <= -P.tp_percent:
                    self._exit(po3, bar, e * (1 - P.tp_percent / 100.0), S_TP)
                if po3.entry_type == "Long" and ((bar.low / e) - 1) * 100 <= -P.sl_percent:
                    self._exit(po3, bar, e * (1 - P.sl_percent / 100.0), S_SL)
                if po3.entry_type == "Short" and ((bar.high / e) - 1) * 100 >= P.sl_percent:
                    self._exit(po3, bar, e * (1 + P.sl_percent / 100.0), S_SL)
            else:
                if po3.entry_type == "Long" and bar.high >= po3.tp_target:
                    self._exit(po3, bar, po3.tp_target, S_TP)
                if po3.entry_type == "Short" and bar.low <= po3.tp_target:
                    self._exit(po3, bar, po3.tp_target, S_TP)
                if po3.entry_type == "Long" and bar.low <= po3.sl_target:
                    self._exit(po3, bar, po3.sl_target, S_SL)
                if po3.entry_type == "Short" and bar.high >= po3.sl_target:
                    self._exit(po3, bar, po3.sl_target, S_SL)

        # --- "Render" block: state -> Done (exitPrice stays set => new PO3 next bar) ---
        if po3.state == S_SL and bar.ts >= po3.exit_ts:
            po3.state = S_DONE
        if po3.state == S_TP:
            po3.state = S_DONE
        return entered

    @staticmethod
    def _exit(po3: PO3, bar: Candle, price: float, state: str) -> None:
        po3.exit_price = price
        po3.exit_ts = bar.ts
        po3.state = state
        po3.outcome = state


@dataclass
class StrategyResult:
    machine: PO3Machine
    entries: list[tuple[int, PO3]] = field(default_factory=list)  # (candle index, PO3 object)
    last_index: int = -1


def run_strategy(candles: list[Candle], params: StrategyParams, first_index: int,
                 machine: Optional[PO3Machine] = None) -> StrategyResult:
    """Advance the state machine over candles[first_index:], strictly chronologically.

    Indicators are computed over ALL supplied candles (Pine computes them on the
    whole chart history); only the state machine is windowed.
    Candles must be sorted ascending, unique and CLOSED.
    """
    n = len(candles)
    machine = machine or PO3Machine(params)
    res = StrategyResult(machine=machine, last_index=n - 1)
    if n == 0:
        return res
    atr = atr_series(candles, params.atr_len)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    L = params.accumulation_length
    hi = rolling_extreme(highs, L, max)
    lo = rolling_extreme(lows, L, min)
    for i in range(max(first_index, 0), n):
        acc_start = candles[i - L].ts if i - L >= 0 else None
        if machine.step(candles[i], atr[i], hi[i], lo[i], acc_start):
            res.entries.append((i, machine.po3))
    return res


def window_start(n_candles: int, params: StrategyParams) -> int:
    """Pine gate: bar_index > last_bar_index - 4800  ->  first processed index."""
    return max(0, n_candles - params.max_distance_to_last_bar)


# ------------------------------------------------- dashboard (validation aid)
def diff_percent(v1: float, v2: float) -> float:
    return (abs(v1 - v2) / v2) * 100.0


def dashboard_stats(trades: list[PO3]) -> dict:
    """Port of the Pine "PO3 Backtesting" table maths (quirks preserved:
    unresolved trades count as losses; percent uses exit price as denominator)."""
    total = 0.0
    wins = losses = 0
    for t in trades:
        if t.entry_price is None:
            continue
        ok = False
        if t.exit_price is not None:
            if (t.entry_type == "Long" and t.exit_price > t.entry_price) or \
               (t.entry_type == "Short" and t.exit_price < t.entry_price):
                total += abs(diff_percent(t.entry_price, t.exit_price))
                ok = True
            else:
                total -= abs(diff_percent(t.entry_price, t.exit_price))
        if ok:
            wins += 1
        else:
            losses += 1
    n = wins + losses
    return {
        "total_entries": n, "wins": wins, "losses": losses,
        "winrate_pct": (100.0 * wins / n) if n else None,
        "average_profit_pct": (total / n) if n else None,
        "total_profit_pct": total,
    }
