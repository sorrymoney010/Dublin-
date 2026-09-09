from __future__ import annotations

from pathlib import Path
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    broker: str = "kraken"
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    kraken_tier: str = "starter"

    http_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    max_retries: int = Field(default=3, ge=1, le=6)
    max_bar_age_multiple: float = Field(default=3.0, gt=0, le=20)
    max_clock_skew_seconds: float = Field(default=30.0, gt=0, le=300)
    max_spread_bps: float = Field(default=50.0, gt=0)
    min_dollar_volume: float = Field(default=1_000_000.0, ge=0)

    audit_log_path: Path = Path("logs/audit.jsonl")
    idempotency_path: Path = Path("logs/orders.json")
    nonce_state_path: Path = Path("logs/nonce.json")
    managed_position_path: Path = Path("logs/managed_position.json")

    # Execution defaults stay locked. LIVE_EXECUTION_ARMED is the final gate
    # that makes the engine construct a submission-capable Kraken gateway.
    paper_trading: bool = True
    allow_live_trading: bool = False
    dry_run: bool = True
    live_execution_armed: bool = False
    live_risk_acknowledgement: str = ""

    symbol: str = "BTC/USD"
    timeframe_minutes: int = Field(default=60, ge=1)
    lookback_bars: int = Field(default=500, ge=220)

    strategy_equity_usd: float = Field(default=25.0, ge=25.0)
    risk_per_trade: float = Field(default=0.01, gt=0, le=0.02)
    max_position_fraction: float = Field(default=0.25, gt=0, le=0.5)
    max_daily_loss_fraction: float = Field(default=0.03, gt=0, le=0.05)
    max_drawdown_fraction: float = Field(default=0.10, gt=0, le=0.20)
    max_orders_per_day: int = Field(default=3, ge=1, le=10)
    cooldown_minutes: int = Field(default=90, ge=0)

    fast_ema: int = Field(default=20, ge=2)
    slow_ema: int = Field(default=50, ge=3)
    regime_ema: int = Field(default=200, ge=10)
    rsi_period: int = Field(default=14, ge=2)
    rsi_min: float = 45.0
    rsi_max: float = 68.0
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=1.5, gt=0)
    breakout_lookback: int = Field(default=20, ge=2)
    volume_lookback: int = Field(default=20, ge=2)
    min_volume_ratio: float = Field(default=1.10, gt=0)
    min_order_notional_usd: float = Field(default=1.0, ge=1.0)

    # COO weighted decision layer.
    coo_entry_score: int = Field(default=72, ge=50, le=100)
    coo_exit_score: int = Field(default=65, ge=40, le=100)
    coo_min_atr_fraction: float = Field(default=0.002, ge=0.0, le=0.05)
    coo_max_atr_fraction: float = Field(default=0.08, gt=0.0, le=0.50)
    coo_confidence_floor: float = Field(default=0.60, ge=0.25, le=1.0)

    journal_path: Path = Path("logs/decisions.jsonl")

    @model_validator(mode="after")
    def validate_safety(self) -> "Settings":
        # Preserve the existing operator/test failure contracts first.
        if not self.paper_trading:
            if not self.allow_live_trading:
                raise ValueError("Live mode blocked: ALLOW_LIVE_TRADING must be true")
            if self.live_risk_acknowledgement != "I_ACCEPT_LIVE_TRADING_RISK":
                raise ValueError("Live mode blocked: acknowledgement is missing")
            if self.dry_run:
                raise ValueError("Live mode blocked: DRY_RUN must be false")
        if self.live_execution_armed:
            if self.paper_trading:
                raise ValueError("Live execution arm requires PAPER_TRADING=false")
            if not self.allow_live_trading:
                raise ValueError("Live execution arm requires ALLOW_LIVE_TRADING=true")
            if self.dry_run:
                raise ValueError("Live execution arm requires DRY_RUN=false")
            if self.live_risk_acknowledgement != "I_ACCEPT_LIVE_TRADING_RISK":
                raise ValueError("Live mode blocked: acknowledgement is missing")

        if not (self.fast_ema < self.slow_ema < self.regime_ema):
            raise ValueError("EMA periods must satisfy fast < slow < regime")
        if self.rsi_min >= self.rsi_max:
            raise ValueError("RSI_MIN must be below RSI_MAX")
        if self.coo_entry_score < self.coo_exit_score:
            raise ValueError("COO_ENTRY_SCORE must be >= COO_EXIT_SCORE")
        if self.coo_min_atr_fraction >= self.coo_max_atr_fraction:
            raise ValueError("COO ATR bounds must satisfy min < max")
        return self

    @property
    def has_credentials(self) -> bool:
        return bool(self.kraken_api_key and self.kraken_api_secret)

    @property
    def safety_locked(self) -> bool:
        return (
            self.paper_trading
            and self.dry_run
            and not self.allow_live_trading
            and not self.live_execution_armed
        )

    @property
    def live_ready(self) -> bool:
        return (
            not self.paper_trading
            and not self.dry_run
            and self.allow_live_trading
            and self.live_execution_armed
            and self.live_risk_acknowledgement == "I_ACCEPT_LIVE_TRADING_RISK"
            and self.has_credentials
        )

    def safety_report(self) -> dict[str, object]:
        return {
            "safety_locked": self.safety_locked,
            "paper_trading": self.paper_trading,
            "dry_run": self.dry_run,
            "allow_live_trading": self.allow_live_trading,
            "live_execution_armed": self.live_execution_armed,
            "live_ready": self.live_ready,
            "live_risk_acknowledgement_present": bool(self.live_risk_acknowledgement),
            "broker": self.broker,
            "credentials_present": self.has_credentials,
            "symbol": self.symbol,
            "strategy_equity_usd": self.strategy_equity_usd,
            "max_orders_per_day": self.max_orders_per_day,
            "coo_entry_score": self.coo_entry_score,
        }
