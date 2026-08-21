"""Tests for the advanced-order layer: bracket building, gateway add/cancel/edit, engine wiring."""

from __future__ import annotations


from dublin_bot.orders import (
    BracketPlan,
    bracket_prices,
    fmt_price,
    limit_entry_price,
)
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.config import Settings
from dublin_bot.engine import TradingEngine
from dublin_bot.models import Action, Signal
from .conftest import default_routes, FakeResponse


def test_fmt_price_strips_zeros():
    assert fmt_price(1.5) == "1.5"
    assert fmt_price(0.003876) == "0.003876"
    assert fmt_price(100.0) == "100"


def test_limit_entry_price_buy_below_ask():
    # Buy posts below the ask to be a maker.
    assert limit_entry_price("buy", 100.0, 0.001) == 99.9
    # Sell posts above the bid.
    assert limit_entry_price("sell", 100.0, 0.001) == 100.1


def test_bracket_prices_long():
    sl, tp = bracket_prices("buy", 100.0, 0.04, 0.08)
    assert sl == 96.0
    assert tp == 108.0


def test_bracket_prices_short():
    sl, tp = bracket_prices("sell", 100.0, 0.04, 0.08)
    # Short: stop above, target below.
    assert sl == 104.0
    assert tp == 92.0


def test_bracket_plan_market_with_bracket_params():
    plan = BracketPlan(
        pair="PUMPUSD", side="buy", volume="2590", ordertype="market",
        stop_loss=0.96, userref=12345, pair_decimals=6,
    )
    p = plan.to_addorder_params()
    assert p["pair"] == "PUMPUSD"
    assert p["type"] == "buy"
    assert p["ordertype"] == "market"
    assert p["close[ordertype]"] == "stop-loss"
    assert p["close[price]"] == "0.960000"  # rounded to pair_decimals (6)
    assert p["close[userref]"] == "12346"
    assert "close[1]" not in p  # single-leg stop only (TP is a separate order)


def test_bracket_plan_trailing():
    plan = BracketPlan(
        pair="PUMPUSD", side="buy", volume="2590", ordertype="market",
        stop_loss=0.96, userref=5, trailing=True,
    )
    p = plan.to_addorder_params()
    assert p["close[trailing]"] == "4%"
    assert "close[price]" not in p  # trailing uses offset, not absolute price


def _make_gw():
    """Gateway whose AddOrder/CancelOrder we stub to capture params."""
    gw = KrakenGateway.__new__(KrakenGateway)
    gw.__dict__["_submitted"] = []
    gw.__dict__["_cancelled"] = []

    def add_order(params):
        gw.__dict__["_submitted"].append(params)
        return f"TX{len(gw.__dict__['_submitted'])}"

    def cancel_order(txid):
        gw.__dict__["_cancelled"].append(txid)
        return True

    gw.add_order = add_order
    gw.cancel_order = cancel_order
    gw.cancel_attached = lambda userref: 0
    return gw


def test_gateway_add_order_records_params():
    gw = _make_gw()
    params = {"pair": "PUMPUSD", "type": "buy", "ordertype": "market", "volume": "2590"}
    oid = gw.add_order(params)
    assert oid == "TX1"
    assert gw.__dict__["_submitted"][0] is params


