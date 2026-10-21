"""Central configuration.

Secrets come ONLY from environment variables / GitHub Secrets.
Strategy inputs default to the exact defaults of the original Pine Script.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .symbol_mapping import FOREX_UNIVERSE, validate_universe

STRATEGY_NAME = "ICT Power of Three | Flux Charts"
STRATEGY_ID = "po3flux-v5"
TIMEFRAME = "1H"
PROVIDER_INTERVAL = "1h"
TD_BASE_URL = "https://api.twelvedata.com"
TG_BASE_URL = "https://api.telegram.org"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class StrategyParams:
    """Pine inputs/constants with original defaults."""
    algorithm_mode: str = "Small Manipulation"   # input.string default
    breakout_method: str = "Wick"                # input.string default
    tpsl_method: str = "Dynamic"                 # input.string default
    risk_amount: str = "Normal"                  # input.string default
    custom_sl_atr_mult: float = 6.5
    tp_percent: float = 0.3                      # Fixed method only
    sl_percent: float = 0.4                      # Fixed method only
    rr: float = 0.86
    atr_len: int = 50
    accumulation_expand_mult: float = 0.5
    max_distance_to_last_bar: int = 4800

    def __post_init__(self):
        if self.algorithm_mode not in ("Small Manipulation", "Short Accumulation", "Big Manipulation"):
            raise ConfigError(f"Bad algorithm_mode {self.algorithm_mode!r}")
        if self.breakout_method not in ("Close", "Wick"):
            raise ConfigError(f"Bad breakout_method {self.breakout_method!r}")
        if self.tpsl_method not in ("Dynamic", "Fixed"):
            raise ConfigError(f"Bad tpsl_method {self.tpsl_method!r}")
        if self.risk_amount not in ("Highest", "High", "Normal", "Low", "Lowest"):
            raise ConfigError(f"Bad risk_amount {self.risk_amount!r}")

    # --- derived exactly as in the Pine script ---
    @property
    def accumulation_length(self) -> int:
        return 40 if self.algorithm_mode == "Small Manipulation" else 11

    @property
    def accumulation_atr_mult(self) -> float:
        return 2.0 if self.algorithm_mode == "Short Accumulation" else 5.0

    @property
    def manipulation_atr_mult(self) -> float:
        return 0.6 if self.algorithm_mode == "Small Manipulation" else 1.0

    @property
    def sl_atr_mult(self) -> float:
        return {"Highest": 9.5, "High": 6.0, "Normal": 5.0, "Low": 4.0, "Lowest": 1.5}.get(
            self.risk_amount, self.custom_sl_atr_mult)


def _f(env, name, default, cast=float):
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name}={raw!r} is not a valid {cast.__name__}") from exc


@dataclass(frozen=True)
class Config:
    universe: tuple[str, ...] = FOREX_UNIVERSE
    strategy: StrategyParams = field(default_factory=StrategyParams)

    # secrets (never logged)
    td_api_key: str = field(default="", repr=False)
    tg_token: str = field(default="", repr=False)
    tg_chat_id: str = field(default="", repr=False)

    state_path: str = "state/state.json"
    log_level: str = "INFO"

    # signal recovery
    lookback_candles: int = 7
    first_run_send_lookback: int = 1
    pending_max_age_hours: float = 24.0
    pending_max_attempts: int = 24
    signal_retention_days: int = 30
    candle_close_grace_seconds: int = 30
    stale_warning_hours: float = 3.0

    # Twelve Data
    batch_size: int = 8
    rate_limit_per_minute: int = 8
    rate_margin_seconds: float = 2.0
    daily_credit_limit: int = 800
    daily_safety_margin: int = 40
    max_credits_per_run: int = 40
    incremental_outputsize: int = 1000
    init_outputsize: int = 5000
    incremental_max_gap_hours: float = 500.0
    max_retries: int = 2
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 30.0
    http_timeout: float = 90.0
    max_invalid_candle_ratio: float = 0.01

    # Telegram
    telegram_interval_seconds: float = 15.0
    telegram_max_retries: int = 3

    @property
    def daily_budget(self) -> int:
        return self.daily_credit_limit - self.daily_safety_margin

    def require_secrets(self, *, telegram: bool = True, data: bool = True) -> None:
        missing = []
        if data and not self.td_api_key:
            missing.append("TWELVE_DATA_API_KEY")
        if telegram and not self.tg_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if telegram and not self.tg_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    def validate(self) -> None:
        validate_universe(self.universe)
        if self.batch_size < 1 or self.batch_size > self.rate_limit_per_minute:
            raise ConfigError("batch_size must be between 1 and rate_limit_per_minute")
        if not (1 <= self.incremental_outputsize <= 5000 and 1 <= self.init_outputsize <= 5000):
            raise ConfigError("outputsize values must be within 1..5000 (Twelve Data limit)")
        if self.init_outputsize < self.strategy.max_distance_to_last_bar:
            raise ConfigError("init_outputsize must cover the strategy window (4800 bars)")
        if self.lookback_candles < 1:
            raise ConfigError("lookback_candles must be >= 1")


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    s_defaults = StrategyParams()
    strategy = StrategyParams(
        algorithm_mode=env.get("PO3_ALGORITHM_MODE") or s_defaults.algorithm_mode,
        breakout_method=env.get("PO3_BREAKOUT_METHOD") or s_defaults.breakout_method,
        tpsl_method=env.get("PO3_TPSL_METHOD") or s_defaults.tpsl_method,
        risk_amount=env.get("PO3_RISK") or s_defaults.risk_amount,
        tp_percent=_f(env, "PO3_TP_PERCENT", s_defaults.tp_percent),
        sl_percent=_f(env, "PO3_SL_PERCENT", s_defaults.sl_percent),
    )
    d = Config()
    cfg = Config(
        strategy=strategy,
        td_api_key=env.get("TWELVE_DATA_API_KEY", "").strip(),
        tg_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        tg_chat_id=env.get("TELEGRAM_CHAT_ID", "").strip(),
        state_path=env.get("STATE_PATH") or d.state_path,
        log_level=env.get("LOG_LEVEL") or d.log_level,
        lookback_candles=_f(env, "LOOKBACK_CANDLES", d.lookback_candles, int),
        first_run_send_lookback=_f(env, "FIRST_RUN_SEND_LOOKBACK", d.first_run_send_lookback, int),
        batch_size=_f(env, "BATCH_SIZE", d.batch_size, int),
        daily_safety_margin=_f(env, "DAILY_SAFETY_MARGIN", d.daily_safety_margin, int),
        max_credits_per_run=_f(env, "MAX_CREDITS_PER_RUN", d.max_credits_per_run, int),
        incremental_outputsize=_f(env, "INCREMENTAL_OUTPUTSIZE", d.incremental_outputsize, int),
        init_outputsize=_f(env, "INIT_OUTPUTSIZE", d.init_outputsize, int),
        telegram_interval_seconds=_f(env, "TELEGRAM_INTERVAL_SECONDS", d.telegram_interval_seconds),
    )
    cfg.validate()
    return cfg
