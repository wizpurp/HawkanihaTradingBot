import csv
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import dashboard
from logs import trade_logger


class BrokerExecutionDiagnosticsTest(unittest.TestCase):
    def test_fetch_time_does_not_fabricate_broker_quote_time(self):
        snapshot = dashboard.quote_execution_snapshot({
            "last": 1.0, "_fetched_at": "2026-09-24T10:00:00-04:00",
        })
        self.assertEqual(snapshot["quote_timestamp"], "")
        self.assertEqual(snapshot["quote_age_seconds"], "")

    def test_selected_bid_uses_bid_timestamp_not_old_last_trade(self):
        snapshot = dashboard.quote_execution_snapshot({
            "bid": 0.85,
            "trade_date": "2026-09-24T09:30:00-04:00",
            "bid_date": "2026-09-24T10:00:00-04:00",
        })
        self.assertEqual(snapshot["price_source"], "BID")
        self.assertEqual(snapshot["quote_timestamp"], "2026-09-24T10:00:00-04:00")

    def test_unfilled_order_does_not_report_fill_or_timestamp(self):
        execution = dashboard.parse_broker_order_execution({
            "order": {"status": "accepted", "avg_fill_price": 1.0,
                      "transaction_date": "2026-09-24T14:00:00Z"},
        })
        self.assertIsNone(execution["fill_price"])
        self.assertEqual(execution["fill_timestamp"], "")

    def test_last_fill_is_not_substituted_for_average_execution_price(self):
        execution = dashboard.parse_broker_order_execution({
            "order": {"status": "filled", "last_fill_price": 1.0},
        })
        self.assertIsNone(execution["fill_price"])

    def test_quote_snapshot_calculates_spread_and_midpoint(self):
        snapshot = dashboard.quote_execution_snapshot({
            "bid": 1.20,
            "ask": 1.40,
            "last": 1.30,
            "trade_date": int(datetime.now().timestamp() * 1000),
        })

        self.assertAlmostEqual(snapshot["midpoint"], 1.30)
        self.assertAlmostEqual(snapshot["spread_dollars"], 0.20)
        self.assertAlmostEqual(snapshot["spread_percent"], (0.20 / 1.30) * 100)
        self.assertEqual(snapshot["price_source"], "LAST")
        self.assertNotEqual(snapshot["quote_timestamp"], "")

    def test_broker_fill_uses_avg_fill_and_transaction_timestamp(self):
        execution = dashboard.parse_broker_order_execution({
            "order": {
                "status": "filled",
                "type": "market",
                "avg_fill_price": 1.36,
                "transaction_date": "2026-09-24T14:35:22.000Z",
            }
        })

        self.assertEqual(execution["order_status"], "filled")
        self.assertEqual(execution["fill_price"], 1.36)
        self.assertEqual(execution["fill_timestamp"], "2026-09-24T14:35:22.000Z")

    def test_fill_lookup_reads_real_order_detail_endpoint_without_submitting_order(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "order": {
                "status": "filled",
                "avg_fill_price": 0.86,
                "transaction_date": "2026-09-24T14:40:00.000Z",
            }
        }
        with patch.object(dashboard.requests, "get", return_value=response) as get_request, \
             patch.object(dashboard.requests, "post") as post_request:
            execution = dashboard.fetch_broker_order_execution("123", retries=1)

        self.assertEqual(execution["fill_price"], 0.86)
        get_request.assert_called_once()
        self.assertIn("/orders/123", get_request.call_args.args[0])
        post_request.assert_not_called()

    def test_trigger_quote_and_exit_fill_remain_separate(self):
        trigger = dashboard.exit_trigger_diagnostics(
            {"bid": 0.81, "ask": 0.83, "last": 0.82},
            768.0,
            1.08,
            0.90,
            0.90,
        )
        diagnostics = dashboard.exit_execution_diagnostics(
            trigger,
            {"bid": 0.85, "ask": 0.87, "last": 0.86},
            767.5,
            {"EntrySpyPrice": "768.0"},
            {"order_status": "filled", "fill_price": 0.855, "fill_timestamp": "2026-09-24T14:40:00Z"},
        )

        self.assertEqual(diagnostics["ExitTriggerPrice"], 0.82)
        self.assertEqual(diagnostics["ExitOptionLast"], 0.86)
        self.assertEqual(diagnostics["ExitBrokerFillPrice"], 0.855)
        self.assertAlmostEqual(diagnostics["ExitSlippageFromTrigger"], 0.035)
        self.assertAlmostEqual(diagnostics["SpyChangeDollars"], -0.5)

    def test_hard_stop_decision_preserves_the_exact_trigger_quote(self):
        config = {
            "entry_rules": {"minimum_signals": 2},
            "minimum_confidence": 2,
            "minimum_dominance_percent": 60,
            "strategy": {"hard_stop_percent": 20, "trailing_stop_percent": 15},
        }
        position = {"symbol": "SPY260924C00768000", "quantity": 1, "cost_basis": 136.0}
        context = {"price": 768.0, "reasons": [], "confidence": 0, "dominance_percent": 0}
        stop_state = {
            "effective_trailing_stop": 1.20,
            "percentage_trailing_stop": 1.20,
            "stop_armed": False,
            "stop_control_rule": "HARD STOP",
        }
        with patch.object(dashboard, "get_market_quote", return_value={"bid": 0.81, "ask": 0.83, "last": 0.82}), \
             patch.object(dashboard, "calculate_stop_state", return_value=stop_state):
            decision, reasons = dashboard.decide_surfer_action(config, [position], context)

        self.assertEqual(decision, "SELL")
        self.assertIn("P/L -39.71%", reasons)
        self.assertEqual(context["exit_trigger_diagnostics"]["ExitTriggerPrice"], 0.82)
        self.assertEqual(context["exit_trigger_diagnostics"]["ExitStopPriceSource"], "LAST")

    def test_option_order_submission_remains_a_market_order(self):
        response = Mock(status_code=200, text='{"order":{"status":"ok","id":123}}')
        with patch.object(dashboard, "load_config", return_value={"symbol": "SPY"}), \
             patch.object(dashboard.requests, "post", return_value=response) as post_request:
            dashboard.submit_option_order("SPY260924C00768000", 1, "buy_to_open")

        submitted = post_request.call_args.kwargs["data"]
        self.assertEqual(submitted["side"], "buy_to_open")
        self.assertEqual(submitted["type"], "market")
        self.assertNotIn("price", submitted)

    def test_trade_csv_persists_entry_and_exit_execution_diagnostics(self):
        with tempfile.TemporaryDirectory() as tempdir:
            trades_file = os.path.join(tempdir, "trades.csv")
            visible_file = os.path.join(tempdir, "visible.csv")
            history_file = os.path.join(tempdir, "history.csv")
            callbacks = {
                "grade_exit_trade": lambda symbol, qty, price, pnl, context: ("C", 70, "test", "0:01:00"),
            }
            with patch.object(trade_logger, "TRADES_FILE", trades_file), \
                 patch.object(trade_logger, "VISIBLE_TRADES_FILE", visible_file), \
                 patch.object(trade_logger, "TRADE_HISTORY_VISIBLE_FILE", history_file), \
                 patch.dict(trade_logger._CALLBACKS, callbacks, clear=True):
                trade_logger.log_trade(
                    "BUY",
                    "SPY260924C00768000",
                    1,
                    1.36,
                    source="BOT",
                    execution_diagnostics={
                        "EntrySpyPrice": 768.0,
                        "EntryOptionMidpoint": 1.35,
                        "EntryBrokerFillPrice": 1.36,
                    },
                )
                trade_logger.log_trade(
                    "SELL",
                    "SPY260924C00768000",
                    1,
                    0.86,
                    -50.0,
                    source="BOT",
                    execution_diagnostics={
                        "ExitTriggerPrice": 0.82,
                        "ExitBrokerFillPrice": 0.86,
                    },
                )

            with open(trades_file, newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["EntrySpyPrice"], "768.0")
        self.assertEqual(rows[1]["EntryBrokerFillPrice"], "1.36")
        self.assertEqual(rows[1]["ExitTriggerPrice"], "0.82")
        self.assertEqual(rows[1]["ExitBrokerFillPrice"], "0.86")

    def test_specific_percentage_discrepancy_is_explained_by_two_quote_prices(self):
        entry = 1.36
        trigger_price = 0.82
        later_exit_quote = 0.86

        trigger_percent = ((trigger_price - entry) / entry) * 100
        displayed_percent = ((later_exit_quote - entry) / entry) * 100

        self.assertAlmostEqual(trigger_percent, -39.705882, places=5)
        self.assertAlmostEqual(displayed_percent, -36.764706, places=5)
        self.assertNotEqual(round(trigger_percent, 2), round(displayed_percent, 2))


if __name__ == "__main__":
    unittest.main()
