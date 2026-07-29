from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy

from .models import BotProfile


TOURNAMENT_PROFILES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "tournament_profiles.json",
)

EXPERIMENT_KEYS = {
    "option_momentum_confirmation_enabled",
    "option_momentum_percent",
    "confirmation_timeout_seconds",
    "pre_confirmation_max_drawdown_percent",
    "pending_entry_retry_cooldown_seconds",
    "two_candle_or_confirmation_enabled",
    "required_breakout_candles",
}


PROFILE_ORDER = [
    "BOT_A_BASELINE",
    "BOT_B_MOMENTUM",
    "BOT_C_TWO_CANDLE_OR",
    "BOT_D_COMBINED",
]


DEFAULT_SHARED_SETTINGS = {
    "minimum_confidence": 4,
    "minimum_dominance_percent": 70,
    "minimum_signals": 4,
    "direction_threshold_percent": 60,
    "allow_calls": True,
    "allow_puts": True,
    "hard_stop_percent": 12,
    "trailing_stop_percent": 12,
    "enable_profit_floor_trailing_stop": True,
    "locked_profit_amount": 0.05,
    "cooldown_minutes": 1,
    "max_trades_per_day": 15,
    "max_contract_price": 1.50,
    "contract_selection_mode": "closest_within_budget",
    "contracts": 1,
    "option_momentum_percent": 1.0,
    "confirmation_timeout_seconds": 60,
    "pre_confirmation_max_drawdown_percent": 5.0,
    "pending_entry_retry_cooldown_seconds": 60,
    "required_breakout_candles": 2,
    "max_quote_age_seconds": 10,
}


PROFILE_DEFINITIONS = {
    "BOT_A_BASELINE": {
        "display_name": "Bot A Baseline",
        "overrides": {
            "option_momentum_confirmation_enabled": False,
            "two_candle_or_confirmation_enabled": False,
        },
    },
    "BOT_B_MOMENTUM": {
        "display_name": "Bot B Momentum",
        "overrides": {
            "option_momentum_confirmation_enabled": True,
            "option_momentum_percent": 1.0,
            "confirmation_timeout_seconds": 60,
            "pre_confirmation_max_drawdown_percent": 5.0,
            "pending_entry_retry_cooldown_seconds": 60,
            "two_candle_or_confirmation_enabled": False,
        },
    },
    "BOT_C_TWO_CANDLE_OR": {
        "display_name": "Bot C Two-Candle OR",
        "overrides": {
            "option_momentum_confirmation_enabled": False,
            "two_candle_or_confirmation_enabled": True,
            "required_breakout_candles": 2,
        },
    },
    "BOT_D_COMBINED": {
        "display_name": "Bot D Combined",
        "overrides": {
            "option_momentum_confirmation_enabled": True,
            "option_momentum_percent": 1.0,
            "confirmation_timeout_seconds": 60,
            "pre_confirmation_max_drawdown_percent": 5.0,
            "pending_entry_retry_cooldown_seconds": 60,
            "two_candle_or_confirmation_enabled": True,
            "required_breakout_candles": 2,
        },
    },
}


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_range(errors, profile_id, key, value, minimum=None, maximum=None):
    if minimum is not None and value < minimum:
        errors.append(f"{profile_id}.{key} below {minimum}")
    if maximum is not None and value > maximum:
        errors.append(f"{profile_id}.{key} above {maximum}")


def default_tournament_profile_settings() -> dict[str, dict]:
    defaults: dict[str, dict] = {}
    for profile_id in PROFILE_ORDER:
        definition = PROFILE_DEFINITIONS[profile_id]
        row = {
            "profile_id": profile_id,
            "display_name": definition["display_name"],
            "enabled": True,
            **deepcopy(DEFAULT_SHARED_SETTINGS),
        }
        row.update(deepcopy(definition["overrides"]))
        defaults[profile_id] = row
    return defaults


