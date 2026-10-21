"""Strategy tests. Expected values are derived by hand from the Pine source logic
(e.g. ATR after one wide bar = (49*1 + TR)/50), NOT from running the port."""
import pytest

from src.config import ConfigError, StrategyParams
from src.strategy import (PO3, PO3Machine, atr_series, dashboard_stats, rolling_extreme, rma,
                          run_strategy, true_range, window_start, S_ENTRY, S_DONE, S_WAIT_ACC_END,
                          S_WAIT_MANIP)
from tests.helpers import bar, entry_key, flat, random_series, ts

P = StrategyParams()


# ---------------------------------------------------------------- inputs
def test_default_inputs_match_pine_defaults():
    assert (P.algorithm_mode, P.breakout_method, P.tpsl_method, P.risk_amount) == \
        ("Small Manipulation", "Wick", "Dynamic", "Normal")
    assert (P.accumulation_length, P.accumulation_atr_mult, P.manipulation_atr_mult) == (40, 5.0, 0.6)
    assert P.sl_atr_mult == 5.0 and P.rr == 0.86 and P.atr_len == 50
    assert P.accumulation_expand_mult == 0.5 and P.max_distance_to_last_bar == 4800
    assert (P.tp_percent, P.sl_percent) == (0.3, 0.4)


@pytest.mark.parametrize("mode,length,accmult,manmult", [
    ("Small Manipulation", 40, 5, 0.6), ("Short Accumulation", 11, 2, 1.0), ("Big Manipulation", 11, 5, 1.0)])
def test_algorithm_mode_derivations(mode, length, accmult, manmult):
    p = StrategyParams(algorithm_mode=mode)
    assert (p.accumulation_length, p.accumulation_atr_mult, p.manipulation_atr_mult) == (length, accmult, manmult)


@pytest.mark.parametrize("risk,mult", [("Highest", 9.5), ("High", 6), ("Normal", 5), ("Low", 4), ("Lowest", 1.5)])
def test_risk_mapping(risk, mult):
    assert StrategyParams(risk_amount=risk).sl_atr_mult == mult


def test_invalid_input_rejected():
    with pytest.raises(ConfigError):
        StrategyParams(breakout_method="Open")


# ------------------------------------------------------------ indicators
def test_true_range_first_bar_is_high_minus_low():
    c = [bar(0, 10, 12, 9, 11), bar(1, 11, 11.5, 10.5, 11)]
    assert true_range(c) == [3, max(1.0, abs(11.5 - 11), abs(10.5 - 11))]


def test_atr_na_for_first_49_then_sma_seed_then_rma():
    c = flat(60)
    atr = atr_series(c, 50)
    assert all(a is None for a in atr[:49])
    assert atr[49] == pytest.approx(1.0)
    c2 = flat(60)
    c2[55] = bar(55, 10, 10.2, 7.0, 8)          # TR = max(3.2, .2, 3.0) wait prev close 10 -> 3.2
    atr2 = atr_series(c2, 50)
    assert atr2[55] == pytest.approx((49 * 1.0 + 3.2) / 50)
    assert atr2[56] == pytest.approx(0.98 * atr2[55] + 0.02 * max(1.0, abs(10.5 - 8), abs(9.5 - 8)))


def test_rma_matches_wilder_definition():
    vals = [float(i % 7 + 1) for i in range(120)]
    out = rma(vals, 10)
    exp = sum(vals[:10]) / 10
    assert out[9] == pytest.approx(exp)
    for i in range(10, 120):
        exp = (vals[i] + 9 * exp) / 10
        assert out[i] == pytest.approx(exp)


def test_rolling_extreme_na_until_window_full():
    out = rolling_extreme([1, 5, 3, 2, 9], 3, max)
    assert out == [None, None, 5, 5, 9]


# -------------------------------------------------------- signal logic
def _run(candles, params=P, first=0):
    return run_strategy(candles, params, first)


def test_no_signal_on_quiet_market():
    res = _run(flat(300))
    assert res.entries == []


def test_bearish_manipulation_gives_BUY_same_bar_with_dynamic_levels():
    c = flat(60) + [bar(60, 10, 10.2, 7.0, 8)]
    res = _run(c)
    assert len(res.entries) == 1
    idx, po3 = res.entries[0]
    assert idx == 60 and po3.entry_ts == ts(60) and po3.entry_type == "Long"
    atr = (49 * 1.0 + 3.2) / 50                     # hand-derived
    assert po3.manip_direction == "Bearish"
    assert po3.acc_top == pytest.approx(11.0) and po3.acc_bottom == pytest.approx(9.0)
    assert po3.entry_price == 8
    assert po3.sl_target == pytest.approx(8 - 5 * atr)
    assert po3.tp_target == pytest.approx(8 + 5 * atr * 0.86)
    assert po3.state == S_ENTRY


def test_bullish_manipulation_gives_SELL():
    c = flat(60) + [bar(60, 10, 13, 9.9, 12)]
    _, po3 = _run(c).entries[0]
    atr = (49 + 3.1) / 50
    assert po3.entry_type == "Short" and po3.manip_direction == "Bullish"
    assert po3.sl_target == pytest.approx(12 + 5 * atr)
    assert po3.tp_target == pytest.approx(12 - 5 * atr * 0.86)


def test_breakout_below_manipulation_threshold_waits_then_signals_next_bar():
    c = flat(60) + [bar(60, 10, 11.3, 9.9, 11), bar(61, 11, 12.0, 10.9, 11.5)]
    res = _run(c[:61])
    assert res.entries == [] and res.machine.po3.state == S_WAIT_MANIP
    res = _run(c)
    assert [i for i, _ in res.entries] == [61]
    assert res.entries[0][1].entry_type == "Short" and res.entries[0][1].entry_price == 11.5


