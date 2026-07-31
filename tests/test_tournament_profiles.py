import importlib
import json
import os
import tempfile
import unittest

from tournament.models import BotMetrics
from tournament.profiles import EXPERIMENT_KEYS, build_tournament_profiles
from tournament.state import create_all_initial_states, load_tournament_state, save_tournament_state


class TournamentProfilesTest(unittest.TestCase):
    def setUp(self):
        self.runtime_config = {
            "symbol": "SPY",
            "minimum_confidence": 4,
            "minimum_dominance_percent": 60,
            "maximum_position_cost_dollars": 100.0,
            "contracts": 1,
            "bot_starting_account_balance": 1000.0,
            "strategy": {
                "hard_stop_percent": 20,
                "trailing_stop_percent": 12,
                "direction_threshold_percent": 60,
                "exit_poll_interval_ms": 1000,
                "enable_profit_floor_trailing_stop": True,
                "locked_profit_dollars": 0.0,
            },
            "entry_rules": {
                "minimum_signals": 2,
                "cooldown_minutes": 1,
                "max_trades_per_day": 10,
            },
            "scanner": {
                "interval_seconds": 60,
            },
            "contract_selection_mode": "strict_atm",
        }
        self.profiles = build_tournament_profiles(self.runtime_config)

    def test_exactly_four_profiles_are_created(self):
        self.assertEqual(
            set(self.profiles),
            {
                "BOT_A_BASELINE",
                "BOT_B_MOMENTUM",
                "BOT_C_TWO_CANDLE_OR",
                "BOT_D_COMBINED",
            },
        )

    def test_momentum_and_or_combinations(self):
        self.assertFalse(self.profiles["BOT_A_BASELINE"].config["option_momentum_confirmation_enabled"])
        self.assertFalse(self.profiles["BOT_A_BASELINE"].config["two_candle_or_confirmation_enabled"])

        self.assertTrue(self.profiles["BOT_B_MOMENTUM"].config["option_momentum_confirmation_enabled"])
        self.assertEqual(self.profiles["BOT_B_MOMENTUM"].config["option_momentum_percent"], 1.0)
        self.assertEqual(self.profiles["BOT_B_MOMENTUM"].config["confirmation_timeout_seconds"], 60)
        self.assertEqual(self.profiles["BOT_B_MOMENTUM"].config["pre_confirmation_max_drawdown_percent"], 5.0)
        self.assertEqual(self.profiles["BOT_B_MOMENTUM"].config["pending_entry_retry_cooldown_seconds"], 60)
        self.assertFalse(self.profiles["BOT_B_MOMENTUM"].config["two_candle_or_confirmation_enabled"])

        self.assertFalse(self.profiles["BOT_C_TWO_CANDLE_OR"].config["option_momentum_confirmation_enabled"])
        self.assertTrue(self.profiles["BOT_C_TWO_CANDLE_OR"].config["two_candle_or_confirmation_enabled"])
        self.assertEqual(self.profiles["BOT_C_TWO_CANDLE_OR"].config["required_breakout_candles"], 2)

        self.assertTrue(self.profiles["BOT_D_COMBINED"].config["option_momentum_confirmation_enabled"])
        self.assertTrue(self.profiles["BOT_D_COMBINED"].config["two_candle_or_confirmation_enabled"])
        self.assertEqual(self.profiles["BOT_D_COMBINED"].config["required_breakout_candles"], 2)

    def test_shared_settings_are_identical(self):
        base_config = self.profiles["BOT_A_BASELINE"].config
        for profile in self.profiles.values():
            for key, value in base_config.items():
                if key in EXPERIMENT_KEYS:
                    continue
                self.assertEqual(profile.config.get(key), value)

    def test_profile_configs_are_independent(self):
        self.profiles["BOT_A_BASELINE"].config["strategy"]["hard_stop_percent"] = 99
        self.assertNotEqual(
            self.profiles["BOT_A_BASELINE"].config["strategy"]["hard_stop_percent"],
            self.profiles["BOT_B_MOMENTUM"].config["strategy"]["hard_stop_percent"],
        )

    def test_runtime_positions_are_independent(self):
        states = create_all_initial_states(self.profiles)
        states["BOT_A_BASELINE"].position.symbol = "SPY"
        states["BOT_A_BASELINE"].position.quantity = 1

        self.assertIsNone(states["BOT_B_MOMENTUM"].position.symbol)
        self.assertEqual(states["BOT_B_MOMENTUM"].position.quantity, 0)

    def test_runtime_metrics_are_independent(self):
        states = create_all_initial_states(self.profiles)
        states["BOT_A_BASELINE"].metrics.trades_today = 10
        states["BOT_A_BASELINE"].metrics.total_pnl = 123.45

        self.assertIsInstance(states["BOT_B_MOMENTUM"].metrics, BotMetrics)
        self.assertEqual(states["BOT_B_MOMENTUM"].metrics.trades_today, 0)
        self.assertEqual(states["BOT_B_MOMENTUM"].metrics.total_pnl, 0.0)

    def test_save_and_load_preserves_all_states(self):
        states = create_all_initial_states(self.profiles)
        states["BOT_C_TWO_CANDLE_OR"].virtual_balance = 1111.0
        states["BOT_C_TWO_CANDLE_OR"].position.option_symbol = "SPY_TEST_CALL"

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            save_tournament_state(path, states)
            loaded = load_tournament_state(path, self.profiles)

        self.assertEqual(set(loaded), set(self.profiles))
        self.assertEqual(loaded["BOT_C_TWO_CANDLE_OR"].virtual_balance, 1111.0)
        self.assertEqual(loaded["BOT_C_TWO_CANDLE_OR"].position.option_symbol, "SPY_TEST_CALL")

    def test_missing_profile_is_initialized_only_for_missing_profile(self):
        states = create_all_initial_states(self.profiles)
        states["BOT_B_MOMENTUM"].virtual_balance = 2222.0

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            save_tournament_state(path, states)
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            raw.pop("BOT_D_COMBINED")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(raw, handle)
            loaded = load_tournament_state(path, self.profiles)

        self.assertEqual(loaded["BOT_B_MOMENTUM"].virtual_balance, 2222.0)
        self.assertEqual(loaded["BOT_D_COMBINED"].virtual_balance, 1000.0)

    def test_corrupt_state_file_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not valid json")

            loaded = load_tournament_state(path, self.profiles)

        self.assertEqual(set(loaded), set(self.profiles))
        self.assertEqual(loaded["BOT_A_BASELINE"].virtual_balance, 1000.0)

    def test_no_tradier_or_order_module_imported(self):
        importlib.import_module("tournament.models")
        importlib.import_module("tournament.profiles")
        importlib.import_module("tournament.state")

        forbidden_modules = {
            "dashboard",
            "option_order",
            "order_filer",
            "paper_buy",
            "paper_sell",
            "positions",
        }
        self.assertTrue(forbidden_modules.isdisjoint(set(importlib.sys.modules)))


if __name__ == "__main__":
    unittest.main()

