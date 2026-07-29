from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time as datetime_time
from zoneinfo import ZoneInfo

from .models import BotProfile, BotRuntimeState, TournamentTrade, VirtualTournamentPosition
from .state import market_date, market_timestamp
from .trades import profile_display_name, save_tournament_trades


MARKET_TZ = ZoneInfo("America/New_York")

RECOVERY_NO_ACTION = "NO_ACTION"
RECOVERY_POSITION_RESUMED = "POSITION_RESUMED"
RECOVERY_TRADE_RECONSTRUCTED = "TRADE_RECONSTRUCTED"
RECOVERY_POSITION_RECONSTRUCTED = "POSITION_RECONSTRUCTED"
RECOVERY_DUPLICATES_CLEANED = "DUPLICATES_CLEANED"
RECOVERY_INVALID_STATE_CANCELLED = "INVALID_STATE_CANCELLED"
RECOVERY_CLOSED_POSITION_CLEARED = "CLOSED_POSITION_CLEARED"
RECOVERY_ERROR = "RECOVERY_ERROR"

EXIT_RECOVERY_INVALID_STATE = "RECOVERY_INVALID_STATE"
EXIT_RECOVERY_DUPLICATE_OPEN_POSITION = "RECOVERY_DUPLICATE_OPEN_POSITION"
EXIT_RECOVERY_ORPHANED_POSITION = "RECOVERY_ORPHANED_POSITION"
EXIT_RECOVERY_ORPHANED_TRADE = "RECOVERY_ORPHANED_TRADE"

REJECT_MARKET_CLOSED = "MARKET_CLOSED"
REJECT_QUOTE_STALE = "QUOTE_STALE"
REJECT_INVALID_QUOTE_TIMESTAMP = "INVALID_QUOTE_TIMESTAMP"


def market_now_from_epoch(now_epoch: float | None = None) -> datetime:
    if now_epoch is None:
        return datetime.now(MARKET_TZ)
    return datetime.fromtimestamp(float(now_epoch), MARKET_TZ)


def is_synthetic_test_epoch(now_epoch: float | None = None) -> bool:
    return now_epoch is not None and float(now_epoch) < 946684800


def tournament_market_is_open(now_epoch: float | None = None) -> bool:
    if is_synthetic_test_epoch(now_epoch):
        return True
    now = market_now_from_epoch(now_epoch)
    if now.weekday() >= 5:
        return False
    return datetime_time(9, 30) <= now.time() <= datetime_time(16, 0)


def parse_quote_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), MARKET_TZ)
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MARKET_TZ)
        return parsed.astimezone(MARKET_TZ)
    except Exception:
        return None


def quote_timestamp(quote_or_snapshot) -> datetime | None:
    if quote_or_snapshot is None:
        return None
    if isinstance(quote_or_snapshot, dict):
        for key in ("_fetched_at", "timestamp", "trade_date", "date"):
            parsed = parse_quote_timestamp(quote_or_snapshot.get(key))
            if parsed:
                return parsed
        return None
    return parse_quote_timestamp(getattr(quote_or_snapshot, "option_quote_timestamp", None))


def quote_is_fresh(quote_or_snapshot, max_age_seconds: int = 10, now_epoch: float | None = None) -> tuple[bool, str | None]:
    timestamp = quote_timestamp(quote_or_snapshot)
    if timestamp is None:
        if is_synthetic_test_epoch(now_epoch):
            return True, None
        return False, REJECT_INVALID_QUOTE_TIMESTAMP
    age = (market_now_from_epoch(now_epoch) - timestamp).total_seconds()
    if age < 0:
        age = 0
    if age > int(max_age_seconds or 10):
        return False, REJECT_QUOTE_STALE
    return True, None


def mark_recovery(state: BotRuntimeState, status: str, trade_id: str | None = None) -> None:
    state.last_recovery_time = market_timestamp()
    state.recovery_count = int(state.recovery_count or 0) + 1
    state.recovery_status = status
    state.recovered_trade_id = trade_id
    state.last_updated_at = state.last_recovery_time


def _trade_sort_key(trade: TournamentTrade) -> float:
    if trade.entry_epoch is not None:
        try:
            return float(trade.entry_epoch)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(trade.entry_time).timestamp()
    except Exception:
        return 0.0


def _profile_strategy(profile: BotProfile) -> dict:
    strategy = profile.config.get("strategy", {}) if isinstance(profile.config, dict) else {}
    return strategy if isinstance(strategy, dict) else {}


