from datetime import timedelta

import pytest

from src.signal_engine import Signal, evaluate_symbol, make_signal_id, needs_init
from src.state_store import utc_iso
from tests.helpers import BUY_BAR, SELL_BAR, bar, flat, make_cfg, random_series, ts

CFG = make_cfg()


def at(n):                       # "now" just after candle n-1 closed (candle n is forming)
    return ts(n) + timedelta(minutes=5)


def test_signal_id_is_deterministic_and_stable():
    a = make_signal_id("EURUSD", ts(60), "BUY")
    assert a == make_signal_id("EURUSD", ts(60), "BUY") == f"po3flux-v5|EURUSD|1H|{ts(60).strftime('%Y%m%dT%H%MZ')}|BUY"
    assert a != make_signal_id("EURUSD", ts(60), "SELL") != make_signal_id("GBPUSD", ts(60), "SELL")


def test_first_run_is_baseline_only_old_signals_not_sent():
    c = flat(60) + [BUY_BAR(60)] + flat(4, 61)
    ev = evaluate_symbol("EURUSD", c, None, CFG, at(65))
    assert ev.mode == "init" and ev.signals == [] and len(ev.stale_signals) == 1
    assert ev.new_state["last_processed_ts"] == utc_iso(ts(64)) and ev.new_state["initialized"]


def test_first_run_sends_signal_on_latest_closed_candle_only():
    c = flat(60) + [BUY_BAR(60)]
    ev = evaluate_symbol("EURUSD", c, None, CFG, at(61))
    assert [s.direction for s in ev.signals] == ["BUY"] and ev.signals[0].candles_ago == 0
    s = ev.signals[0]
    atr = (49 + 3.2) / 50
    assert s.entry == 8 and s.stop_loss == pytest.approx(8 - 5 * atr) and s.take_profit == pytest.approx(8 + 4.3 * atr)
    assert s.candle_ts == ts(60) and s.timeframe == "1H" and s.status == "OPEN"


def test_first_run_lookback_configurable():
    c = flat(60) + [BUY_BAR(60)] + flat(2, 61)
    ev = evaluate_symbol("EURUSD", c, None, make_cfg(first_run_send_lookback=7), at(63))
    assert len(ev.signals) == 1 and ev.signals[0].candles_ago == 2


def test_forming_candle_is_never_evaluated():
    forming = BUY_BAR(61)                                   # candle 61 opens at ts(61); now is 5 min into it
    c = flat(60) + [flat(1, 60)[0], forming]
    ev = evaluate_symbol("EURUSD", c, None, CFG, at(61))
    assert ev.closed_count == 61 and ev.signals == [] and ev.stale_signals == []
    assert ev.new_state["last_processed_ts"] == utc_iso(ts(60))


def test_incremental_run_finds_new_signal_within_lookback_and_no_repeat():
    st = evaluate_symbol("EURUSD", flat(60), None, CFG, at(60)).new_state
    c = flat(60) + [BUY_BAR(60)]
    ev = evaluate_symbol("EURUSD", c, st, CFG, at(61))
    assert ev.mode == "incremental" and ev.new_candles == 1 and len(ev.signals) == 1
    again = evaluate_symbol("EURUSD", c, ev.new_state, CFG, at(61))
    assert again.signals == [] and again.new_candles == 0 and again.new_state is None


def test_delayed_run_recovers_signal_up_to_seven_candles_old():
    st = evaluate_symbol("EURUSD", flat(60), None, CFG, at(60)).new_state
    c = flat(60) + [BUY_BAR(60)] + flat(6, 61)               # signal 6 candles before the latest closed
    ev = evaluate_symbol("EURUSD", c, st, CFG, at(67))
    assert len(ev.signals) == 1 and ev.signals[0].candles_ago == 6 and ev.stale_signals == []


def test_signal_older_than_lookback_is_stale_not_delivered():
    st = evaluate_symbol("EURUSD", flat(60), None, CFG, at(60)).new_state
    c = flat(60) + [BUY_BAR(60)] + flat(7, 61)               # 7 candles ago => outside the previous-7 window
    ev = evaluate_symbol("EURUSD", c, st, CFG, at(68))
    assert ev.signals == [] and len(ev.stale_signals) == 1 and ev.stale_signals[0].candles_ago == 7


def test_lookback_window_is_configurable():
    st = evaluate_symbol("EURUSD", flat(60), None, CFG, at(60)).new_state
    c = flat(60) + [BUY_BAR(60)] + flat(7, 61)
    ev = evaluate_symbol("EURUSD", c, st, make_cfg(lookback_candles=10), at(68))
    assert len(ev.signals) == 1


def test_signal_status_reflects_later_tp_or_sl_hit():
    st = evaluate_symbol("EURUSD", flat(60), None, CFG, at(60)).new_state
    c = flat(60) + [BUY_BAR(60), bar(61, 8, 20, 7.9, 12)]     # TP (12.49) reached on candle 61
    ev = evaluate_symbol("EURUSD", c, st, CFG, at(62))
    assert ev.signals[0].status == "TP" and ev.signals[0].status_ts == ts(61)


def test_machine_state_survives_between_runs():
    # run 1 ends while a trade is open; run 2 must see the open trade (no second entry) and then its exit
    st1 = evaluate_symbol("EURUSD", flat(60) + [BUY_BAR(60)], None, make_cfg(first_run_send_lookback=5), at(61)).new_state
    c = flat(60) + [BUY_BAR(60)] + [bar(61 + k, 8.5, 9, 8, 8.5) for k in range(20)]
    ev = evaluate_symbol("EURUSD", c, st1, CFG, at(81))
    assert ev.signals == [] and ev.stale_signals == [] and ev.new_state["machine"]["state"] == "Entry Taken"


def test_split_runs_equal_single_run():
    c = random_series(1200)
    big = make_cfg(first_run_send_lookback=10_000, lookback_candles=10_000)
    full = evaluate_symbol("EURUSD", c, None, big, at(1200))
    first = evaluate_symbol("EURUSD", c[:700], None, big, at(700))
    second = evaluate_symbol("EURUSD", c, first.new_state, big, at(1200))
    ids_full = [s.id for s in full.signals]
    ids_split = [s.id for s in first.signals] + [s.id for s in second.signals]
    assert ids_full == ids_split and len(ids_full) >= 3
    assert second.new_state["machine"] == full.new_state["machine"]


def test_checkpoint_older_than_history_triggers_reinit_baseline():
    st = {"initialized": True, "last_processed_ts": utc_iso(ts(5)), "machine": None}
    ev = evaluate_symbol("EURUSD", flat(40, 100) , st, CFG, at(141))
    assert ev.mode == "reinit" and ev.signals == []


def test_needs_init_rules():
    now = at(100)
    assert needs_init(None, CFG, now) and needs_init({}, CFG, now)
    fresh = {"initialized": True, "last_processed_ts": utc_iso(ts(90)), "machine": None}
    assert not needs_init(fresh, CFG, now)
    assert needs_init({"initialized": True, "last_processed_ts": utc_iso(ts(0) - timedelta(days=30))}, CFG, now)


def test_no_candles_no_signal_no_state_change():
    ev = evaluate_symbol("EURUSD", [], None, CFG, at(10))
    assert ev.signals == [] and ev.new_state is None


def test_signal_dict_roundtrip():
    s = evaluate_symbol("EURUSD", flat(60) + [SELL_BAR(60)], None, CFG, at(61)).signals[0]
    assert s.direction == "SELL" and Signal.from_dict(s.to_dict()).to_dict() == s.to_dict()
