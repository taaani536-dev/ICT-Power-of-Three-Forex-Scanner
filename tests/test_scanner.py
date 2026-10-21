"""End-to-end orchestration tests with mocked Twelve Data and Telegram."""
import json
from datetime import timedelta

import pytest

import main as cli
from src.market_data import CreditBudget, RateLimiter, TwelveDataClient
from src.scanner import run_scan
from src.state_store import StateStore
from src.symbol_mapping import FOREX_UNIVERSE
from src.telegram_notifier import TelegramNotifier
from tests.helpers import (BUY_BAR, SELL_BAR, Clock, FakeResponse, FakeSession, Router, bar, flat, make_cfg, ts)


def at(n):
    return ts(n) + timedelta(minutes=5)


def series(n, triggers=None):
    """28 flat series of n candles; `triggers` = {symbol: bar_fn} replace the last candle."""
    out = {}
    for s in FOREX_UNIVERSE:
        c = flat(n)
        if triggers and s in triggers:
            c[-1] = triggers[s](n - 1)
        out[s] = c
    return out


class Env:
    def __init__(self, tmp_path, tg_script=None, **cfg):
        self.cfg = make_cfg(state_path=str(tmp_path / "state.json"), **cfg)
        self.clk = Clock()
        self.tg = FakeSession(tg_script if tg_script is not None else [FakeResponse(200, {"ok": True, "result": {"message_id": i}}) for i in range(1, 60)])
        self.notifier = TelegramNotifier(self.cfg, session=self.tg, sleep=self.clk.sleep)

    def run(self, data, now, dry_run=False, router=None, notifier=True):
        store = StateStore(self.cfg.state_path)
        store.load()
        router = router or Router(data)
        client = TwelveDataClient(self.cfg, CreditBudget(self.cfg.max_credits_per_run, self.cfg.daily_budget,
                                                         store.credits_used_today(now)), session=router,
                                  limiter=RateLimiter(8, 0.0, clock=self.clk.now, sleep=self.clk.sleep),
                                  sleep=self.clk.sleep, wall=lambda: 0.0, now_fn=lambda: now)
        rep = run_scan(self.cfg, store, client, self.notifier if notifier else None, now, dry_run=dry_run)
        self.router, self.store = router, store
        return rep

    def state(self):
        return json.loads(open(self.cfg.state_path).read())


def test_first_run_establishes_baseline_uses_28_credits_and_sends_nothing(tmp_path):
    e = Env(tmp_path)
    rep = e.run(series(60), at(60))
    st = e.state()
    assert rep.fetched_ok == 28 and rep.credits_used_run == 28 and rep.delivered == 0 and e.tg.calls == []
    assert len(st["symbols"]) == 28 and all(v["initialized"] for v in st["symbols"].values())
    assert st["api_usage"]["credits"] == 28
    assert [len(c["symbol"].split(",")) for c in e.router.calls] == [8, 8, 8, 4]
    assert e.clk.t >= 3 * 60                               # pacing between the four batches


def test_new_signals_delivered_chronologically_with_gap_and_marked_sent(tmp_path):
    e = Env(tmp_path)
    e.run(series(60), at(60))
    rep = e.run(series(61, {"GBPUSD": SELL_BAR, "EURUSD": BUY_BAR}), at(61))
    assert rep.detected == 2 and rep.delivered == 2
    order = [c["json"]["text"].split("Symbol: ")[1].split("\n")[0] for c in e.tg.calls]
    assert order == ["EURUSD", "GBPUSD"]                    # same candle -> alphabetical, deterministic
    assert e.clk.sleeps.count(15.0) == 1
    sigs = e.state()["signals"]
    assert len(sigs) == 2 and all(r["status"] == "sent" and r["message_id"] for r in sigs.values())


def test_rerun_with_same_data_sends_nothing_duplicate_protection(tmp_path):
    e = Env(tmp_path)
    e.run(series(60), at(60))
    data = series(61, {"EURUSD": BUY_BAR})
    e.run(data, at(61))
    calls_before = len(e.tg.calls)
    rep = e.run(data, at(61) + timedelta(minutes=20))       # duplicated / late workflow trigger
    assert rep.detected == 0 and rep.delivered == 0 and len(e.tg.calls) == calls_before


def test_checkpoint_loss_but_ledger_kept_still_dedups_by_signal_id(tmp_path):
    e = Env(tmp_path)
    e.run(series(60), at(60))
    data = series(61, {"EURUSD": BUY_BAR})
    e.run(data, at(61))
    st = e.state()
    st["symbols"] = {}                                      # simulate lost checkpoints only
    open(e.cfg.state_path, "w").write(json.dumps(st))
    rep = e.run(data, at(61))                               # re-initialises; baseline lookback=1 sees the signal
    assert rep.duplicates_skipped == 1 and rep.delivered == 0 and len(e.tg.calls) == 1


def test_failed_delivery_stays_pending_and_only_that_one_is_retried(tmp_path):
    ok = lambda i: FakeResponse(200, {"ok": True, "result": {"message_id": i}})
    err = FakeResponse(500, {"ok": False, "description": "down"})
    e = Env(tmp_path, tg_script=[err, err, err, ok(2)])
    e.run(series(60), at(60))
    rep = e.run(series(61, {"EURUSD": BUY_BAR, "GBPUSD": SELL_BAR}), at(61))
    assert rep.delivered == 1 and rep.delivery_failed == 1 and rep.has_warnings
    recs = {r["symbol"]: r for r in e.state()["signals"].values()}
    assert recs["EURUSD"]["status"] == "pending" and recs["EURUSD"]["attempts"] == 1 and recs["GBPUSD"]["status"] == "sent"
    e.tg.script = [ok(3)]
    n_before = len(e.tg.calls)
    rep2 = e.run(series(62, {}), at(62))
    assert rep2.delivered == 1 and len(e.tg.calls) == n_before + 1
    assert "EURUSD" in e.tg.calls[-1]["json"]["text"]
    assert all(r["status"] == "sent" for r in e.state()["signals"].values())


