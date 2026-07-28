from __future__ import annotations

from .models import BotProfile, BotRuntimeState, ProfileDecision
from .snapshot import MarketSnapshot
from .state import market_timestamp


def _get_nested(config: dict, section: str, key: str, default):
    nested = config.get(section)
    if isinstance(nested, dict) and key in nested:
        return nested.get(key, default)
    return config.get(key, default)


def _decision(
    profile: BotProfile,
    state: BotRuntimeState,
    snapshot: MarketSnapshot,
    accepted: bool,
    direction: str | None,
    status: str,
    rejection_reason: str | None,
    momentum_required: bool,
    momentum_status: str,
    or_confirmation_required: bool,
    or_confirmation_status: str,
) -> ProfileDecision:
    decision = ProfileDecision(
        profile_id=profile.profile_id,
        timestamp=market_timestamp(),
        accepted=accepted,
        direction=direction,
        status=status,
        rejection_reason=rejection_reason,
        bullish_score=snapshot.bullish_score,
        bearish_score=snapshot.bearish_score,
        confidence=snapshot.confidence,
        dominance_percent=snapshot.dominance_percent,
        momentum_required=momentum_required,
        momentum_status=momentum_status,
        or_confirmation_required=or_confirmation_required,
        or_confirmation_status=or_confirmation_status,
    )
    state.last_decision = decision
    state.decisions_evaluated += 1
    state.last_updated_at = decision.timestamp
    return decision


def _winning_direction(snapshot: MarketSnapshot) -> str | None:
    if snapshot.bullish_score > snapshot.bearish_score:
        return "CALL"
    if snapshot.bearish_score > snapshot.bullish_score:
        return "PUT"
    return None


def _or_confirmed(snapshot: MarketSnapshot, direction: str, required_candles: int) -> bool:
    closes = snapshot.completed_closes[-required_candles:]
    if direction == "CALL":
        return all(close > snapshot.opening_range_high for close in closes)
    if direction == "PUT":
        return all(close < snapshot.opening_range_low for close in closes)
    return False


def evaluate_profile(
    profile: BotProfile,
    state: BotRuntimeState,
    snapshot: MarketSnapshot,
) -> ProfileDecision:
    config = profile.config or {}
    direction = snapshot.suggested_direction
    momentum_required = bool(config.get("option_momentum_confirmation_enabled", False))
    or_confirmation_required = bool(config.get("two_candle_or_confirmation_enabled", False))
    momentum_status = "WAITING" if momentum_required else "NOT_REQUIRED"
    or_confirmation_status = "WAITING" if or_confirmation_required else "NOT_REQUIRED"

    if not profile.enabled or not state.enabled or not config.get("bot_enabled", True):
        return _decision(profile, state, snapshot, False, direction, "DISABLED", "BOT_DISABLED", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)

    if direction not in {"CALL", "PUT"}:
        return _decision(profile, state, snapshot, False, None, "NO_SIGNAL", "NO_DIRECTION", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)

    winning_direction = _winning_direction(snapshot)
    if winning_direction is None:
        return _decision(profile, state, snapshot, False, direction, "NO_SIGNAL", "NO_DIRECTION", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)
    if direction != winning_direction:
        return _decision(profile, state, snapshot, False, direction, "INVALID_DIRECTION", "NO_DIRECTION", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)

    minimum_confidence = int(config.get("minimum_confidence", 0) or 0)
    if snapshot.confidence < minimum_confidence:
        return _decision(profile, state, snapshot, False, direction, "REJECTED", "CONFIDENCE_BELOW_MINIMUM", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)

    minimum_dominance = float(config.get("minimum_dominance_percent", 0) or 0)
    if snapshot.dominance_percent < minimum_dominance:
        return _decision(profile, state, snapshot, False, direction, "REJECTED", "DOMINANCE_BELOW_MINIMUM", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)

    minimum_signals = int(_get_nested(config, "entry_rules", "minimum_signals", 0) or 0)
    winning_score = snapshot.bullish_score if direction == "CALL" else snapshot.bearish_score
    if winning_score < minimum_signals:
        return _decision(profile, state, snapshot, False, direction, "REJECTED", "SIGNAL_COUNT_BELOW_MINIMUM", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)

    if direction == "CALL" and not bool(_get_nested(config, "entry_rules", "allow_calls", True)):
        return _decision(profile, state, snapshot, False, direction, "REJECTED", "CALLS_DISABLED", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)
    if direction == "PUT" and not bool(_get_nested(config, "entry_rules", "allow_puts", True)):
        return _decision(profile, state, snapshot, False, direction, "REJECTED", "PUTS_DISABLED", momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)

    if or_confirmation_required:
        required_candles = max(1, int(config.get("required_breakout_candles", 2) or 2))
        if not snapshot.opening_range_ready:
            return _decision(profile, state, snapshot, False, direction, "WAITING_OR_CONFIRMATION", "OPENING_RANGE_NOT_READY", momentum_required, momentum_status, or_confirmation_required, "WAITING")
        if len(snapshot.completed_closes) < required_candles:
            return _decision(profile, state, snapshot, False, direction, "WAITING_OR_CONFIRMATION", "INSUFFICIENT_COMPLETED_CANDLES", momentum_required, momentum_status, or_confirmation_required, "WAITING")
        if not _or_confirmed(snapshot, direction, required_candles):
            return _decision(profile, state, snapshot, False, direction, "WAITING_OR_CONFIRMATION", "TWO_CANDLE_OR_NOT_CONFIRMED", momentum_required, momentum_status, or_confirmation_required, "FAIL")
        or_confirmation_status = "PASS"

    if momentum_required:
        return _decision(profile, state, snapshot, False, direction, "WAITING_MOMENTUM", "MOMENTUM_CONFIRMATION_REQUIRED", momentum_required, "WAITING", or_confirmation_required, or_confirmation_status)

    return _decision(profile, state, snapshot, True, direction, "ACCEPTED", None, momentum_required, momentum_status, or_confirmation_required, or_confirmation_status)


def evaluate_all_profiles(
    profiles: dict[str, BotProfile],
    states: dict[str, BotRuntimeState],
    snapshot: MarketSnapshot,
) -> dict[str, ProfileDecision]:
    return {
        profile_id: evaluate_profile(profile, states[profile_id], snapshot)
        for profile_id, profile in profiles.items()
    }