def reconstruct_position_from_trade(profile: BotProfile, trade: TournamentTrade) -> VirtualTournamentPosition | None:
    if trade.status != "OPEN" or float(trade.entry_price or 0) <= 0 or int(trade.contracts or 0) < 1:
        return None
    strategy = _profile_strategy(profile)
    timestamp = market_timestamp()
    return VirtualTournamentPosition(
        profile_id=trade.profile_id,
        profile_display_name=profile_display_name(trade.profile_id),
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        option_symbol=trade.option_symbol,
        direction=trade.direction,
        contracts=int(trade.contracts),
        status="OPEN",
        entry_time=trade.entry_time,
        entry_epoch=float(trade.entry_epoch or 0.0),
        entry_price=float(trade.entry_price),
        entry_price_source=trade.entry_price_source,
        entry_cost=float(trade.entry_cost),
        current_price=float(trade.entry_price),
        current_price_source=trade.entry_price_source,
        current_value=float(trade.entry_cost),
        unrealized_pnl_dollars=0.0,
        unrealized_pnl_percent=0.0,
        peak_price=float(trade.peak_price or trade.entry_price),
        lowest_price=float(trade.lowest_price or trade.entry_price),
        hard_stop_percent=float(strategy.get("hard_stop_percent", 0) or 0),
        trailing_stop_percent=float(strategy.get("trailing_stop_percent", 0) or 0),
        enable_profit_floor_trailing_stop=bool(strategy.get("enable_profit_floor_trailing_stop", False)),
        locked_profit_amount=float(strategy.get("locked_profit_amount", 0) or 0),
        confidence=int(trade.confidence or 0),
        dominance_percent=float(trade.dominance_percent or 0.0),
        bullish_score=int(trade.bullish_score or 0),
        bearish_score=int(trade.bearish_score or 0),
        market_state=trade.market_state,
        momentum_required=bool(trade.momentum_required),
        or_confirmation_required=bool(trade.or_confirmation_required),
        created_at=trade.created_at or timestamp,
        updated_at=timestamp,
        max_profit_dollars=float(trade.max_profit_dollars or 0.0),
        max_drawdown_dollars=float(trade.max_drawdown_dollars or 0.0),
    )


def reconstruct_trade_from_position(profile: BotProfile, position: VirtualTournamentPosition) -> TournamentTrade | None:
    if position.status != "OPEN" or float(position.entry_price or 0) <= 0 or int(position.contracts or 0) < 1:
        return None
    timestamp = market_timestamp()
    return TournamentTrade(
        trade_id=position.trade_id,
        profile_id=position.profile_id,
        profile_display_name=profile_display_name(position.profile_id),
        symbol=position.symbol,
        option_symbol=position.option_symbol,
        direction=position.direction,
        contracts=int(position.contracts),
        status="OPEN",
        entry_time=position.entry_time,
        entry_epoch=position.entry_epoch,
        entry_price=position.entry_price,
        entry_price_source=position.entry_price_source,
        entry_cost=position.entry_cost,
        peak_price=position.peak_price,
        lowest_price=position.lowest_price,
        max_profit_dollars=position.max_profit_dollars,
        max_drawdown_dollars=position.max_drawdown_dollars,
        signal=position.direction,
        confidence=position.confidence,
        dominance_percent=position.dominance_percent,
        bullish_score=position.bullish_score,
        bearish_score=position.bearish_score,
        market_state=position.market_state,
        momentum_required=position.momentum_required,
        or_confirmation_required=position.or_confirmation_required,
        created_at=position.created_at or timestamp,
        updated_at=timestamp,
    )


def cancel_trade(trade: TournamentTrade, reason: str) -> TournamentTrade:
    payload = asdict(trade)
    payload["status"] = "CANCELLED"
    payload["exit_reason"] = reason
    payload["updated_at"] = market_timestamp()
    return TournamentTrade(**payload)


def apply_daily_reset(profile: BotProfile, state: BotRuntimeState, now_epoch: float | None = None) -> bool:
    if is_synthetic_test_epoch(now_epoch):
        return False
    today = market_now_from_epoch(now_epoch).date().isoformat()
    if state.virtual_trading_date == today:
        return False
    state.virtual_trading_date = today
    state.virtual_trades_today = 0
    if state.virtual_entry_cooldown_until_epoch and float(state.virtual_entry_cooldown_until_epoch) <= float(now_epoch or 0):
        state.virtual_entry_cooldown_until_epoch = None
    if not state.virtual_position or state.virtual_position.status != "OPEN":
        state.last_entry_fingerprint = None
    state.last_updated_at = market_timestamp()
    return True


