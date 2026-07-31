from __future__ import annotations

from dataclasses import replace

from .models import BotProfile, BotRuntimeState, TournamentTrade, VirtualTournamentPosition
from .recovery import quote_is_fresh
from .state import market_timestamp
from .trades import TOURNAMENT_TRADES_FILE, update_tournament_trade


EXIT_HARD_STOP = "HARD_STOP"
EXIT_TRAILING_STOP = "TRAILING_STOP"
EXIT_PROFIT_FLOOR_STOP = "PROFIT_FLOOR_STOP"
EXIT_INVALID_POSITION = "INVALID_POSITION"
EXIT_MANUAL_TEST_CLOSE = "MANUAL_TEST_CLOSE"


def valid_bid(bid_price) -> bool:
    try:
        return bid_price is not None and float(bid_price) > 0
    except (TypeError, ValueError):
        return False


def position_entry_cost(position: VirtualTournamentPosition) -> float:
    return position.entry_price * 100 * position.contracts


def position_value(position: VirtualTournamentPosition, price: float) -> float:
    return price * 100 * position.contracts


def pnl_dollars(position: VirtualTournamentPosition, price: float) -> float:
    return position_value(position, price) - position_entry_cost(position)


def pnl_percent(position: VirtualTournamentPosition, price: float) -> float:
    entry_cost = position_entry_cost(position)
    return (pnl_dollars(position, price) / entry_cost * 100) if entry_cost else 0.0


def hard_stop_price(position: VirtualTournamentPosition) -> float:
    return position.entry_price * (1 - position.hard_stop_percent / 100)


def trailing_stop_price(position: VirtualTournamentPosition) -> float:
    return position.peak_price * (1 - position.trailing_stop_percent / 100)


def profit_floor_price(position: VirtualTournamentPosition) -> float | None:
    if not position.enable_profit_floor_trailing_stop:
        return None
    return position.entry_price + (position.locked_profit_dollars / 100 / position.contracts)


def profit_floor_activated(position: VirtualTournamentPosition) -> bool:
    floor = profit_floor_price(position)
    return floor is not None and position.peak_price >= floor


def stop_snapshot(position: VirtualTournamentPosition) -> dict:
    floor = profit_floor_price(position)
    trailing = trailing_stop_price(position)
    return {
        "hard_stop_price": hard_stop_price(position),
        "trailing_stop_price": trailing,
        "profit_floor_price": floor,
        "profit_floor_activated": profit_floor_activated(position),
        "effective_trailing_stop": max(trailing, floor) if floor is not None and profit_floor_activated(position) else trailing,
    }


def update_virtual_position_price(
    position: VirtualTournamentPosition,
    bid_price: float,
    now_epoch: float,
) -> VirtualTournamentPosition:
    bid_price = float(bid_price)
    current_value = position_value(position, bid_price)
    current_pnl = pnl_dollars(position, bid_price)
    peak = max(position.peak_price, bid_price)
    lowest = min(position.lowest_price, bid_price)
    timestamp = market_timestamp()

    return replace(
        position,
        current_price=bid_price,
        current_price_source="BID",
        current_value=current_value,
        unrealized_pnl_dollars=current_pnl,
        unrealized_pnl_percent=pnl_percent(position, bid_price),
        peak_price=peak,
        lowest_price=lowest,
        max_profit_dollars=max(position.max_profit_dollars, current_pnl),
        max_drawdown_dollars=min(position.max_drawdown_dollars, current_pnl),
        updated_at=timestamp,
    )


def evaluate_virtual_exit(
    position: VirtualTournamentPosition,
    bid_price: float,
    now_epoch: float,
) -> str | None:
    if position is None or position.status != "OPEN" or not valid_bid(bid_price):
        return None

    updated = update_virtual_position_price(position, bid_price, now_epoch)
    bid_price = float(bid_price)
    hard_stop = hard_stop_price(updated)
    trailing_stop = trailing_stop_price(updated)
    floor = profit_floor_price(updated)
    floor_active = profit_floor_activated(updated)

    if bid_price <= hard_stop:
        return EXIT_HARD_STOP

    if floor_active and floor is not None and bid_price <= max(trailing_stop, floor):
        return EXIT_PROFIT_FLOOR_STOP

    if updated.peak_price > updated.entry_price and bid_price <= trailing_stop:
        return EXIT_TRAILING_STOP

    return None


def close_virtual_position(
    profile: BotProfile,
    state: BotRuntimeState,
    bid_price: float,
    exit_reason: str,
    now_epoch: float,
) -> TournamentTrade:
    position = state.virtual_position
    if position is None or position.status != "OPEN":
        raise ValueError(EXIT_INVALID_POSITION)
    if not valid_bid(bid_price):
        raise ValueError("invalid bid_price")

    updated_position = update_virtual_position_price(position, bid_price, now_epoch)
    exit_value = position_value(updated_position, float(bid_price))
    final_pnl_dollars = pnl_dollars(updated_position, float(bid_price))
    final_pnl_percent = pnl_percent(updated_position, float(bid_price))
    timestamp = market_timestamp()
    trade = update_tournament_trade(
        updated_position.trade_id,
        {
            "status": "CLOSED",
            "exit_time": timestamp,
            "exit_epoch": now_epoch,
            "exit_price": float(bid_price),
            "exit_price_source": "BID",
            "exit_value": exit_value,
            "exit_reason": exit_reason,
            "pnl_dollars": final_pnl_dollars,
            "pnl_percent": final_pnl_percent,
            "peak_price": updated_position.peak_price,
            "lowest_price": updated_position.lowest_price,
            "max_profit_dollars": updated_position.max_profit_dollars,
            "max_drawdown_dollars": updated_position.max_drawdown_dollars,
            "updated_at": timestamp,
        },
        TOURNAMENT_TRADES_FILE,
    )
    state.virtual_position = None
    state.last_updated_at = timestamp
    return trade


def process_virtual_exits(
    profiles: dict[str, BotProfile],
    states: dict[str, BotRuntimeState],
    option_quotes: dict[str, dict],
    now_epoch: float,
) -> list[TournamentTrade]:
    closed_trades = []
    for profile_id, profile in profiles.items():
        state = states.get(profile_id)
        if not state or not state.virtual_position or state.virtual_position.status != "OPEN":
            continue

        position = state.virtual_position
        quote = option_quotes.get(position.option_symbol) or {}
        max_quote_age = int((profile.config or {}).get("max_quote_age_seconds", 10) or 10)
        fresh, _ = quote_is_fresh(quote, max_quote_age, now_epoch)
        if not fresh:
            continue
        bid = quote.get("bid")
        if not valid_bid(bid):
            continue

        updated_position = update_virtual_position_price(position, float(bid), now_epoch)
        state.virtual_position = updated_position
        reason = evaluate_virtual_exit(updated_position, float(bid), now_epoch)
        if reason:
            closed_trades.append(close_virtual_position(profile, state, float(bid), reason, now_epoch))
    return closed_trades
