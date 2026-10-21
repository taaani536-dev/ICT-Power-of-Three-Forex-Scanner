import logging
from datetime import timedelta

import requests

from src.signal_engine import Signal
from src.telegram_notifier import TelegramNotifier, format_message
from tests.helpers import Clock, FakeResponse, FakeSession, make_cfg, ts

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def sig(symbol="EURUSD", direction="BUY", i=60, **kw):
    base = dict(id=f"id|{symbol}|{i}|{direction}", symbol=symbol, direction=direction, strategy="ICT Power of Three | Flux Charts",
                timeframe="1H", candle_ts=ts(i), detected_at=ts(i + 1), entry=1.085432, stop_loss=1.07654321,
                take_profit=1.0930999)
    base.update(kw)
    return Signal(**base)


def ok(mid=1):
    return FakeResponse(200, {"ok": True, "result": {"message_id": mid}})


def notifier(script, **cfg):
    clk = Clock()
    sess = FakeSession(script)
    return TelegramNotifier(make_cfg(**cfg), session=sess, sleep=clk.sleep), sess, clk


def test_message_format_buy_and_sell_required_fields():
    m = format_message(sig())
    for line in ("ICT POWER OF THREE — FOREX SIGNAL", "Symbol: EURUSD", "Direction: BUY", "Timeframe: 1H",
                 "Entry: 1.08543", "Stop Loss: 1.07654", "Take Profit: 1.09310",
                 "Strategy: ICT Power of Three | Flux Charts"):
        assert line in m
    assert "Signal Candle: 2026-03-04 12:00 UTC" in m and "Delayed" not in m and "Status" not in m
    assert "Direction: SELL" in format_message(sig(direction="SELL"))


def test_jpy_pairs_use_three_decimals():
    m = format_message(sig("USDJPY", entry=150.12345, stop_loss=149.0, take_profit=151.2))
    assert "Entry: 150.123" in m and "Stop Loss: 149.000" in m


def test_delayed_and_resolved_annotations():
    m = format_message(sig(candles_ago=3, status="SL", status_ts=ts(62)))
    assert "Delayed: detected 3 candle(s)" in m and "stop-loss level was already reached" in m


def test_send_success_returns_message_id_and_payload():
    n, sess, _ = notifier([ok(55)])
    r = n.send_text("hi")
    assert r.ok and r.message_id == 55
    c = sess.calls[0]
    assert c["url"].endswith("/sendMessage") and c["json"]["chat_id"] == "42" and c["json"]["text"] == "hi"


def test_multiple_signals_sent_in_order_with_15s_gaps_but_not_after_last():
    n, sess, clk = notifier([ok(1), ok(2), ok(3)])
    out = []
    n.deliver([sig(i=60), sig("GBPUSD", i=61), sig("USDJPY", i=62)], lambda s, r: out.append((s.symbol, r.ok)))
    assert out == [("EURUSD", True), ("GBPUSD", True), ("USDJPY", True)]
    assert clk.sleeps == [15.0, 15.0]                                  # 3 messages -> 2 gaps, none trailing
    assert [c["json"]["text"].split("Symbol: ")[1].split("\n")[0] for c in sess.calls] == ["EURUSD", "GBPUSD", "USDJPY"]


def test_single_signal_has_no_delay():
    n, _, clk = notifier([ok()])
    n.deliver([sig()], lambda s, r: None)
    assert clk.sleeps == []


def test_failure_of_one_does_not_block_or_resend_others():
    err = FakeResponse(500, {"ok": False, "description": "server"})
    n, sess, _ = notifier([err, err, err, ok(9)])
    results = {}
    n.deliver([sig(i=60), sig("GBPUSD", i=61)], lambda s, r: results.__setitem__(s.symbol, r))
    assert not results["EURUSD"].ok and results["GBPUSD"].ok and len(sess.calls) == 4


def test_429_honours_retry_after_then_succeeds():
    rl = FakeResponse(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}})
    n, _, clk = notifier([rl, ok()])
    assert n.send_text("x").ok and 8.0 in clk.sleeps


def test_fatal_error_aborts_remaining_deliveries():
    bad = FakeResponse(401, {"ok": False, "description": "Unauthorized"})
    n, sess, _ = notifier([bad])
    results = []
    cont = n.deliver([sig(i=60), sig("GBPUSD", i=61)], lambda s, r: results.append(r))
    assert cont is False and len(results) == 1 and results[0].fatal and len(sess.calls) == 1


def test_network_errors_retried_with_limit():
    n, sess, _ = notifier([requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")] * 3)
    r = n.send_text("x")
    assert not r.ok and len(sess.calls) == 3 and TOKEN not in r.error


def test_token_not_leaked_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    n, _, _ = notifier([FakeResponse(400, {"ok": False, "description": f"bad {TOKEN}"})])
    r = n.send_text("x")
    assert not r.fatal is False and TOKEN not in r.error and TOKEN not in caplog.text
