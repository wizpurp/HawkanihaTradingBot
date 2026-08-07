from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import BotMetrics, BotProfile, BotRuntimeState, PendingEntryState, ProfileDecision, TournamentMomentumCandidate, VirtualPosition, VirtualTournamentPosition, default_pipeline_counters


MARKET_TZ = ZoneInfo("America/New_York")
TOURNAMENT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "tournament_state.json",
)
TOURNAMENT_STATE_BACKUP_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "tournament_state.backup.json",
)
LAST_STATE_LOAD_SOURCE = "EMPTY"


def market_timestamp() -> str:
    return datetime.now(MARKET_TZ).isoformat()


def market_date() -> str:
    return datetime.now(MARKET_TZ).date().isoformat()


def backup_path_for(path: str) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    filename = os.path.basename(path)
    if filename == "tournament_state.json":
        return os.path.join(directory, "tournament_state.backup.json")
    if filename.endswith(".json"):
        return os.path.join(directory, filename[:-5] + ".backup.json")
    return path + ".backup"


def read_json_file(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: str, payload, preserve_previous: bool = True) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    backup_path = backup_path_for(path)
    if preserve_previous and os.path.exists(path):
        try:
            shutil.copy2(path, backup_path)
        except OSError:
            pass

    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def load_json_with_backup(path: str, empty_value):
    if os.path.exists(path):
        try:
            return read_json_file(path), "MAIN"
        except Exception:
            pass

    backup_path = backup_path_for(path)
    if os.path.exists(backup_path):
        try:
            payload = read_json_file(backup_path)
            write_json_atomic(path, payload, preserve_previous=False)
            return payload, "BACKUP"
        except Exception:
            pass

    return empty_value, "EMPTY"


def _coerce_dataclass(model_type, value):
    if is_dataclass(value):
        return value
    if not isinstance(value, dict):
        value = {}
    if model_type is VirtualTournamentPosition and "locked_profit_dollars" not in value and "locked_profit_amount" in value:
        value = dict(value)
        value["locked_profit_dollars"] = value.get("locked_profit_amount")
    allowed_fields = {field.name for field in fields(model_type)}
    return model_type(**{key: value.get(key) for key in allowed_fields if key in value})


def _coerce_pipeline_counters(value) -> dict:
    counters = default_pipeline_counters()
    if not isinstance(value, dict):
        return counters
    for key in counters:
        if key == "entry_block_reasons":
            reasons = value.get(key)
            counters[key] = dict(reasons) if isinstance(reasons, dict) else {}
        else:
            counters[key] = int(value.get(key, 0) or 0)
    return counters


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
        virtual_trading_date=market_date(),
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
    virtual_position = None
    if isinstance(value.get("virtual_position"), dict):
        virtual_position = _coerce_dataclass(VirtualTournamentPosition, value.get("virtual_position"))
    momentum_candidate = None
    if isinstance(value.get("momentum_candidate"), dict):
        momentum_candidate = _coerce_dataclass(TournamentMomentumCandidate, value.get("momentum_candidate"))
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
        virtual_position=virtual_position,
        last_virtual_entry_epoch=value.get("last_virtual_entry_epoch"),
        virtual_trades_today=int(value.get("virtual_trades_today", 0) or 0),
        virtual_entry_cooldown_until_epoch=value.get("virtual_entry_cooldown_until_epoch"),
        last_entry_fingerprint=value.get("last_entry_fingerprint"),
        virtual_trading_date=value.get("virtual_trading_date"),
        last_recovery_time=value.get("last_recovery_time"),
        recovery_count=int(value.get("recovery_count", 0) or 0),
        recovery_status=value.get("recovery_status"),
        recovered_trade_id=value.get("recovered_trade_id"),
        momentum_candidate=momentum_candidate,
        pipeline_counters=_coerce_pipeline_counters(value.get("pipeline_counters")),
        candidate_transitions=list(value.get("candidate_transitions") or [])[-20:] if isinstance(value.get("candidate_transitions"), list) else [],
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
    global LAST_STATE_LOAD_SOURCE
    raw, source = load_json_with_backup(path, {})
    LAST_STATE_LOAD_SOURCE = source

    if not isinstance(raw, dict):
        LAST_STATE_LOAD_SOURCE = "EMPTY"
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
    payload = _serialize_states(states)
    write_json_atomic(path, payload)
