from __future__ import annotations

from dataclasses import replace

from .evaluator import evaluate_profile
from .models import BotProfile, BotRuntimeState, ProfileDecision, TournamentMomentumCandidate
from .option_symbols import option_symbol_matches_direction
from .recovery import REJECT_INVALID_QUOTE_TIMESTAMP, REJECT_QUOTE_STALE, quote_is_fresh
from .snapshot import MarketSnapshot


MOMENTUM_NOT_REQUIRED = "NOT_REQUIRED"
MOMENTUM_NO_CANDIDATE = "NO_CANDIDATE"
MOMENTUM_TRACKING = "TRACKING"
MOMENTUM_CONFIRMED = "CONFIRMED"
MOMENTUM_TIMEOUT = "TIMEOUT"
MOMENTUM_DIRECTION_CHANGED = "DIRECTION_CHANGED"
MOMENTUM_OPTION_CHANGED = "OPTION_CHANGED"
MOMENTUM_DRAWDOWN_FAILED = "DRAWDOWN_FAILED"
MOMENTUM_INVALID_QUOTE = "INVALID_QUOTE"
MOMENTUM_QUOTE_STALE = "QUOTE_STALE"


def _float_or_none(value):
    try:
        if value is None:
            return None
        value = float(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _config_float(config: dict, *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in config:
            try:
                return float(config.get(key))
            except (TypeError, ValueError):
                return default
    return default


def _config_int(config: dict, key: str, default: int) -> int:
    try:
        return int(config.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _quote_price_from_row(row: dict | None) -> tuple[float | None, str | None]:
    if not isinstance(row, dict):
        return None, None
    bid = _float_or_none(row.get("bid"))
    ask = _float_or_none(row.get("ask"))
    last = _float_or_none(row.get("last"))
    midpoint = _float_or_none(row.get("midpoint"))
    if midpoint is not None:
        return midpoint, "MIDPOINT"
    if bid is not None and ask is not None:
        return (bid + ask) / 2, "MIDPOINT"
    if ask is not None:
        return ask, "ASK"
    if last is not None:
        return last, "LAST"
    if bid is not None:
        return bid, "BID"
    return None, None


def option_price_for_momentum(snapshot: MarketSnapshot, direction: str, locked_option_symbol: str | None = None) -> tuple[float | None, str | None, str | None]:
    if locked_option_symbol and not option_symbol_matches_direction(locked_option_symbol, direction):
        return None, None, locked_option_symbol
    if locked_option_symbol and isinstance(snapshot.locked_option_quotes, dict):
        locked_quote = snapshot.locked_option_quotes.get(locked_option_symbol)
        price, source = _quote_price_from_row(locked_quote)
        if price is not None:
            return price, source, locked_option_symbol

    if direction == "CALL":
        bid = _float_or_none(snapshot.call_option_bid)
        ask = _float_or_none(snapshot.call_option_ask)
        last = _float_or_none(snapshot.call_option_last)
        symbol = snapshot.call_option_symbol
    elif direction == "PUT":
        bid = _float_or_none(snapshot.put_option_bid)
        ask = _float_or_none(snapshot.put_option_ask)
        last = _float_or_none(snapshot.put_option_last)
        symbol = snapshot.put_option_symbol
    else:
        return None, None, None

    if symbol and not option_symbol_matches_direction(symbol, direction):
        return None, None, symbol

    if bid is not None and ask is not None:
        return (bid + ask) / 2, "MIDPOINT", symbol
    if ask is not None:
        return ask, "ASK", symbol
    if last is not None:
        return last, "LAST", symbol
    return None, None, symbol


def _candidate_active(candidate: TournamentMomentumCandidate | None) -> bool:
    return candidate is not None and candidate.status == MOMENTUM_TRACKING


def _movement_percent(starting_price: float, current_price: float) -> float:
    return ((current_price - starting_price) / starting_price) * 100 if starting_price > 0 else 0.0


def _drawdown_percent(starting_price: float, current_price: float) -> float:
    return max(0.0, ((starting_price - current_price) / starting_price) * 100) if starting_price > 0 else 0.0


def _counter(state: BotRuntimeState, key: str) -> None:
    counters = state.pipeline_counters if isinstance(state.pipeline_counters, dict) else {}
    counters[key] = int(counters.get(key, 0) or 0) + 1
    state.pipeline_counters = counters


def _transition(
    state: BotRuntimeState,
    profile: BotProfile,
    old_direction: str | None,
    new_direction: str | None,
    old_option: str | None,
    new_option: str | None,
    status: str,
    reason: str | None,
    candidate_age: float,
    now_epoch: float,
) -> None:
    row = {
        "time": now_epoch,
        "profile_id": profile.profile_id,
        "profile_name": profile.display_name,
        "old_direction": old_direction,
        "new_direction": new_direction,
        "old_option": old_option,
        "new_option": new_option,
        "status": status,
        "reason": reason,
        "candidate_age": max(0.0, candidate_age),
    }
    history = list(state.candidate_transitions or [])
    history.append(row)
    state.candidate_transitions = history[-20:]


def _apply_trace(decision: ProfileDecision, candidate: TournamentMomentumCandidate | None, status: str, reason: str | None, now_epoch: float) -> None:
    decision.momentum_status = status
    decision.momentum_block_reason = reason
    if candidate is None:
        decision.momentum_candidate_direction = None
        decision.momentum_candidate_option_symbol = None
        decision.momentum_starting_price = None
        decision.momentum_current_price = None
        decision.momentum_observed_percent = 0.0
        decision.momentum_required_percent = 0.0
        decision.momentum_candidate_age_seconds = 0.0
        decision.momentum_time_remaining_seconds = 0.0
        return

    decision.momentum_candidate_direction = candidate.direction
    decision.momentum_candidate_option_symbol = candidate.option_symbol
    decision.momentum_starting_price = candidate.starting_option_price
    decision.momentum_current_price = candidate.current_option_price
    decision.momentum_observed_percent = candidate.observed_movement_percent
    decision.momentum_required_percent = candidate.required_movement_percent
    decision.momentum_candidate_age_seconds = max(0.0, now_epoch - candidate.started_epoch)
    decision.momentum_time_remaining_seconds = max(0.0, candidate.deadline_epoch - now_epoch)


def _cancel_candidate(
    state: BotRuntimeState,
    candidate: TournamentMomentumCandidate,
    status: str,
    reason: str,
    now_epoch: float,
    retry_cooldown_seconds: int,
) -> TournamentMomentumCandidate:
    cancelled = replace(
        candidate,
        status=status,
        block_reason=reason,
        cancelled_epoch=now_epoch,
        retry_until_epoch=now_epoch + max(0, retry_cooldown_seconds),
    )
    state.momentum_candidate = cancelled
    return cancelled


def update_profile_momentum(
    profile: BotProfile,
    state: BotRuntimeState,
    decision: ProfileDecision,
    snapshot: MarketSnapshot,
    now_epoch: float,
) -> bool:
    config = profile.config or {}
    if not bool(config.get("option_momentum_confirmation_enabled", False)):
        state.momentum_candidate = None
        decision.momentum_passed = True
        _apply_trace(decision, None, MOMENTUM_NOT_REQUIRED, None, now_epoch)
        return False

    direction = decision.preliminary_direction or decision.direction
    if direction not in {"CALL", "PUT"}:
        candidate = state.momentum_candidate
        if _candidate_active(candidate):
            retry = _config_int(config, "pending_entry_retry_cooldown_seconds", 60)
            candidate = _cancel_candidate(state, candidate, MOMENTUM_DIRECTION_CHANGED, "no dominant direction", now_epoch, retry)
            _apply_trace(decision, candidate, MOMENTUM_DIRECTION_CHANGED, "no dominant direction", now_epoch)
        else:
            _apply_trace(decision, candidate, MOMENTUM_NO_CANDIDATE, "no dominant direction", now_epoch)
        decision.momentum_passed = False
        return False

    max_quote_age = _config_int(config, "max_quote_age_seconds", 10)
    fresh, freshness_reason = quote_is_fresh(snapshot, max_quote_age, now_epoch)
    if not fresh:
        status = MOMENTUM_QUOTE_STALE if freshness_reason == REJECT_QUOTE_STALE else MOMENTUM_INVALID_QUOTE
        candidate = state.momentum_candidate
        if _candidate_active(candidate):
            retry = _config_int(config, "pending_entry_retry_cooldown_seconds", 60)
            old_candidate = candidate
            candidate = _cancel_candidate(state, candidate, status, freshness_reason or REJECT_INVALID_QUOTE_TIMESTAMP, now_epoch, retry)
            _counter(state, "candidate_quote_stale")
            _transition(state, profile, old_candidate.direction, direction, old_candidate.option_symbol, old_candidate.option_symbol, status, freshness_reason or REJECT_INVALID_QUOTE_TIMESTAMP, now_epoch - old_candidate.started_epoch, now_epoch)
        _apply_trace(decision, candidate, status, freshness_reason or REJECT_INVALID_QUOTE_TIMESTAMP, now_epoch)
        decision.momentum_passed = False
        return False

    candidate = state.momentum_candidate
    retry_cooldown_seconds = _config_int(config, "pending_entry_retry_cooldown_seconds", 60)
    if _candidate_active(candidate) and candidate.direction != direction:
        old_candidate = candidate
        candidate = _cancel_candidate(state, candidate, MOMENTUM_DIRECTION_CHANGED, "direction changed", now_epoch, retry_cooldown_seconds)
        _counter(state, "candidate_direction_changed")
        _transition(state, profile, old_candidate.direction, direction, old_candidate.option_symbol, None, MOMENTUM_DIRECTION_CHANGED, "direction changed", now_epoch - old_candidate.started_epoch, now_epoch)
        _apply_trace(decision, candidate, MOMENTUM_DIRECTION_CHANGED, "direction changed", now_epoch)
        decision.momentum_passed = False
        return False

    locked_option_symbol = candidate.option_symbol if _candidate_active(candidate) or (candidate and candidate.status == MOMENTUM_CONFIRMED) else None
    _, _, selected_option_symbol = option_price_for_momentum(snapshot, direction, None)
    current_price, price_source, option_symbol = option_price_for_momentum(snapshot, direction, locked_option_symbol)
    if current_price is None or not option_symbol:
        if _candidate_active(candidate):
            retry = _config_int(config, "pending_entry_retry_cooldown_seconds", 60)
            candidate = _cancel_candidate(state, candidate, MOMENTUM_INVALID_QUOTE, "invalid option quote", now_epoch, retry)
            _transition(state, profile, candidate.direction, direction, candidate.option_symbol, option_symbol, MOMENTUM_INVALID_QUOTE, "invalid option quote", now_epoch - candidate.started_epoch, now_epoch)
        _apply_trace(decision, candidate, MOMENTUM_INVALID_QUOTE, "invalid option quote", now_epoch)
        decision.momentum_passed = False
        return False

    required_percent = _config_float(config, "option_momentum_confirmation_percent", "option_momentum_percent", "momentum_percent", default=1.0)
    timeout_seconds = _config_int(config, "confirmation_timeout_seconds", 60)
    max_drawdown_percent = _config_float(config, "pre_confirmation_max_drawdown_percent", default=5.0)

    if candidate and candidate.status == MOMENTUM_CONFIRMED and candidate.direction == direction:
        observed = _movement_percent(candidate.starting_option_price, current_price)
        candidate = replace(
            candidate,
            current_option_price=current_price,
            highest_candidate_price=max(candidate.highest_candidate_price, current_price),
            lowest_candidate_price=min(candidate.lowest_candidate_price, current_price),
            observed_movement_percent=observed,
            required_movement_percent=required_percent,
        )
        state.momentum_candidate = candidate
        _apply_trace(decision, candidate, MOMENTUM_CONFIRMED, None, now_epoch)
        decision.momentum_passed = True
        return True

    if candidate and candidate.retry_until_epoch and now_epoch < candidate.retry_until_epoch and not _candidate_active(candidate):
        _apply_trace(decision, candidate, candidate.status, f"retry cooldown active; {int(candidate.retry_until_epoch - now_epoch)} seconds remaining", now_epoch)
        decision.momentum_passed = False
        return False

    if not _candidate_active(candidate):
        candidate = TournamentMomentumCandidate(
            direction=direction,
            option_symbol=option_symbol,
            starting_underlying_price=snapshot.current_price,
            starting_option_price=current_price,
            started_epoch=now_epoch,
            deadline_epoch=now_epoch + timeout_seconds,
            highest_candidate_price=current_price,
            lowest_candidate_price=current_price,
            observed_movement_percent=0.0,
            required_movement_percent=required_percent,
            status=MOMENTUM_TRACKING,
            current_option_price=current_price,
        )
        state.momentum_candidate = candidate
        _counter(state, "candidate_created")
        _transition(state, profile, None, direction, None, option_symbol, MOMENTUM_TRACKING, "candidate created", 0.0, now_epoch)
        if required_percent <= 0:
            candidate = replace(candidate, status=MOMENTUM_CONFIRMED, block_reason=None)
            state.momentum_candidate = candidate
            _counter(state, "candidate_confirmed")
            _transition(state, profile, direction, direction, option_symbol, option_symbol, MOMENTUM_CONFIRMED, "zero percent momentum confirmed on creation", 0.0, now_epoch)
            _apply_trace(decision, candidate, MOMENTUM_CONFIRMED, None, now_epoch)
            decision.momentum_passed = True
            return True
        _apply_trace(decision, candidate, MOMENTUM_TRACKING, "candidate created", now_epoch)
        decision.momentum_passed = False
        return False

    if selected_option_symbol and candidate.option_symbol != selected_option_symbol:
        _counter(state, "candidate_option_changed")
        _transition(state, profile, candidate.direction, direction, candidate.option_symbol, selected_option_symbol, MOMENTUM_OPTION_CHANGED, "selector changed; candidate stayed locked", now_epoch - candidate.started_epoch, now_epoch)

    _counter(state, "candidate_survived_next_cycle")
    high = max(candidate.highest_candidate_price, current_price)
    low = min(candidate.lowest_candidate_price, current_price)
    observed = _movement_percent(candidate.starting_option_price, current_price)
    candidate = replace(
        candidate,
        current_option_price=current_price,
        highest_candidate_price=high,
        lowest_candidate_price=low,
        observed_movement_percent=observed,
        required_movement_percent=required_percent,
    )
    state.momentum_candidate = candidate

    if max_drawdown_percent > 0 and _drawdown_percent(candidate.starting_option_price, current_price) >= max_drawdown_percent:
        candidate = _cancel_candidate(state, candidate, MOMENTUM_DRAWDOWN_FAILED, "pre-confirmation drawdown exceeded", now_epoch, retry_cooldown_seconds)
        _counter(state, "candidate_drawdown_failed")
        _transition(state, profile, candidate.direction, direction, candidate.option_symbol, option_symbol, MOMENTUM_DRAWDOWN_FAILED, candidate.block_reason, now_epoch - candidate.started_epoch, now_epoch)
        _apply_trace(decision, candidate, MOMENTUM_DRAWDOWN_FAILED, candidate.block_reason, now_epoch)
        decision.momentum_passed = False
        return False

    if now_epoch >= candidate.deadline_epoch:
        candidate = _cancel_candidate(state, candidate, MOMENTUM_TIMEOUT, "confirmation timeout expired", now_epoch, retry_cooldown_seconds)
        _counter(state, "candidate_timed_out")
        _transition(state, profile, candidate.direction, direction, candidate.option_symbol, option_symbol, MOMENTUM_TIMEOUT, candidate.block_reason, now_epoch - candidate.started_epoch, now_epoch)
        _apply_trace(decision, candidate, MOMENTUM_TIMEOUT, candidate.block_reason, now_epoch)
        decision.momentum_passed = False
        return False

    if observed >= required_percent:
        candidate = replace(candidate, status=MOMENTUM_CONFIRMED, block_reason=None)
        state.momentum_candidate = candidate
        _counter(state, "candidate_confirmed")
        _transition(state, profile, candidate.direction, direction, candidate.option_symbol, option_symbol, MOMENTUM_CONFIRMED, None, now_epoch - candidate.started_epoch, now_epoch)
        _apply_trace(decision, candidate, MOMENTUM_CONFIRMED, None, now_epoch)
        decision.momentum_passed = True
        return True

    _apply_trace(decision, candidate, MOMENTUM_TRACKING, f"movement {observed:.2f}% below required {required_percent:.2f}%", now_epoch)
    decision.momentum_passed = False
    return False


def apply_tournament_momentum(
    profiles: dict[str, BotProfile],
    states: dict[str, BotRuntimeState],
    decisions: dict[str, ProfileDecision],
    snapshot: MarketSnapshot,
    now_epoch: float,
) -> dict[str, ProfileDecision]:
    updated_decisions = dict(decisions)
    for profile_id, profile in profiles.items():
        state = states[profile_id]
        decision = updated_decisions[profile_id]
        confirmed = update_profile_momentum(profile, state, decision, snapshot, now_epoch)
        if confirmed:
            confirmed_snapshot = replace(snapshot, momentum_confirmed=True)
            confirmed_decision = evaluate_profile(profile, state, confirmed_snapshot)
            candidate = state.momentum_candidate
            _apply_trace(confirmed_decision, candidate, MOMENTUM_CONFIRMED, None, now_epoch)
            confirmed_decision.momentum_passed = True
            updated_decisions[profile_id] = confirmed_decision
    return updated_decisions
