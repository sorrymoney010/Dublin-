"""Coin Control: operator-selected coin, manual lock, and persistence.

Covers the requirements that a coin selection from the phone (a) rejects
symbols outside ``allowed_symbols``, (b) never places an order, (c) persists
only non-secret preferences to ``logs/coin_control.json``, (d) locks the engine
to the chosen coin (no rotation) until Auto is explicitly re-enabled, and
(e) reports the speed/risk posture (15m timeframe, 15m cadence, 15m cooldown,
unlimited orders/day).

Fully offline: no HTTP, no private API calls, no orders.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from http.server import ThreadingHTTPServer
from threading import Thread

from dublin_bot.config import Settings
from dublin_bot.dashboard import (
    apply_coin_control,
    coin_control_data,
    make_handler,
    PaperMonitor,
    serve_dashboard,
)
from dublin_bot.engine import TradingEngine


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        broker="kraken",
        paper_trading=True,
        dry_run=True,
        allow_live_trading=False,
        kraken_api_key="fake-key",
        kraken_api_secret="fake-secret",
        symbol="BTC/USD",
        coin_control_path=tmp_path / "coin_control.json",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def stub_engine(settings: Settings) -> TradingEngine:
    """An engine shell with just the pieces ``_select_symbol`` touches."""
    engine = object.__new__(TradingEngine)
    engine.settings = settings
    engine.audit = MagicMock()
    engine.gateway = MagicMock()
    engine.gateway.account_equity.return_value = 100.0
    engine._rotation = 0
    return engine


# ── allowed_symbols ──────────────────────────────────────────────

def test_allowed_symbols_is_canonical_basket_ignoring_current_selection(tmp_path):
    # The basket is fixed and independent of the currently selected coin.
    settings = make_settings(tmp_path, symbol="XRP/USD")
    allowed = settings.allowed_symbols
    # canonical order preserved, deduped
    assert allowed == [
        "BTC/USD", "XRP/USD", "TRX/USD", "DOGE/USD",
        "PUMP/USD", "KAITO/USD", "UNI/USD", "JTO/USD", "HYPE/USD",
    ]
    assert len(allowed) == len(set(allowed))
    # BTC/USD is in the basket even though it is not a "fallback" coin and is
    # not the current symbol — it must never disappear when another coin is pinned.
    assert "BTC/USD" in allowed


def test_selecting_kaito_keeps_btc_allowed(tmp_path):
    """Regression: pinning KAITO/USD must not drop BTC/USD from the basket."""
    settings = make_settings(tmp_path)
    result = apply_coin_control(settings, {"symbol": "KAITO/USD"})
    assert result["ok"] is True
    assert settings.symbol == "KAITO/USD"
    assert "BTC/USD" in settings.allowed_symbols
    assert "KAITO/USD" in settings.allowed_symbols
    # basket is unchanged by selection
    assert settings.allowed_symbols == [
        "BTC/USD", "XRP/USD", "TRX/USD", "DOGE/USD",
        "PUMP/USD", "KAITO/USD", "UNI/USD", "JTO/USD", "HYPE/USD",
    ]


# ── validation ───────────────────────────────────────────────────

def test_invalid_symbol_rejected_and_nothing_changes(tmp_path):
    settings = make_settings(tmp_path)
    result = apply_coin_control(settings, {"symbol": "SCAM/USD"})
    assert result["ok"] is False
    assert "not allowed" in str(result["error"])
    assert settings.symbol == "BTC/USD"
    assert settings.auto_symbol_rotation is True
    assert not (tmp_path / "coin_control.json").exists()


# ── selection places no order ────────────────────────────────────

def test_valid_selection_places_no_order(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        TradingEngine, "run_cycle",
        lambda self: calls.append("run_cycle"),
    )
    monkeypatch.setattr(
        TradingEngine, "run_once",
        lambda self: calls.append("run_once"),
    )
    result = apply_coin_control(settings, {"symbol": "DOGE/USD"})
    assert result["ok"] is True
    assert result["active_symbol"] == "DOGE/USD"
    assert calls == []


# ── manual selection disables rotation; explicit toggle restores ─

def test_manual_selection_disables_rotation_and_toggle_reenables(tmp_path):
    settings = make_settings(tmp_path)
    manual = apply_coin_control(settings, {"symbol": "KAITO/USD"})
    assert manual["auto_rotation"] is False
    assert manual["mode"] == "manual"
    assert settings.preferred_symbol == "KAITO/USD"

    auto = apply_coin_control(settings, {"auto_rotation": True})
    assert auto["auto_rotation"] is True
    assert auto["mode"] == "auto"
    assert settings.auto_symbol_rotation is True


# ── persistence ──────────────────────────────────────────────────

def test_selection_persists_non_secret_state_and_reloads(tmp_path):
    settings = make_settings(tmp_path)
    apply_coin_control(settings, {"symbol": "TRX/USD"})

    path = tmp_path / "coin_control.json"
    assert path.exists()
    import json
    stored = json.loads(path.read_text())
    assert stored == {"preferred_symbol": "TRX/USD", "auto_symbol_rotation": False}
    # no secrets are ever written
    assert "fake-secret" not in path.read_text()

    fresh = make_settings(tmp_path)
    fresh.load_coin_control()
    assert fresh.preferred_symbol == "TRX/USD"
    assert fresh.symbol == "TRX/USD"
    assert fresh.auto_symbol_rotation is False


# ── engine honours the manual lock ───────────────────────────────

def test_engine_keeps_preferred_symbol_in_manual_mode(tmp_path):
    settings = make_settings(tmp_path, auto_symbol_rotation=False,
                             preferred_symbol="XRP/USD")
    engine = stub_engine(settings)
    for _ in range(5):
        engine._select_symbol()
        assert settings.symbol == "XRP/USD"
    # manual mode must not consult the account or rotate the basket
    engine.gateway.account_equity.assert_not_called()
    assert engine._rotation == 0


def test_engine_rotates_when_auto_rotation_enabled(tmp_path):
    settings = make_settings(tmp_path, auto_symbol_rotation=True)
    engine = stub_engine(settings)
    engine.gateway.size_buy.return_value = 1.0  # every coin is affordable
    engine._select_symbol()
    engine.gateway.account_equity.assert_called()
    assert settings.symbol in settings.allowed_symbols


# ── speed / risk display ─────────────────────────────────────────

def test_coin_control_reports_speed_and_risk_posture(tmp_path):
    settings = make_settings(tmp_path)
    data = coin_control_data(settings)
    assert data["timeframe_minutes"] == 15
    assert data["cadence_minutes"] == 15
    assert data["cooldown_minutes"] == 15
    assert data["max_orders_per_day"] == 0  # 0 = unlimited orders/day
    assert data["rapid_mode"] is True
    assert data["active_mode"] == "paper"
    assert data["risk_per_trade"] == pytest.approx(0.01)
    assert data["allowed_symbols"] == settings.allowed_symbols


# ── dashboard startup restores persisted manual lock ─────────────

def test_dashboard_startup_reloads_saved_xrp_lock(tmp_path):
    """Regression: serve_dashboard must call load_coin_control so a saved manual
    lock survives a restart. ``run=False`` exercises the startup path without
    binding a real socket."""
    settings = make_settings(tmp_path)
    # Operator previously pinned XRP/USD on the phone (persisted to disk).
    apply_coin_control(settings, {"symbol": "XRP/USD"})
    assert (tmp_path / "coin_control.json").exists()

    fresh = make_settings(tmp_path)
    assert fresh.symbol == "BTC/USD"  # not yet loaded
    # Startup initialization (run=False) must reload the persisted lock.
    assert serve_dashboard(fresh, run=False) == 0
    assert fresh.symbol == "XRP/USD"
    assert fresh.preferred_symbol == "XRP/USD"
    assert fresh.auto_symbol_rotation is False


# ── auto_rotation only accepts real JSON booleans ───────────────

def test_auto_rotation_rejects_string_false(tmp_path):
    """A mis-sent string 'false' must be rejected, not silently coerced."""
    settings = make_settings(tmp_path)
    before = settings.auto_symbol_rotation
    result = apply_coin_control(settings, {"auto_rotation": "false"})
    assert result["ok"] is False
    assert "boolean" in str(result["error"])
    # nothing changed
    assert settings.auto_symbol_rotation is before
    assert not (tmp_path / "coin_control.json").exists()


def test_auto_rotation_accepts_real_booleans(tmp_path):
    settings = make_settings(tmp_path)
    off = apply_coin_control(settings, {"auto_rotation": False})
    assert off["ok"] is True
    assert settings.auto_symbol_rotation is False
    on = apply_coin_control(settings, {"auto_rotation": True})
    assert on["ok"] is True
    assert settings.auto_symbol_rotation is True


# ── real local HTTP handler: POST /api/coin-control ─────────────

def test_http_coin_control_valid_returns_200_invalid_returns_400(tmp_path, monkeypatch):
    """Spin up the actual handler via http.server against a real socket and
    prove valid selection -> 200, invalid pair -> 400, with no trading."""

    # Guard: ensure the handler never reaches run_once / run_cycle / orders.
    calls: list[str] = []

    def _blocked_run_once(self):
        calls.append("run_once")
        raise AssertionError("run_once must not be called by coin-control POST")

    def _blocked_run_cycle(self):
        calls.append("run_cycle")
        raise AssertionError("run_cycle must not be called by coin-control POST")

    monkeypatch.setattr(TradingEngine, "run_once", _blocked_run_once)
    monkeypatch.setattr(TradingEngine, "run_cycle", _blocked_run_cycle)

    settings = make_settings(tmp_path)
    monitor = PaperMonitor(settings)
    handler_cls = make_handler(settings, monitor)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.request

        def post(payload: dict) -> tuple[int, dict]:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/coin-control",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        # valid selection -> 200
        status, body = post({"symbol": "XRP/USD"})
        assert status == 200, body
        assert body["ok"] is True
        assert body["active_symbol"] == "XRP/USD"
        assert "BTC/USD" in body["allowed_symbols"]

        # invalid pair -> 400
        status, body = post({"symbol": "SCAM/USD"})
        assert status == 400, body
        assert body["ok"] is False

        # reject string auto_rotation -> 400
        status, body = post({"auto_rotation": "false"})
        assert status == 400, body
        assert body["ok"] is False

        assert calls == []  # never ran a cycle or order
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
