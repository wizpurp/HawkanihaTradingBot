from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import BotMetrics, BotProfile, BotRuntimeState, PendingEntryState, ProfileDecision, VirtualPosition


MARKET_TZ = ZoneInfo("America/New_York")


def market_timestamp() -> str:
    return datetime.now(MARKET_TZ).isoformat()


def _coerce_dataclass(model_type, value):
    if is_dataclass(value):
        return value
    if not isinstance(value, dict):
        value = {}
    allowed_fields = {field.name for field in fields(model_type)}
    return model_type(**{key: value.get(key) for key in allowed_fields if key in value})


def create_initial_state(profile: BotProfile) -> BotRuntimeState:
    starting_balance = float(
        profile.config.get(
            "bot_starting_account_balance",
            profile.config.get("starting_balance", 0.0),
        )
        or 0.0
    )
    return BotRuntimeState(
        profile_id=profile.profile_id,
        virtual_balance=starting_balance,
        starting_balance=starting_balance,
        position=VirtualPosition(),
        pending_entry=PendingEntryState(),
        metrics=BotMetrics(),
        cooldown_until=None,
        last_updated_at=market_timestamp(),
        enabled=profile.enabled,
    )


def create_all_initial_states(
    profiles: dict[str, BotProfile]
) -> dict[str, BotRuntimeState]:
    return {
        profile_id: create_initial_state(profile)
        for profile_id, profile in profiles.items()
    }


def _state_from_dict(profile_id: str, value: dict, profile: BotProfile) -> BotRuntimeState:
    if not isinstance(value, dict):
        raise ValueError("state row must be a dictionary")

    position = _coerce_dataclass(VirtualPosition, value.get("position"))
    pending_entry = _coerce_dataclass(PendingEntryState, value.get("pending_entry"))
    metrics = _coerce_dataclass(BotMetrics, value.get("metrics"))
    last_decision = None
    if isinstance(value.get("last_decision"), dict):
        last_decision = _coerce_dataclass(ProfileDecision, value.get("last_decision"))
    starting_balance = float(value.get("starting_balance", 0.0) or 0.0)

    return BotRuntimeState(
        profile_id=str(value.get("profile_id") or profile_id),
        virtual_balance=float(value.get("virtual_balance", starting_balance) or 0.0),
        starting_balance=starting_balance,
        position=position,
        pending_entry=pending_entry,
        metrics=metrics,
        cooldown_until=value.get("cooldown_until"),
        last_updated_at=value.get("last_updated_at") or market_timestamp(),
        enabled=bool(value.get("enabled", profile.enabled)),
        last_decision=last_decision,
        decisions_evaluated=int(value.get("decisions_evaluated", 0) or 0),
    )


def _serialize_states(states: dict[str, BotRuntimeState]) -> dict:
    return {
        profile_id: asdict(state)
        for profile_id, state in states.items()
    }


def load_tournament_state(
    path: str,
    profiles: dict[str, BotProfile]
) -> dict[str, BotRuntimeState]:
    if not os.path.exists(path):
        return create_all_initial_states(profiles)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return create_all_initial_states(profiles)

    if not isinstance(raw, dict):
        return create_all_initial_states(profiles)

    states: dict[str, BotRuntimeState] = {}
    for profile_id, profile in profiles.items():
        if profile_id not in raw:
            states[profile_id] = create_initial_state(profile)
            continue
        try:
            states[profile_id] = _state_from_dict(profile_id, raw[profile_id], profile)
        except Exception:
            states[profile_id] = create_initial_state(profile)

    return states


def save_tournament_state(
    path: str,
    states: dict[str, BotRuntimeState]
) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    payload = _serialize_states(states)

    fd, temp_path = tempfile.mkstemp(
        prefix=".tournament_state.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
