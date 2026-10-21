"""Telegram delivery: formatting, retries, 15 s spacing, secret-safe errors."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from .config import Config, TG_BASE_URL
from .logging_config import redact
from .signal_engine import Signal
from .state_store import utc_iso
from .symbol_mapping import price_decimals

log = logging.getLogger("telegram")

FATAL_CODES = {400, 401, 403, 404}     # bad token / chat id / blocked bot: retrying cannot help


def format_message(sig: Signal) -> str:
    d = price_decimals(sig.symbol)
    lines = [
        "ICT POWER OF THREE — FOREX SIGNAL", "",
        f"Symbol: {sig.symbol}",
        f"Direction: {sig.direction}",
        f"Timeframe: {sig.timeframe}",
        f"Signal Candle: {sig.candle_ts.strftime('%Y-%m-%d %H:%M')} UTC (opened; signal confirmed at its close)",
        "",
        f"Entry: {sig.entry:.{d}f} (close of signal candle)",
        f"Stop Loss: {sig.stop_loss:.{d}f}",
        f"Take Profit: {sig.take_profit:.{d}f}",
    ]
    extra = []
    if sig.candles_ago > 0:
        extra.append(f"Delayed: detected {sig.candles_ago} candle(s) after the signal candle")
    if sig.status in ("TP", "SL") and sig.status_ts:
        word = "take-profit" if sig.status == "TP" else "stop-loss"
        extra.append(f"Status: the indicator's {word} level was already reached on candle "
                     f"{sig.status_ts.strftime('%Y-%m-%d %H:%M')} UTC")
    if extra:
        lines += [""] + extra
    lines += ["", f"Strategy: {sig.strategy}"]
    return "\n".join(lines)


@dataclass
class SendResult:
    ok: bool
    message_id: Optional[int] = None
    error: str = ""
    fatal: bool = False


class TelegramNotifier:
    def __init__(self, cfg: Config, session=None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.sleep = sleep

    def send_text(self, text: str) -> SendResult:
        url = f"{TG_BASE_URL}/bot{self.cfg.tg_token}/sendMessage"
        payload = {"chat_id": self.cfg.tg_chat_id, "text": text, "disable_web_page_preview": True}
        last_err = ""
        for attempt in range(self.cfg.telegram_max_retries):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_err = f"network error {type(exc).__name__}"
                log.warning("Telegram %s (attempt %d)", last_err, attempt + 1)
                self.sleep(min(2.0 * (2 ** attempt), 20.0))
                continue
            except requests.RequestException as exc:
                return SendResult(False, error=redact(f"{type(exc).__name__}", [self.cfg.tg_token]), fatal=False)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 200 and isinstance(body, dict) and body.get("ok"):
                return SendResult(True, message_id=(body.get("result") or {}).get("message_id"))
            desc = redact(str(body.get("description", "")), [self.cfg.tg_token, self.cfg.tg_chat_id]) if isinstance(body, dict) else ""
            if resp.status_code == 429:
                retry_after = 5.0
                if isinstance(body, dict):
                    retry_after = float((body.get("parameters") or {}).get("retry_after", 5))
                last_err = f"rate limited (retry_after={retry_after:.0f}s)"
                log.warning("Telegram %s", last_err)
                if retry_after > 60:
                    return SendResult(False, error=last_err)
                self.sleep(retry_after + 1)
                continue
            if resp.status_code in FATAL_CODES:
                return SendResult(False, error=f"HTTP {resp.status_code}: {desc}", fatal=True)
            last_err = f"HTTP {resp.status_code}: {desc}"
            log.warning("Telegram transient failure %s (attempt %d)", last_err, attempt + 1)
            self.sleep(min(2.0 * (2 ** attempt), 20.0))
        return SendResult(False, error=last_err or "unknown error")

    def deliver(self, signals: list[Signal], on_result: Callable[[Signal, SendResult], None]) -> bool:
        """Send chronologically with a fixed gap BETWEEN messages (none after the last).
        Returns False if a fatal error aborted the remaining deliveries."""
        for i, sig in enumerate(signals):
            if i > 0:
                self.sleep(self.cfg.telegram_interval_seconds)
            result = self.send_text(format_message(sig))
            on_result(sig, result)
            if result.ok:
                log.info("Telegram delivered %s", sig.id)
            else:
                log.error("Telegram delivery FAILED for %s: %s", sig.id, result.error)
                if result.fatal:
                    log.error("Fatal Telegram error - aborting remaining deliveries this run")
                    return False
        return True
