"""Two-candle breakout timing on a mocked clock. No broker calls; all files are temporary."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import dashboard

ET = dashboard.MARKET_TZ
SYMBOL = "SPY261110C00700000"
LEVEL = 500.0  # opening-range high: CALL breakout candles must close above this


def at(hour, minute, second=0):
    return datetime(2026, 11, 10, hour, minute, second, tzinfo=ET)  # a Tuesday


class BreakoutTimingTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = lambda name: os.path.join(self.directory.name, name)
        self.now = at(10, 15, 2)
        self.closes = {}  # bar start -> close; unset bars close inside the opening range
        self.unpublished = set()  # completed bars the data feed has not delivered yet
        self.price = 1.00
        self.config = {
            "symbol": "SPY", "contracts": 1, "bot_enabled": True, "auto_entries_enabled": True,
            "decision_time": "09:35", "strategy": {}, "entry_rules": {},
            "option_momentum_confirmation_enabled": True, "option_momentum_percent": 1.0,
            "two_candle_or_confirmation_enabled": True, "required_breakout_candles": 2,
            "confirmation_timeout_seconds": 130, "pre_confirmation_max_drawdown_percent": 5.0,
        }
        self.buy = Mock(return_value=True)
        patches = [
            patch.dict(dashboard.BOT_STATE, {
                "pending_entry": dashboard.default_pending_entry(), "reason_log": [],
                "opening_range_confirmation": {"CALL": dashboard.default_opening_range_confirmation_state(),
                                               "PUT": dashboard.default_opening_range_confirmation_state()},
            }),
            patch.object(dashboard, "market_now", side_effect=lambda: self.now),
            patch.object(dashboard.time, "time", side_effect=lambda: self.now.timestamp()),
            patch.object(dashboard, "load_config", side_effect=lambda: self.config),
            patch.object(dashboard, "get_intraday_bars", side_effect=self.bars),
            patch.object(dashboard, "get_market_quote", side_effect=lambda symbol: {"bid": self.price, "ask": self.price}),
            patch.object(dashboard, "scanner_market_is_open", return_value=True),
            patch.object(dashboard, "option_market_is_open", return_value=True),
            patch.object(dashboard, "execute_entry_buy", self.buy),
            patch.object(dashboard, "record_shadow_gate_state_from_pending"),
            patch.object(dashboard, "broker_request", side_effect=AssertionError("no broker calls in these tests")),
            patch.object(dashboard, "BOT_AUDIT_FILE", path("audit.csv")),
            patch.object(dashboard, "BOT_AUDIT_VISIBLE_FILE", path("audit_visible.csv")),
            patch.object(dashboard, "BOT_AUDIT_BACKUP_FILE", path("audit_backup.csv")),
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_FILE", path("pending.json")),
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_VISIBLE_FILE", path("pending_visible.json")),
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_BACKUP_FILE", path("pending_backup.json")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def bars(self, symbol):
        """Every one-minute bar that has started, including the unfinished current one."""
        rows, start = [], at(9, 30)
        while start <= self.now:
            if start not in self.unpublished:
                opening = start < at(9, 35)
                close = self.closes.get(start, LEVEL - 1)
                rows.append({"time": start, "open": close, "high": LEVEL if opening else close,
                             "low": close - 1, "close": close, "volume": 1})
            start += timedelta(minutes=1)
        return rows

    def create(self, second, timeout=None):
        self.now = at(10, 15, second)
        if timeout is not None:
            self.config["confirmation_timeout_seconds"] = timeout
        dashboard.create_pending_entry(self.config, "BUY CALL", "CALL", {"symbol": SYMBOL, "bid": 0.99, "ask": 1.01},
                                       1, 1.00, {}, "MIDPOINT")
        return dashboard.get_pending_entry()

    def poll_at(self, hour, minute, second):
        self.now = at(hour, minute, second)
        dashboard.process_pending_entry(self.config)
        return dashboard.get_pending_entry()

    # The cause, shown with the counting code alone (no new check involved).
    def test_cause_second_candle_closes_after_a_60_second_deadline(self):
        for second in (0, 2, 30, 58, 59):
            with self.subTest(second=second):
                self.now = at(10, 15, second)
                dashboard.initialize_opening_range_breakout_confirmation(self.config, "CALL")
                self.closes.update({at(10, 15): LEVEL + 1, at(10, 16): LEVEL + 1})
                self.now = at(10, 15, second) + timedelta(seconds=60)  # the 60s deadline
                result = dashboard.update_opening_range_breakout_confirmation(self.config, "CALL")
                self.assertEqual(result["count"], 1, "only the signal-minute candle has closed")
                self.assertNotEqual(result["status"], "PASS")

    def test_60_second_timeout_is_refused_at_every_second_of_the_minute(self):
        for second in range(60):
            with self.subTest(second=second):
                dashboard.clear_pending_entry()
                pending = self.create(second, timeout=60)
                self.assertFalse(pending["active"])
                message = dashboard.BOT_STATE["reason_log"][-1]
                self.assertIn("confirmation_timeout_seconds is 60", message)
                self.assertIn("at least 130", message)
        self.assertEqual(self.config["confirmation_timeout_seconds"], 60, "the setting is never changed")

    def test_130_second_timeout_allows_every_second_of_the_minute(self):
        for second in range(60):
            with self.subTest(second=second):
                dashboard.clear_pending_entry()
                self.assertTrue(self.create(second, timeout=130)["active"])

    def test_candidate_near_start_of_minute(self):
        self.assertFalse(self.create(2, timeout=125)["active"], "needs 128s at :02")
        dashboard.clear_pending_entry()
        self.assertTrue(self.create(2, timeout=130)["active"])

    def test_candidate_near_end_of_minute(self):
        self.assertFalse(self.create(58, timeout=60)["active"], "needs 72s at :58")
        dashboard.clear_pending_entry()
        self.assertTrue(self.create(58, timeout=72)["active"])

    def test_momentum_only_is_unaffected(self):
        self.config["two_candle_or_confirmation_enabled"] = False
        self.assertTrue(self.create(2, timeout=60)["active"])

    def test_two_candles_confirm_inside_a_valid_window(self):
        for second in (2, 58):
            with self.subTest(second=second):
                dashboard.clear_pending_entry()
                self.buy.reset_mock()
                self.closes = {at(10, 15): LEVEL + 1, at(10, 16): LEVEL + 1}
                self.price = 1.00
                self.assertTrue(self.create(second)["active"])
                self.price = 1.02
                pending = self.poll_at(10, 16, 1)
                self.assertEqual((pending["momentum_status"], pending["current_breakout_candle"]), ("PASS", 1))
                pending = self.poll_at(10, 17, 1)
                self.assertEqual(pending["status"], "CONFIRMED")
                self.assertEqual(self.buy.call_count, 1)

    def test_repeated_polling_counts_each_completed_candle_once(self):
        self.closes = {at(10, 15): LEVEL + 1, at(10, 16): LEVEL + 5}  # 10:16 looks like a breakout while unfinished
        self.create(2)
        for second in range(1, 60, 7):
            pending = self.poll_at(10, 16, second)
            self.assertEqual(pending["current_breakout_candle"], 1, "unfinished 10:16 candle is never counted")
            self.assertTrue(pending["active"])
        pending = self.poll_at(10, 17, 0)
        self.assertEqual(pending["current_breakout_candle"], 2)
        self.assertEqual(self.buy.call_count, 0, "momentum has not passed")
        self.assertEqual(self.poll_at(10, 17, 5)["current_breakout_candle"], 2, "no double count")

    def test_genuine_breakout_timeout(self):
        self.closes = {at(10, 15): LEVEL + 1, at(10, 16): LEVEL + 1}
        self.unpublished = {at(10, 16)}  # second candle's data never arrives in time
        self.create(2)
        self.price = 1.02
        self.assertTrue(self.poll_at(10, 16, 1)["active"])
        self.assertTrue(self.poll_at(10, 17, 11)["active"], "still inside the 130s window")
        pending = self.poll_at(10, 17, 12)
        self.assertEqual(pending["status"], "CANCELLED")
        self.assertEqual(pending["reason"], "breakout confirmation failed")
        self.buy.assert_not_called()

    def test_genuine_momentum_timeout(self):
        self.closes = {at(10, 15): LEVEL + 1, at(10, 16): LEVEL + 1}
        self.create(2)
        self.poll_at(10, 17, 1)
        pending = self.poll_at(10, 17, 12)
        self.assertEqual(pending["reason"], "momentum confirmation timeout expired")
        self.buy.assert_not_called()

    def test_drawdown_still_cancels_while_waiting_for_candles(self):
        self.closes = {at(10, 15): LEVEL + 1}
        self.create(2)
        self.assertEqual(self.poll_at(10, 16, 1)["current_breakout_candle"], 1)
        self.price = 0.94
        pending = self.poll_at(10, 16, 10)
        self.assertEqual(pending["status"], "CANCELLED")
        self.assertEqual(pending["reason"], "maximum pre-confirmation drawdown hit")
        self.buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
