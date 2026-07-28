from __future__ import annotations

from copy import deepcopy

from .models import BotProfile


EXPERIMENT_KEYS = {
    "option_momentum_confirmation_enabled",
    "option_momentum_percent",
    "confirmation_timeout_seconds",
    "pre_confirmation_max_drawdown_percent",
    "pending_entry_retry_cooldown_seconds",
    "two_candle_or_confirmation_enabled",
    "required_breakout_candles",
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


def build_tournament_profiles(runtime_config: dict) -> dict[str, BotProfile]:
    profiles: dict[str, BotProfile] = {}

    for profile_id, definition in PROFILE_DEFINITIONS.items():
        config = deepcopy(runtime_config or {})
        for key, value in definition["overrides"].items():
            config[key] = value

        profiles[profile_id] = BotProfile(
            profile_id=profile_id,
            display_name=definition["display_name"],
            enabled=True,
            config=config,
        )

    return profiles

