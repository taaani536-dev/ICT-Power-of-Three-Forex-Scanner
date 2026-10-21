"""Shared synthetic-data helpers (candles whose ATR is analytically known)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from src.strategy import Candle

BASE = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)   # a Monday


def ts(i: int) -> datetime:
    return BASE + timedelta(hours=i)


def bar(i, o, h, l, c) -> Candle:
    return Candle(ts(i), o, h, l, c)


def flat(n: int, start: int = 0) -> list[Candle]:
    """o=c=10, h=10.5, l=9.5 -> TR==1 on every bar -> ATR(50)==1 from index 49."""
    return [bar(start + k, 10, 10.5, 9.5, 10) for k in range(n)]


def random_series(n: int, seed: int = 7) -> list[Candle]:
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(n):
        o = price
        step = rng.gauss(0, 0.15)
        if rng.random() < 0.06:
            step += rng.gauss(0, 2.0)
        c = o + step
        h = max(o, c) + abs(rng.gauss(0, 0.08))
        l = min(o, c) - abs(rng.gauss(0, 0.08))
        out.append(bar(i, o, h, l, c))
        price = c
    return out


def entry_key(p):
    return (p.entry_ts, p.entry_type, round(p.entry_price, 9), round(p.sl_target, 9),
            round(p.tp_target, 9), p.outcome, p.exit_ts)


# ------------------------------------------------------------ fake HTTP
class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text_body=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._text = text_body

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Scripted responses; items may be FakeResponse or an Exception to raise."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, kind, url, **kw):
        self.calls.append({"kind": kind, "url": url, **kw})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self._next("get", url, **kw)

    def post(self, url, **kw):
        return self._next("post", url, **kw)


def td_rows(n, end_idx=None, start_hour=0):
    """Provider-style rows, newest first, ending at hour index end_idx."""
    end_idx = n - 1 if end_idx is None else end_idx
    rows = []
    for i in range(end_idx, end_idx - n, -1):
        t = ts(i).strftime("%Y-%m-%d %H:%M:%S")
        rows.append({"datetime": t, "open": "10.0", "high": "10.5", "low": "9.5", "close": "10.0", "volume": "0"})
    return rows


def td_ok(rows):
    return {"meta": {"symbol": "X", "interval": "1h"}, "values": rows, "status": "ok"}


def make_cfg(**over):
    from src.config import Config
    base = dict(td_api_key="KEY1234567890SECRET", tg_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="42", backoff_base_seconds=0.0, rate_margin_seconds=0.0)
    base.update(over)
    return Config(**base)


class Clock:
    def __init__(self, t=0.0):
        self.t = t
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def rows_from(candles):
    return [{"datetime": c.ts.strftime("%Y-%m-%d %H:%M:%S"), "open": repr(c.open), "high": repr(c.high),
             "low": repr(c.low), "close": repr(c.close), "volume": "0"} for c in reversed(candles)]


BUY_BAR = lambda i: bar(i, 10, 10.2, 7.0, 8)       # bearish manipulation -> Long/BUY
SELL_BAR = lambda i: bar(i, 10, 13, 9.9, 12)       # bullish manipulation -> Short/SELL


class Router:
    """Fake Twelve Data: answers /time_series batch requests from per-symbol candle lists.
    `override[sym]` may be a dict body to return instead; `request_hook(n)` may raise/return a response."""

    def __init__(self, series, override=None, hook=None):
        self.series, self.override, self.hook = series, override or {}, hook
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("forex_pairs"):
            self.calls.append({"symbol": "CATALOGUE"})
            return FakeResponse(200, {"status": "ok", "count": len(self.series),
                                      "data": [{"symbol": f"{k[:3]}/{k[3:]}"} for k in self.series
                                               if k not in getattr(self, "missing_from_catalogue", ())]})
        self.calls.append(params)
        if self.hook:
            r = self.hook(len(self.calls), params)
            if r is not None:
                return r
        syms = params["symbol"].split(",")
        body = {}
        for p in syms:
            internal = p.replace("/", "")
            body[p] = self.override.get(internal) or td_ok(rows_from(self.series[internal]))
        return FakeResponse(200, body if len(syms) > 1 else body[syms[0]],
                            {"api-credits-used": str(len(syms)), "api-credits-left": "0"})