def normalize_profile_settings(raw_settings: dict | None, *, strict: bool = False) -> tuple[dict[str, dict], list[str]]:
    defaults = default_tournament_profile_settings()
    raw_settings = raw_settings if isinstance(raw_settings, dict) else {}
    errors: list[str] = []
    normalized: dict[str, dict] = {}

    for profile_id in PROFILE_ORDER:
        raw = raw_settings.get(profile_id, {})
        if not isinstance(raw, dict):
            raw = {}
        base = deepcopy(defaults[profile_id])
        base.update(raw)
        base["profile_id"] = profile_id
        base["display_name"] = str(base.get("display_name") or defaults[profile_id]["display_name"])
        base["enabled"] = _bool(base.get("enabled"), True)

        numeric_rules = {
            "minimum_confidence": ("int", 0, None, defaults[profile_id]["minimum_confidence"]),
            "minimum_dominance_percent": ("float", 0, 100, defaults[profile_id]["minimum_dominance_percent"]),
            "minimum_signals": ("int", 1, None, defaults[profile_id]["minimum_signals"]),
            "direction_threshold_percent": ("float", 0, 100, defaults[profile_id]["direction_threshold_percent"]),
            "option_momentum_percent": ("float", 0, None, defaults[profile_id]["option_momentum_percent"]),
            "confirmation_timeout_seconds": ("int", 1, None, defaults[profile_id]["confirmation_timeout_seconds"]),
            "pre_confirmation_max_drawdown_percent": ("float", 0, 100, defaults[profile_id]["pre_confirmation_max_drawdown_percent"]),
            "pending_entry_retry_cooldown_seconds": ("int", 0, None, defaults[profile_id]["pending_entry_retry_cooldown_seconds"]),
            "required_breakout_candles": ("int", 1, None, defaults[profile_id]["required_breakout_candles"]),
            "hard_stop_percent": ("float", 0, 100, defaults[profile_id]["hard_stop_percent"]),
            "trailing_stop_percent": ("float", 0, 100, defaults[profile_id]["trailing_stop_percent"]),
            "locked_profit_amount": ("float", 0, None, defaults[profile_id]["locked_profit_amount"]),
            "cooldown_minutes": ("int", 0, None, defaults[profile_id]["cooldown_minutes"]),
            "max_trades_per_day": ("int", 1, None, defaults[profile_id]["max_trades_per_day"]),
            "max_contract_price": ("float", 0, None, defaults[profile_id]["max_contract_price"]),
            "contracts": ("int", 1, None, defaults[profile_id]["contracts"]),
            "max_quote_age_seconds": ("int", 1, None, defaults[profile_id]["max_quote_age_seconds"]),
        }
        for key, (kind, minimum, maximum, default) in numeric_rules.items():
            value = _int(base.get(key), default) if kind == "int" else _float(base.get(key), default)
            _validate_range(errors, profile_id, key, value, minimum, maximum)
            base[key] = value

        for key in [
            "option_momentum_confirmation_enabled",
            "two_candle_or_confirmation_enabled",
            "allow_calls",
            "allow_puts",
            "enable_profit_floor_trailing_stop",
        ]:
            base[key] = _bool(base.get(key), defaults[profile_id].get(key, False))

        mode = str(base.get("contract_selection_mode") or defaults[profile_id]["contract_selection_mode"])
        if mode not in {"strict_atm", "closest_within_budget"}:
            errors.append(f"{profile_id}.contract_selection_mode invalid")
            mode = defaults[profile_id]["contract_selection_mode"]
        base["contract_selection_mode"] = mode
        normalized[profile_id] = base

    if strict and errors:
        raise ValueError("; ".join(errors))
    return normalized, errors


def load_tournament_profile_settings(path: str = TOURNAMENT_PROFILES_FILE) -> dict[str, dict]:
    if not os.path.exists(path):
        return default_tournament_profile_settings()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return default_tournament_profile_settings()
    normalized, errors = normalize_profile_settings(raw, strict=False)
    if errors:
        return default_tournament_profile_settings()
    return normalized


def save_tournament_profile_settings(settings: dict, path: str = TOURNAMENT_PROFILES_FILE) -> dict[str, dict]:
    normalized, _ = normalize_profile_settings(settings, strict=True)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tournament_profiles.", suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return normalized


def reset_tournament_profile_settings(path: str = TOURNAMENT_PROFILES_FILE) -> dict[str, dict]:
    return save_tournament_profile_settings(default_tournament_profile_settings(), path)


def copy_tournament_profile_settings(source_profile_id: str, copy_mode: str, path: str = TOURNAMENT_PROFILES_FILE) -> dict[str, dict]:
    settings = load_tournament_profile_settings(path)
    if source_profile_id not in settings:
        raise ValueError("unknown source_profile_id")
    if copy_mode not in {"SHARED_ONLY", "EVERYTHING"}:
        raise ValueError("copy_mode must be SHARED_ONLY or EVERYTHING")

    source = deepcopy(settings[source_profile_id])
    for profile_id, destination in settings.items():
        if profile_id == source_profile_id:
            continue
        preserved = {
            key: destination.get(key)
            for key in EXPERIMENT_KEYS
        }
        display_name = destination.get("display_name")
        settings[profile_id].update(deepcopy(source))
        settings[profile_id]["profile_id"] = profile_id
        settings[profile_id]["display_name"] = display_name
        if copy_mode == "SHARED_ONLY":
            settings[profile_id].update(preserved)

    return save_tournament_profile_settings(settings, path)


