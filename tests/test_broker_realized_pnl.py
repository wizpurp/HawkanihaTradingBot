import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import dashboard
from logs import trade_logger


class BrokerRealizedPnlTest(unittest.TestCase):
    def row(self, entry, exit_price, quote_pnl):
        return {"Action": "SELL", "Source": "BOT", "Symbol": "SPY261026C00768000",
                "Qty": 1, "Price": .42, "PnL": quote_pnl,
                "EntryBrokerFillPrice": entry, "ExitBrokerFillPrice": exit_price,
                "EntryBrokerOrderStatus": "filled", "ExitBrokerOrderStatus": "filled",
                "ExitTriggerPrice": .42}

    def test_equal_fills_have_zero_realized_preserving_quote_loss(self):
        result = trade_logger.broker_realized_values(self.row(.51, .51, -9))
        self.assertEqual(result["PnL"], 0)
        self.assertEqual(result["PnLPercent"], 0)
        self.assertEqual(result["QuotePnL"], -9)
        self.assertAlmostEqual(result["TriggerPnL"], -9)
        self.assertEqual(result["PnLSource"], "BROKER_FILLS")

    def test_fill_loss_overrides_quote_profit(self):
        result = trade_logger.broker_realized_values(self.row(.27, .25, 3))
        self.assertEqual(result["PnL"], -2)
        self.assertAlmostEqual(result["PnLPercent"], -7.4074074)
        self.assertEqual(result["QuotePnL"], 3)

    def test_legacy_missing_fills_is_preserved_without_inventing_fills(self):
        row = self.row(.27, "", 3)
        original = copy.deepcopy(row)
        result = trade_logger.broker_realized_values(row)
        self.assertEqual(row, original)
        self.assertEqual(result["PnL"], 3)
        self.assertEqual(result["PnLSource"], "QUOTE / LEGACY")
        self.assertEqual(result["BrokerRealizedPnL"], "")

    def test_partial_fill_is_not_treated_as_full_quantity_realized(self):
        row = self.row(.27, .25, 3)
        row["ExitBrokerOrderStatus"] = "partially_filled"
        self.assertEqual(trade_logger.broker_realized_values(row)["PnLSource"], "QUOTE / LEGACY")

    def test_logger_grades_and_persists_broker_pnl(self):
        with tempfile.TemporaryDirectory() as directory:
            grader = Mock(return_value=("C", 70, "test", "0:01:00"))
            with patch.object(trade_logger, "TRADES_FILE", os.path.join(directory, "trades.csv")), \
                 patch.object(trade_logger, "VISIBLE_TRADES_FILE", os.path.join(directory, "visible.csv")), \
                 patch.object(trade_logger, "TRADE_HISTORY_VISIBLE_FILE", os.path.join(directory, "history.csv")), \
                 patch.dict(trade_logger._CALLBACKS, {"grade_exit_trade": grader}, clear=True):
                trade_logger.log_trade("BUY", "SPY261026C00768000", 1, .27, source="BOT",
                                       execution_diagnostics={"EntryBrokerFillPrice": .27, "EntryBrokerOrderStatus": "filled"})
                row = trade_logger.log_trade("SELL", "SPY261026C00768000", 1, .30, 3, source="BOT",
                                             execution_diagnostics={"ExitBrokerFillPrice": .25, "ExitBrokerOrderStatus": "filled"})
                self.assertEqual(row["PnL"], -2)
                self.assertEqual(row["Price"], .30)
                self.assertEqual(row["QuotePnL"], 3)
                self.assertEqual(grader.call_args.args[3], -2)
                self.assertAlmostEqual(grader.call_args.args[4]["broker_realized_pnl_percent"], -7.4074074)

    def test_ledger_and_card_use_historical_confirmed_fills(self):
        row = self.row(.27, .25, 3)
        with patch.object(dashboard, "find_last_buy", return_value=None):
            summary = dashboard.summarize_ledger([row], "BOT", {})
            display = dashboard.enrich_trade_rows([row])[0]
        self.assertEqual(summary["total_pnl"], -2)
        self.assertEqual(summary["win_rate"], 0)
        self.assertEqual(display["Exit"], .25)
        self.assertIn("Broker Realized P/L", dashboard.render_sell_trade_card(display))
        self.assertIn("Trigger P/L", dashboard.render_sell_trade_card(display))

    def test_last_bid_discrepancy_current_behavior_is_preserved(self):
        quote = {"bid": .51, "ask": .52, "last": .42}
        self.assertEqual(dashboard.get_quote_price(quote), .42)
        self.assertAlmostEqual((dashboard.get_quote_price(quote) - .51) / .51 * 100, -17.6470588)
        self.assertEqual((quote["bid"] - .51) / .51 * 100, 0)
        config = {"entry_rules": {}, "strategy": {"hard_stop_percent": 15}}
        context = {}
        position = {"symbol": "SPY261026C00768000", "quantity": 1, "cost_basis": 51}
        with patch.object(dashboard, "get_market_quote", return_value=quote), \
             patch.object(dashboard, "calculate_stop_state", return_value={"stop_armed": False}), \
             patch.dict(dashboard.BOT_STATE, {"position_peaks": {}}, clear=False):
            decision, reasons = dashboard.decide_surfer_action(config, [position], context)
        self.assertEqual(decision, "SELL")
        self.assertIn("Hard stop hit", reasons)

    def test_environment_and_opt_in_guard_with_override(self):
        now = datetime.now(dashboard.MARKET_TZ)
        quote = {"last": .51, "trade_date": (now - timedelta(seconds=902)).isoformat()}
        context = {"price": 768, "spy_quote_timestamp": (now - timedelta(seconds=977)).isoformat()}
        with patch.object(dashboard, "BASE_URL", "https://sandbox.tradier.com/v1"):
            self.assertEqual(dashboard.market_data_environment(), "SANDBOX_DELAYED")
            self.assertTrue(dashboard.entry_quote_freshness({}, quote, context)[0])
            config = {"require_realtime_entry_quotes": True, "max_entry_quote_age_seconds": 30}
            self.assertFalse(dashboard.entry_quote_freshness(config, quote, context)[0])
            config["allow_delayed_sandbox_entries"] = True
            self.assertTrue(dashboard.entry_quote_freshness(config, quote, context)[0])
        with patch.object(dashboard, "BASE_URL", "https://api.tradier.com/v1"):
            self.assertEqual(dashboard.market_data_environment(), "PRODUCTION_REALTIME")
            self.assertFalse(dashboard.entry_quote_freshness(config, quote, context)[0])

    def test_enabled_guard_accepts_fresh_quotes_and_rejects_unknown_age(self):
        quote = {"last": .51, "trade_date": datetime.now(dashboard.MARKET_TZ).isoformat()}
        context = {"price": 768, "spy_quote_timestamp": quote["trade_date"]}
        config = {"require_realtime_entry_quotes": True}
        self.assertTrue(dashboard.entry_quote_freshness(config, quote, context)[0])
        self.assertFalse(dashboard.entry_quote_freshness(config, {}, context)[0])


if __name__ == "__main__":
    unittest.main()
