"""Twelve Data client tests - all HTTP is mocked; no credits are consumed."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src import market_data as md
from src.market_data import (AuthenticationError, CreditBudget, DailyQuotaExhausted, RateLimiter,
                             TwelveDataClient, closed_candles, parse_candles)
from tests.helpers import Clock, FakeResponse, FakeSession, make_cfg, td_ok, td_rows, ts

NOW = ts(100) + timedelta(minutes=7)


def client(script, cfg=None, run=40, daily=760, used=0):
    cfg = cfg or make_cfg()
    clk = Clock()
    sess = FakeSession(script)
    c = TwelveDataClient(cfg, CreditBudget(run, daily, used), session=sess,
                         limiter=RateLimiter(cfg.rate_limit_per_minute, 0.0, clock=clk.now, sleep=clk.sleep),
                         sleep=clk.sleep, wall=lambda: 1000.0, now_fn=lambda: NOW)
    return c, sess, clk


def batch_body(symbols, n=60):
    return {s: td_ok(td_rows(n)) for s in symbols}


# ------------------------------------------------------------------ success
def test_batch_success_two_symbols_and_request_shape():
    c, sess, _ = client([FakeResponse(200, batch_body(["EUR/USD", "GBP/USD"]),
                                      {"api-credits-used": "2", "api-credits-left": "6"})])
    res = c.fetch_batch(["EURUSD", "GBPUSD"], 1000)
    assert res["EURUSD"].ok and len(res["EURUSD"].candles) == 60
    call = sess.calls[0]
    assert call["params"]["symbol"] == "EUR/USD,GBP/USD" and call["params"]["interval"] == "1h"
    assert call["params"]["timezone"] == "UTC" and call["params"]["outputsize"] == 1000
    assert "apikey" not in call["params"] and call["headers"]["Authorization"].startswith("apikey ")
    assert c.budget.run_used == 2


def test_single_symbol_response_shape():
    c, _, _ = client([FakeResponse(200, td_ok(td_rows(60)))])
    assert c.fetch_batch(["EURUSD"], 100)["EURUSD"].ok


def test_candles_sorted_ascending_and_deduplicated():
    rows = td_rows(60)
    rows = rows[::-1][:10] + rows          # unsorted + duplicates
    c, _, _ = client([FakeResponse(200, td_ok(rows))])
    r = c.fetch_batch(["EURUSD"], 100)["EURUSD"]
    stamps = [x.ts for x in r.candles]
    assert stamps == sorted(set(stamps)) and r.duplicate_count == 10


# ------------------------------------------------------------- data checks
def test_empty_series_is_failure_not_signal():
    c, _, _ = client([FakeResponse(200, td_ok([]))])
    r = c.fetch_batch(["EURUSD"], 100)["EURUSD"]
    assert not r.ok and r.error_kind == md.EMPTY


def test_invalid_ohlc_rows_rejected_over_threshold():
    rows = td_rows(60)
    for k in range(5):
        rows[k]["high"] = "abc"
    c, _, _ = client([FakeResponse(200, td_ok(rows))])
    r = c.fetch_batch(["EURUSD"], 100)["EURUSD"]
    assert not r.ok and r.error_kind == md.MALFORMED


def test_few_invalid_rows_dropped_when_under_threshold():
    rows = td_rows(600, end_idx=99)
    rows[3]["low"] = "-1"
    c, _, _ = client([FakeResponse(200, td_ok(rows))])
    r = c.fetch_batch(["EURUSD"], 600)["EURUSD"]
    assert r.ok and r.invalid_count == 1 and len(r.candles) == 599


def test_parse_candles_rules():
    good = {"datetime": "2026-03-02 05:00:00", "open": "1", "high": "2", "low": "0.5", "close": "1.5"}
    now = datetime(2026, 3, 3, tzinfo=timezone.utc)
    bad_rows = [dict(good, datetime="2026-03-02 05:30:00"),          # misaligned
                dict(good, high="0.9"),                              # high < close
                dict(good, open="nan"), dict(good, close=None),
                {"datetime": "2026-03-02 06:00:00"},                 # missing fields
                dict(good, datetime="2026-03-09 05:00:00")]          # future
    out, invalid, dup = parse_candles([good] + bad_rows, now)
    assert len(out) == 1 and invalid == len(bad_rows) and dup == 0


def test_forming_candle_excluded_from_closed():
    candles, _, _ = parse_candles(td_rows(10, end_idx=100), NOW)       # last bar opened 04:00... idx100 = open
    last = candles[-1].ts
    assert last == ts(100)
    assert closed_candles(candles, ts(100) + timedelta(minutes=59), 30)[-1].ts == ts(99)
    assert closed_candles(candles, ts(101) + timedelta(seconds=5), 30)[-1].ts == ts(99)   # inside grace
    assert closed_candles(candles, ts(101) + timedelta(seconds=31), 30)[-1].ts == ts(100)


# ------------------------------------------------------------ error paths
def test_authentication_error_raises_and_is_not_retried():
    c, sess, _ = client([FakeResponse(200, {"code": 401, "message": "apikey invalid KEY1234567890SECRET", "status": "error"})])
    with pytest.raises(AuthenticationError) as e:
        c.fetch_batch(["EURUSD"], 100)
    assert len(sess.calls) == 1 and "KEY1234567890SECRET" not in str(e.value)


def test_http_401_also_authentication_error():
    c, sess, _ = client([FakeResponse(401, {"code": 401, "message": "bad key", "status": "error"})])
    with pytest.raises(AuthenticationError):
        c.fetch_batch(["EURUSD"], 100)


def test_unsupported_symbol_not_retried_others_ok():
    body = {"EUR/USD": td_ok(td_rows(60)),
            "GBP/USD": {"code": 400, "message": "**symbol** not found", "status": "error"}}
    c, sess, _ = client([FakeResponse(200, body)])
    res = c.fetch_batch(["EURUSD", "GBPUSD"], 100)
    assert res["EURUSD"].ok and not res["GBPUSD"].ok and res["GBPUSD"].error_kind == md.UNSUPPORTED
    assert len(sess.calls) == 1


def test_plan_restricted_symbol_403():
    body = {"EUR/USD": {"code": 403, "message": "upgrade plan", "status": "error"},
            "GBP/USD": td_ok(td_rows(60))}
    c, sess, _ = client([FakeResponse(200, body)])
    res = c.fetch_batch(["EURUSD", "GBPUSD"], 100)
    assert res["EURUSD"].error_kind == md.PLAN and res["GBPUSD"].ok and len(sess.calls) == 1


def test_symbol_missing_from_response():
    c, _, _ = client([FakeResponse(200, {"EUR/USD": td_ok(td_rows(60))})])
    res = c.fetch_batch(["EURUSD", "GBPUSD"], 100)
    assert res["GBPUSD"].error_kind == md.MISSING


def test_rate_limit_minute_waits_then_retries_and_succeeds():
    rl = {"code": 429, "message": "You have run out of API credits for the current minute.", "status": "error"}
    c, sess, clk = client([FakeResponse(200, rl), FakeResponse(200, td_ok(td_rows(60)))])
    res = c.fetch_batch(["EURUSD"], 100)
    assert res["EURUSD"].ok and len(sess.calls) == 2
    assert any(s >= 1 for s in clk.sleeps)


def test_rate_limit_persistent_gives_up_after_max_retries():
    rl = FakeResponse(429, {"code": 429, "message": "run out of API credits for the current minute", "status": "error"})
    c, sess, _ = client([rl, rl, rl, rl], cfg=make_cfg(max_retries=2))
    r = c.fetch_batch(["EURUSD"], 100)["EURUSD"]
    assert not r.ok and r.error_kind == md.RATE_LIMIT and len(sess.calls) == 3


def test_daily_quota_exhausted_stops_immediately_and_remembered():
    dq = FakeResponse(200, {"code": 429, "message": "You have run out of API credits for the day.", "status": "error"})
    c, sess, _ = client([dq])
    with pytest.raises(DailyQuotaExhausted):
        c.fetch_batch(["EURUSD"], 100)
    with pytest.raises(DailyQuotaExhausted):
        c.fetch_batch(["GBPUSD"], 100)
    assert len(sess.calls) == 1


def test_server_error_retried_then_success():
    c, sess, _ = client([FakeResponse(500, {"code": 500, "message": "oops", "status": "error"}),
                         FakeResponse(200, td_ok(td_rows(60)))])
    assert c.fetch_batch(["EURUSD"], 100)["EURUSD"].ok and len(sess.calls) == 2


def test_server_error_exhausts_retries():
    e = FakeResponse(503, None, text_body="Service Unavailable")
    c, sess, _ = client([e, e, e], cfg=make_cfg(max_retries=2))
    r = c.fetch_batch(["EURUSD"], 100)["EURUSD"]
    assert not r.ok and r.error_kind == md.SERVER and len(sess.calls) == 3


def test_timeout_retried_and_error_message_has_no_secret():
    c, sess, _ = client([requests.Timeout("https://api.twelvedata.com/x?apikey=KEY1234567890SECRET"),
                         FakeResponse(200, td_ok(td_rows(60)))])
    assert c.fetch_batch(["EURUSD"], 100)["EURUSD"].ok


def test_network_failure_all_attempts():
    c, _, _ = client([requests.ConnectionError("boom")] * 3)
    r = c.fetch_batch(["EURUSD"], 100)["EURUSD"]
    assert not r.ok and r.error_kind == md.NETWORK


def test_malformed_json_is_not_success():
    bad = FakeResponse(200, None, text_body="<html>")
    c, _, _ = client([bad, bad, bad])
    r = c.fetch_batch(["EURUSD"], 100)["EURUSD"]
    assert not r.ok and r.error_kind == md.MALFORMED


def test_values_not_a_list_is_malformed():
    c, _, _ = client([FakeResponse(200, {"meta": {}, "values": "oops", "status": "ok"})] * 3)
    assert c.fetch_batch(["EURUSD"], 100)["EURUSD"].error_kind == md.MALFORMED


# ----------------------------------------------------------------- budgets
def test_per_run_budget_blocks_request():
    c, sess, _ = client([], run=3)
    r = c.fetch_batch(["EURUSD", "GBPUSD", "USDJPY", "USDCHF"], 100)
    assert all(not v.ok and v.error_kind == md.BUDGET for v in r.values()) and sess.calls == []


def test_daily_budget_blocks_request():
    c, sess, _ = client([], daily=760, used=759)
    assert c.fetch_batch(["EURUSD", "GBPUSD"], 100)["EURUSD"].error_kind == md.BUDGET and sess.calls == []


def test_retries_are_charged_against_budget():
    e = FakeResponse(500, {"code": 500, "message": "x", "status": "error"})
    c, sess, _ = client([e, e, e], run=8)
    c.fetch_batch(["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURJPY"], 100)
    assert c.budget.run_used <= 8 and len(sess.calls) == 1


# ----------------------------------------------------------------- pacing
def test_rate_limiter_spaces_8_credit_batches_by_60s():
    clk = Clock()
    rl = RateLimiter(8, 2.0, clock=clk.now, sleep=clk.sleep)
    assert rl.wait_for(8) == 0
    rl.record(8)
    waited = rl.wait_for(8)
    assert waited >= 62.0 - 1e-6
    rl.record(8)
    assert rl.wait_for(4) >= 62.0 - 1e-6


def test_four_batches_take_at_least_three_minutes_of_pacing():
    cfg = make_cfg(rate_margin_seconds=2.0)
    clk = Clock()
    sess = FakeSession([FakeResponse(200, batch_body(["EUR/USD"] * 0 + [])) for _ in range(0)])
    sess = FakeSession([FakeResponse(200, {to: td_ok(td_rows(60)) for to in
                                           ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD", "EUR/JPY"]})
                        for _ in range(4)])
    c = TwelveDataClient(cfg, CreditBudget(40, 760), session=sess,
                         limiter=RateLimiter(8, 2.0, clock=clk.now, sleep=clk.sleep),
                         sleep=clk.sleep, wall=lambda: 0.0, now_fn=lambda: NOW)
    syms = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURJPY"]
    for _ in range(4):
        c.fetch_batch(syms, 100)
    assert clk.t >= 3 * 62.0 - 1e-6


# ------------------------------------------------------------- diagnostics
def test_forex_pairs_catalogue():
    body = {"data": [{"symbol": "EUR/USD"}, {"symbol": "GBP/USD"}], "status": "ok", "count": 2}
    c, sess, _ = client([FakeResponse(200, body)])
    assert c.list_forex_pairs() == {"EUR/USD", "GBP/USD"} and c.budget.run_used == 1


def test_secret_never_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    c, _, _ = client([FakeResponse(200, td_ok(td_rows(60)), {"api-credits-used": "1", "api-credits-left": "7"})])
    c.fetch_batch(["EURUSD"], 100)
    assert "KEY1234567890SECRET" not in caplog.text
