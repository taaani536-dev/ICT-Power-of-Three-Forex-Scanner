"""CLI-level tests (network and Telegram mocked)."""
import json
from datetime import timedelta

import pytest

import main as cli
from src.market_data import CreditBudget, RateLimiter, TwelveDataClient
from src.symbol_mapping import FOREX_UNIVERSE
from tests.helpers import BUY_BAR, Clock, Router, flat, ts
from tests.test_scanner import series


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"STATE_PATH": str(tmp_path / "state.json"), "TWELVE_DATA_API_KEY": "KEYKEYKEY123",
                 "TELEGRAM_BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ", "TELEGRAM_CHAT_ID": "5"}.items():
        monkeypatch.setenv(k, v)
    return tmp_path


def patch_client(monkeypatch, router):
    clk = Clock()

    def make_client(cfg, store, now):
        return TwelveDataClient(cfg, CreditBudget(cfg.max_credits_per_run, cfg.daily_budget, store.credits_used_today(now)),
                                session=router, limiter=RateLimiter(8, 0, clock=clk.now, sleep=clk.sleep),
                                sleep=clk.sleep, wall=lambda: 0.0, now_fn=lambda: now)
    monkeypatch.setattr(cli, "make_client", make_client)
    return clk


def test_scan_dry_run_cli_exit_0_and_state_written(env, monkeypatch):
    now_candles = 200
    # CLI uses the real clock; build series that end just before "now"
    from datetime import datetime, timezone
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    data = {}
    for s in FOREX_UNIVERSE:
        data[s] = [type(c)(end - timedelta(hours=(now_candles - i)), c.open, c.high, c.low, c.close)
                   for i, c in enumerate(flat(now_candles))]
    patch_client(monkeypatch, Router(data))
    assert cli.main(["scan", "--dry-run"]) == 0
    st = json.loads((env / "state.json").read_text())
    assert st["api_usage"]["credits"] == 28 and st["symbols"] == {}


def test_diagnose_reports_catalogue_gaps_with_exit_1(env, monkeypatch):
    from datetime import datetime, timezone
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    data = {s: [type(c)(end - timedelta(hours=5 - i), c.open, c.high, c.low, c.close) for i, c in enumerate(flat(5))]
            for s in FOREX_UNIVERSE}
    router = Router(data)
    router.missing_from_catalogue = ("EURNZD",)
    patch_client(monkeypatch, router)
    assert cli.main(["diagnose"]) == 1
    assert len(router.calls) == 1 + 4                      # catalogue + 4 batches (29 credits)


def test_diagnose_ok(env, monkeypatch):
    from datetime import datetime, timezone
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    data = {s: [type(c)(end - timedelta(hours=5 - i), c.open, c.high, c.low, c.close) for i, c in enumerate(flat(5))]
            for s in FOREX_UNIVERSE}
    patch_client(monkeypatch, Router(data))
    assert cli.main(["diagnose"]) == 0


def test_replay_rejects_symbols_outside_universe(env, monkeypatch):
    patch_client(monkeypatch, Router({}))
    assert cli.main(["replay", "XAUUSD"]) == 1


def test_replay_outputs_dashboard_stats(env, monkeypatch, capsys):
    from datetime import datetime, timezone
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    base = flat(60) + [BUY_BAR(60)] + flat(10, 61)
    data = {"EURUSD": [type(c)(end - timedelta(hours=len(base) - i), c.open, c.high, c.low, c.close) for i, c in enumerate(base)]}
    patch_client(monkeypatch, Router(data))
    assert cli.main(["replay", "eur/usd"]) == 0
    out = capsys.readouterr().out
    j = json.loads(out[out.index("{\n"):])
    assert j["symbol"] == "EURUSD" and j["candles"] == 71
    assert j["stats"]["total_entries"] == 1 and j["trades"][0]["type"] == "Long" and j["trades"][0]["outcome"] is None


def test_merge_state_cli(tmp_path, env):
    from src.state_store import new_state
    a, b = new_state(), new_state()
    (tmp_path / "a.json").write_text(json.dumps(a))
    (tmp_path / "b.json").write_text(json.dumps(b))
    assert cli.main(["merge-state", "--ours", str(tmp_path / "a.json"), "--theirs", str(tmp_path / "b.json"),
                     "--output", str(tmp_path / "o.json")]) == 0
    assert json.loads((tmp_path / "o.json").read_text())["schema_version"] == 1
