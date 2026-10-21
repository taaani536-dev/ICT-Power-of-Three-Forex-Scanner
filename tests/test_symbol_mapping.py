import pytest

from src.config import Config, ConfigError, load_config
from src.symbol_mapping import (FOREX_UNIVERSE, SymbolError, chunk, from_provider_symbol, normalize_symbol,
                                price_decimals, to_provider_symbol, validate_universe)

EXPECTED = ("EURUSD GBPUSD USDJPY USDCHF USDCAD AUDUSD NZDUSD EURJPY GBPJPY EURGBP EURCHF EURCAD GBPCAD GBPCHF "
            "GBPAUD GBPNZD AUDJPY AUDCAD AUDNZD AUDCHF NZDJPY NZDCAD NZDCHF CADJPY CADCHF CHFJPY EURAUD EURNZD").split()


def test_universe_is_exactly_the_28_requested_pairs_in_order():
    assert list(FOREX_UNIVERSE) == EXPECTED and len(set(FOREX_UNIVERSE)) == 28
    validate_universe()


@pytest.mark.parametrize("bad", ["XAUUSD", "BTCUSD", "NAS100", "US100", "AAPL", "ETHUSD", "XAU/USD", "EURXYZ", "USDMXN"])
def test_excluded_instruments_rejected(bad):
    with pytest.raises(SymbolError):
        normalize_symbol(bad)


def test_provider_mapping_roundtrip_for_all_pairs():
    for s in FOREX_UNIVERSE:
        p = to_provider_symbol(s)
        assert p == f"{s[:3]}/{s[3:]}" and from_provider_symbol(p) == s


@pytest.mark.parametrize("raw", ["eur/usd", "EUR-USD", " EURUSD ", "eur_usd"])
def test_normalization_variants(raw):
    assert normalize_symbol(raw) == "EURUSD"


def test_decimals():
    assert price_decimals("USDJPY") == 3 and price_decimals("EURUSD") == 5 and price_decimals("GBPJPY") == 3


def test_batch_layout_8_8_8_4():
    assert [len(b) for b in chunk(list(FOREX_UNIVERSE), 8)] == [8, 8, 8, 4]


def test_validate_universe_rejects_bad_sets():
    with pytest.raises(SymbolError):
        validate_universe(FOREX_UNIVERSE[:27])
    with pytest.raises(SymbolError):
        validate_universe(FOREX_UNIVERSE[:27] + ("XAUUSD",))


# ---- config
def test_load_config_defaults_and_secret_names():
    cfg = load_config({"TWELVE_DATA_API_KEY": "SECRETKEY123", "TELEGRAM_BOT_TOKEN": "SECRETTOKEN456",
                       "TELEGRAM_CHAT_ID": "98765"})
    assert cfg.td_api_key == "SECRETKEY123" and cfg.lookback_candles == 7 and cfg.telegram_interval_seconds == 15
    assert cfg.strategy.sl_atr_mult == 5 and cfg.daily_budget == 760
    assert not any(x in repr(cfg) for x in ("SECRETKEY123", "SECRETTOKEN456", "98765"))   # excluded from repr


def test_missing_secrets_reported_by_name():
    cfg = load_config({})
    with pytest.raises(ConfigError) as e:
        cfg.require_secrets()
    assert all(n in str(e.value) for n in ("TWELVE_DATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))


def test_invalid_numeric_env_rejected():
    with pytest.raises(ConfigError):
        load_config({"LOOKBACK_CANDLES": "seven"})


def test_outputsize_limits_enforced():
    with pytest.raises(ConfigError):
        load_config({"INIT_OUTPUTSIZE": "6000"})
    with pytest.raises(ConfigError):
        load_config({"INIT_OUTPUTSIZE": "1000"})      # smaller than the 4800-bar strategy window


def test_strategy_env_override_validated():
    assert load_config({"PO3_RISK": "High"}).strategy.sl_atr_mult == 6
    with pytest.raises(ConfigError):
        load_config({"PO3_RISK": "Insane"})


def test_batch_size_env_validated():
    assert load_config({"BATCH_SIZE": "4"}).batch_size == 4
    with pytest.raises(ConfigError):
        load_config({"BATCH_SIZE": "9"})      # would exceed 8 credits/minute in one request
