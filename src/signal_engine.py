"""Turns validated candles + persisted checkpoint into new, deduplicated signals.

Recovery design (see README): the strategy is a stateful, path-dependent state
machine, so it is advanced strictly chronologically over every NEW closed candle
after the persisted checkpoint (never "just the latest bar").  Signals found on
candles inside the lookback window (default 7 closed candles) are eligible for
delivery; older ones are recorded as expired and never sent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import Config, STRATEGY_ID, STRATEGY_NAME, TIMEFRAME
from .market_data import closed_candles, market_expected_open
from .state_store import utc_iso, parse_iso
from .strategy import Candle, PO3, PO3Machine, run_strategy, window_start

log = logging.getLogger("signal_engine")


@dataclass
class Signal:
    id: str
    symbol: str
    direction: str                 # BUY / SELL
    strategy: str
    timeframe: str
    candle_ts: datetime            # bar OPEN time (UTC); signal confirmed at close
    detected_at: datetime
    entry: float                   # original strategy: close of signal candle
    stop_loss: float               # original strategy (Dynamic ATR or Fixed %)
    take_profit: float             # original strategy
    values: dict = field(default_factory=dict)   # supporting original-strategy values
    candles_ago: int = 0           # supplementary: closed candles between signal and latest
    status: str = "OPEN"           # supplementary: OPEN / TP / SL as of latest processed candle
    status_ts: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {"id": self.id, "symbol": self.symbol, "direction": self.direction, "strategy": self.strategy,
                "timeframe": self.timeframe, "candle_ts": utc_iso(self.candle_ts),
                "detected_at": utc_iso(self.detected_at), "entry": self.entry, "stop_loss": self.stop_loss,
                "take_profit": self.take_profit, "values": self.values, "candles_ago": self.candles_ago,
                "status": self.status, "status_ts": utc_iso(self.status_ts) if self.status_ts else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Signal":
        return cls(id=d["id"], symbol=d["symbol"], direction=d["direction"], strategy=d["strategy"],
                   timeframe=d["timeframe"], candle_ts=parse_iso(d["candle_ts"]),
                   detected_at=parse_iso(d["detected_at"]), entry=d["entry"], stop_loss=d["stop_loss"],
                   take_profit=d["take_profit"], values=d.get("values", {}), candles_ago=d.get("candles_ago", 0),
                   status=d.get("status", "OPEN"),
                   status_ts=parse_iso(d["status_ts"]) if d.get("status_ts") else None)


def make_signal_id(symbol: str, candle_ts: datetime, direction: str) -> str:
    """Deterministic: strategy|symbol|timeframe|candle-open-time|direction.
    The Pine strategy can enter at most once per bar, so this is unique per event."""
    return f"{STRATEGY_ID}|{symbol}|{TIMEFRAME}|{candle_ts.strftime('%Y%m%dT%H%MZ')}|{direction}"


@dataclass
class Evaluation:
    symbol: str
    new_state: Optional[dict]                 # symbol checkpoint to persist (None => unchanged)
    signals: list[Signal] = field(default_factory=list)      # eligible (within lookback)
    stale_signals: list[Signal] = field(default_factory=list)  # found but outside lookback
    mode: str = "none"                        # init / incremental / reinit / none
    closed_count: int = 0
    new_candles: int = 0
    latest_closed: Optional[datetime] = None
    stale_data: bool = False
    notes: list[str] = field(default_factory=list)


def needs_init(sym_state: Optional[dict], cfg: Config, now: datetime) -> bool:
    """Decides (before fetching) whether the large history request is required."""
    if not sym_state or not sym_state.get("last_processed_ts") or not sym_state.get("initialized"):
        return True
    age_h = (now - parse_iso(sym_state["last_processed_ts"])).total_seconds() / 3600
    return age_h > cfg.incremental_max_gap_hours


def _make_signal(po3: PO3, symbol: str, idx: int, closed: list[Candle], now: datetime) -> Signal:
    direction = "BUY" if po3.entry_type == "Long" else "SELL"
    sig = Signal(
        id=make_signal_id(symbol, po3.entry_ts, direction), symbol=symbol, direction=direction,
        strategy=STRATEGY_NAME, timeframe=TIMEFRAME, candle_ts=po3.entry_ts, detected_at=now,
        entry=po3.entry_price, stop_loss=po3.sl_target, take_profit=po3.tp_target,
        values={"manipulation_direction": po3.manip_direction, "atr": po3.entry_atr,
                "accumulation_top": po3.acc_top, "accumulation_bottom": po3.acc_bottom,
                "manipulation_top": po3.manip_top, "manipulation_bottom": po3.manip_bottom},
        candles_ago=len(closed) - 1 - idx)
    refresh_status(sig, po3)
    return sig


def refresh_status(sig: Signal, po3: PO3) -> None:
    if po3.outcome == "Take Profit":
        sig.status, sig.status_ts = "TP", po3.exit_ts
    elif po3.outcome == "Stop Loss":
        sig.status, sig.status_ts = "SL", po3.exit_ts
    else:
        sig.status, sig.status_ts = "OPEN", None


def evaluate_symbol(symbol: str, raw_candles: list[Candle], sym_state: Optional[dict],
                    cfg: Config, now: datetime) -> Evaluation:
    P = cfg.strategy
    closed = closed_candles(raw_candles, now, cfg.candle_close_grace_seconds)
    ev = Evaluation(symbol=symbol, new_state=None, closed_count=len(closed))
    if not closed:
        ev.notes.append("no closed candles available")
        return ev
    ev.latest_closed = closed[-1].ts
    if market_expected_open(now) and (now - ev.latest_closed - timedelta(hours=1)).total_seconds() / 3600 > cfg.stale_warning_hours:
        ev.stale_data = True
        ev.notes.append(f"latest closed candle {utc_iso(ev.latest_closed)} looks stale")

    # ---- decide how to run ----
    machine: Optional[PO3Machine] = None
    first_index = 0
    send_window = cfg.lookback_candles
    if sym_state and sym_state.get("initialized") and sym_state.get("last_processed_ts"):
        last_ts = parse_iso(sym_state["last_processed_ts"])
        if closed[0].ts <= last_ts:
            ev.mode = "incremental"
            first_index = next((i for i, c in enumerate(closed) if c.ts > last_ts), len(closed))
            machine = PO3Machine.from_dict(P, sym_state.get("machine"))
        else:
            ev.mode = "reinit"
            ev.notes.append("checkpoint older than available history -> re-initialising (baseline)")
    else:
        ev.mode = "init"
    if ev.mode in ("init", "reinit"):
        first_index = window_start(len(closed), P)
        send_window = cfg.first_run_send_lookback
        machine = PO3Machine(P)
        if len(closed) < P.max_distance_to_last_bar:
            ev.notes.append(f"only {len(closed)} candles available (< {P.max_distance_to_last_bar}); "
                            "state machine starts at the first candle")

    ev.new_candles = len(closed) - first_index
    if ev.new_candles <= 0:
        ev.notes.append("no new closed candles since checkpoint")
        return ev

    res = run_strategy(closed, P, first_index, machine)
    for idx, po3 in res.entries:
        sig = _make_signal(po3, symbol, idx, closed, now)
        if sig.candles_ago < send_window:
            ev.signals.append(sig)
        else:
            ev.stale_signals.append(sig)

    ev.new_state = {"initialized": True, "last_processed_ts": utc_iso(closed[-1].ts),
                    "machine": machine.to_dict(), "mode": ev.mode,
                    "updated_at": utc_iso(now)}
    return ev