def test_state_saved_before_delivery_so_crash_cannot_lose_signal(tmp_path):
    class Boom(Exception):
        pass

    e = Env(tmp_path, tg_script=[Boom("crash while sending")])
    e.run(series(60), at(60))
    with pytest.raises(Boom):
        e.run(series(61, {"EURUSD": BUY_BAR}), at(61))
    st = e.state()
    assert st["signals"] and next(iter(st["signals"].values()))["status"] == "pending"
    assert st["symbols"]["EURUSD"]["last_processed_ts"].endswith("Z")


def test_fatal_telegram_error_keeps_signals_pending(tmp_path):
    e = Env(tmp_path, tg_script=[FakeResponse(401, {"ok": False, "description": "Unauthorized"})])
    e.run(series(60), at(60))
    rep = e.run(series(61, {"EURUSD": BUY_BAR, "GBPUSD": SELL_BAR}), at(61))
    assert rep.fatal and rep.exit_code == 1 and len(e.tg.calls) == 1
    assert all(r["status"] == "pending" for r in e.state()["signals"].values())


def test_dry_run_sends_nothing_and_does_not_touch_checkpoints_or_signals(tmp_path):
    e = Env(tmp_path)
    e.run(series(60), at(60))
    before = e.state()
    rep = e.run(series(61, {"EURUSD": BUY_BAR}), at(61), dry_run=True, notifier=False)
    after = e.state()
    assert [s.symbol for s in rep.would_send] == ["EURUSD"] and e.tg.calls == []
    assert after["symbols"] == before["symbols"] and after["signals"] == before["signals"]
    assert after["api_usage"]["credits"] == 56            # usage IS tracked


def test_authentication_failure_stops_all_further_requests(tmp_path):
    e = Env(tmp_path)
    router = Router(series(60), hook=lambda n, p: FakeResponse(200, {"code": 401, "message": "bad key", "status": "error"}))
    rep = e.run(None, at(60), router=router)
    assert rep.exit_code == 1 and "Authentication" in rep.fatal and len(router.calls) == 1
    assert len(rep.deferred) == 28 and rep.fetched_ok == 0


def test_unsupported_symbol_is_reported_and_others_continue(tmp_path):
    e = Env(tmp_path)
    router = Router(series(60), override={"EURNZD": {"code": 400, "message": "**symbol** not found", "status": "error"}})
    rep = e.run(None, at(60), router=router)
    assert rep.fetched_ok == 27 and "EURNZD" in rep.failed and rep.exit_code == 0 and rep.has_warnings
    assert e.state()["symbols"]["EURNZD"]["consecutive_failures"] == 1
    assert "initialized" not in e.state()["symbols"]["EURNZD"]


def test_failed_symbol_checkpoint_not_advanced(tmp_path):
    e = Env(tmp_path)
    e.run(series(60), at(60))
    ck = e.state()["symbols"]["EURUSD"]["last_processed_ts"]
    router = Router(series(61), override={"EURUSD": {"code": 500, "message": "x", "status": "error"}})
    e.run(None, at(61), router=router)
    assert e.state()["symbols"]["EURUSD"]["last_processed_ts"] == ck


def test_daily_quota_exhausted_defers_remaining_symbols(tmp_path):
    e = Env(tmp_path)
    dq = FakeResponse(200, {"code": 429, "message": "You have run out of API credits for the day.", "status": "error"})
    router = Router(series(60), hook=lambda n, p: dq if n == 2 else None)
    rep = e.run(None, at(60), router=router)
    assert rep.fetched_ok == 8 and len(rep.deferred) == 20 and rep.exit_code == 1 and len(router.calls) == 2


def test_daily_budget_from_persisted_usage_limits_requests(tmp_path):
    e = Env(tmp_path)
    st = StateStore(e.cfg.state_path)
    st.load()
    st.set_credits_used_today(at(60), 757)                 # 757 + 8 > 760 budget -> blocked
    st.save()
    rep = e.run(series(60), at(60))
    assert rep.fetched_ok == 0 and rep.exit_code == 1 and len(e.router.calls) == 0


def test_stale_pending_signal_expires_without_sending(tmp_path):
    e = Env(tmp_path)
    e.run(series(60), at(60))
    st = StateStore(e.cfg.state_path)
    st.load()
    from tests.test_state_store import rec
    r = rec("old|signal", ts="2026-03-02T00:00:00Z")
    r["signal"] = {}
    st.add_signal(r)
    st.save()
    rep = e.run(series(61), at(61))
    assert rep.expired_pending == 1 and e.tg.calls == [] and e.state()["signals"]["old|signal"]["status"] == "expired"


def test_corrupt_state_makes_cli_exit_3_without_network(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("{broken")
    for k, v in {"STATE_PATH": str(p), "TWELVE_DATA_API_KEY": "k1k1k1k1", "TELEGRAM_BOT_TOKEN": "1:abcdefghijk",
                 "TELEGRAM_CHAT_ID": "5"}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["scan"]) == 3 and p.read_text() == "{broken"


def test_cli_missing_secrets_exit_2(monkeypatch):
    for k in ("TWELVE_DATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["scan"]) == 2
