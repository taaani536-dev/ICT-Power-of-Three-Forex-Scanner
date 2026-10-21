"""Symbol universe and provider symbol mapping.

Internal strategy symbols (``EURUSD``) are kept strictly separate from
Twelve Data provider symbols (``EUR/USD``).  The universe is a fixed
whitelist of exactly 28 Forex pairs; nothing outside it can be scanned.
"""
from __future__ import annotations

import re

FOREX_UNIVERSE: tuple[str, ...] = (
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURJPY", "GBPJPY", "EURGBP", "EURCHF", "EURCAD", "GBPCAD", "GBPCHF",
    "GBPAUD", "GBPNZD", "AUDJPY", "AUDCAD", "AUDNZD", "AUDCHF", "NZDJPY",
    "NZDCAD", "NZDCHF", "CADJPY", "CADCHF", "CHFJPY", "EURAUD", "EURNZD",
)

ALLOWED_CURRENCIES = frozenset({"EUR", "GBP", "USD", "JPY", "CHF", "CAD", "AUD", "NZD"})

# Explicitly forbidden (defence in depth; the whitelist already excludes them)
FORBIDDEN_FRAGMENTS = ("XAU", "XAG", "BTC", "ETH", "NAS", "US100", "SPX", "US30", "DJI")


class SymbolError(ValueError):
    """Raised for symbols outside the configured Forex universe."""


def normalize_symbol(raw: str) -> str:
    """'eur/usd', 'EUR-USD', 'EURUSD' -> 'EURUSD' (must be in the universe)."""
    if not isinstance(raw, str):
        raise SymbolError(f"Symbol must be a string, got {type(raw).__name__}")
    cleaned = re.sub(r"[\s/_\-]", "", raw).upper()
    if any(f in cleaned for f in FORBIDDEN_FRAGMENTS):
        raise SymbolError(f"Symbol {raw!r} is explicitly excluded from this project")
    if cleaned not in FOREX_UNIVERSE:
        raise SymbolError(f"Symbol {raw!r} is not in the 28-pair Forex universe")
    return cleaned


def to_provider_symbol(symbol: str) -> str:
    """'EURUSD' -> 'EUR/USD' (Twelve Data format)."""
    s = normalize_symbol(symbol)
    return f"{s[:3]}/{s[3:]}"


def from_provider_symbol(provider_symbol: str) -> str:
    """'EUR/USD' -> 'EURUSD'."""
    return normalize_symbol(provider_symbol)


def price_decimals(symbol: str) -> int:
    """Display precision: 3 for JPY-quoted pairs, otherwise 5."""
    return 3 if normalize_symbol(symbol).endswith("JPY") else 5


def validate_universe(universe: tuple[str, ...] = FOREX_UNIVERSE) -> None:
    if len(universe) != 28 or len(set(universe)) != 28:
        raise SymbolError("Universe must contain exactly 28 unique pairs")
    for s in universe:
        if len(s) != 6 or s[:3] not in ALLOWED_CURRENCIES or s[3:] not in ALLOWED_CURRENCIES:
            raise SymbolError(f"Invalid pair in universe: {s}")
        if s[:3] == s[3:]:
            raise SymbolError(f"Degenerate pair: {s}")


def chunk(symbols: list[str], size: int) -> list[list[str]]:
    """Split into logical batches (default layout: 8/8/8/4)."""
    if size < 1:
        raise ValueError("batch size must be >= 1")
    return [symbols[i:i + size] for i in range(0, len(symbols), size)]
