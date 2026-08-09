from dublin_bot.config import Settings
from dublin_bot.dashboard import safety_status


def test_dashboard_requires_all_safe_defaults():
    status = safety_status(Settings(_env_file=None))
    assert status["safe"] is True
    assert status["paper_trading"] is True
    assert status["dry_run"] is True
    assert status["live_allowed"] is False
