"""Loads settings from config.toml, falling back to safe defaults."""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class BotSettings:
    mode: str = "paper"
    pair: str = "ETHUSDT"
    asset: str = "ETH"
    interval_minutes: int = 240
    poll_seconds: int = 60
    starting_balance: float = 100.0
    db_path: str = "data/bot.db"


@dataclass
class StrategySettings:
    fast_ema: int = 20
    slow_ema: int = 50
    atr_period: int = 14
    atr_stop_mult: float = 2.5
    risk_per_trade: float = 0.02
    max_position_pct: float = 0.95
    min_trade_usd: float = 10.0


@dataclass
class CostSettings:
    pool_fee_pct: float = 0.05
    slippage_pct: float = 0.10
    gas_per_swap_usd: float = 0.05


@dataclass
class SurvivalSettings:
    monthly_running_cost_usd: float = 6.0
    floor_usd: float = 50.0
    daily_loss_limit_pct: float = 5.0

    @property
    def daily_cost_usd(self) -> float:
        return self.monthly_running_cost_usd * 12 / 365


@dataclass
class Settings:
    bot: BotSettings = field(default_factory=BotSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    costs: CostSettings = field(default_factory=CostSettings)
    survival: SurvivalSettings = field(default_factory=SurvivalSettings)


def _fill(cls, data: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys in config: {sorted(unknown)}")
    return cls(**data)


def load_settings(path: str | Path = "config.toml") -> Settings:
    path = Path(path)
    if not path.exists():
        return Settings()
    raw = tomllib.loads(path.read_text())
    settings = Settings(
        bot=_fill(BotSettings, raw.get("bot", {})),
        strategy=_fill(StrategySettings, raw.get("strategy", {})),
        costs=_fill(CostSettings, raw.get("costs", {})),
        survival=_fill(SurvivalSettings, raw.get("survival", {})),
    )
    if settings.bot.mode != "paper":
        raise ValueError("Only mode = \"paper\" is supported in this version.")
    return settings
