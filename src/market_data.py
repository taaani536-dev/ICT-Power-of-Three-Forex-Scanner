"""Twelve Data market-data client: batching, pacing, quota control, validation.

Documented facts this module relies on (Twelve Data docs / support, checked
during development):
  * /time_series costs 1 API credit PER SYMBOL; comma-separated symbols in one
    request ("batch") are charged per symbol, not once per request.
  * Credits reset every minute; Basic plan = 8 credits/min, 800/day.
  * Exceeding credits -> error code 429.  Responses carry the headers
    ``api-credits-used`` and ``api-credits-left``.
  * outputsize 1..5000; ``timezone=UTC``; default order is newest first.
  * Auth via ``Authorization: apikey <key>`` header (keeps the key out of URLs
    and therefore out of exception messages / logs).
Live behaviour has NOT been verified in this repository's test environment.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests

from .config import Config, PROVIDER_INTERVAL, TD_BASE_URL
from .logging_config import redact
from .strategy import Candle
from .symbol_mapping import from_provider_symbol, to_provider_symbol

log = logging.getLogger("market_data")

# error kinds
AUTH, DAILY_QUOTA, RATE_LIMIT, UNSUPPORTED, PLAN, BAD_REQUEST = (
    "auth", "daily_quota", "rate_limit", "unsupported", "plan", "bad_request")
SERVER, NETWORK, MALFORMED, EMPTY, BUDGET, MISSING = (
    "server", "network", "malformed", "empty", "budget", "missing")


class DataError(Exception):
    pass


class AuthenticationError(DataError):
    """Invalid API key - never retried."""


class DailyQuotaExhausted(DataError):
    """Daily credits exhausted - all further requests must stop."""


@dataclass
class FetchResult:
    symbol: str
    ok: bool
    candles: list[Candle] = field(default_factory=list)   # valid, sorted, unique (may include forming candle)
    error_kind: Optional[str] = None
    message: str = ""
    raw_count: int = 0
    invalid_count: int = 0
    duplicate_count: int = 0


# ---------------------------------------------------------------- budgeting
class CreditBudget:
    def __init__(self, run_limit: int, daily_budget: int, daily_used: int = 0):
        self.run_limit = run_limit
        self.daily_budget = daily_budget
        self.daily_used = daily_used
        self.run_used = 0

    def can_spend(self, n: int) -> bool:
        return self.run_used + n <= self.run_limit and self.daily_used + n <= self.daily_budget

    def spend(self, n: int) -> None:
        self.run_used += n
        self.daily_used += n

    def reason_blocked(self, n: int) -> str:
        if self.run_used + n > self.run_limit:
            return f"per-run credit budget exhausted ({self.run_used}/{self.run_limit})"
        return f"daily credit budget exhausted ({self.daily_used}/{self.daily_budget})"


class RateLimiter:
    """Conservative rolling 60 s window (stricter than the provider's per-minute reset)."""

    def __init__(self, per_minute: int, margin: float,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.per_minute, self.margin, self.clock, self.sleep = per_minute, margin, clock, sleep
        self.events: deque[tuple[float, int]] = deque()

    def _purge(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= 60.0 + self.margin:
            self.events.popleft()

    def wait_for(self, credits: int) -> float:
        waited = 0.0
        while True:
            now = self.clock()
            self._purge(now)
            used = sum(c for _, c in self.events)
            if used + credits <= self.per_minute:
                return waited
            delay = max(self.events[0][0] + 60.0 + self.margin - now, 0.1)
            log.info("Rate limiter: sleeping %.1fs (used %d/%d credits in window)", delay, used, self.per_minute)
            self.sleep(delay)
            waited += delay

    def record(self, credits: int) -> None:
        self.events.append((self.clock(), credits))


# --------------------------------------------------------------- validation
def _parse_dt(raw: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
    return None


def parse_candles(values, now: datetime) -> tuple[list[Candle], int, int]:
    """Validate raw provider rows -> (sorted unique valid candles, invalid_count, duplicate_count)."""
    good: dict[datetime, Candle] = {}
    invalid = dup = 0
    for row in values:
        try:
            ts = _parse_dt(row["datetime"])
            o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        if ts is None or ts.minute != 0 or ts.second != 0 or ts > now:
            invalid += 1
            continue
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            invalid += 1
            continue
        eps = 1e-9 * max(h, 1.0)
        if h < l - eps or h < max(o, c) - eps or l > min(o, c) + eps:
            invalid += 1
            continue
        if ts in good:
            dup += 1
        good[ts] = Candle(ts, o, h, l, c)       # last occurrence wins, deterministic
    return [good[k] for k in sorted(good)], invalid, dup


def closed_candles(candles: list[Candle], now: datetime, grace_seconds: int = 30) -> list[Candle]:
    """Drop the still-forming candle: a 1h candle is closed only once open+1h(+grace) <= now."""
    limit = now - timedelta(hours=1, seconds=grace_seconds)
    return [c for c in candles if c.ts <= limit]


def market_expected_open(now: datetime) -> bool:
    """Rough FX session check (UTC) used ONLY to decide whether stale data deserves a warning."""
    wd, hr = now.weekday(), now.hour + now.minute / 60
    if wd == 5:
        return False
    if wd == 4 and hr >= 21:
        return False
    if wd == 6 and hr < 22.5:
        return False
    return True


# ------------------------------------------------------------------- client
class TwelveDataClient:
    def __init__(self, cfg: Config, budget: CreditBudget, session=None,
                 limiter: Optional[RateLimiter] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 wall: Callable[[], float] = time.time,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cfg, self.budget = cfg, budget
        self.session = session or requests.Session()
        self.sleep, self.wall, self.now_fn = sleep, wall, now_fn
        self.limiter = limiter or RateLimiter(cfg.rate_limit_per_minute, cfg.rate_margin_seconds, sleep=sleep)
        self.last_headers: dict[str, str] = {}
        self.daily_exhausted = False

    def _red(self, text: str) -> str:
        return redact(text, [self.cfg.td_api_key])

    # ---- low level ----
    def _get(self, path: str, params: dict) -> tuple[int, object, dict]:
        url = f"{TD_BASE_URL}/{path}"
        headers = {"Authorization": f"apikey {self.cfg.td_api_key}", "Accept": "application/json"}
        resp = self.session.get(url, params=params, headers=headers, timeout=self.cfg.http_timeout)
        hdrs = {k.lower(): v for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
        self.last_headers = hdrs
        if "api-credits-used" in hdrs or "api-credits-left" in hdrs:
            log.info("API credits header: used=%s left=%s", hdrs.get("api-credits-used"), hdrs.get("api-credits-left"))
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body, hdrs

    def _backoff(self, attempt: int) -> None:
        delay = min(self.cfg.backoff_base_seconds * (2 ** attempt), self.cfg.backoff_max_seconds)
        log.info("Backing off %.1fs before retry", delay)
        self.sleep(delay)

    def _sleep_to_next_minute(self) -> None:
        delay = min(61.0 - (self.wall() % 60.0) + self.cfg.rate_margin_seconds, 70.0)
        log.warning("Per-minute credit limit hit; waiting %.1fs for reset", delay)
        self.sleep(max(delay, 1.0))

    @staticmethod
    def _is_daily(message: str) -> bool:
        m = (message or "").lower()
        return any(k in m for k in ("for the day", "daily", "per day", "today"))

    # ---- time series (batch) ----
    def fetch_batch(self, symbols: list[str], outputsize: int) -> dict[str, FetchResult]:
        """Fetch 1h candles for internal symbols in ONE request. Never raises for per-symbol
        problems; raises AuthenticationError / DailyQuotaExhausted for fatal conditions."""
        results: dict[str, FetchResult] = {}
        pending = list(symbols)
        attempt = 0
        while pending:
            n = len(pending)
            if self.daily_exhausted:
                raise DailyQuotaExhausted("Daily credits exhausted earlier in this run")
            if not self.budget.can_spend(n):
                reason = self.budget.reason_blocked(n)
                log.error("Not requesting %s: %s", pending, reason)
                for s in pending:
                    results[s] = FetchResult(s, False, error_kind=BUDGET, message=reason)
                break
            self.limiter.wait_for(n)
            self.budget.spend(n)
            self.limiter.record(n)
            params = {"symbol": ",".join(to_provider_symbol(s) for s in pending),
                      "interval": PROVIDER_INTERVAL, "outputsize": outputsize,
                      "timezone": "UTC", "order": "desc", "format": "JSON"}
            transient: list[str] = []
            wait_minute = False
            try:
                http_status, body, _ = self._get("time_series", params)
            except (requests.Timeout, requests.ConnectionError) as exc:
                log.warning("Network error (%s) for batch of %d symbols", type(exc).__name__, n)
                transient, kind_msg = list(pending), (NETWORK, f"{type(exc).__name__}")
                self._finish_attempt(results, pending, transient, kind_msg, attempt)
                pending = [s for s in pending if s not in results]
                attempt += 1
                if pending:
                    self._backoff(attempt - 1)
                continue
            except requests.RequestException as exc:
                msg = self._red(f"{type(exc).__name__}: {exc}")
                for s in pending:
                    results[s] = FetchResult(s, False, error_kind=NETWORK, message=msg)
                break

            # ---- request-level errors ----
            req_err = self._request_level_error(body, http_status, n)
            if req_err:
                code, message = req_err
                if code == 401:
                    raise AuthenticationError(f"Twelve Data rejected the API key (401): {self._red(message)}")
                if code == 429:
                    if self._is_daily(message):
                        self.daily_exhausted = True
                        raise DailyQuotaExhausted(self._red(message))
                    wait_minute, transient, kind_msg = True, list(pending), (RATE_LIMIT, self._red(message))
                elif code >= 500:
                    transient, kind_msg = list(pending), (SERVER, f"HTTP/API {code}: {self._red(message)}")
                elif code == 403:
                    for s in pending:
                        results[s] = FetchResult(s, False, error_kind=PLAN, message=self._red(message))
                    break
                elif code in (400, 404, 414):
                    for s in pending:
                        results[s] = FetchResult(s, False, error_kind=UNSUPPORTED if code != 414 else BAD_REQUEST,
                                                 message=self._red(message))
                    break
                else:
                    transient, kind_msg = list(pending), (SERVER, f"Unexpected code {code}: {self._red(message)}")
            elif body is None or not isinstance(body, dict):
                transient, kind_msg = list(pending), (MALFORMED, "Response was not valid JSON object")
            else:
                transient, kind_msg = self._consume_body(body, pending, results, outputsize)
                if any(results[s].error_kind == RATE_LIMIT for s in transient if s in results):
                    pass
                wait_minute = bool(transient) and kind_msg[0] == RATE_LIMIT
            self._finish_attempt(results, pending, transient, kind_msg, attempt)
            pending = [s for s in pending if s not in results]
            attempt += 1
            if pending:
                if wait_minute:
                    self._sleep_to_next_minute()
                else:
                    self._backoff(attempt - 1)
        return results

    def _finish_attempt(self, results, pending, transient, kind_msg, attempt) -> None:
        """Transient symbols stay pending until retries are exhausted, then become failures."""
        if not transient:
            return
        if attempt >= self.cfg.max_retries:
            for s in transient:
                if s not in results:
                    results[s] = FetchResult(s, False, error_kind=kind_msg[0],
                                             message=f"{kind_msg[1]} (after {attempt + 1} attempts)")

    @staticmethod
    def _request_level_error(body, http_status: int, n: int):
        if isinstance(body, dict) and body.get("status") == "error" and "code" in body and "meta" not in body:
            try:
                return int(body["code"]), str(body.get("message", ""))
            except (TypeError, ValueError):
                return 500, str(body.get("message", ""))
        if http_status >= 400:
            msg = str(body.get("message", "")) if isinstance(body, dict) else f"HTTP {http_status}"
            return http_status, msg
        return None

    def _consume_body(self, body: dict, pending: list[str], results: dict, outputsize: int):
        """Parse a 2xx body; fill `results`; return (transient_symbols, (kind,msg))."""
        now = self.now_fn()
        if len(pending) == 1 and ("values" in body or "meta" in body):
            entries = {pending[0]: body}
        else:
            entries = {}
            for key, val in body.items():
                try:
                    entries[from_provider_symbol(key)] = val
                except ValueError:
                    log.warning("Ignoring unexpected symbol key in response: %r", key)
        transient: list[str] = []
        kind_msg = (SERVER, "")
        for s in pending:
            entry = entries.get(s)
            if entry is None:
                results[s] = FetchResult(s, False, error_kind=MISSING, message="Symbol missing from response")
                continue
            if not isinstance(entry, dict):
                transient.append(s)
                kind_msg = (MALFORMED, "Symbol entry malformed")
                continue
            if entry.get("status") == "error" or ("code" in entry and "values" not in entry):
                code = entry.get("code")
                msg = self._red(str(entry.get("message", "")))
                try:
                    code = int(code)
                except (TypeError, ValueError):
                    code = 500
                if code == 429:
                    if self._is_daily(msg):
                        self.daily_exhausted = True
                        raise DailyQuotaExhausted(msg)
                    transient.append(s)
                    kind_msg = (RATE_LIMIT, msg)
                elif code >= 500:
                    transient.append(s)
                    kind_msg = (SERVER, msg)
                elif code == 401:
                    raise AuthenticationError(f"Twelve Data rejected the API key (401): {msg}")
                elif code == 403:
                    results[s] = FetchResult(s, False, error_kind=PLAN, message=msg)
                else:
                    results[s] = FetchResult(s, False, error_kind=UNSUPPORTED, message=msg)
                continue
            values = entry.get("values")
            if not isinstance(values, list):
                transient.append(s)
                kind_msg = (MALFORMED, "'values' missing or not a list")
                continue
            if not values:
                results[s] = FetchResult(s, False, error_kind=EMPTY, message="Empty time series")
                continue
            candles, invalid, dup = parse_candles(values, now)
            ratio = invalid / len(values)
            if not candles or ratio > self.cfg.max_invalid_candle_ratio:
                results[s] = FetchResult(s, False, error_kind=MALFORMED, raw_count=len(values),
                                         invalid_count=invalid,
                                         message=f"{invalid}/{len(values)} invalid candles (ratio {ratio:.3f})")
                continue
            if invalid or dup:
                log.warning("%s: dropped %d invalid and %d duplicate candles of %d", s, invalid, dup, len(values))
            results[s] = FetchResult(s, True, candles=candles, raw_count=len(values),
                                     invalid_count=invalid, duplicate_count=dup)
        return transient, kind_msg

    # ---- reference data (diagnostics) ----
    def list_forex_pairs(self) -> set[str]:
        """Catalogue of provider forex symbols (1 credit)."""
        if not self.budget.can_spend(1):
            raise DataError(self.budget.reason_blocked(1))
        self.limiter.wait_for(1)
        self.budget.spend(1)
        self.limiter.record(1)
        status, body, _ = self._get("forex_pairs", {})
        err = self._request_level_error(body, status, 1)
        if err:
            if err[0] == 401:
                raise AuthenticationError("Twelve Data rejected the API key (401)")
            raise DataError(f"forex_pairs failed: {err[0]} {self._red(err[1])}")
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise DataError("forex_pairs returned an unexpected payload")
        return {str(d.get("symbol", "")).upper() for d in body["data"] if isinstance(d, dict)}
