"""Persistent state: per-symbol strategy checkpoints, signal delivery ledger, API usage.

Stored as one JSON file (committed to a dedicated git branch by the workflow).
Writes are atomic (tmp file + fsync + rename).  A *corrupt* file is a hard error
(never silently replaced), because duplicate protection would otherwise be lost.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

SCHEMA_VERSION = 1
PENDING, SENT, EXPIRED = "pending", "sent", "expired"
_RANK = {PENDING: 0, EXPIRED: 1, SENT: 2}     # merge precedence: sent > expired > pending


class StateCorruptError(Exception):
    pass


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def new_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "updated_at": None, "symbols": {},
            "signals": {}, "api_usage": {"date": None, "credits": 0}}


def validate_state(d) -> dict:
    if not isinstance(d, dict) or d.get("schema_version") != SCHEMA_VERSION:
        raise StateCorruptError("State file has missing/unsupported schema_version")
    for key, typ in (("symbols", dict), ("signals", dict), ("api_usage", dict)):
        if not isinstance(d.get(key), typ):
            raise StateCorruptError(f"State field {key!r} missing or wrong type")
    return d


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = new_state()
        self.existed = False

    # ---- persistence ----
    def load(self) -> dict:
        if not os.path.exists(self.path):
            self.data, self.existed = new_state(), False
            return self.data
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise StateCorruptError(f"Cannot read state file {self.path}: {type(exc).__name__}") from exc
        self.data, self.existed = validate_state(raw), True
        return self.data

    def save(self, now: Optional[datetime] = None) -> None:
        self.data["updated_at"] = utc_iso(now or datetime.now(timezone.utc))
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ---- symbols ----
    def symbol_state(self, symbol: str) -> Optional[dict]:
        return self.data["symbols"].get(symbol)

    def set_symbol_state(self, symbol: str, st: dict) -> None:
        self.data["symbols"][symbol] = st

    # ---- signals ledger ----
    def has_signal(self, sid: str) -> bool:
        return sid in self.data["signals"]

    def add_signal(self, record: dict) -> bool:
        """Insert a signal record; returns False (no-op) if the id already exists."""
        if record["id"] in self.data["signals"]:
            return False
        self.data["signals"][record["id"]] = record
        return True

    def pending(self) -> list[dict]:
        recs = [r for r in self.data["signals"].values() if r["status"] == PENDING]
        return sorted(recs, key=lambda r: (r["candle_ts"], r["symbol"], r["direction"], r["id"]))

    def mark_sent(self, sid: str, now: datetime, message_id=None) -> None:
        r = self.data["signals"][sid]
        r.update(status=SENT, sent_at=utc_iso(now), message_id=message_id, last_error=None)

    def mark_attempt_failed(self, sid: str, error: str) -> None:
        r = self.data["signals"][sid]
        r["attempts"] = int(r.get("attempts", 0)) + 1
        r["last_error"] = error[:300]

    def mark_expired(self, sid: str, reason: str) -> None:
        r = self.data["signals"][sid]
        r.update(status=EXPIRED, last_error=reason[:300])

    def prune(self, now: datetime, retention_days: int) -> int:
        cutoff = now - timedelta(days=retention_days)
        drop = [sid for sid, r in self.data["signals"].items()
                if r["status"] != PENDING and parse_iso(r["candle_ts"]) < cutoff]
        for sid in drop:
            del self.data["signals"][sid]
        return len(drop)

    # ---- API usage (own accounting, UTC day) ----
    def credits_used_today(self, now: datetime) -> int:
        u = self.data["api_usage"]
        return int(u.get("credits", 0)) if u.get("date") == now.strftime("%Y-%m-%d") else 0

    def set_credits_used_today(self, now: datetime, credits: int) -> None:
        self.data["api_usage"] = {"date": now.strftime("%Y-%m-%d"), "credits": int(credits)}


def merge_states(ours: dict, theirs: dict) -> dict:
    """Deterministic union used when two runs raced on the git state branch."""
    validate_state(ours)
    validate_state(theirs)
    out = copy.deepcopy(ours)
    # symbols: keep the one that processed later candles
    for sym, st in theirs["symbols"].items():
        mine = out["symbols"].get(sym)
        if mine is None or (st.get("last_processed_ts") or "") > (mine.get("last_processed_ts") or ""):
            out["symbols"][sym] = copy.deepcopy(st)
    # signals: union, strongest status wins, attempts = max
    for sid, rec in theirs["signals"].items():
        mine = out["signals"].get(sid)
        if mine is None:
            out["signals"][sid] = copy.deepcopy(rec)
            continue
        winner = rec if _RANK[rec["status"]] > _RANK[mine["status"]] else mine
        merged = copy.deepcopy(winner)
        merged["attempts"] = max(int(mine.get("attempts", 0)), int(rec.get("attempts", 0)))
        out["signals"][sid] = merged
    # api usage: same day -> max, else later day
    a, b = ours["api_usage"], theirs["api_usage"]
    if a.get("date") == b.get("date"):
        out["api_usage"] = {"date": a.get("date"), "credits": max(int(a.get("credits", 0)), int(b.get("credits", 0)))}
    elif (b.get("date") or "") > (a.get("date") or ""):
        out["api_usage"] = copy.deepcopy(b)
    out["updated_at"] = max(ours.get("updated_at") or "", theirs.get("updated_at") or "") or None
    return out