def test_close_breakout_method_ignores_wicks():
    c = flat(60) + [bar(60, 10, 10.2, 7.0, 9.5)]
    assert len(_run(c).entries) == 1                                         # Wick -> signal
    assert _run(c, StrategyParams(breakout_method="Close")).entries == []     # Close 9.5 inside box


def test_accumulation_requires_range_within_atr_multiple():
    c = flat(90)
    c[30] = bar(30, 10, 10.5, 3.0, 10)     # swing bar: 40-bar range 7.5 > 5*ATR(~1.13)
    assert _run(c[:55]).machine.po3.state == "Waiting For Accumulation"      # swing inside window
    assert _run(c[:75]).machine.po3.state == S_WAIT_ACC_END                  # swing left window (idx>=71)


def test_signal_only_after_atr_available():
    c = flat(30) + [bar(30, 10, 10.2, 1.0, 2)]
    assert _run(c).entries == []


# ---------------------------------------------------------------- exits
def _long_entry():
    return flat(60) + [bar(60, 10, 10.2, 7.0, 8)]


def test_take_profit_exit_resets_and_new_po3_next_bar():
    atr = (49 + 3.2) / 50
    tp = 8 + 5 * atr * 0.86
    c = _long_entry() + [bar(61, 8, tp + 0.1, 7.9, 12)]
    m = _run(c).machine
    assert m.po3.outcome == "Take Profit" and m.po3.exit_price == pytest.approx(tp) and m.po3.state == S_DONE
    m2 = _run(c + [bar(62, 12, 12.2, 11.8, 12)]).machine
    assert m2.po3.entry_ts is None and m2.po3.state != S_DONE      # fresh PO3 created


def test_stop_loss_exit():
    atr = (49 + 3.2) / 50
    sl = 8 - 5 * atr
    c = _long_entry() + [bar(61, 8, 8.1, sl - 0.1, 3)]
    p = _run(c).machine.po3
    assert p.outcome == "Stop Loss" and p.exit_price == pytest.approx(sl)


def test_sl_wins_when_tp_and_sl_hit_on_same_bar():
    c = _long_entry() + [bar(61, 8, 20, 0.5, 8)]
    p = _run(c).machine.po3
    assert p.outcome == "Stop Loss"


def test_no_exit_checked_on_entry_bar_and_no_new_entry_while_trade_open():
    c = _long_entry() + [bar(61 + k, 8.5, 9, 8, 8.5) for k in range(100)]
    res = _run(c)
    assert len(res.entries) == 1 and res.machine.po3.state == S_ENTRY and res.machine.po3.exit_price is None


def test_fixed_tpsl_method():
    pf = StrategyParams(tpsl_method="Fixed")
    c = _long_entry()
    _, p = _run(c, pf).entries[0]
    assert p.sl_target == pytest.approx(8 * (1 - 0.004)) and p.tp_target == pytest.approx(8 * 1.003)
    q = _run(c + [bar(61, 8, 8.03, 7.99, 8)], pf).machine.po3
    assert q.outcome == "Take Profit" and q.exit_price == pytest.approx(8 * 1.003)
    q2 = _run(c + [bar(61, 8, 8.01, 7.9, 8)], pf).machine.po3
    assert q2.outcome == "Stop Loss" and q2.exit_price == pytest.approx(8 * 0.996)


# -------------------------------------------------- windowing & determinism
def test_window_gate_matches_pine_last_4800_bars():
    assert window_start(5000, P) == 200 and window_start(4800, P) == 0 and window_start(100, P) == 0
    c = _long_entry()
    assert _run(c, first=0).entries and _run(c, first=61).entries == []


def test_deterministic_same_input_same_output():
    c = random_series(1500)
    a = [entry_key(p) for _, p in _run(c).entries]
    b = [entry_key(p) for _, p in _run(c).entries]
    assert a == b and len(a) >= 3


@pytest.mark.parametrize("split", [300, 777, 1111, 1499])
def test_checkpoint_resume_equals_single_pass(split):
    """The key persistence property: serialising the machine and resuming must
    give exactly the same entries as one uninterrupted pass."""
    c = random_series(1500)
    full = [entry_key(p) for _, p in _run(c).entries]
    part1 = run_strategy(c[:split], P, 0)
    saved = part1.machine.to_dict()
    resumed = PO3Machine.from_dict(P, saved)
    part2 = run_strategy(c[:1500], P, split, resumed)
    combined = [entry_key(p) for _, p in part1.entries] + [entry_key(p) for _, p in part2.entries]
    assert combined == full


def test_po3_dict_roundtrip():
    _, p = _run(_long_entry()).entries[0]
    assert PO3.from_dict(p.to_dict()).to_dict() == p.to_dict()


def test_no_lookahead_entries_unchanged_when_future_appended():
    c = random_series(900)
    key = lambda p: (p.entry_ts, p.entry_type, p.entry_price, p.sl_target, p.tp_target)
    base = [key(p) for _, p in _run(c[:600]).entries]
    longer = [key(p) for i, p in _run(c).entries if i < 600]
    assert base == longer and len(base) >= 2


# ------------------------------------------------------------- dashboard
def test_dashboard_stats_quirks():
    win = PO3(state="Done", entry_type="Long", entry_price=100.0, exit_price=101.0)
    loss = PO3(state="Done", entry_type="Long", entry_price=100.0, exit_price=99.0)
    open_ = PO3(state="Entry Taken", entry_type="Short", entry_price=100.0)
    s = dashboard_stats([win, loss, open_])
    assert (s["total_entries"], s["wins"], s["losses"]) == (3, 1, 2)       # open trade counts as loss
    assert s["total_profit_pct"] == pytest.approx(1 / 101 * 100 - 1 / 99 * 100)   # exit price denominator
