from __future__ import annotations

from pathlib import Path
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    paper_trading: bool = True
    allow_live_trading: bool = False
    dry_run: bool = True
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
        return bool(self.alpaca_api_key and self.alpaca_api_secret)
