import copy
import csv
import os
import tempfile
import unittest
from unittest.mock import patch

import dashboard
from logs import trade_logger


class ExecutionAnomalyTests(unittest.TestCase):
    def row(self, ask=1.02, midpoint=1.02, fill=1.39, exit_price=1.10):
        return {"Action": "SELL", "Source": "BOT", "Symbol": "SPY261105C00768000",
                "Qty": 1, "Price": exit_price, "PnL": -29,
                "EntryOptionAsk": ask, "EntryOptionMidpoint": midpoint,
                "EntryBrokerFillPrice": fill, "ExitBrokerFillPrice": exit_price,
                "EntryBrokerOrderStatus": "filled", "ExitBrokerOrderStatus": "filled"}

    def classify(self, row, threshold=15):
        return trade_logger.execution_quality_values(trade_logger.broker_realized_values(row), threshold)

    def test_large_fill_deviation_is_anomaly_without_mutating_result(self):
        row = self.row()
        original = copy.deepcopy(row)
        result = self.classify(row)
        self.assertEqual(row, original)
        self.assertEqual(result["ExecutionQuality"], "EXECUTION_ANOMALY")
        self.assertEqual(result["StrategyPerformanceEligible"], "NO")
        self.assertEqual(result["ExecutionExclusionReason"], "ENTRY_EXECUTION_ANOMALY")
        self.assertAlmostEqual(result["EntryFillVsAskPercent"], 36.2745098)
        self.assertEqual(result["BrokerRealizedPnL"], -29)

    def test_normal_slippage_is_eligible(self):
        result = self.classify(self.row(1.10, 1.095, 1.10))
        self.assertEqual(result["ExecutionQuality"], "NORMAL")
        self.assertEqual(result["StrategyPerformanceEligible"], "YES")

    def test_threshold_strict_boundary(self):
        for fill, quality in ((1.15, "NORMAL"), (1.15001, "EXECUTION_ANOMALY"), (1.14999, "NORMAL")):
            with self.subTest(fill=fill):
                self.assertEqual(self.classify(self.row(1, 1, fill))["ExecutionQuality"], quality)

    def test_both_quotes_must_exceed_threshold(self):
        self.assertEqual(self.classify(self.row(1.2, 1, 1.3))["ExecutionQuality"], "NORMAL")

    def test_missing_invalid_and_nonfinite_data_are_unknown(self):
        for field in ("EntryOptionAsk", "EntryOptionMidpoint", "EntryBrokerFillPrice"):
            for value in (None, "", "bad", 0, -1, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    row = self.row()
                    row[field] = value
                    result = self.classify(row)
                    self.assertEqual(result["ExecutionQuality"], "UNKNOWN")
                    self.assertEqual(result["StrategyPerformanceEligible"], "N/A")

    def test_anomaly_and_unknown_stay_in_broker_totals_only(self):
        rows = [self.row(), self.row(1, 1, 1, 1.2), self.row(1, 1, 1, .9), self.row(None, None, 1, 1.5)]
        summary = dashboard.summarize_ledger(rows, "BOT", {})
        stats = summary["execution_performance"]
        self.assertEqual(summary["total_pnl"], 31)
        self.assertEqual(stats["all_broker_trades"]["realized_pnl"], 31)
        self.assertEqual(stats["all_broker_trades"]["count"], 4)
        self.assertEqual(stats["all_broker_trades"]["wins"], 2)
        self.assertEqual(stats["all_broker_trades"]["losses"], 2)
        clean = stats["clean_strategy_trades"]
        self.assertEqual(clean["count"], 2)
        self.assertEqual(clean["realized_pnl"], 10)
        self.assertEqual(clean["average_winner"], 20)
        self.assertEqual(clean["average_loser"], -10)
        self.assertEqual(clean["win_rate"], 50)
        self.assertEqual(stats["unknown_execution_count"], 1)

    def test_configurable_threshold_and_persisted_threshold(self):
        self.assertEqual(self.classify(self.row(), 40)["ExecutionQuality"], "NORMAL")
        row = self.row()
        row["ExecutionAnomalyThresholdPercent"] = "15"
        self.assertEqual(self.classify(row, 40)["ExecutionQuality"], "EXECUTION_ANOMALY")

    def test_logger_persists_classification_in_isolated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = {key: os.path.join(directory, key + ".csv") for key in
                     ("TRADES_FILE", "VISIBLE_TRADES_FILE", "TRADE_HISTORY_VISIBLE_FILE")}
            with patch.multiple(trade_logger, **paths), patch.dict(trade_logger._CALLBACKS, {
                    "load_config": lambda: {"execution_anomaly_threshold_percent": 15}}, clear=True):
                trade_logger.log_trade("SELL", self.row()["Symbol"], 1, 1.10, -29,
                                       source="BOT", execution_diagnostics=self.row())
            for path in paths.values():
                with open(path, newline="") as handle:
                    row = next(csv.DictReader(handle))
                self.assertEqual(row["ExecutionQuality"], "EXECUTION_ANOMALY")
                self.assertEqual(float(row["BrokerRealizedPnL"]), -29)

    def test_display_and_legacy_values(self):
        result = self.classify(self.row())
        card = dashboard.render_sell_trade_card(result)
        for label in ("Entry Fill vs Ask", "Entry Fill vs Mid", "Execution Quality", "Strategy Performance Eligible", "ENTRY_EXECUTION_ANOMALY"):
            self.assertIn(label, card)
        legacy = {"Action": "SELL", "Source": "BOT", "PnL": 5}
        result = self.classify(legacy)
        self.assertEqual(result["PnL"], 5)
        self.assertEqual(result["ExecutionQuality"], "UNKNOWN")
        self.assertEqual(trade_logger.execution_performance_summary([result])["all_broker_trades"]["count"], 0)

    def test_human_untouched_and_reporting_has_no_order_calls(self):
        row = self.row()
        row["Source"] = "HUMAN"
        self.assertEqual(self.classify(row), row)
        with patch.object(dashboard, "submit_option_order", side_effect=AssertionError("No broker orders")) as submit:
            dashboard.summarize_ledger([self.row()], "BOT", {})
            submit.assert_not_called()
