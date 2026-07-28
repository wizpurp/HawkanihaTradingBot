from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProfileDecision:
    profile_id: str
    timestamp: str
    accepted: bool
    direction: str | None
    status: str
    rejection_reason: str | None
    bullish_score: int
    bearish_score: int
    confidence: int
    dominance_percent: float
    momentum_required: bool
    momentum_status: str
    or_confirmation_required: bool
    or_confirmation_status: str


@dataclass
class BotProfile:
    profile_id: str
    display_name: str
    enabled: bool
    config: dict = field(default_factory=dict)


@dataclass
class VirtualPosition:
    symbol: str | None = None
    option_symbol: str | None = None
    direction: str | None = None
    quantity: int = 0
    entry_price: float | None = None
    current_price: float | None = None
    peak_price: float | None = None
    effective_stop: float | None = None
    opened_at: str | None = None
    profit_floor_active: bool = False


@dataclass
class PendingEntryState:
    active: bool = False
    direction: str | None = None
    option_symbol: str | None = None
    initial_price: float | None = None
    confirmation_target: float | None = None
    started_at: str | None = None
    expires_at: str | None = None
    rejection_reason: str | None = None


@dataclass
class BotMetrics:
    trades_today: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    average_pnl_per_trade: float = 0.0
    average_winner: float = 0.0
    average_loser: float = 0.0
    maximum_drawdown: float = 0.0
    full_hard_stop_losses: int = 0


@dataclass
class BotRuntimeState:
    profile_id: str
    virtual_balance: float
    starting_balance: float
    position: VirtualPosition = field(default_factory=VirtualPosition)
    pending_entry: PendingEntryState = field(default_factory=PendingEntryState)
    metrics: BotMetrics = field(default_factory=BotMetrics)
    cooldown_until: str | None = None
    last_updated_at: str | None = None
    enabled: bool = True
    last_decision: ProfileDecision | None = None
    decisions_evaluated: int = 0
