from dublin_bot.config import Settings
from dublin_bot import dashboard
from dublin_bot.dashboard import risk_state_snapshot, safety_status


def test_dashboard_requires_all_safe_defaults():
    status = safety_status(Settings(_env_file=None))
    assert status["safe"] is True
    assert status["paper_trading"] is True
    assert status["dry_run"] is True
    assert status["live_allowed"] is False


def test_risk_snapshot_uses_live_equity_and_engine_state_path(tmp_path, monkeypatch):
    class FakeGateway:
        def account_equity(self):
            return 31.62

    settings = Settings(
        _env_file=None,
        kraken_api_key="present",
        kraken_api_secret="present",
        strategy_equity_usd=100.0,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dublin_bot.engine.build_gateway", lambda _settings: FakeGateway())
    dashboard._state_cache.update(at=0.0, data={})

    snapshot = risk_state_snapshot(settings)

    assert snapshot["current_equity"] == 31.62
    assert snapshot["start_equity"] == 31.62
    assert snapshot["max_daily_loss"] == 0.95
