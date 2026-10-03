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

    @property
    def daily_cost_usd(self) -> float:
        return self.monthly_running_cost_usd * 12 / 365


@dataclass
class GuardrailSettings:
    """Hard limits in code that the bot's brain cannot override. Each bot has its own set."""
    style: str = "careful"  # "careful" or "full send": also sets Claude's trading personality
    max_risk_per_trade: float = 0.03  # max loss if the stop is hit, as a share of equity
    min_stop_distance_pct: float = 1.0
    max_stop_distance_pct: float = 15.0
    max_trades_per_day: int = 4  # 0 means no limit
    daily_loss_limit_pct: float = 5.0  # 0 means no daily limit
    allow_adding: bool = False  # buy more while already holding
    allow_short: bool = False
    max_leverage: float = 1.0  # total exposure as a multiple of equity
    max_positions: int = 1  # positions held at once, one per coin
    min_volume_usd: float = 0.0  # coins other than the primary need this much 24h volume (0 = primary only)
    stops_only_up: bool = True


@dataclass
class ClaudeSettings:
    enabled: bool = True
    db_path: str = "data/claude.db"
    monthly_budget_usd: float = 15.0
    smart_model: str = "claude-opus-5-5"
    lean_model: str = "claude-sonnet-5-5"
    review_model: str = "claude-opus-5-5"
    heartbeat_hours: float = 24.0
    move_trigger_pct: float = 3.0
    min_minutes_between_wakes: int = 60
    min_check_hours: float = 2.0
    review_every_days: int = 7
    web_searches_per_wake: int = 2
    allow_mainnet: bool = False  # real money stays locked until the testnet run has proved itself


@dataclass
class Settings:
    bot: BotSettings = field(default_factory=BotSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    costs: CostSettings = field(default_factory=CostSettings)
    survival: SurvivalSettings = field(default_factory=SurvivalSettings)
    guardrails: GuardrailSettings = field(default_factory=GuardrailSettings)
    claude_guardrails: GuardrailSettings = field(default_factory=GuardrailSettings)
    claude: ClaudeSettings = field(default_factory=ClaudeSettings)


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
        guardrails=_fill(GuardrailSettings, raw.get("guardrails", {})),
        claude_guardrails=_fill(GuardrailSettings, raw.get("claude_guardrails", raw.get("guardrails", {}))),
        claude=_fill(ClaudeSettings, raw.get("claude", {})),
    )
    if settings.bot.mode != "paper":
        raise ValueError("Only mode = \"paper\" is supported in this version.")
    return settings
