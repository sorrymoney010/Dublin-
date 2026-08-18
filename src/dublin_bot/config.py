from __future__ import annotations

import json
from pathlib import Path
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical tradeable basket. Fixed and independent of the *currently selected*
# coin: BTC/USD is the master coin (always first/allowed) so it never disappears
# when the operator pins another coin. Order is stable; ``allowed_symbols``
# dedupes but preserves this canonical ordering.
DEFAULT_COIN_BASKET: tuple[str, ...] = (
    "BTC/USD",       # master coin
    "UNI/USD",       # Uniswap
    "XRP/USD",
    "PUMP/USD",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Active broker ──────────────────────────────────────
    broker: str = "kraken"  # kraken only (alpaca decommissioned)

    # ── Kraken Spot ─────────────────────────────────────────
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    kraken_tier: str = "starter"  # starter | intermediate | pro

    # ── Transport / reliability ────────────────────────────
    http_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    max_retries: int = Field(default=3, ge=1, le=6)

    # ── Data freshness ─────────────────────────────────────
    max_bar_age_multiple: float = Field(default=3.0, gt=0, le=20)
    max_clock_skew_seconds: float = Field(default=30.0, gt=0, le=300)

    # ── Market quality gates ───────────────────────────────
    max_spread_bps: float = Field(default=50.0, gt=0)
    min_dollar_volume: float = Field(default=1_000_000.0, ge=0)

    # ── Operational state paths ────────────────────────────
    audit_log_path: Path = Path("logs/audit.jsonl")
    idempotency_path: Path = Path("logs/orders.json")
    nonce_state_path: Path = Path("logs/nonce.json")

    paper_trading: bool = True
    allow_live_trading: bool = False
    dry_run: bool = True
    live_risk_acknowledgement: str = ""

    symbol: str = "BTC/USD"
    # Rapid mode: 15-minute bars, 15-minute monitor cadence, and a 15-minute
    # cooldown between entries. Strategy RSI/momentum gates are NOT loosened.
    timeframe_minutes: int = Field(default=15, ge=1)
    lookback_bars: int = Field(default=500, ge=220)
    strategy_equity_usd: float = Field(default=25.0, ge=25.0)
    # Auto-scale risk per trade based on session win/loss streak. The base risk
    # (risk_per_trade) is multiplied by this factor, which the engine moves
    # between min_risk_scale and max_risk_scale as the bot wins/loses — so a
    # winning streak compounds allocation up, a losing streak tightens it down.
    adaptive_risk: bool = Field(default=True)
    min_risk_scale: float = Field(default=0.5, gt=0, le=1.0)
    max_risk_scale: float = Field(default=2.0, gt=1.0)
    risk_step: float = Field(default=0.15, gt=0, le=0.5)
    # When the real account balance is too small to trade the configured symbol
    # at the minimum notional, automatically fall back to a cheaper allowed coin.
    auto_cheaper_symbol: bool = Field(default=True)
    fallback_symbols: list[str] = Field(default_factory=lambda: ["PUMP/USD", "XRP/USD", "UNI/USD", "BTC/USD"])
    # Canonical, always-allowed basket. Defaults to DEFAULT_COIN_BASKET and is
    # intentionally independent of the mutable ``symbol`` selection, so BTC/USD
    # (and the rest of the basket) is never lost when a different coin is pinned.
    coin_basket: list[str] = Field(default_factory=lambda: list(DEFAULT_COIN_BASKET))
    # ── Coin control (user-selected coin / rotation mode) ───
    # preferred_symbol is the coin the operator pinned from the dashboard. When
    # auto_symbol_rotation is False the engine keeps it and never rotates away.
    preferred_symbol: str = "PUMP/USD"  # rotation focus: MR has positive expectancy on PUMP
    auto_symbol_rotation: bool = Field(default=True)
    coin_control_path: Path = Path("logs/coin_control.json")
    # ── Sentiment agent (Stage 1) ──────────────────────────────
    # When enabled, live news/Reddit sentiment acts as a confirmation filter:
    # bearish mood blocks fresh BUYs, a collapse forces a protective SELL. It
    # never originates a trade on its own.
    sentiment_enabled: bool = Field(default=True)
    # ── Active signal strategy ─────────────────────────────────
    # "momentum" (default, RSI band gate) or "mean_reversion" (oversold
    # stretch + reversion exit) — the latter backtested best on XRP/USD.
    strategy: str = Field(default="mean_reversion")
    # ── DCA accumulator sleeve ─────────────────────────────────
    # Mechanical fixed-USD accumulation on a timer, UNDER the same risk gates as
    # every other order. PUMP/USD only (the coin with proven positive MR
    # expectancy). Caps prevent unbounded stacking. OFF by default.
    dca_enabled: bool = Field(default=False)
    dca_symbol: str = Field(default="PUMP/USD")
    dca_interval_minutes: int = Field(default=240, ge=30)
    dca_fixed_usd: float = Field(default=2.0, gt=0)
    dca_max_buys_per_day: int = Field(default=4, ge=0)
    dca_max_total_buys: int = Field(default=40, ge=0)
    dca_stop_buffer: float = Field(default=0.05, gt=0, le=0.5)  # synthetic stop for risk gate only
    # Margin (leveraged) trading. OFF by default — spot only. When enabled the
    # gateway submits margin orders and tracks positions via OpenPositions. Kraken
    # can force-liquidate a margin position, so the exposure cap is tightened and
    # a hard leverage ceiling is enforced; leverage is never auto-raised above it.
    margin_enabled: bool = Field(default=False)
    max_leverage: float = Field(default=2.0, gt=0, le=5.0)
    margin_exposure_fraction: float = Field(default=0.25, gt=0, le=0.5)
    risk_per_trade: float = Field(default=0.02, gt=0, le=0.02)
    max_position_fraction: float = Field(default=0.40, gt=0, le=0.5)
    # Vol-target / fractional-Kelly sizing (see dublin_bot.sizing).
    target_vol: float = Field(default=0.12, gt=0, le=1.0)
    kelly_fraction: float = Field(default=0.25, gt=0, le=0.5)
    max_exposure_fraction: float = Field(default=0.5, gt=0, le=1.0)
    max_daily_loss_fraction: float = Field(default=0.03, gt=0, le=0.05)
    max_drawdown_fraction: float = Field(default=0.10, gt=0, le=0.20)
    # Daily order cap. Set to 0 for unlimited orders per day (cooldown still
    # applies between entries). Bounded at 10 when a cap is used.
    max_orders_per_day: int = Field(default=0, ge=0, le=10)
    cooldown_minutes: int = Field(default=10, ge=0)
    monitor_interval_seconds: int = Field(default=900, ge=60, le=86400)
    # Rapid mode flag (status only). Does not loosen strategy RSI/momentum or
    # any risk/loss/exposure threshold — it only selects the faster cadence above.
    rapid_mode: bool = Field(default=True)

    fast_ema: int = Field(default=20, ge=2)
    slow_ema: int = Field(default=50, ge=3)
    regime_ema: int = Field(default=200, ge=10)
    rsi_period: int = Field(default=14, ge=2)
    rsi_min: float = 45.0
    rsi_max: float = 68.0
    rsi_oversold: float = 32.0   # strict MR entry (backtest-proven best on PUMP); aggression comes from size/cooldown, not a wider band
    rsi_exit: float = 55.0       # mean-reversion exit threshold (recovered)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=1.5, gt=0)
    breakout_lookback: int = Field(default=20, ge=2)
    volume_lookback: int = Field(default=20, ge=2)
    min_volume_ratio: float = Field(default=1.10, gt=0)
    min_order_notional_usd: float = Field(default=1.0, ge=1.0)
    journal_path: Path = Path("logs/decisions.jsonl")

    @model_validator(mode="after")
    def validate_safety(self) -> "Settings":
        if not self.paper_trading:
            if not self.allow_live_trading:
                raise ValueError("Live mode blocked: ALLOW_LIVE_TRADING must be true")
            if self.live_risk_acknowledgement != "I_ACCEPT_LIVE_TRADING_RISK":
                raise ValueError("Live mode blocked: acknowledgement is missing")
        if not (self.fast_ema < self.slow_ema < self.regime_ema):
            raise ValueError("EMA periods must satisfy fast < slow < regime")
        if self.rsi_min >= self.rsi_max:
            raise ValueError("RSI_MIN must be below RSI_MAX")
        return self

    @property
    def has_credentials(self) -> bool:
        return bool(self.kraken_api_key and self.kraken_api_secret)

    @property
    def allowed_symbols(self) -> list[str]:
        """Deduplicated tradeable basket — fixed and independent of selection.

        Derived from the canonical ``coin_basket`` (which always contains
        BTC/USD, SOL/USD, XRP/USD, ADA/USD, DOGE/USD, TRX/USD, HYPE/USD) so the
        basket never changes when the operator pins a different coin.
        """
        ordered: list[str] = []
        for sym in self.coin_basket:
            sym = str(sym).strip().upper()
            if sym and sym not in ordered:
                ordered.append(sym)
        return ordered

    # ── coin-control persistence (non-secret only) ──────────

    def coin_control_state(self) -> dict[str, object]:
        return {
            "preferred_symbol": self.preferred_symbol,
            "auto_symbol_rotation": self.auto_symbol_rotation,
        }

    def save_coin_control(self) -> dict[str, object]:
        """Persist ONLY the non-secret coin-control preferences to disk."""
        state = self.coin_control_state()
        path = Path(self.coin_control_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state

    def load_coin_control(self) -> dict[str, object]:
        """Restore persisted coin-control preferences, ignoring unknown coins."""
        path = Path(self.coin_control_path)
        if not path.exists():
            return self.coin_control_state()
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self.coin_control_state()
        symbol = str(stored.get("preferred_symbol", "")).strip().upper()
        if symbol and symbol in self.allowed_symbols:
            self.preferred_symbol = symbol
            self.symbol = symbol
        if isinstance(stored.get("auto_symbol_rotation"), bool):
            self.auto_symbol_rotation = stored["auto_symbol_rotation"]
        return self.coin_control_state()

    @property
    def safety_locked(self) -> bool:
        """True when all three independent locks forbid real-money execution."""
        return self.paper_trading and self.dry_run and not self.allow_live_trading

    @property
    def active_mode(self) -> str:
        """Human-readable active execution mode: 'live' or 'paper'."""
        if self.allow_live_trading and not self.paper_trading and not self.dry_run:
            return "live"
        return "paper"

    def safety_report(self) -> dict[str, object]:
        """Credential-free summary suitable for logs, audit, and the dashboard."""
        return {
            "safety_locked": self.safety_locked,
            "paper_trading": self.paper_trading,
            "dry_run": self.dry_run,
            "allow_live_trading": self.allow_live_trading,
            "live_risk_acknowledgement_present": bool(self.live_risk_acknowledgement),
            "broker": self.broker,
            "credentials_present": self.has_credentials,
            "symbol": self.symbol,
            "strategy_equity_usd": self.strategy_equity_usd,
            "max_orders_per_day": self.max_orders_per_day,
        }