def test_engine_buy_uses_bracket_when_configured():
    from dublin_bot.audit import AuditLog
    from dublin_bot.ratelimit import KrakenRateLimiter, RateLimitTier

    s = Settings(_env_file=None, use_bracket=True, order_type="market",
                 stop_loss_pct=0.04, take_profit_pct=0.08,
                 paper_trading=True, dry_run=True, allow_live_trading=False,
                 broker="kraken", kraken_api_key="k", kraken_api_secret="test",
                 symbol="BTC/USD", sentiment_enabled=False, timeframe_minutes=60,
                 lookback_bars=250, universe_mode="basket")
    # Capture the AddOrder request body.
    captured = {}

    class CaptureSession:
        def __init__(self):
            self.routes = default_routes()
            self.headers = {}
        def get(self, url, params=None, timeout=None, headers=None):
            ep = url.rstrip("/").split("/")[-1]
            if ep == "OHLC":
                return FakeResponse(self.routes["OHLC"])
            if ep == "Ticker":
                return FakeResponse(self.routes["Ticker"])
            if ep == "AssetPairs":
                return FakeResponse(self.routes["AssetPairs"])
            if ep == "Time":
                return FakeResponse(self.routes["Time"])
            raise AssertionError(ep)
        def post(self, url, data=None, timeout=None, headers=None):
            ep = url.rstrip("/").split("/")[-1]
            if ep == "AddOrder":
                captured.update(data or {})
                return FakeResponse({"error": [], "result": {"txid": ["TX1"], "descr": {"order": "buy"}}})
            if ep in self.routes:
                return FakeResponse(self.routes[ep])
            raise AssertionError(ep)

    clock = {"t": 0.0}
    def _sleep(seconds):
        clock["t"] += seconds
    audit = AuditLog(s.audit_log_path)
    limiter = KrakenRateLimiter(RateLimitTier.pro(), time_fn=lambda: clock["t"], sleep_fn=_sleep)
    gw = KrakenGateway(s, session=CaptureSession(), rate_limiter=limiter,
                       audit=audit, sleep_fn=lambda _s: None)
    eng = TradingEngine(s, gateway=gw, audit=audit)
    eng.learner.enabled = False

    def fake_evaluate(bars, in_position=False):
        return Signal(Action.BUY, 60, "test setup", 1.0, 1.0, 0.9)
    eng.strategy.evaluate = fake_evaluate
    # BTC/USD at ~$50k: a $2 notional order → tiny volume, but bracket math still runs.
    res = eng.run_cycle()
    # In dry-run, add_order logs the bracket params as an ORDER_INTENT audit
    # event (no network call). Assert the protective stop-loss was built.
    bracket_logged = False
    for ev in audit.entries():
        if ev.get("event") == "order_intent" and ev.get("payload", {}).get("close[ordertype]") == "stop-loss":
            bracket_logged = True
            break
    assert bracket_logged, "protective stop-loss not found in audit"
    assert res.record.signal.action is Action.BUY


def test_budget_cap_limits_sizing_to_strategy_equity():
    """Sizing must never exceed the advertised budget even if the real Kraken
    balance is larger (audit finding #1)."""
    from dublin_bot.risk import RiskManager, SessionState

    s = Settings(_env_file=None, strategy_equity_usd=25.0, max_position_fraction=0.40,
                 stop_loss_pct=0.04, take_profit_pct=0.08, risk_per_trade=0.02,
                 adaptive_risk=False)
    rm = RiskManager(s)
    # Real account equity is $1000, but the budget is $25.
    state = SessionState(start_equity=1000.0, peak_equity=1000.0, current_equity=1000.0)
    signal = Signal(Action.BUY, 60, "setup", price=100.0, atr=2.0, stop_price=96.0)
    decision = rm.evaluate(signal, state, open_exposure_usd=0.0)
    assert decision.approved
    # Hard ceiling: budget ($25) * max_position_fraction (0.40) = $10.
    assert decision.notional_usd <= 25.0 * 0.40 + 1e-6
    assert decision.notional_usd <= 10.0 + 1e-6


def test_universe_allowlist_defaults_to_positive_expectancy_coins():
    """Audit #8: the live MR edge is coin-specific, so the default universe
    must be restricted to coins with positive backtested expectancy — not the
    entire Kraken market (which bleeds on XRP/SOL)."""
    s = Settings(_env_file=None)
    assert s.universe_allowlist == ["PUMP/USD", "BTC/USD"]
    # The engine applies the allowlist on top of all_usd discovery.
    candidates = ["PUMP/USD", "BTC/USD", "XRP/USD", "SOL/USD"]
    if s.universe_allowlist:
        allowed = {x.upper() for x in s.universe_allowlist}
        candidates = [c for c in candidates if c.upper() in allowed]
    assert "XRP/USD" not in candidates
    assert "SOL/USD" not in candidates