def _apply_profile_settings(config: dict, settings: dict) -> dict:
    config["minimum_confidence"] = settings["minimum_confidence"]
    config["minimum_dominance_percent"] = settings["minimum_dominance_percent"]
    config["max_contract_price"] = settings["max_contract_price"]
    config["contract_selection_mode"] = settings["contract_selection_mode"]
    config["contracts"] = settings["contracts"]
    config["option_momentum_confirmation_enabled"] = settings["option_momentum_confirmation_enabled"]
    config["option_momentum_percent"] = settings["option_momentum_percent"]
    config["confirmation_timeout_seconds"] = settings["confirmation_timeout_seconds"]
    config["pre_confirmation_max_drawdown_percent"] = settings["pre_confirmation_max_drawdown_percent"]
    config["pending_entry_retry_cooldown_seconds"] = settings["pending_entry_retry_cooldown_seconds"]
    config["two_candle_or_confirmation_enabled"] = settings["two_candle_or_confirmation_enabled"]
    config["required_breakout_candles"] = settings["required_breakout_candles"]
    config["max_quote_age_seconds"] = settings["max_quote_age_seconds"]

    entry_rules = config.setdefault("entry_rules", {})
    entry_rules["minimum_signals"] = settings["minimum_signals"]
    entry_rules["allow_calls"] = settings["allow_calls"]
    entry_rules["allow_puts"] = settings["allow_puts"]
    entry_rules["cooldown_minutes"] = settings["cooldown_minutes"]
    entry_rules["max_trades_per_day"] = settings["max_trades_per_day"]

    strategy = config.setdefault("strategy", {})
    strategy["direction_threshold_percent"] = settings["direction_threshold_percent"]
    strategy["hard_stop_percent"] = settings["hard_stop_percent"]
    strategy["trailing_stop_percent"] = settings["trailing_stop_percent"]
    strategy["enable_profit_floor_trailing_stop"] = settings["enable_profit_floor_trailing_stop"]
    strategy["locked_profit_amount"] = settings["locked_profit_amount"]
    return config


def _settings_from_runtime(runtime_config: dict | None) -> dict[str, dict]:
    runtime_config = runtime_config or {}
    strategy = runtime_config.get("strategy", {}) if isinstance(runtime_config.get("strategy"), dict) else {}
    entry_rules = runtime_config.get("entry_rules", {}) if isinstance(runtime_config.get("entry_rules"), dict) else {}
    settings = default_tournament_profile_settings()
    runtime_field_sources = {
        "minimum_confidence": runtime_config,
        "minimum_dominance_percent": runtime_config,
        "max_contract_price": runtime_config,
        "contract_selection_mode": runtime_config,
        "contracts": runtime_config,
        "option_momentum_percent": runtime_config,
        "confirmation_timeout_seconds": runtime_config,
        "pre_confirmation_max_drawdown_percent": runtime_config,
        "pending_entry_retry_cooldown_seconds": runtime_config,
        "required_breakout_candles": runtime_config,
        "max_quote_age_seconds": runtime_config,
        "minimum_signals": entry_rules,
        "allow_calls": entry_rules,
        "allow_puts": entry_rules,
        "cooldown_minutes": entry_rules,
        "max_trades_per_day": entry_rules,
        "direction_threshold_percent": strategy,
        "hard_stop_percent": strategy,
        "trailing_stop_percent": strategy,
        "enable_profit_floor_trailing_stop": strategy,
        "locked_profit_amount": strategy,
    }
    for row in settings.values():
        for field, source in runtime_field_sources.items():
            if field in source:
                row[field] = source[field]
    normalized, _ = normalize_profile_settings(settings, strict=False)
    return normalized


def build_tournament_profiles(runtime_config: dict, settings_path: str = TOURNAMENT_PROFILES_FILE) -> dict[str, BotProfile]:
    fallback_settings = _settings_from_runtime(runtime_config)
    if os.path.exists(settings_path):
        saved_settings = load_tournament_profile_settings(settings_path)
    else:
        saved_settings = fallback_settings
    profiles: dict[str, BotProfile] = {}

    for profile_id in PROFILE_ORDER:
        definition = PROFILE_DEFINITIONS[profile_id]
        profile_settings = deepcopy(saved_settings[profile_id])

        config = deepcopy(runtime_config or {})
        config = _apply_profile_settings(config, profile_settings)

        profiles[profile_id] = BotProfile(
            profile_id=profile_id,
            display_name=profile_settings["display_name"],
            enabled=profile_settings["enabled"],
            config=config,
        )

    return profiles