def apply_daily_resets(profiles: dict[str, BotProfile], states: dict[str, BotRuntimeState], now_epoch: float | None = None) -> bool:
    changed = False
    for profile_id, profile in profiles.items():
        state = states.get(profile_id)
        if state and apply_daily_reset(profile, state, now_epoch):
            changed = True
    return changed


def reconcile_tournament_state(
    profiles: dict[str, BotProfile],
    states: dict[str, BotRuntimeState],
    trades: list[TournamentTrade],
) -> tuple[dict[str, BotRuntimeState], list[TournamentTrade], dict]:
    summary = {
        "profiles": len(profiles),
        "open_positions": 0,
        "recovered_positions": 0,
        "cancelled_invalid": 0,
        "duplicates_cleaned": 0,
        "state_source": "MAIN",
        "statuses": {},
    }
    changed = False
    trades_by_profile: dict[str, list[TournamentTrade]] = {profile_id: [] for profile_id in profiles}
    for trade in trades:
        if trade.profile_id in trades_by_profile:
            trades_by_profile[trade.profile_id].append(trade)

    for profile_id, profile in profiles.items():
        state = states.get(profile_id)
        if state is None:
            continue
        try:
            profile_trades = trades_by_profile.get(profile_id, [])
            open_trades = sorted([trade for trade in profile_trades if trade.status == "OPEN"], key=_trade_sort_key, reverse=True)
            position = state.virtual_position if state.virtual_position and state.virtual_position.status == "OPEN" else None
            closed_trade_ids = {trade.trade_id for trade in profile_trades if trade.status == "CLOSED"}
            status = RECOVERY_NO_ACTION
            recovered_trade_id = None

            if len(open_trades) > 1:
                keep = open_trades[0]
                cancelled_ids = {trade.trade_id for trade in open_trades[1:]}
                trades = [
                    cancel_trade(trade, EXIT_RECOVERY_DUPLICATE_OPEN_POSITION) if trade.trade_id in cancelled_ids else trade
                    for trade in trades
                ]
                summary["duplicates_cleaned"] += len(cancelled_ids)
                changed = True
                open_trades = [keep]
                status = RECOVERY_DUPLICATES_CLEANED

            if position and position.trade_id in closed_trade_ids:
                state.virtual_position = None
                position = None
                status = RECOVERY_CLOSED_POSITION_CLEARED
                changed = True

            matching_open = next((trade for trade in open_trades if position and trade.trade_id == position.trade_id), None)
            if position and matching_open:
                status = status if status != RECOVERY_NO_ACTION else RECOVERY_POSITION_RESUMED
                recovered_trade_id = position.trade_id
            elif position and not matching_open:
                reconstructed = reconstruct_trade_from_position(profile, position)
                if reconstructed:
                    trades.append(reconstructed)
                    status = RECOVERY_TRADE_RECONSTRUCTED
                    recovered_trade_id = reconstructed.trade_id
                    summary["recovered_positions"] += 1
                    changed = True
                else:
                    state.virtual_position = None
                    status = RECOVERY_INVALID_STATE_CANCELLED
                    summary["cancelled_invalid"] += 1
                    changed = True
            elif not position and open_trades:
                reconstructed_position = reconstruct_position_from_trade(profile, open_trades[0])
                if reconstructed_position:
                    state.virtual_position = reconstructed_position
                    status = RECOVERY_POSITION_RECONSTRUCTED
                    recovered_trade_id = open_trades[0].trade_id
                    summary["recovered_positions"] += 1
                    changed = True
                else:
                    trades = [
                        cancel_trade(trade, EXIT_RECOVERY_INVALID_STATE) if trade.trade_id == open_trades[0].trade_id else trade
                        for trade in trades
                    ]
                    status = RECOVERY_INVALID_STATE_CANCELLED
                    summary["cancelled_invalid"] += 1
                    changed = True

            if state.virtual_position and state.virtual_position.status == "OPEN":
                summary["open_positions"] += 1
            mark_recovery(state, status, recovered_trade_id)
            summary["statuses"][profile_id] = status
        except Exception:
            mark_recovery(state, RECOVERY_ERROR)
            summary["statuses"][profile_id] = RECOVERY_ERROR
            changed = True

    summary["changed"] = changed
    return states, trades, summary
