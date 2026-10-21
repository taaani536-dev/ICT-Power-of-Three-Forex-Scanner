import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.state_store import (PENDING, SENT, EXPIRED, StateCorruptError, StateStore, merge_states, new_state)

NOW = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)


def rec(sid, status=PENDING, ts="2026-03-05T10:00:00Z", attempts=0):
    return {"id": sid, "symbol": "EURUSD", "direction": "BUY", "candle_ts": ts, "detected_at": ts,
            "status": status, "attempts": attempts, "signal": {}}


def test_missing_file_gives_fresh_state(tmp_path):
    s = StateStore(str(tmp_path / "s.json"))
    s.load()
    assert s.data == new_state() and not s.existed


def test_save_and_reload_roundtrip_atomic(tmp_path):
    p = tmp_path / "d" / "s.json"
    s = StateStore(str(p))
    s.load()
    s.set_symbol_state("EURUSD", {"last_processed_ts": "2026-03-05T10:00:00Z"})
    s.add_signal(rec("a"))
    s.save(NOW)
    assert [f.name for f in p.parent.iterdir()] == ["s.json"]          # no temp leftovers
    s2 = StateStore(str(p))
    s2.load()
    assert s2.existed and s2.data["signals"]["a"]["status"] == PENDING and s2.data["updated_at"] == "2026-03-05T12:00:00Z"


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"schema_version": 99}), ""])
def test_corrupt_state_is_hard_error_and_file_untouched(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_text(content)
    with pytest.raises(StateCorruptError):
        StateStore(str(p)).load()
    assert p.read_text() == content


def test_duplicate_add_is_noop():
    s = StateStore("x")
    assert s.add_signal(rec("a")) and not s.add_signal(rec("a", status=SENT))
    assert s.data["signals"]["a"]["status"] == PENDING


def test_status_transitions_and_pending_order():
    s = StateStore("x")
    s.add_signal(rec("b", ts="2026-03-05T09:00:00Z"))
    s.add_signal(rec("a", ts="2026-03-05T08:00:00Z"))
    assert [r["id"] for r in s.pending()] == ["a", "b"]
    s.mark_attempt_failed("a", "boom")
    assert s.data["signals"]["a"]["attempts"] == 1 and s.data["signals"]["a"]["status"] == PENDING
    s.mark_sent("a", NOW, 77)
    s.mark_expired("b", "old")
    assert s.pending() == [] and s.data["signals"]["a"]["message_id"] == 77 and s.data["signals"]["b"]["status"] == EXPIRED


def test_prune_keeps_pending_and_recent():
    s = StateStore("x")
    old = (NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s.add_signal(rec("old_sent", SENT, old))
    s.add_signal(rec("old_pending", PENDING, old))
    s.add_signal(rec("new_sent", SENT))
    assert s.prune(NOW, 30) == 1 and set(s.data["signals"]) == {"old_pending", "new_sent"}


def test_api_usage_resets_on_new_day():
    s = StateStore("x")
    s.set_credits_used_today(NOW, 100)
    assert s.credits_used_today(NOW) == 100 and s.credits_used_today(NOW + timedelta(days=1)) == 0


def test_merge_union_status_precedence_and_checkpoints():
    a, b = new_state(), new_state()
    a["signals"]["x"] = rec("x", PENDING, attempts=1)
    b["signals"]["x"] = rec("x", SENT, attempts=0)
    a["signals"]["only_a"] = rec("only_a")
    b["signals"]["only_b"] = rec("only_b")
    a["symbols"]["EURUSD"] = {"last_processed_ts": "2026-03-05T10:00:00Z", "v": "a"}
    b["symbols"]["EURUSD"] = {"last_processed_ts": "2026-03-05T11:00:00Z", "v": "b"}
    a["symbols"]["GBPUSD"] = {"last_processed_ts": "2026-03-05T12:00:00Z", "v": "a"}
    a["api_usage"] = {"date": "2026-03-05", "credits": 100}
    b["api_usage"] = {"date": "2026-03-05", "credits": 130}
    m = merge_states(a, b)
    assert m["signals"]["x"]["status"] == SENT and m["signals"]["x"]["attempts"] == 1
    assert set(m["signals"]) == {"x", "only_a", "only_b"}
    assert m["symbols"]["EURUSD"]["v"] == "b" and m["symbols"]["GBPUSD"]["v"] == "a"
    assert m["api_usage"]["credits"] == 130
    assert merge_states(a, b)["signals"] == merge_states(b, a)["signals"]


def test_merge_rejects_invalid_state():
    with pytest.raises(StateCorruptError):
        merge_states(new_state(), {"schema_version": 5})
