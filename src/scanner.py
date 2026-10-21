"""Orchestration: fetch -> evaluate -> persist -> deliver."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import Config
from .market_data import (AuthenticationError, DailyQuotaExhausted, TwelveDataClient, closed_candles)
from .signal_engine import Signal, evaluate_symbol, needs_init
from .state_store import PENDING, StateStore, parse_iso, utc_iso
from .strategy import dashboard_stats, run_strategy, window_start, PO3Machine
from .symbol_mapping import chunk, to_provider_symbol
from .telegram_notifier import SendResult, TelegramNotifier

log = logging.getLogger("scanner")


@dataclass
class RunReport:
    symbols_total: int = 0
    fetched_ok: int = 0
    failed: dict = field(default_factory=dict)         # symbol -> "kind: message"
    deferred: list = field(default_factory=list)       # symbols never requested (fatal stop)
    detected: int = 0
    duplicates_skipped: int = 0
    stale_recorded: int = 0
    delivered: int = 0
    delivery_failed: int = 0
    expired_pending: int = 0
    would_send: list = field(default_factory=list)     # dry-run only
    credits_used_run: int = 0
    credits_used_today: int = 0
    fatal: Optional[str] = None
    notes: list = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        # A run that leaves symbols unrequested is incomplete (most commonly due
        # to API quota/rate-budget exhaustion). Do not let GitHub Actions mark it
        # green just because an earlier batch succeeded. Individual symbol errors
        # remain warnings when the rest of the universe was actually attempted.
        if self.fatal or self.deferred:
            return 1
        if self.symbols_total and self.fetched_ok == 0:
            return 1
        return 0

    @property
    def has_warnings(self) -> bool:
        return bool(self.failed or self.deferred or self.delivery_failed or self.expired_pending)


def _record(sig: Signal, status: str = PENDING, reason: str = "") -> dict:
    return {"id": sig.id, "symbol": sig.symbol, "direction": sig.direction,
            "candle_ts": utc_iso(sig.candle_ts), "detected_at": utc_iso(sig.detected_at),
            "status": status, "attempts": 0, "sent_at": None, "message_id": None,
            "last_error": reason or None, "signal": sig.to_dict()}


def run_scan(cfg: Config, store: StateStore, client: TwelveDataClient,
             notifier: Optional[TelegramNotifier], now: Optional[datetime] = None,
             dry_run: bool = False) -> RunReport:
    now = now or datetime.now(timezone.utc)
    rep = RunReport(symbols_total=len(cfg.universe))
    log.info("Scan start %s | symbols=%d dry_run=%s", utc_iso(now), len(cfg.universe), dry_run)

    batches = chunk(list(cfg.universe), cfg.batch_size)
    stop = False
    for bi, batch in enumerate(batches, 1):
        if stop:
            rep.deferred += batch
            continue
        init_needed = any(needs_init(store.symbol_state(s), cfg, now) for s in batch)
        size = cfg.init_outputsize if init_needed else cfg.incremental_outputsize
        log.info("Batch %d/%d: %s (outputsize=%d)", bi, len(batches), ",".join(batch), size)
        try:
            results = client.fetch_batch(batch, size)
        except AuthenticationError as exc:
            rep.fatal = f"Authentication error: {exc}"
            log.error(rep.fatal)
            rep.deferred += batch
            stop = True
            continue
        except DailyQuotaExhausted as exc:
            rep.fatal = None
            rep.notes.append(f"Daily quota exhausted: {exc}")
            log.error("Daily quota exhausted - deferring remaining symbols: %s", exc)
            rep.deferred += batch
            stop = True
            continue
        for sym in batch:
            res = results.get(sym)
            if res is None or not res.ok:
                kind = res.error_kind if res else "missing"
                msg = res.message if res else "no result"
                rep.failed[sym] = f"{kind}: {msg}"
                log.error("%s: data fetch FAILED (%s: %s)", sym, kind, msg)
                if not dry_run:
                    st = dict(store.symbol_state(sym) or {})
                    st["consecutive_failures"] = int(st.get("consecutive_failures", 0)) + 1
                    st["last_error"] = f"{kind}: {msg}"[:200]
                    store.set_symbol_state(sym, st)
                    if st["consecutive_failures"] >= 3:
                        log.error("%s has failed %d consecutive scans", sym, st["consecutive_failures"])
                continue
            rep.fetched_ok += 1
            ev = evaluate_symbol(sym, res.candles, store.symbol_state(sym), cfg, now)
            log.info("%s: ok candles=%d closed=%d latest_closed=%s mode=%s new=%d signals=%d stale_signals=%d%s",
                     sym, len(res.candles), ev.closed_count, utc_iso(ev.latest_closed) if ev.latest_closed else "-",
                     ev.mode, ev.new_candles, len(ev.signals), len(ev.stale_signals),
                     " STALE-DATA" if ev.stale_data else "")
            for note in ev.notes:
                log.info("%s: %s", sym, note)
            if ev.stale_data:
                rep.notes.append(f"{sym}: stale data")
            if dry_run:
                rep.would_send += [s for s in ev.signals if not store.has_signal(s.id)]
                rep.detected += len(ev.signals)
                continue
            if ev.new_state is not None:
                ev.new_state["consecutive_failures"] = 0
                store.set_symbol_state(sym, ev.new_state)
            for sig in ev.signals:
                if store.add_signal(_record(sig)):
                    rep.detected += 1
                    log.info("%s: NEW SIGNAL %s %s candle=%s entry=%.5f", sym, sig.direction, sig.id,
                             utc_iso(sig.candle_ts), sig.entry)
                else:
                    rep.duplicates_skipped += 1
                    log.info("%s: duplicate signal skipped %s", sym, sig.id)
            if ev.mode == "incremental":
                for sig in ev.stale_signals:
                    if store.add_signal(_record(sig, "expired", "outside lookback window")):
                        rep.stale_recorded += 1
            elif ev.stale_signals:
                log.info("%s: %d historical signal(s) recorded as baseline only (not sent)", sym, len(ev.stale_signals))

    rep.credits_used_run = client.budget.run_used
    rep.credits_used_today = client.budget.daily_used
    store.set_credits_used_today(now, client.budget.daily_used)

    if dry_run:
        store.save(now)  # only api usage changed
        log.info("Dry run: %d signal(s) would be sent: %s", len(rep.would_send), [s.id for s in rep.would_send])
        return rep

    store.save(now)  # checkpoints + pending signals are durable BEFORE any delivery attempt
    log.info("State saved (checkpoints + %d pending signal(s))", len(store.pending()))

    # ---- expire hopeless pending signals ----
    for r in store.pending():
        age_h = (now - parse_iso(r["candle_ts"])).total_seconds() / 3600
        if age_h > cfg.pending_max_age_hours or r["attempts"] >= cfg.pending_max_attempts:
            store.mark_expired(r["id"], f"pending too long (age {age_h:.1f}h, attempts {r['attempts']})")
            rep.expired_pending += 1
            log.warning("Signal %s expired undelivered", r["id"])

    # ---- deliver (chronological, 15 s apart) ----
    pending = store.pending()
    if pending and notifier is None:
        rep.fatal = "Telegram notifier not configured but signals are pending"
    elif pending:
        sigs = [Signal.from_dict(r["signal"]) for r in pending]

        def on_result(sig: Signal, result: SendResult) -> None:
            if result.ok:
                store.mark_sent(sig.id, datetime.now(timezone.utc), result.message_id)
                rep.delivered += 1
            else:
                store.mark_attempt_failed(sig.id, result.error)
                rep.delivery_failed += 1
            store.save(now)   # persist after EVERY message to minimise duplicate risk

        ok = notifier.deliver(sigs, on_result)
        if not ok:
            rep.fatal = "Telegram fatal error (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"

    pruned = store.prune(now, cfg.signal_retention_days)
    if pruned:
        log.info("Pruned %d old signal records", pruned)
    store.save(now)
    return rep


# ------------------------------------------------------------ diagnostics
def run_diagnose(cfg: Config, client: TwelveDataClient, now: Optional[datetime] = None) -> dict:
    """Checks catalogue + 1h data access for all 28 pairs. No Telegram, no state changes."""
    now = now or datetime.now(timezone.utc)
    out = {"catalogue_missing": [], "catalogue_error": None, "symbols": {}}
    try:
        catalogue = client.list_forex_pairs()
        out["catalogue_missing"] = [s for s in cfg.universe if to_provider_symbol(s) not in catalogue]
    except AuthenticationError:
        raise
    except Exception as exc:  # noqa: BLE001 - diagnostic must continue
        out["catalogue_error"] = str(exc)
    for batch in chunk(list(cfg.universe), cfg.batch_size):
        try:
            results = client.fetch_batch(batch, 5)
        except DailyQuotaExhausted as exc:
            for s in batch:
                out["symbols"][s] = {"ok": False, "error": f"daily quota: {exc}"}
            continue
        for s in batch:
            r = results[s]
            closed = closed_candles(r.candles, now, cfg.candle_close_grace_seconds) if r.ok else []
            out["symbols"][s] = {"ok": r.ok, "error": None if r.ok else f"{r.error_kind}: {r.message}",
                                 "latest_closed": utc_iso(closed[-1].ts) if closed else None}
    return out


def run_replay(cfg: Config, client: TwelveDataClient, symbol: str, now: Optional[datetime] = None) -> dict:
    """Full 4800-bar replay of one symbol (1 credit) for manual TradingView comparison."""
    now = now or datetime.now(timezone.utc)
    res = client.fetch_batch([symbol], cfg.init_outputsize)[symbol]
    if not res.ok:
        raise RuntimeError(f"{symbol}: {res.error_kind}: {res.message}")
    closed = closed_candles(res.candles, now, cfg.candle_close_grace_seconds)
    out = run_strategy(closed, cfg.strategy, window_start(len(closed), cfg.strategy), PO3Machine(cfg.strategy))
    trades = [p for _, p in out.entries]
    return {"symbol": symbol, "candles": len(closed),
            "first_candle": utc_iso(closed[0].ts), "last_candle": utc_iso(closed[-1].ts),
            "window_start": utc_iso(closed[window_start(len(closed), cfg.strategy)].ts),
            "stats": dashboard_stats(trades),
            "trades": [{"candle": utc_iso(p.entry_ts), "type": p.entry_type, "entry": p.entry_price,
                        "sl": p.sl_target, "tp": p.tp_target, "outcome": p.outcome,
                        "exit": utc_iso(p.exit_ts) if p.exit_ts else None} for p in trades]}
