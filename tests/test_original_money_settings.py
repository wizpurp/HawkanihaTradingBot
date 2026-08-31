import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import dashboard


def original_config(contracts=1, maximum_position_cost_dollars=150.0, locked_profit_dollars=5.0):
    return {
        "mode": "sandbox",
        "symbol": "SPY",
        "asset_type": "option",
        "contracts": contracts,
        "bot_enabled": True,
        "strategy_mode": "SURFER",
        "decision_time": "09:35",
        "bot_budget": 1000.0,
        "maximum_position_cost_dollars": maximum_position_cost_dollars,
        "max_open_contracts": 1,
        "contract_selection_mode": "strict_atm",
        "option_momentum_confirmation_enabled": False,
        "two_candle_or_confirmation_enabled": False,
        "minimum_confidence": 1,
        "minimum_dominance_percent": 50,
        "strategy": {
            "hard_stop_percent": 20,
            "trailing_stop_percent": 15,
            "enable_profit_floor_trailing_stop": True,
            "locked_profit_dollars": locked_profit_dollars,
            "exit_poll_interval_ms": 1000,
            "direction_threshold_percent": 60,
        },
        "scanner": {"interval_seconds": 60},
        "history": {"use_global_limit": True, "global_limit": 20},
        "entry_rules": {
            "ema_alignment": True,
            "macd_confirmation": True,
            "vwap_confirmation": True,
            "volume_confirmation": True,
            "minimum_signals": 1,
            "allow_calls": True,
            "allow_puts": True,
            "cooldown_minutes": 0,
            "max_trades_per_day": 10,
        },
    }


def market_context():
    return {
        "decision": "BUY CALL",
        "symbol": "SPY",
        "bullish_score": 5,
        "bearish_score": 0,
        "confidence": 5,
        "dominance_percent": 100.0,
        "decision_reasons": ["test"],
    }


class OriginalMoneySettingsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.pending_file = os.path.join(self.tempdir.name, "pending_entry_history.json")
        self.pending_visible_file = os.path.join(self.tempdir.name, "dashboard_pending_entry_history_visible.json")
        self.pending_backup_file = os.path.join(self.tempdir.name, "dashboard_pending_entry_history_last_cleared.json")
        self.pending_path_patches = [
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_FILE", self.pending_file),
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_VISIBLE_FILE", self.pending_visible_file),
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_BACKUP_FILE", self.pending_backup_file),
        ]
        for path_patch in self.pending_path_patches:
            path_patch.start()
            self.addCleanup(path_patch.stop)
        self.addCleanup(self.tempdir.cleanup)
        dashboard.clear_pending_entry()

    def run_entry(self, ask, contracts=1, maximum_position_cost_dollars=150.0):
        config = original_config(
            contracts=contracts,
            maximum_position_cost_dollars=maximum_position_cost_dollars,
        )
        contract = {"symbol": "SPYTESTCALL", "ask": ask, "option_type": "call"}
        with patch.object(dashboard, "sync_trade_limits_from_file", return_value=(0, 0.0)), \
            patch.object(dashboard, "is_after_decision_time", return_value=True), \
            patch.object(dashboard, "get_cooldown_state", return_value=("", 0)), \
            patch.object(dashboard, "select_entry_contract", return_value=contract), \
            patch.object(dashboard, "get_option_trade_price", return_value=ask), \
            patch.object(dashboard, "add_bot_reason"), \
            patch.object(dashboard, "log_bot_audit"), \
            patch.object(dashboard, "execute_entry_buy") as execute_entry_buy:
            dashboard.try_surfer_entry(config, [], market_context(), contract, None)
            return execute_entry_buy

    def test_original_ui_value_150_accepts_ask_150_one_contract(self):
        execute_entry_buy = self.run_entry(1.50, contracts=1, maximum_position_cost_dollars=150.0)
        self.assertEqual(execute_entry_buy.call_count, 1)

    def test_original_ui_value_150_rejects_ask_151_one_contract(self):
        execute_entry_buy = self.run_entry(1.51, contracts=1, maximum_position_cost_dollars=150.0)
        self.assertEqual(execute_entry_buy.call_count, 0)

    def test_original_ui_value_150_accepts_ask_075_two_contracts(self):
        execute_entry_buy = self.run_entry(0.75, contracts=2, maximum_position_cost_dollars=150.0)
        self.assertEqual(execute_entry_buy.call_count, 1)

    def test_original_ui_locked_profit_5_floor_one_contract(self):
        config = original_config(contracts=1, locked_profit_dollars=5.0)
        stops = dashboard.calculate_stop_state("SPYTESTCALL", 1.20, 1.25, config, update_state=False)
        self.assertEqual(stops["profit_floor_price"], 1.25)
        self.assertEqual(stops["required_premium_movement"], 0.05)

    def test_original_ui_locked_profit_5_floor_two_contracts(self):
        config = original_config(contracts=2, locked_profit_dollars=5.0)
        stops = dashboard.calculate_stop_state("SPYTESTCALL", 1.20, 1.225, config, update_state=False)
        self.assertAlmostEqual(stops["profit_floor_price"], 1.225)
        self.assertEqual(stops["required_premium_movement"], 0.025)

    def test_legacy_original_money_config_migrates_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            payload = original_config()
            payload.pop("maximum_position_cost_dollars")
            payload["max_contract_price"] = 1.5
            payload["strategy"].pop("locked_profit_dollars")
            payload["strategy"]["locked_profit_amount"] = 0.05
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            cwd = os.getcwd()
            try:
                os.chdir(directory)
                first = dashboard.load_config()
                second = dashboard.load_config()
            finally:
                os.chdir(cwd)

        self.assertEqual(first["maximum_position_cost_dollars"], 150.0)
        self.assertEqual(first["strategy"]["locked_profit_dollars"], 5.0)
        self.assertEqual(second["maximum_position_cost_dollars"], 150.0)
        self.assertEqual(second["strategy"]["locked_profit_dollars"], 5.0)

    def test_original_and_tournament_money_semantics_match(self):
        original = original_config(contracts=2, maximum_position_cost_dollars=150.0, locked_profit_dollars=5.0)
        self.assertEqual(dashboard.option_position_cost(0.75, original["contracts"]), 150.0)
        self.assertEqual(dashboard.locked_profit_premium_increment(original["strategy"]["locked_profit_dollars"], original["contracts"]), 0.025)

    def test_original_money_input_labels_include_dollars(self):
        html = dashboard.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("Maximum Position Cost ($)", html)
        self.assertIn("Locked Profit ($)", html)

    def create_pending_for_momentum(self, start_price=0.97, direction="CALL", breakout_enabled=False):
        config = original_config()
        config["option_momentum_confirmation_enabled"] = True
        config["option_momentum_percent"] = 1.0
        config["confirmation_timeout_seconds"] = 60
        config["pre_confirmation_max_drawdown_percent"] = 5.0
        config["two_candle_or_confirmation_enabled"] = breakout_enabled
        contract = {"symbol": f"SPYTEST{direction}", "ask": start_price}
        with patch.object(dashboard, "add_bot_reason"), \
            patch.object(dashboard, "log_bot_audit"), \
            patch.object(dashboard, "initialize_opening_range_breakout_confirmation", return_value={
                "status": "WAITING",
                "count": 0,
                "required": 2,
                "level": 500.0,
                "reason": "waiting",
            }):
            dashboard.create_pending_entry(
                config,
                f"BUY {direction}",
                direction,
                contract,
                1,
                start_price,
                market_context(),
                "MIDPOINT",
            )
        return config

    def process_pending_with_price(self, config, price, breakout=None, execute_return=True):
        breakout = breakout or {
            "status": "PASS",
            "count": 0,
            "required": 0,
            "level": None,
            "reason": "disabled",
        }
        quote = {"bid": price, "ask": price, "last": price}
        with patch.object(dashboard, "option_market_is_open", return_value=True), \
            patch.object(dashboard, "get_market_quote", return_value=quote), \
            patch.object(dashboard, "update_opening_range_breakout_confirmation", return_value=breakout), \
            patch.object(dashboard, "add_bot_reason"), \
            patch.object(dashboard, "log_bot_audit"), \
            patch.object(dashboard, "upsert_pending_history"), \
            patch.object(dashboard, "execute_entry_buy", return_value=execute_return) as execute_entry_buy:
            dashboard.process_pending_entry(config)
            return dashboard.get_pending_entry(), execute_entry_buy

    def test_pending_momentum_097_to_101_passes(self):
        config = self.create_pending_for_momentum(0.97)
        pending, execute_entry_buy = self.process_pending_with_price(config, 1.01)
        self.assertEqual(pending["current_momentum_gain_percent"], ((1.01 - 0.97) / 0.97) * 100)
        self.assertEqual(pending["momentum_status"], "PASS")
        self.assertEqual(execute_entry_buy.call_count, 1)

    def test_pending_momentum_063_to_068_passes(self):
        config = self.create_pending_for_momentum(0.63)
        pending, _ = self.process_pending_with_price(config, 0.68)
        self.assertGreaterEqual(pending["current_momentum_gain_percent"], 1.0)
        self.assertEqual(pending["momentum_status"], "PASS")

    def test_pending_momentum_091_to_103_passes(self):
        config = self.create_pending_for_momentum(0.91)
        pending, _ = self.process_pending_with_price(config, 1.03)
        self.assertGreaterEqual(pending["current_momentum_gain_percent"], 1.0)
        self.assertEqual(pending["momentum_status"], "PASS")

    def test_pending_momentum_086_to_106_passes(self):
        config = self.create_pending_for_momentum(0.86)
        pending, _ = self.process_pending_with_price(config, 1.06)
        self.assertGreaterEqual(pending["current_momentum_gain_percent"], 1.0)
        self.assertEqual(pending["momentum_status"], "PASS")

    def test_pending_momentum_097_to_096_waits(self):
        config = self.create_pending_for_momentum(0.97)
        pending, execute_entry_buy = self.process_pending_with_price(config, 0.96)
        self.assertLess(pending["current_momentum_gain_percent"], 1.0)
        self.assertEqual(pending["momentum_status"], "WAITING")
        self.assertEqual(execute_entry_buy.call_count, 0)

    def test_put_pending_uses_same_option_price_increase_formula(self):
        config = self.create_pending_for_momentum(0.89, direction="PUT")
        pending, execute_entry_buy = self.process_pending_with_price(config, 0.94)
        self.assertAlmostEqual(pending["current_momentum_gain_percent"], ((0.94 - 0.89) / 0.89) * 100)
        self.assertEqual(pending["momentum_status"], "PASS")
        self.assertEqual(execute_entry_buy.call_count, 1)

    def test_pending_drawdown_uses_lowest_observed_price(self):
        config = self.create_pending_for_momentum(1.00)
        pending, _ = self.process_pending_with_price(config, 0.98)
        self.assertEqual(pending["lowest_option_price"], 0.98)
        self.assertAlmostEqual(pending["current_pre_confirmation_drawdown_percent"], 2.0)

    def test_pending_highest_and_lowest_update_across_polls(self):
        config = self.create_pending_for_momentum(1.00)
        pending, _ = self.process_pending_with_price(config, 0.99)
        pending, _ = self.process_pending_with_price(config, 1.005)
        self.assertEqual(pending["lowest_option_price"], 0.99)
        self.assertEqual(pending["highest_option_price"], 1.005)

    def test_pending_state_is_not_recreated_each_scan(self):
        config = self.create_pending_for_momentum(1.00)
        original_id = dashboard.get_pending_entry()["id"]
        pending, _ = self.process_pending_with_price(config, 1.005)
        self.assertEqual(pending["id"], original_id)

    def test_display_values_match_decision_values(self):
        config = self.create_pending_for_momentum(1.00)
        pending, _ = self.process_pending_with_price(config, 1.005)
        html = dashboard.render_current_pending_entry(pending)
        self.assertIn("Current Momentum Gain %", html)
        self.assertIn("0.50%", html)

    def test_pending_history_tests_use_isolated_paths(self):
        self.assertTrue(self.pending_file.startswith(self.tempdir.name))
        self.assertTrue(self.pending_visible_file.startswith(self.tempdir.name))
        self.assertTrue(self.pending_backup_file.startswith(self.tempdir.name))
        self.assertEqual(dashboard.PENDING_ENTRY_HISTORY_FILE, self.pending_file)
        self.assertEqual(dashboard.PENDING_ENTRY_HISTORY_VISIBLE_FILE, self.pending_visible_file)
        self.assertEqual(dashboard.PENDING_ENTRY_HISTORY_BACKUP_FILE, self.pending_backup_file)

    def test_cleanup_test_pending_history_only_removes_spytest_records(self):
        real_record = {"id": "real", "option_symbol": "SPY260828C00500000", "final_status": "WAITING"}
        test_record = {"id": "test", "option_symbol": "SPYTESTCALL", "final_status": "WAITING"}
        for path in [self.pending_file, self.pending_visible_file, self.pending_backup_file]:
            dashboard.write_json_history(path, [real_record, test_record])

        cleaned = dashboard.cleanup_test_pending_history_records()

        self.assertEqual(cleaned[self.pending_file], 1)
        self.assertEqual(cleaned[self.pending_visible_file], 1)
        self.assertEqual(cleaned[self.pending_backup_file], 1)
        for path in [self.pending_file, self.pending_visible_file, self.pending_backup_file]:
            self.assertEqual(dashboard.load_json_history(path), [real_record])

    def test_final_quote_is_evaluated_before_timeout(self):
        config = self.create_pending_for_momentum(0.97)
        pending = dashboard.get_pending_entry()
        pending["expires_epoch"] = time.time() - 1
        dashboard.set_pending_entry(pending)
        pending, execute_entry_buy = self.process_pending_with_price(config, 1.01)
        self.assertEqual(pending["status"], "CONFIRMED")
        self.assertEqual(execute_entry_buy.call_count, 1)

    def test_momentum_pass_can_wait_for_breakout(self):
        config = self.create_pending_for_momentum(1.00, breakout_enabled=True)
        pending, execute_entry_buy = self.process_pending_with_price(
            config,
            1.02,
            breakout={"status": "WAITING", "count": 1, "required": 2, "level": 500.0, "reason": "one candle"},
        )
        self.assertEqual(pending["momentum_status"], "PASS")
        self.assertEqual(pending["breakout_status"], "WAITING")
        self.assertEqual(pending["current_breakout_candle"], 1)
        self.assertEqual(execute_entry_buy.call_count, 0)

    def test_breakout_progress_updates_from_completed_candles(self):
        config = self.create_pending_for_momentum(1.00, breakout_enabled=True)
        pending, _ = self.process_pending_with_price(
            config,
            1.005,
            breakout={"status": "WAITING", "count": 1, "required": 2, "level": 500.0, "reason": "one candle"},
        )
        self.assertEqual(pending["current_breakout_candle"], 1)

    def test_passing_both_confirmations_uses_normal_entry_path(self):
        config = self.create_pending_for_momentum(1.00, breakout_enabled=True)
        pending, execute_entry_buy = self.process_pending_with_price(
            config,
            1.02,
            breakout={"status": "PASS", "count": 2, "required": 2, "level": 500.0, "reason": "confirmed"},
        )
        self.assertEqual(pending["status"], "CONFIRMED")
        self.assertEqual(execute_entry_buy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
