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
class VirtualTournamentPosition:
    profile_id: str
    profile_display_name: str
    trade_id: str
    symbol: str
    option_symbol: str
    direction: str
    contracts: int
    status: str
    entry_time: str
    entry_epoch: float
    entry_price: float
    entry_price_source: str
    entry_cost: float
    current_price: float
    current_price_source: str
    current_value: float
    unrealized_pnl_dollars: float
    unrealized_pnl_percent: float
    peak_price: float
    lowest_price: float
    hard_stop_percent: float
    trailing_stop_percent: float
    enable_profit_floor_trailing_stop: bool
    locked_profit_amount: float
    confidence: int
    dominance_percent: float
    bullish_score: int
    bearish_score: int
    market_state: str | None
    momentum_required: bool
    or_confirmation_required: bool
    created_at: str
    updated_at: str
    max_profit_dollars: float = 0.0
    max_drawdown_dollars: float = 0.0


@dataclass
class TournamentTrade:
    trade_id: str
    profile_id: str
    profile_display_name: str
    symbol: str
    option_symbol: str
    direction: str
    contracts: int
    status: str
    entry_time: str
    entry_epoch: float | None
    entry_price: float
    entry_price_source: str
    entry_cost: float
    exit_time: str | None = None
    exit_epoch: float | None = None
    exit_price: float | None = None
    exit_price_source: str | None = None
    exit_value: float | None = None
    exit_reason: str | None = None
    pnl_dollars: float | None = None
    pnl_percent: float | None = None
    peak_price: float | None = None
    lowest_price: float | None = None
    max_profit_dollars: float | None = None
    max_drawdown_dollars: float | None = None
    signal: str = ""
    confidence: int = 0
    dominance_percent: float = 0.0
    bullish_score: int = 0
    bearish_score: int = 0
    market_state: str | None = None
    momentum_required: bool = False
    or_confirmation_required: bool = False
    created_at: str = ""
    updated_at: str = ""


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
    virtual_position: VirtualTournamentPosition | None = None
    last_virtual_entry_epoch: float | None = None
    virtual_trades_today: int = 0
    virtual_entry_cooldown_until_epoch: float | None = None
    last_entry_fingerprint: str | None = None
    virtual_trading_date: str | None = None
    last_recovery_time: str | None = None
    recovery_count: int = 0
    recovery_status: str | None = None
    recovered_trade_id: str | None = None
