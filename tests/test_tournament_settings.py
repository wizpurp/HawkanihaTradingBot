import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from tournament.evaluator import evaluate_profile
from tournament.profiles import (
    build_tournament_profiles,
    copy_tournament_profile_settings,
    default_tournament_profile_settings,
    load_tournament_profile_settings,
    reset_tournament_profile_settings,
    save_tournament_profile_settings,
)
from tournament.state import create_all_initial_states
from tournament.snapshot import MarketSnapshot


def runtime_config():
    return {
        "symbol": "SPY",
        "bot_enabled": True,
        "minimum_confidence": 1,
        "minimum_dominance_percent": 50,
        "max_contract_price": 9.99,
        "contract_selection_mode": "strict_atm",
        "contracts": 2,
        "strategy": {
            "direction_threshold_percent": 50,
            "hard_stop_percent": 20,
            "trailing_stop_percent": 20,
            "enable_profit_floor_trailing_stop": False,
            "locked_profit_amount": 0.0,
        },
        "entry_rules": {
            "minimum_signals": 1,
            "allow_calls": True,
            "allow_puts": True,
            "cooldown_minutes": 5,
            "max_trades_per_day": 99,
        },
    }


def bullish_snapshot():
    return MarketSnapshot(
        timestamp="2026-07-28T09:35:00-04:00",
        symbol="SPY",
        current_price=500.0,
        bullish_score=3,
        bearish_score=0,
        confidence=3,
        dominance_percent=100.0,
        market_state="BULLISH",
        suggested_direction="CALL",
        signal="CALL",
        ema_bullish=True,
        ema_bearish=False,
        macd_bullish=True,
        macd_bearish=False,
        above_vwap=True,
        below_vwap=False,
        volume_confirmed=False,
        opening_range_ready=True,
        opening_range_high=499.0,
        opening_range_low=497.0,
        completed_closes=(500.0, 501.0),
    )


class TournamentSettingsTest(unittest.TestCase):
    def temp_path(self, directory):
        return os.path.join(directory, "tournament_profiles.json")

    def test_saving_and_loading_independent_profile_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            settings = default_tournament_profile_settings()
            settings["BOT_A_BASELINE"]["minimum_confidence"] = 2
            settings["BOT_B_MOMENTUM"]["minimum_confidence"] = 7
            save_tournament_profile_settings(settings, path)

            loaded = load_tournament_profile_settings(path)

        self.assertEqual(loaded["BOT_A_BASELINE"]["minimum_confidence"], 2)
        self.assertEqual(loaded["BOT_B_MOMENTUM"]["minimum_confidence"], 7)

    def test_corrupt_settings_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")

            loaded = load_tournament_profile_settings(path)

        self.assertEqual(loaded["BOT_A_BASELINE"]["minimum_confidence"], 4)
        self.assertFalse(loaded["BOT_A_BASELINE"]["option_momentum_confirmation_enabled"])

    def test_api_validation_rejects_invalid_values(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            reset_tournament_profile_settings(path)
            bad = default_tournament_profile_settings()
            bad["BOT_A_BASELINE"]["minimum_confidence"] = -1
            with patch.object(dashboard, "TOURNAMENT_PROFILES_FILE", path):
                response = dashboard.app.test_client().post(
                    "/api/tournament/settings",
                    json={"profiles": bad},
                    headers={"Accept": "application/json"},
                )

        self.assertEqual(response.status_code, 400)
        self.assertIn("minimum_confidence", response.get_json()["errors"][0])

    def test_shared_only_copy_preserves_experiment_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            settings = reset_tournament_profile_settings(path)
            settings["BOT_A_BASELINE"]["minimum_confidence"] = 8
            save_tournament_profile_settings(settings, path)

            copied = copy_tournament_profile_settings("BOT_A_BASELINE", "SHARED_ONLY", path)

        self.assertEqual(copied["BOT_B_MOMENTUM"]["minimum_confidence"], 8)
        self.assertTrue(copied["BOT_B_MOMENTUM"]["option_momentum_confirmation_enabled"])
        self.assertTrue(copied["BOT_C_TWO_CANDLE_OR"]["two_candle_or_confirmation_enabled"])

    def test_copy_everything_copies_all_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            settings = reset_tournament_profile_settings(path)
            settings["BOT_A_BASELINE"]["option_momentum_confirmation_enabled"] = False
            settings["BOT_A_BASELINE"]["two_candle_or_confirmation_enabled"] = False
            save_tournament_profile_settings(settings, path)

            copied = copy_tournament_profile_settings("BOT_A_BASELINE", "EVERYTHING", path)

        self.assertFalse(copied["BOT_B_MOMENTUM"]["option_momentum_confirmation_enabled"])
        self.assertFalse(copied["BOT_C_TWO_CANDLE_OR"]["two_candle_or_confirmation_enabled"])

    def test_editing_bot_a_does_not_mutate_bot_b(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            settings = reset_tournament_profile_settings(path)
            settings["BOT_A_BASELINE"]["minimum_signals"] = 9
            save_tournament_profile_settings(settings, path)
            profiles = build_tournament_profiles(runtime_config(), path)
            profiles["BOT_A_BASELINE"].config["entry_rules"]["minimum_signals"] = 1

        self.assertEqual(profiles["BOT_B_MOMENTUM"].config["entry_rules"]["minimum_signals"], 4)

    def test_evaluator_uses_saved_per_profile_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            settings = reset_tournament_profile_settings(path)
            settings["BOT_A_BASELINE"]["minimum_confidence"] = 4
            settings["BOT_B_MOMENTUM"]["minimum_confidence"] = 2
            settings["BOT_B_MOMENTUM"]["minimum_signals"] = 2
            settings["BOT_B_MOMENTUM"]["option_momentum_confirmation_enabled"] = False
            save_tournament_profile_settings(settings, path)
            profiles = build_tournament_profiles(runtime_config(), path)
            states = create_all_initial_states(profiles)

            bot_a = evaluate_profile(profiles["BOT_A_BASELINE"], states["BOT_A_BASELINE"], bullish_snapshot())
            bot_b = evaluate_profile(profiles["BOT_B_MOMENTUM"], states["BOT_B_MOMENTUM"], bullish_snapshot())

        self.assertEqual(bot_a.rejection_reason, "CONFIDENCE_BELOW_MINIMUM")
        self.assertEqual(bot_b.status, "ACCEPTED")

    def test_original_bot_config_remains_unchanged(self):
        config = runtime_config()
        before = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            reset_tournament_profile_settings(path)
            build_tournament_profiles(config, path)

        self.assertEqual(config, before)


if __name__ == "__main__":
    unittest.main()
