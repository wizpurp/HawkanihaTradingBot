import json
import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
