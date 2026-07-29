from __future__ import annotations

from .models import BotProfile, BotRuntimeState, ProfileDecision, TournamentTrade, VirtualTournamentPosition
from .profiles import PROFILE_ORDER
from .recovery import (
    REJECT_INVALID_QUOTE_TIMESTAMP,
    REJECT_MARKET_CLOSED,
    apply_daily_reset,
    quote_is_fresh,
    tournament_market_is_open,
)
from .snapshot import MarketSnapshot
from .state import market_timestamp
from .trades import TOURNAMENT_TRADES_FILE, append_tournament_trade, generate_trade_id, profile_display_name


def _entry_rules(config: dict) -> dict:
    rules = config.get("entry_rules")
    return rules if isinstance(rules, dict) else {}


def _strategy(config: dict) -> dict:
    strategy = config.get("strategy")
    return strategy if isinstance(strategy, dict) else {}


def _entry_fingerprint(profile_id: str, option_symbol: str, direction: str, snapshot: MarketSnapshot, decision: ProfileDecision) -> str:
    timestamp = decision.timestamp or snapshot.timestamp
    return f"{profile_id}|{option_symbol}|{direction}|{timestamp}"


def _cooldown_active(state: BotRuntimeState, now_epoch: float) -> bool:
    cooldown_until = state.virtual_entry_cooldown_until_epoch
    return cooldown_until is not None and now_epoch < float(cooldown_until)


def _reject(decision: ProfileDecision, status: str, reason: str) -> None:
    decision.accepted = False
    decision.status = status
    decision.rejection_reason = reason
    decision.entry_status = "BLOCKED"
    decision.entry_block_reason = reason


def _block_entry(decision: ProfileDecision, reason: str) -> None:
    decision.entry_status = "BLOCKED"
    decision.entry_block_reason = reason


def _selected_option(snapshot: MarketSnapshot, direction: str) -> dict:
    if direction == "CALL":
        return {
            "symbol": snapshot.call_option_symbol or snapshot.option_symbol,
            "bid": snapshot.call_option_bid if snapshot.call_option_bid is not None else snapshot.option_bid,
            "ask": snapshot.call_option_ask if snapshot.call_option_ask is not None else snapshot.option_ask,
            "last": snapshot.call_option_last if snapshot.call_option_last is not None else snapshot.option_last,
            "midpoint": snapshot.call_option_midpoint if snapshot.call_option_midpoint is not None else snapshot.option_midpoint,
        }
    if direction == "PUT":
        return {
            "symbol": snapshot.put_option_symbol or snapshot.option_symbol,
            "bid": snapshot.put_option_bid if snapshot.put_option_bid is not None else snapshot.option_bid,
            "ask": snapshot.put_option_ask if snapshot.put_option_ask is not None else snapshot.option_ask,
            "last": snapshot.put_option_last if snapshot.put_option_last is not None else snapshot.option_last,
            "midpoint": snapshot.put_option_midpoint if snapshot.put_option_midpoint is not None else snapshot.option_midpoint,
        }
    return {"symbol": None, "bid": None, "ask": None, "last": None, "midpoint": None}


def _option_symbol_matches_direction(option_symbol: str | None, direction: str) -> bool:
    text = str(option_symbol or "").upper()
    if not text:
        return False
    if text.endswith("CALL") or text.endswith("PUT"):
        return text.endswith(direction)
    if len(text) >= 9 and ("C00" in text or "P00" in text):
        return ("C00" in text and direction == "CALL") or ("P00" in text and direction == "PUT")
    return True


