from __future__ import annotations

from dataclasses import dataclass, field


def default_pipeline_counters() -> dict:
    return {
        "candidates_started": 0,
        "momentum_confirmed": 0,
        "or_confirmed": 0,
        "decisions_accepted": 0,
        "entries_attempted": 0,
        "entries_opened": 0,
        "entry_block_reasons": {},
    }


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
    total_signals: int = 0
    bullish_dominance_percent: float = 0.0
    bearish_dominance_percent: float = 0.0
    direction_threshold: float = 0.0
    minimum_dominance: float = 0.0
    preliminary_direction: str | None = None
    momentum_passed: bool = False
    two_candle_or_passed: bool = False
    final_direction: str | None = None
    entry_status: str = "NOT_ATTEMPTED"
    entry_block_reason: str | None = None
    momentum_candidate_direction: str | None = None
    momentum_candidate_option_symbol: str | None = None
    momentum_starting_price: float | None = None
    momentum_current_price: float | None = None
    momentum_observed_percent: float = 0.0
    momentum_required_percent: float = 0.0
    momentum_candidate_age_seconds: float = 0.0
    momentum_time_remaining_seconds: float = 0.0
    momentum_block_reason: str | None = None


@dataclass
class TournamentMomentumCandidate:
    direction: str
    option_symbol: str
    starting_underlying_price: float | None
    starting_option_price: float
    started_epoch: float
    deadline_epoch: float
    highest_candidate_price: float
    lowest_candidate_price: float
    observed_movement_percent: float
    required_movement_percent: float
    status: str
    current_option_price: float | None = None
    block_reason: str | None = None
    cancelled_epoch: float | None = None
    retry_until_epoch: float | None = None


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
    locked_profit_dollars: float
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
    momentum_candidate: TournamentMomentumCandidate | None = None
    pipeline_counters: dict = field(default_factory=default_pipeline_counters)
