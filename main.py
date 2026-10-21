#!/usr/bin/env python3
"""CLI entry point.

Exit codes: 0 = completed (possibly with warnings), 1 = unrecoverable failure,
2 = configuration / missing secrets, 3 = corrupt state file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from src.config import ConfigError, load_config
from src.logging_config import redact, setup_logging
from src.market_data import AuthenticationError, CreditBudget, DataError, TwelveDataClient
from src.scanner import run_diagnose, run_replay, run_scan
from src.state_store import StateCorruptError, StateStore, merge_states, validate_state
from src.symbol_mapping import SymbolError, normalize_symbol
from src.telegram_notifier import TelegramNotifier

log = logging.getLogger("main")


def gh(kind: str, msg: str) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind}::{redact(msg)}")


def write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(redact("\n".join(lines)) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ICT Power Of Three - Forex signal scanner (1H)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="fetch, evaluate, persist, send Telegram alerts")
    s.add_argument("--dry-run", action="store_true", help="no Telegram, no signal/checkpoint changes")
    sub.add_parser("diagnose", help="check catalogue + data access for all 28 pairs (no Telegram)")
    r = sub.add_parser("replay", help="replay one symbol over the 4800-bar window (TradingView comparison)")
    r.add_argument("symbol")
    sub.add_parser("telegram-test", help="send ONE test message (explicit live test)")
    sub.add_parser("validate-config", help="validate configuration and secrets presence, no network")
    m = sub.add_parser("merge-state", help="merge two state files (git push conflict resolution)")
    m.add_argument("--ours", required=True)
    m.add_argument("--theirs", required=True)
    m.add_argument("--output", required=True)
    return p


def make_client(cfg, store, now):
    budget = CreditBudget(cfg.max_credits_per_run, cfg.daily_budget, store.credits_used_today(now))
    return TwelveDataClient(cfg, budget)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        setup_logging(cfg.log_level)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)

    try:
        if args.cmd == "merge-state":
            with open(args.ours) as a, open(args.theirs) as b:
                merged = merge_states(json.load(a), json.load(b))
            with open(args.output, "w") as out:
                json.dump(merged, out, indent=1, sort_keys=True)
                out.write("\n")
            log.info("Merged state written to %s", args.output)
            return 0

        if args.cmd == "validate-config":
            cfg.require_secrets(telegram=True, data=True)
            log.info("Configuration OK: %d symbols, secrets present (values not shown)", len(cfg.universe))
            return 0

        if args.cmd == "telegram-test":
            cfg.require_secrets(telegram=True, data=False)
            res = TelegramNotifier(cfg).send_text("✅ ICT Power of Three scanner: Telegram test message.")
            log.info("Telegram test: %s", "delivered" if res.ok else f"FAILED ({res.error})")
            return 0 if res.ok else 1

        cfg.require_secrets(telegram=(args.cmd == "scan" and not args.dry_run), data=True)
        store = StateStore(cfg.state_path)
        store.load()
        client = make_client(cfg, store, now)

        if args.cmd == "diagnose":
            out = run_diagnose(cfg, client, now)
            bad = [s for s, v in out["symbols"].items() if not v["ok"]]
            log.info("Catalogue missing: %s", out["catalogue_missing"] or "none")
            if out["catalogue_error"]:
                log.warning("Catalogue check failed: %s", out["catalogue_error"])
            for s, v in out["symbols"].items():
                log.info("%-7s %s %s", s, "OK  " if v["ok"] else "FAIL", v.get("latest_closed") or v.get("error"))
            store.set_credits_used_today(now, client.budget.daily_used)
            store.save(now)
            log.info("Credits used this run: %d (today: %d/%d)", client.budget.run_used,
                     client.budget.daily_used, cfg.daily_credit_limit)
            for s in bad:
                gh("error", f"{s} unavailable: {out['symbols'][s]['error']}")
            return 1 if bad or out["catalogue_missing"] else 0

        if args.cmd == "replay":
            sym = normalize_symbol(args.symbol)
            out = run_replay(cfg, client, sym, now)
            print(json.dumps(out, indent=1))
            return 0

        # ---- scan ----
        notifier = None if args.dry_run else TelegramNotifier(cfg)
        rep = run_scan(cfg, store, client, notifier, now, dry_run=args.dry_run)
        summary = [f"### Forex scan {now:%Y-%m-%d %H:%M} UTC" + (" (dry run)" if args.dry_run else ""),
                   f"- Symbols fetched OK: **{rep.fetched_ok}/{rep.symbols_total}**",
                   f"- New signals: **{rep.detected}** | delivered: **{rep.delivered}** | "
                   f"delivery failed: **{rep.delivery_failed}** | duplicates skipped: {rep.duplicates_skipped}",
                   f"- API credits: run **{rep.credits_used_run}**, today **{rep.credits_used_today}/{cfg.daily_credit_limit}**"]
        for s, why in rep.failed.items():
            summary.append(f"- ⚠️ {s}: {why}")
            gh("warning", f"{s} failed: {why}")
        for n in rep.notes:
            summary.append(f"- ℹ️ {n}")
        if rep.deferred:
            summary.append(f"- ⚠️ Deferred (not requested): {', '.join(rep.deferred)}")
        if rep.fatal:
            summary.append(f"- ❌ {rep.fatal}")
            gh("error", rep.fatal)
        write_summary(summary)
        log.info("Scan finished: ok=%d/%d failed=%d detected=%d delivered=%d delivery_failed=%d credits(run/today)=%d/%d",
                 rep.fetched_ok, rep.symbols_total, len(rep.failed), rep.detected, rep.delivered,
                 rep.delivery_failed, rep.credits_used_run, rep.credits_used_today)
        return rep.exit_code

    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    except StateCorruptError as exc:
        log.error("STATE FILE PROBLEM - refusing to continue (duplicate protection would be lost): %s", exc)
        gh("error", f"State file problem: {exc}")
        return 3
    except AuthenticationError as exc:
        log.error("%s", exc)
        gh("error", str(exc))
        return 1
    except (SymbolError, DataError, RuntimeError) as exc:
        log.error("Failure: %s", redact(str(exc)))
        return 1
    except Exception:  # noqa: BLE001
        log.exception("Unexpected error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
