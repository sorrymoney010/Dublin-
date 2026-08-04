import pytest

from dublin_bot.config import Settings
from dublin_bot.kraken_gateway import KrakenGateway


def test_kraken_defaults_to_local_dry_run():
    settings = Settings(_env_file=None, exchange="kraken")
    assert settings.paper_trading is True
    assert settings.dry_run is True
    assert settings.live_execution_enabled is False


def test_kraken_paper_mode_cannot_disable_dry_run():
    with pytest.raises(ValueError, match="no Spot paper endpoint"):
        Settings(_env_file=None, exchange="kraken", paper_trading=True, dry_run=False)


def test_kraken_live_mode_requires_full_unlock():
    with pytest.raises(ValueError, match="ALLOW_LIVE_TRADING"):
        Settings(
            _env_file=None,
            exchange="kraken",
            paper_trading=False,
            dry_run=False,
            allow_live_trading=False,
        )


def test_kraken_dry_run_never_submits_order():
    settings = Settings(_env_file=None, exchange="kraken")
    gateway = KrakenGateway(settings)
    assert gateway.buy_notional(5.0) == "kraken-dry-run-buy-5.00"
    assert gateway.close_position() == "kraken-dry-run-close"