def try_open_virtual_position(
    profile: BotProfile,
    state: BotRuntimeState,
    decision: ProfileDecision,
    snapshot: MarketSnapshot,
    now_epoch: float,
) -> TournamentTrade | None:
    config = profile.config or {}
    rules = _entry_rules(config)
    strategy = _strategy(config)
    direction = decision.direction
    selected_option = _selected_option(snapshot, direction)
    option_symbol = selected_option["symbol"]
    option_ask = selected_option["ask"]

    apply_daily_reset(profile, state, now_epoch)
    if profile.profile_id not in PROFILE_ORDER:
        return None
    if not profile.enabled or not state.enabled:
        return None
    if not decision.accepted:
        return None
    if not tournament_market_is_open(now_epoch):
        _reject(decision, "REJECTED", REJECT_MARKET_CLOSED)
        return None
    if direction not in {"CALL", "PUT"}:
        return None
    if not _option_symbol_matches_direction(option_symbol, direction):
        _reject(decision, "REJECTED", "OPTION_DIRECTION_MISMATCH")
        return None
    if state.virtual_position and state.virtual_position.status == "OPEN":
        _block_entry(decision, "POSITION_ALREADY_OPEN")
        return None
    if _cooldown_active(state, now_epoch):
        _block_entry(decision, "COOLDOWN_ACTIVE")
        return None

    max_trades = int(rules.get("max_trades_per_day", 1) or 1)
    if state.virtual_trades_today >= max_trades:
        _block_entry(decision, "MAX_TRADES_REACHED")
        return None
    if not option_symbol:
        _block_entry(decision, "OPTION_SYMBOL_MISSING")
        return None
    if option_ask is None or float(option_ask) <= 0:
        _block_entry(decision, "OPTION_ASK_INVALID")
        return None

    max_quote_age = int(config.get("max_quote_age_seconds", 10) or 10)
    fresh, stale_reason = quote_is_fresh(snapshot, max_quote_age, now_epoch)
    if not fresh:
        _reject(decision, "REJECTED", stale_reason or REJECT_INVALID_QUOTE_TIMESTAMP)
        return None

    max_contract_price = float(config.get("max_contract_price", 0) or 0)
    if max_contract_price > 0 and float(option_ask) > max_contract_price:
        _block_entry(decision, "MAX_CONTRACT_PRICE_EXCEEDED")
        return None

    contracts = int(config.get("contracts", 1) or 1)
    if contracts < 1:
        _block_entry(decision, "INVALID_CONTRACTS")
        return None

    fingerprint = _entry_fingerprint(profile.profile_id, option_symbol, direction, snapshot, decision)
    if state.last_entry_fingerprint == fingerprint:
        _block_entry(decision, "DUPLICATE_ENTRY_FINGERPRINT")
        return None

    timestamp = market_timestamp()
    entry_price = float(option_ask)
    entry_cost = entry_price * 100 * contracts
    display_name = profile_display_name(profile.profile_id)
    trade_id = generate_trade_id(profile.profile_id, option_symbol, timestamp)

    trade = TournamentTrade(
        trade_id=trade_id,
        profile_id=profile.profile_id,
        profile_display_name=display_name,
        symbol=snapshot.symbol,
        option_symbol=option_symbol,
        direction=direction,
        contracts=contracts,
        status="OPEN",
        entry_time=timestamp,
        entry_epoch=now_epoch,
        entry_price=entry_price,
        entry_price_source="ASK",
        entry_cost=entry_cost,
        peak_price=entry_price,
        lowest_price=entry_price,
        max_profit_dollars=0.0,
        max_drawdown_dollars=0.0,
        signal=snapshot.signal or "",
        confidence=snapshot.confidence,
        dominance_percent=snapshot.dominance_percent,
        bullish_score=snapshot.bullish_score,
        bearish_score=snapshot.bearish_score,
        market_state=snapshot.market_state,
        momentum_required=decision.momentum_required,
        or_confirmation_required=decision.or_confirmation_required,
        created_at=timestamp,
        updated_at=timestamp,
    )
    append_tournament_trade(trade, TOURNAMENT_TRADES_FILE)

    state.virtual_position = VirtualTournamentPosition(
        profile_id=profile.profile_id,
        profile_display_name=display_name,
        trade_id=trade_id,
        symbol=snapshot.symbol,
        option_symbol=option_symbol,
        direction=direction,
        contracts=contracts,
        status="OPEN",
        entry_time=timestamp,
        entry_epoch=now_epoch,
        entry_price=entry_price,
        entry_price_source="ASK",
        entry_cost=entry_cost,
        current_price=entry_price,
        current_price_source="ASK",
        current_value=entry_cost,
        unrealized_pnl_dollars=0.0,
        unrealized_pnl_percent=0.0,
        peak_price=entry_price,
        lowest_price=entry_price,
        max_profit_dollars=0.0,
        max_drawdown_dollars=0.0,
        hard_stop_percent=float(strategy.get("hard_stop_percent", 0) or 0),
        trailing_stop_percent=float(strategy.get("trailing_stop_percent", 0) or 0),
        enable_profit_floor_trailing_stop=bool(strategy.get("enable_profit_floor_trailing_stop", False)),
        locked_profit_amount=float(strategy.get("locked_profit_amount", 0) or 0),
        confidence=snapshot.confidence,
        dominance_percent=snapshot.dominance_percent,
        bullish_score=snapshot.bullish_score,
        bearish_score=snapshot.bearish_score,
        market_state=snapshot.market_state,
        momentum_required=decision.momentum_required,
        or_confirmation_required=decision.or_confirmation_required,
        created_at=timestamp,
        updated_at=timestamp,
    )
    state.last_virtual_entry_epoch = now_epoch
    state.virtual_trades_today += 1
    cooldown_minutes = int(rules.get("cooldown_minutes", 0) or 0)
    state.virtual_entry_cooldown_until_epoch = now_epoch + (cooldown_minutes * 60) if cooldown_minutes > 0 else None
    state.last_entry_fingerprint = fingerprint
    state.last_updated_at = timestamp
    decision.entry_status = "OPENED"
    decision.entry_block_reason = None
    return trade
