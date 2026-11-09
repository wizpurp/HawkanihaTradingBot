"""Bid-based option exits. Mocked broker, temporary settings, state and logs only."""
import copy
import csv
import json
import math
import os
import shutil
import tempfile
import threading
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import dashboard
from broker_connection import BrokerConnectionError, Connection
from logs import trade_logger
from tournament.exits import valid_bid

SYMBOL = "SPY261120C00700000"
REAL_LOAD_CONFIG = dashboard.load_config
REAL_SUBMIT_ORDER = dashboard.submit_option_order
REAL_BROKER_REQUEST = dashboard.broker_request
ISOLATED = os.environ.get("HAWKANIHA_ISOLATED_TESTS") == "1"


def order_body(order_id, status):
    return json.dumps({"order": {"id": order_id, "status": status}})


class FakeBroker:
    """Stands in for submit_option_order and broker_request; records every call."""

    def __init__(self):
        self.submitted = []
        self.tags = []
        self.orders = {}
        self.next_id = 1000
        self.submit_script = []
        self.unexpected = []
        self.list_error = None
        self.list_params = []
        self.tags_supported = True
        self.order_lookup_error = None
        self.positions = []

    def add_order(self, qty, tag, status="open", exec_quantity=0, symbol=SYMBOL, side="sell_to_close"):
        self.next_id += 1
        self.orders[str(self.next_id)] = {
            "id": self.next_id, "status": status, "quantity": qty, "exec_quantity": exec_quantity,
            "remaining_quantity": qty - exec_quantity, "option_symbol": symbol, "side": side, "tag": tag,
            "create_date": dashboard.market_now().isoformat(),
        }
        return self.next_id

    def accept_then(self, failure):
        """The broker receives the order, but the caller never sees the response."""
        def step(symbol, qty, tag):
            self.add_order(qty, tag)
            raise failure
        return step

    def submit(self, option_symbol, qty, action="buy_to_open", tag=None):
        self.submitted.append((option_symbol, qty, action))
        self.tags.append(tag)
        if self.submit_script:
            step = self.submit_script.pop(0)
            if isinstance(step, Exception):
                raise step
            if callable(step):
                step(option_symbol, qty, tag)
            else:
                return step
        order_id = self.add_order(qty, tag)
        return 200, order_body(order_id, "ok")

    def list_orders(self, params):
        self.list_params.append(params)
        if isinstance(self.list_error, Exception):
            raise self.list_error
        if isinstance(self.list_error, int):
            return Mock(status_code=self.list_error, json=Mock(return_value={}))
        if self.list_error == "malformed":
            return Mock(status_code=200, json=Mock(return_value={"unexpected": True}))
        include = params.get("includeTags") == "true" and self.tags_supported
        rows = [{k: v for k, v in order.items() if include or k != "tag"} for order in self.orders.values()]
        body = "null" if not rows else {"order": rows[0] if len(rows) == 1 else rows}
        return Mock(status_code=200, json=Mock(return_value={"orders": body}))

    def request(self, method, path, **kwargs):
        if method == "GET" and path.startswith("/markets/"):
            return Mock(status_code=503, json=Mock(return_value={}))
        if method == "GET" and path.endswith("/orders"):
            return self.list_orders(kwargs.get("params") or {})
        if method == "GET" and path.endswith("/positions"):
            rows = [dict(row) for row in self.positions]
            body = "null" if not rows else {"position": rows[0] if len(rows) == 1 else rows}
            return Mock(status_code=200, json=Mock(return_value={"positions": body}))
        if method == "GET" and "/orders/" in path and self.order_lookup_error:
            raise self.order_lookup_error
        if method != "GET" or "/orders/" not in path:
            self.unexpected.append((method, path))
            raise AssertionError(f"unexpected broker call {method} {path}")
        order = self.orders.get(path.rsplit("/", 1)[1])
        if order is None:
            return Mock(status_code=404, json=Mock(return_value={}))
        return Mock(status_code=200, json=Mock(return_value={"order": dict(order)}))

    def last_order(self):
        return self.orders[str(self.next_id)]

    def fill(self, price, quantity=None, status="filled", when="2026-11-09T15:00:00.000Z"):
        order = self.last_order()
        quantity = order["quantity"] if quantity is None else quantity
        order.update(status=status, exec_quantity=quantity, avg_fill_price=price,
                     remaining_quantity=order["quantity"] - quantity, transaction_date=when)


class BidExitCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        join = lambda name: os.path.join(self.directory.name, name)
        self.state_file = join("exit_state.json")
        self.recovery_file = join("exit_recovery.json")
        self.trades_file = join("trades.csv")
        self.audit_file = join("audit.csv")
        self.broker = FakeBroker()
        self.quote = None
        self.config = {
            "symbol": "SPY", "contracts": 1, "broker_selection_version": 0, "bot_enabled": False,
            "strategy": {"hard_stop_percent": 20.0, "trailing_stop_percent": 15.0,
                         "enable_profit_floor_trailing_stop": False, "locked_profit_dollars": 0.0},
            "entry_rules": {"minimum_signals": 3, "max_trades_per_day": 10, "cooldown_minutes": 0},
        }
        self.original_config = copy.deepcopy(self.config)
        self.connection = Connection("sandbox", "TESTACCOUNT1", "dummy-token")

        def fake_connection(config=None):
            if self.connection is None:
                raise BrokerConnectionError("unverifiable")
            return self.connection

        fresh_state = {key: {} for key in (
            "position_peaks", "position_peak_basis", "position_exit_entry_prices",
            "position_profit_floor_active", "position_effective_stops", "position_max_profit",
            "position_max_drawdown", "pending_exit_orders", "exit_confirmed_remaining", "exit_quote_errors", "exit_retry_after",
            "exit_closed_awaiting", "unscoped_exit_positions",
            "exit_state_absent_counts", "market_context", "last_trade_review",
        )}
        fresh_state.update(reason_log=[], last_error="None", exit_state_error="", exit_intent_clear_result="", exit_recovery=None, running=False,
                           last_position_status="OK_POSITION", day=None)
        patches = [
            patch.dict(dashboard.BOT_STATE, fresh_state),
            patch.object(dashboard, "EXIT_STATE_FILE", self.state_file),
            patch.object(dashboard, "EXIT_RECOVERY_FILE", self.recovery_file),
            patch.object(dashboard, "BOT_AUDIT_FILE", self.audit_file),
            patch.object(dashboard, "BOT_AUDIT_VISIBLE_FILE", join("audit_visible.csv")),
            patch.object(dashboard, "BOT_AUDIT_BACKUP_FILE", join("audit_backup.csv")),
            patch.object(trade_logger, "TRADES_FILE", self.trades_file),
            patch.object(trade_logger, "VISIBLE_TRADES_FILE", join("visible.csv")),
            patch.object(trade_logger, "TRADE_HISTORY_VISIBLE_FILE", join("history.csv")),
            patch.object(dashboard, "load_config", side_effect=lambda: self.config),
            patch.object(dashboard, "resolve_connection", side_effect=fake_connection),
            patch.object(dashboard, "market_data_environment", return_value="PRODUCTION"),
            patch.object(dashboard, "scanner_market_is_open", return_value=True),
            patch.object(dashboard, "option_market_is_open", return_value=True),
            patch.object(dashboard, "get_market_quote", side_effect=lambda symbol: self.quote),
            patch.object(dashboard, "submit_option_order", side_effect=self.broker.submit),
            patch.object(dashboard, "broker_request", side_effect=self.broker.request),
            patch.object(dashboard.time, "sleep"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.assertEqual(self.broker.unexpected, [])
        self.assertEqual(self.config, self.original_config, "settings must not change")

    def now(self):
        return dashboard.market_now()

    def set_quote(self, bid, ask=None, last=None, age=1.0, **extra):
        self.quote = {"symbol": SYMBOL, "bid": bid, "ask": ask, "last": last,
                      "bid_date": (self.now() - timedelta(seconds=age)).isoformat(), **extra}
        return self.quote

    def position(self, entry=1.25, qty=1):
        return {"symbol": SYMBOL, "quantity": qty, "cost_basis": entry * 100 * qty, "_broker_selection_version": 0,
                "date_acquired": "2026-11-01T14:00:00.000Z"}

    def poll(self, entry=1.25, qty=1):
        return dashboard.fast_exit_poll(self.config, [self.position(entry, qty)])

    def state(self, name):
        if name == "pending_exit_orders":
            scope = dashboard.exit_broker_scope(self.config)
            return next((record for record in dashboard.BOT_STATE[name].values()
                         if record.get("symbol") == SYMBOL and record.get("scope") == scope), None)
        return dashboard.BOT_STATE[name].get(self.key())

    def key(self):
        return dashboard.exit_state_key(SYMBOL, self.config)

    def set_peak(self, peak, entry=1.25, basis="BID"):
        dashboard.BOT_STATE["position_peaks"][self.key()] = peak
        dashboard.BOT_STATE["position_peak_basis"][self.key()] = basis
        dashboard.BOT_STATE["position_exit_entry_prices"][self.key()] = entry

    def sell_rows(self):
        if not os.path.exists(self.trades_file):
            return []
        with open(self.trades_file, newline="") as handle:
            return [row for row in csv.DictReader(handle) if row["Action"] == "SELL"]

    def audit_rows(self):
        if not os.path.exists(self.audit_file):
            return []
        with open(self.audit_file, newline="") as handle:
            return list(csv.DictReader(handle))

    def restart(self):
        """Simulate a process restart: all in-memory exit state is lost, then reloaded."""
        for name in ("position_peaks", "position_peak_basis", "position_exit_entry_prices",
                     "position_profit_floor_active", "position_effective_stops", "pending_exit_orders",
                     "exit_confirmed_remaining", "exit_closed_awaiting", "unscoped_exit_positions"):
            dashboard.BOT_STATE[name].clear()
        dashboard.BOT_STATE["exit_recovery"] = None
        return dashboard.load_exit_state()


class StopTriggerTests(BidExitCase):
    def test_last_above_stop_with_bid_below_triggers_exit(self):
        self.set_quote(bid=0.90, ask=1.15, last=1.10)
        self.assertTrue(self.poll())
        self.assertEqual(self.broker.submitted, [(SYMBOL, 1, "sell_to_close")])

    def test_decide_surfer_action_uses_bid_and_records_trigger(self):
        self.set_quote(bid=0.90, ask=1.15, last=1.10)
        context = {"price": 700.0, "reasons": []}
        decision, reasons = dashboard.decide_surfer_action(self.config, [self.position()], context)
        self.assertEqual(decision, "SELL")
        self.assertIn("Hard stop hit", reasons)
        trigger = context["exit_trigger_diagnostics"]
        self.assertEqual(trigger["ExitStopPriceSource"], "BID")
        self.assertEqual(trigger["ExitTriggerPrice"], 0.90)
        self.assertEqual(trigger["ExitTriggerOptionLast"], 1.10)
        self.assertEqual(trigger["ExitHardStopPrice"], 1.00)
        self.assertNotEqual(trigger["ExitTriggerQuoteTimestamp"], "")

    def test_stale_last_below_stop_alone_does_not_trigger(self):
        self.set_quote(bid=1.05, ask=1.15, last=0.95)
        self.assertFalse(self.poll())
        self.assertEqual(self.broker.submitted, [])
        context = {"price": 700.0, "reasons": []}
        decision, _ = dashboard.decide_surfer_action(self.config, [self.position()], context)
        self.assertEqual(decision, "HOLD")

    def test_bid_exactly_equal_to_stop_triggers(self):
        cases = [
            ("armed trailing", 1.00, 1.30, 1.105),   # 1.30 * 0.85, above entry so armed
            ("hard stop float edge", 1.00, None, 0.80),  # P/L computes to -19.999...%
        ]
        for name, entry, peak, bid in cases:
            with self.subTest(name):
                dashboard.BOT_STATE["position_peaks"].clear()
                dashboard.BOT_STATE["pending_exit_orders"].clear()
                dashboard.BOT_STATE["position_exit_entry_prices"].clear()
                self.broker.submitted.clear()
                if peak:
                    self.set_peak(peak, entry)
                self.set_quote(bid=bid, ask=bid + 0.05, last=bid + 0.20)
                self.assertTrue(self.poll(entry=entry))
                self.assertEqual(len(self.broker.submitted), 1)

    def test_one_cent_above_armed_stop_holds(self):
        self.set_peak(1.60)  # stop 1.36
        self.set_quote(bid=1.37, ask=1.40, last=1.30)
        self.assertFalse(self.poll())
        self.assertEqual(self.broker.submitted, [])


class BidPeakTests(BidExitCase):
    def test_bid_derived_peak_controls_trailing(self):
        # A last-based peak of 2.00 would put the stop at 1.70 and sell at bid 1.60.
        self.set_quote(bid=1.60, ask=2.10, last=2.00)
        self.assertFalse(self.poll())
        self.assertEqual(self.state("position_peaks"), 1.60)
        self.assertEqual(self.state("position_peak_basis"), "BID")
        self.set_quote(bid=1.37, ask=1.60, last=1.80)  # stop = 1.36
        self.assertFalse(self.poll())
        self.assertEqual(self.broker.submitted, [])
        self.set_quote(bid=1.36, ask=1.40, last=1.80)
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)

    def test_profit_floor_activation_uses_bid(self):
        self.config["strategy"].update(enable_profit_floor_trailing_stop=True, locked_profit_dollars=20.0)
        self.original_config = copy.deepcopy(self.config)
        # entry 1.00, floor 1.20. High last/ask must not activate it.
        self.set_quote(bid=1.15, ask=1.40, last=1.30)
        self.assertFalse(self.poll(entry=1.00))
        self.assertFalse(self.state("position_profit_floor_active"))
        self.set_quote(bid=1.25, ask=1.30, last=1.28)
        self.assertFalse(self.poll(entry=1.00))
        self.assertTrue(self.state("position_profit_floor_active"))
        self.assertEqual(self.state("position_effective_stops"), 1.20)
        self.set_quote(bid=1.21, ask=1.30, last=1.10)
        self.assertFalse(self.poll(entry=1.00))
        self.assertEqual(self.broker.submitted, [])
        self.set_quote(bid=1.20, ask=1.30, last=1.40)
        self.assertTrue(self.poll(entry=1.00))
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertIn("PROFIT_FLOOR_STOP", self.audit_rows()[-1]["sell_trigger_reason"])


class DisplayIsolationTests(BidExitCase):
    def assert_state_untouched(self):
        self.assertEqual(self.state("position_peaks"), 1.40)
        self.assertEqual(self.state("position_peak_basis"), "BID")
        self.assertFalse(self.state("position_profit_floor_active"))
        self.assertIsNone(self.state("position_effective_stops"))
        self.assertFalse(os.path.exists(self.state_file), "display must not persist state")

    def test_display_cannot_overwrite_bid_peak(self):
        self.set_peak(1.40)
        self.set_quote(bid=1.35, ask=9.00, last=8.00)
        with patch.object(dashboard, "position_hold_time", return_value="N/A"), \
             patch.object(dashboard, "find_last_buy", return_value=None):
            data = dashboard.get_position_pl_data(self.position())
        self.assert_state_untouched()
        self.assertEqual(data["current_price"], 1.35)
        self.assertEqual(data["peak_price"], 1.40)

    def test_debug_and_unbased_stop_calls_are_read_only(self):
        self.set_peak(1.40)
        dashboard.stop_debug_values(SYMBOL, 1.25, 9.00, self.config)
        dashboard.calculate_stop_state(SYMBOL, 1.25, 9.00, self.config)
        for basis in ("LAST", "ASK", None):
            dashboard.calculate_stop_state(SYMBOL, 1.25, 9.00, self.config, update_state=True, price_basis=basis)
        self.assert_state_untouched()

    def test_trade_log_stop_lookup_cannot_overwrite_peak(self):
        self.set_peak(1.40)
        with patch.object(dashboard, "sync_trade_limits_from_file", return_value=(0, 0)):
            trade_logger.log_trade("SELL", SYMBOL, 1, 9.00, 10.0, source="HUMAN", market_context={})
        self.assert_state_untouched()

    def test_market_context_reads_stops_on_bid_without_writing_peak(self):
        self.set_peak(1.40)
        self.set_quote(bid=1.35, ask=9.00, last=8.00)
        samples = [{"time": self.now(), "price": 700.0 + index / 10, "volume": None} for index in range(30)]
        original = dashboard.calculate_stop_state
        seen = []

        def recording(symbol, entry, price, config=None, update_state=False, price_basis=None):
            seen.append((price, update_state))
            return original(symbol, entry, price, config, update_state=update_state, price_basis=price_basis)

        with patch.dict(dashboard.BOT_STATE, {"samples": samples}), \
             patch.object(dashboard, "calculate_stop_state", side_effect=recording):
            context = dashboard.build_market_context(self.config, [self.position()])
        self.assertEqual(seen, [(1.35, False)])
        self.assertAlmostEqual(context["current_pl"], 10.0)
        self.assert_state_untouched()

    def test_get_quote_price_for_signals_and_entries_is_unchanged(self):
        self.assertEqual(dashboard.get_quote_price({"last": 1.1, "bid": 0.9, "ask": 1.2}), 1.1)
        self.assertEqual(dashboard.get_quote_price({"bid": 0.9, "ask": 1.2}), 0.9)


class InvalidQuoteTests(BidExitCase):
    INVALID = {
        "no quote": (None, "MISSING_QUOTE"),
        "empty quote": ({}, "MISSING_QUOTE"),
        "no bid": ({"ask": 1.0, "last": 1.0}, "MISSING_BID"),
        "zero bid": ({"bid": 0}, "ZERO_BID"),
        "negative bid": ({"bid": -0.5}, "NEGATIVE_BID"),
        "nan bid": ({"bid": float("nan")}, "NON_FINITE_BID"),
        "infinite bid": ({"bid": float("inf")}, "NON_FINITE_BID"),
        "text bid": ({"bid": "abc"}, "INVALID_BID"),
        "boolean bid": ({"bid": True}, "INVALID_BID"),
        "crossed": ({"bid": 1.5, "ask": 1.0}, "CROSSED_QUOTE"),
    }

    def build(self, overrides):
        if overrides is None or overrides == {}:
            return overrides
        quote = {"bid": None, "ask": 1.60, "last": 1.50, "bid_date": self.now().isoformat()}
        quote.update(overrides)
        return quote

    def assert_data_error(self, status):
        self.assertFalse(self.poll())
        self.assertEqual(self.broker.submitted, [])
        self.assertEqual(self.state("position_peaks"), 1.40, "peak must not move")
        context = dashboard.BOT_STATE["market_context"]
        self.assertEqual(context["decision"], "QUOTE DATA ERROR")
        self.assertIsNone(context["current_pl"])
        self.assertIn(status, dashboard.BOT_STATE["last_error"])
        self.assertIn("UNRESOLVED EXPOSURE", dashboard.BOT_STATE["last_error"])
        self.assertIn("stops are NOT being evaluated", dashboard.BOT_STATE["last_error"])
        row = self.audit_rows()[-1]
        self.assertEqual(row["decision"], "QUOTE DATA ERROR")
        self.assertEqual(row["skip_reason"], "option quote data error")
        self.assertEqual(row["current_price"], "", "no fabricated price in the audit log")

    def test_invalid_quotes_produce_data_error(self):
        for name, (overrides, status) in self.INVALID.items():
            with self.subTest(name):
                self.set_peak(1.40)
                self.quote = self.build(overrides)
                self.assert_data_error(status)

    def test_stale_and_unverifiable_timestamps_produce_data_error(self):
        cases = {
            "stale": (self.now() - timedelta(seconds=120)).isoformat(),
            "missing": None,
            "garbage": "not-a-date",
            "future": (self.now() + timedelta(seconds=120)).isoformat(),
        }
        statuses = {"stale": "STALE_BID", "missing": "MISSING_BID_TIMESTAMP",
                    "garbage": "INVALID_BID_TIMESTAMP", "future": "FUTURE_BID_TIMESTAMP"}
        for name, stamp in cases.items():
            with self.subTest(name):
                self.set_peak(1.40)
                self.quote = {"bid": 0.50, "ask": 0.60, "last": 0.55, "bid_date": stamp}
                self.assert_data_error(statuses[name])

    def test_age_limit_is_configurable_and_sandbox_allows_delay(self):
        quote = {"bid": 1.0, "bid_date": (self.now() - timedelta(seconds=45)).isoformat()}
        self.assertEqual(dashboard.get_option_exit_price(quote, {})["status"], "STALE_BID")
        self.assertTrue(dashboard.get_option_exit_price(quote, {"max_exit_quote_age_seconds": 60})["ok"])
        delayed = {"bid": 1.0, "bid_date": (self.now() - timedelta(minutes=15)).isoformat()}
        self.assertEqual(dashboard.get_option_exit_price(delayed, {})["status"], "STALE_BID")
        with patch.object(dashboard, "market_data_environment", return_value="SANDBOX_DELAYED"):
            self.assertTrue(dashboard.get_option_exit_price(delayed, {})["ok"])

    def test_decide_surfer_action_reports_data_error(self):
        self.quote = {"ask": 2.0, "last": 2.0, "bid_date": self.now().isoformat()}
        decision, reasons = dashboard.decide_surfer_action(self.config, [self.position()], {"reasons": []})
        self.assertEqual(decision, "QUOTE DATA ERROR")
        self.assertIn("MISSING_BID", reasons[0])
        self.assertIn("UNRESOLVED EXPOSURE", reasons[1])

    def test_display_shows_error_not_a_fabricated_price(self):
        self.set_peak(1.40)
        self.quote = {"ask": 2.0, "last": 2.0, "bid_date": self.now().isoformat()}
        with patch.object(dashboard, "position_hold_time", return_value="N/A"), \
             patch.object(dashboard, "find_last_buy", return_value=None):
            data = dashboard.get_position_pl_data(self.position())
        self.assertIsNone(data["current_price"])
        self.assertIsNone(data["pnl"])
        self.assertIsNone(data["pnl_percent"])
        self.assertEqual(data["current_exit_decision"], "QUOTE DATA ERROR")
        self.assertIn("MISSING_BID", data["quote_status"])
        self.assertEqual(data["hard_stop_price"], 1.00, "stop levels stay visible")

    @unittest.skipUnless(ISOLATED, "full page render touches runtime files; use tests/run_isolated.py")
    def test_dashboard_page_renders_during_quote_outage(self):
        self.config = REAL_LOAD_CONFIG()  # the throwaway copy's config.json
        self.original_config = copy.deepcopy(self.config)
        self.set_peak(1.40)
        self.quote = {"ask": 2.0, "last": 2.0, "bid_date": self.now().isoformat()}
        with patch.object(dashboard, "get_position", return_value=[self.position()]), \
             patch.object(dashboard, "position_hold_time", return_value="N/A"):
            response = dashboard.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("QUOTE DATA ERROR", page)
        self.assertIn("MISSING_BID", page)

    def test_retry_recovers_and_then_triggers(self):
        self.set_peak(1.40)
        self.quote = None
        self.assertFalse(self.poll())
        self.assertFalse(self.poll())
        self.assertEqual(self.state("exit_quote_errors")["count"], 2)
        self.set_quote(bid=0.95, ask=1.00, last=1.30)
        self.assertTrue(self.poll())
        self.assertIsNone(self.state("exit_quote_errors"))
        self.assertEqual(len(self.broker.submitted), 1)

    def test_quote_error_sends_no_broker_mutation(self):
        self.quote = None
        with patch.object(dashboard, "broker_request") as request:
            self.poll()
        request.assert_not_called()

    def test_tournament_rejects_non_finite_bid(self):
        for bad in (float("nan"), float("inf"), 0, -1, None, "x"):
            self.assertFalse(valid_bid(bad), bad)
        self.assertTrue(valid_bid(0.5))
        self.assertTrue(math.isfinite(0.5))


class RecoveryTests(BidExitCase):
    def test_peak_survives_restart_and_still_triggers(self):
        self.set_quote(bid=1.60, ask=1.70, last=1.65)
        self.poll()
        self.assertTrue(self.restart())
        self.assertEqual(self.state("position_peaks"), 1.60)
        self.assertEqual(self.state("position_peak_basis"), "BID")
        self.set_quote(bid=1.36, ask=1.40, last=1.50)  # 1.60 * 0.85; without the peak this holds
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)

    def test_profit_floor_and_stop_are_not_lowered_after_restart(self):
        self.config["strategy"].update(enable_profit_floor_trailing_stop=True, locked_profit_dollars=20.0)
        self.original_config = copy.deepcopy(self.config)
        self.set_quote(bid=1.25, ask=1.30, last=1.28)
        self.poll(entry=1.00)
        self.restart()
        self.assertTrue(self.state("position_profit_floor_active"))
        self.assertEqual(self.state("position_effective_stops"), 1.20)
        self.set_quote(bid=1.20, ask=1.30, last=1.40)
        self.assertTrue(self.poll(entry=1.00))
        self.assertEqual(len(self.broker.submitted), 1)

    def test_legacy_peak_is_kept_and_flagged_not_loosened(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            json.dump({"version": 3, "positions": {self.key(): {"peak_price": 1.80, "entry_price": 1.25}}}, handle)
        dashboard.load_exit_state()
        self.assertEqual(self.state("position_peaks"), 1.80)
        self.assertEqual(self.state("position_peak_basis"), "LEGACY_UNVERIFIED")
        self.assertTrue(any("LEGACY_UNVERIFIED" in line for line in dashboard.BOT_STATE["reason_log"]))
        self.set_quote(bid=1.53, ask=1.60, last=1.70)  # legacy stop = 1.53
        self.assertTrue(self.poll())
        self.assertEqual(self.state("position_peaks"), 1.80)

    def test_legacy_peak_becomes_bid_based_once_bid_exceeds_it(self):
        self.set_peak(1.40, basis=None)
        self.set_quote(bid=1.50, ask=1.60, last=1.55)
        self.poll()
        self.assertEqual(self.state("position_peaks"), 1.50)
        self.assertEqual(self.state("position_peak_basis"), "BID")

    def test_corrupt_state_is_preserved_and_reported(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertFalse(dashboard.load_exit_state())
        self.assertIn("exit intents NOT restored", dashboard.BOT_STATE["exit_state_error"])
        preserved = [name for name in os.listdir(self.directory.name) if ".corrupt-" in name]
        self.assertEqual(len(preserved), 1)

    def test_invalid_saved_peak_requires_recovery(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            handle.write('{"positions": {"%s": {"peak_price": NaN, "peak_basis": "BID"}}}' % self.key())
        dashboard.load_exit_state()
        self.assertIsNone(self.state("position_peaks"))
        self.assertIn("invalid saved peak", dashboard.BOT_STATE["exit_state_error"])
        self.assertNotEqual(dashboard.exit_recovery_block_reason(self.config), "")

    def test_new_trading_day_keeps_peak_of_open_position(self):
        self.set_peak(1.60)
        dashboard.BOT_STATE["day"] = "2000-01-01"
        dashboard.sync_trade_limits_from_file(self.config)
        self.assertEqual(self.state("position_peaks"), 1.60)

    def test_changed_entry_price_resets_stale_peak_explicitly(self):
        self.set_peak(3.00, entry=2.50)
        self.set_quote(bid=1.30, ask=1.40, last=1.35)
        self.assertFalse(self.poll(entry=1.25))
        self.assertEqual(self.state("position_peaks"), 1.30)
        self.assertTrue(any("EXIT STATE RESET" in line for line in dashboard.BOT_STATE["reason_log"]))

    def test_state_cleared_only_after_repeated_flat_confirmations(self):
        self.set_peak(1.60)
        for _ in range(2):
            dashboard.reconcile_exit_state_with_broker(None)
        self.assertEqual(self.state("position_peaks"), 1.60)
        dashboard.BOT_STATE["last_position_status"] = "ERROR timeout"
        dashboard.reconcile_exit_state_with_broker(None)
        self.assertEqual(self.state("position_peaks"), 1.60, "a broker error is not proof of flat")
        dashboard.BOT_STATE["last_position_status"] = "OK_NO_POSITION"
        dashboard.reconcile_exit_state_with_broker(None)
        self.assertIsNone(self.state("position_peaks"))

    def test_pending_exit_survives_restart_without_resubmitting(self):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.poll()
        self.assertEqual(len(self.broker.submitted), 1)
        self.restart()
        self.assertIsNotNone(self.state("pending_exit_orders"))
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)
        self.broker.fill(0.88)
        dashboard.process_all_pending_exits(None)
        self.assertEqual(self.sell_rows()[0]["ExitBrokerFillPrice"], "0.88")
        self.assertIsNone(self.state("pending_exit_orders"))


class PendingExitTests(BidExitCase):
    def trigger(self, qty=1):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll(qty=qty))

    def test_acceptance_is_not_a_completed_exit(self):
        self.trigger()
        self.assertEqual(self.sell_rows(), [])
        self.assertEqual(self.state("position_peaks"), 1.25)
        self.assertIsNotNone(self.state("pending_exit_orders"))
        self.assertEqual(self.audit_rows()[-1]["action"], "EXIT_PENDING")

    def test_repeated_polling_does_not_duplicate_exit(self):
        self.trigger()
        for _ in range(10):
            self.assertTrue(self.poll())
        context = {"decision": "SELL", "decision_reasons": ["Hard stop hit"]}
        self.assertTrue(dashboard.try_surfer_exit(self.config, [self.position()], context))
        self.assertEqual(len(self.broker.submitted), 1)

    def test_manual_sell_does_not_duplicate_pending_exit(self):
        self.trigger()
        with patch.object(dashboard, "get_position", return_value=[self.position()]):
            _, message = dashboard.sell_all_positions()
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertIn("already pending", message)

    def test_confirmed_fill_drives_realized_pnl_and_clears_state(self):
        self.trigger()
        self.broker.fill(0.87)
        self.assertTrue(self.poll())
        row = self.sell_rows()[0]
        self.assertEqual(row["ExitTriggerPrice"], "0.9")
        self.assertEqual(row["ExitStopPriceSource"], "BID")
        self.assertEqual(row["ExitBrokerFillPrice"], "0.87")
        self.assertEqual(row["ExitBrokerOrderStatus"], "filled")
        self.assertAlmostEqual(float(row["ExitSlippageFromTrigger"]), -0.03)
        self.assertAlmostEqual(float(row["PnL"]), (0.87 - 1.25) * 100)
        self.assertIsNone(self.state("position_peaks"))
        self.assertIsNone(self.state("pending_exit_orders"))
        self.assertEqual(self.audit_rows()[-1]["action"], "SELL")

    def test_immediate_rejection_keeps_position_tracked_and_backs_off(self):
        self.set_peak(1.25)
        self.broker.submit_script.append((200, order_body(77, "rejected")))
        self.trigger()
        self.assertEqual(self.sell_rows(), [])
        self.assertEqual(self.state("position_peaks"), 1.25)
        self.assertIsNone(self.state("pending_exit_orders"))
        self.assertTrue(self.poll())  # inside backoff
        self.assertEqual(len(self.broker.submitted), 1)
        dashboard.BOT_STATE["exit_retry_after"][self.key()] = 0
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 2)

    def test_rejection_after_acceptance_keeps_position_tracked(self):
        self.trigger()
        self.broker.last_order().update(status="rejected")
        self.assertTrue(self.poll())
        self.assertEqual(self.sell_rows(), [])
        self.assertEqual(self.state("position_peaks"), 1.25)
        self.assertIsNone(self.state("pending_exit_orders"))
        self.assertGreater(dashboard.exit_retry_wait_seconds(SYMBOL), 0)

    def test_working_partial_fill_does_not_duplicate(self):
        self.trigger(qty=2)
        self.broker.fill(0.88, quantity=1, status="partially_filled")
        self.assertTrue(self.poll(qty=2))
        self.assertTrue(self.poll(qty=2))
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.sell_rows(), [])
        pending = self.state("pending_exit_orders")
        self.assertEqual(pending["filled_qty"], 1)
        self.assertEqual(pending["remaining_qty"], 1)

    def test_partial_fill_then_cancel_tracks_only_the_remainder(self):
        self.trigger(qty=2)
        self.broker.fill(0.88, quantity=1, status="canceled")
        self.assertTrue(self.poll(qty=2))
        rows = self.sell_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Qty"], "1")
        self.assertEqual(rows[0]["ExitBrokerFillPrice"], "0.88")
        self.assertAlmostEqual(float(rows[0]["PnL"]), (0.88 - 1.25) * 100)
        self.assertEqual(self.state("position_peaks"), 1.25, "remaining contract keeps its stop state")
        dashboard.BOT_STATE["exit_retry_after"][self.key()] = 0

        self.set_quote(bid=0.85, ask=0.90, last=1.00)
        self.assertTrue(self.poll(qty=1))
        self.assertEqual(self.broker.submitted[-1], (SYMBOL, 1, "sell_to_close"))
        self.broker.fill(0.84)
        self.assertTrue(self.poll(qty=1))
        self.assertEqual(len(self.sell_rows()), 2)
        self.assertIsNone(self.state("position_peaks"))

    def test_ambiguous_submit_stays_blocked_after_timeout_without_broker_evidence(self):
        """Replaces the old test that let an ambiguous order become retryable after 30 seconds."""
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        self.assertEqual(self.state("pending_exit_orders")["order_id"], "")
        self.broker.list_error = ConnectionError("order lookup down")
        for _ in range(5):
            stored = dashboard.BOT_STATE["pending_exit_orders"][self.state("pending_exit_orders")["key"]]
            stored["submitted_epoch"] -= 3600
            stored["last_reconcile_epoch"] = 0
            self.assertTrue(self.poll())  # broker still shows the position unchanged
        self.assertEqual(len(self.broker.submitted), 1, "time and an unchanged position prove nothing")
        self.assertEqual(self.state("pending_exit_orders")["unresolved_code"], "ORDER_LOOKUP_FAILED")
        self.assertEqual(self.sell_rows(), [])

    def test_ambiguous_submit_is_not_resolved_by_the_position_disappearing(self):
        self.broker.submit_script.append((503, "gateway error"))
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        dashboard.BOT_STATE["last_position_status"] = "OK_NO_POSITION"
        dashboard.process_all_pending_exits(None)
        self.assertEqual(self.state("pending_exit_orders")["unresolved_code"], "POSITION_CHANGED_NO_ORDER")
        self.assertEqual(self.sell_rows(), [], "no fill is invented from a position change")

    def test_lookup_failure_keeps_order_pending(self):
        self.trigger()
        with patch.object(dashboard, "broker_request", side_effect=ConnectionError("down")):
            self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertIsNotNone(self.state("pending_exit_orders"))
        self.assertEqual(self.sell_rows(), [])

    def test_bid_exit_order_remains_market_sell_to_close(self):
        with patch.object(dashboard, "submit_option_order", wraps=self.broker.submit) as submit:
            self.trigger()
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args, (SYMBOL, 1, "sell_to_close"))
        self.assertTrue(submit.call_args.kwargs["tag"].startswith("hkexit-"))


class SimulatedCrash(BaseException):
    """Stands in for the process dying; passes through `except Exception` like a real kill."""


class ExitIntentRecoveryTests(BidExitCase):
    def trigger(self, qty=1):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        return self.poll(qty=qty)

    def intent(self):
        return self.state("pending_exit_orders")

    def allow_reconcile(self, age=0):
        stored = dashboard.BOT_STATE["pending_exit_orders"][self.intent()["key"]]
        stored["last_reconcile_epoch"] = 0
        stored["submitted_epoch"] -= age

    def poll_reconcile(self, age=0, qty=1):
        self.allow_reconcile(age)
        return self.poll(qty=qty)

    def assert_blocked(self, code, sends=1):
        self.assertEqual(len(self.broker.submitted), sends, "no duplicate sell")
        self.assertIsNotNone(self.intent(), "protection must remain")
        self.assertEqual(self.intent()["unresolved_code"], code)
        self.assertIn("ACTION", self.intent()["status_message"])
        self.assertIn("ACTION", dashboard.BOT_STATE["last_error"])

    # 1
    def test_timeout_with_order_still_working_past_30s_sends_no_second_sell(self):
        self.broker.submit_script.append(self.broker.accept_then(TimeoutError("read timed out")))
        self.assertTrue(self.trigger())
        self.assertEqual(self.intent()["phase"], "SEND_UNCONFIRMED")
        for _ in range(4):
            self.assertTrue(self.poll_reconcile(age=3600))
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.intent()["order_id"], str(self.broker.next_id))
        self.assertEqual(self.intent()["phase"], "ACCEPTED")
        self.assertEqual(self.sell_rows(), [])

    # 2
    def test_unknown_submission_partial_fill_with_working_remainder_sends_no_second_sell(self):
        self.broker.submit_script.append(self.broker.accept_then(TimeoutError("read timed out")))
        self.assertTrue(self.trigger(qty=2))
        self.broker.fill(0.88, quantity=1, status="partially_filled")
        for _ in range(3):
            self.assertTrue(self.poll_reconcile(age=3600, qty=1))  # broker now shows 1 contract
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.intent()["filled_qty"], 1)
        self.assertEqual(self.intent()["remaining_qty"], 1)
        self.assertEqual(self.sell_rows(), [])

    # 3
    def test_partial_fill_then_cancel_retries_only_confirmed_remainder(self):
        self.broker.submit_script.append(self.broker.accept_then(TimeoutError("read timed out")))
        self.assertTrue(self.trigger(qty=2))
        self.broker.fill(0.88, quantity=1, status="cancelled")
        self.assertTrue(self.poll_reconcile(age=60, qty=2))
        self.assertIsNone(self.intent())
        self.assertEqual([row["Qty"] for row in self.sell_rows()], ["1"])
        dashboard.BOT_STATE["exit_retry_after"][self.key()] = 0
        self.set_quote(bid=0.85, ask=0.90, last=1.00)
        self.assertTrue(self.poll(qty=2))  # position feed lags and still says 2
        self.assertEqual(self.broker.submitted[-1], (SYMBOL, 1, "sell_to_close"))
        self.assertEqual(len(self.broker.submitted), 2)

    def test_nothing_is_retried_while_partial_order_is_not_terminal(self):
        self.trigger(qty=2)
        self.broker.fill(0.88, quantity=1, status="partially_filled")
        dashboard.BOT_STATE["exit_retry_after"][self.key()] = 0
        self.assertTrue(self.poll(qty=1))
        self.assertEqual(len(self.broker.submitted), 1)

    def test_terminal_order_without_executed_quantity_stays_unresolved(self):
        self.trigger(qty=2)
        order = self.broker.last_order()
        order["status"] = "cancelled"
        del order["exec_quantity"]
        self.assertTrue(self.poll(qty=2))
        self.assert_blocked("FILL_QUANTITY_MISSING")

    # 4
    def test_crash_after_intent_saved_before_submission(self):
        with patch.object(dashboard, "submit_option_order", side_effect=SimulatedCrash()), \
             self.assertRaises(SimulatedCrash):
            self.trigger()
        self.assertEqual(self.broker.submitted, [])
        with open(self.state_file, encoding="utf-8") as handle:
            first_tag = next(iter(json.load(handle)["pending_exit_orders"].values()))["tag"]

        self.restart()
        self.assertTrue(self.poll_reconcile())
        self.assert_blocked("NO_ORDER_FOUND_YET", sends=0)  # too soon to trust an empty list
        self.assertTrue(self.poll_reconcile(age=60))
        self.assertTrue(self.poll_reconcile())
        self.assertEqual(self.broker.submitted, [])
        self.assertIsNotNone(self.intent())
        self.assertTrue(self.poll_reconcile())
        self.assertIsNone(self.intent(), "three complete broker lists prove absence")
        self.assertEqual(self.broker.submitted, [])

        dashboard.BOT_STATE["exit_retry_after"][self.key()] = 0
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertNotEqual(self.broker.tags[-1], first_tag, "a new intent gets a new identifier")

    def test_one_interrupted_empty_lookup_restarts_the_absence_count(self):
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.trigger()
        self.poll_reconcile(age=60)
        self.poll_reconcile()
        self.broker.list_error = ConnectionError("down")
        self.poll_reconcile()
        self.broker.list_error = None
        self.poll_reconcile()
        self.poll_reconcile()
        self.assertIsNotNone(self.intent())
        self.assertEqual(self.intent()["absent_checks"], 2)

    # 5
    def test_crash_after_broker_acceptance_before_order_id_saved(self):
        self.broker.submit_script.append(self.broker.accept_then(SimulatedCrash()))
        with self.assertRaises(SimulatedCrash):
            self.trigger()
        with open(self.state_file, encoding="utf-8") as handle:
            saved = next(iter(json.load(handle)["pending_exit_orders"].values()))
        self.assertEqual(saved["order_id"], "")
        self.assertEqual(saved["tag"], self.broker.tags[0], "intent reached disk before the broker call")

        self.restart()
        self.assertTrue(self.poll_reconcile())
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.intent()["order_id"], str(self.broker.next_id))
        self.broker.fill(0.87)
        self.assertTrue(self.poll())
        self.assertEqual(self.sell_rows()[0]["ExitBrokerFillPrice"], "0.87")
        self.assertIsNone(self.intent())

    def test_intent_is_on_disk_before_order_is_sent(self):
        seen = []

        def observe(symbol, qty, tag):
            with open(self.state_file, encoding="utf-8") as handle:
                seen.append([record["tag"] for record in json.load(handle)["pending_exit_orders"].values()])

        self.broker.submit_script.append(observe)
        self.trigger()
        self.assertEqual(seen, [[self.broker.tags[0]]])

    # 6
    def test_restart_with_working_then_partially_filled_order(self):
        self.trigger(qty=2)
        self.restart()
        self.assertTrue(self.poll(qty=2))
        self.broker.fill(0.88, quantity=1, status="partially_filled")
        self.restart()
        self.assertTrue(self.poll(qty=1))
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.intent()["filled_qty"], 1)
        self.broker.fill(0.86, quantity=2)
        self.restart()
        self.assertTrue(self.poll(qty=1))
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual([row["Qty"] for row in self.sell_rows()], ["2"])
        self.assertIsNone(self.intent())

    # 7
    def test_broker_lookup_failures_remain_unresolved(self):
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.trigger()
        for failure in (ConnectionError("down"), 500, "malformed"):
            with self.subTest(failure=failure):
                self.broker.list_error = failure
                self.assertTrue(self.poll_reconcile(age=3600))
                self.assert_blocked("ORDER_LOOKUP_FAILED")

    def test_multiple_tagged_orders_remain_unresolved(self):
        self.broker.submit_script.append(self.broker.accept_then(TimeoutError("read timed out")))
        self.trigger()
        self.broker.add_order(1, self.broker.tags[0])
        self.assertTrue(self.poll_reconcile(age=3600))
        self.assert_blocked("MULTIPLE_TAGGED_ORDERS")
        self.assertEqual(self.intent()["order_id"], "")

    def test_possible_untagged_orders_remain_unresolved(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.broker.orders.clear()
                dashboard.BOT_STATE["pending_exit_orders"].clear()
                self.broker.submitted.clear()
                self.broker.submit_script.append(TimeoutError("read timed out"))
                self.trigger()
                for _ in range(count):
                    self.broker.add_order(1, None, status="filled", exec_quantity=1)
                for _ in range(5):
                    self.assertTrue(self.poll_reconcile(age=3600))
                self.assert_blocked("POSSIBLE_MATCHING_ORDERS")
                self.assertEqual(self.sell_rows(), [], "an untagged fill is not attributed to this intent")

    def test_position_decrease_without_order_evidence_stays_unresolved(self):
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.trigger(qty=2)
        for _ in range(5):
            self.assertTrue(self.poll_reconcile(age=3600, qty=1))
        self.assert_blocked("POSITION_CHANGED_NO_ORDER")
        self.assertEqual(self.sell_rows(), [], "a smaller position is not a confirmed fill")

    def test_flat_position_without_order_evidence_stays_unresolved(self):
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.trigger()
        dashboard.BOT_STATE["last_position_status"] = "OK_NO_POSITION"
        self.allow_reconcile(age=3600)
        dashboard.process_all_pending_exits(None)
        self.assert_blocked("POSITION_CHANGED_NO_ORDER")
        self.assertEqual(self.sell_rows(), [])

    def test_unconfirmed_position_cannot_prove_absence(self):
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.trigger()
        dashboard.BOT_STATE["last_position_status"] = "ERROR timeout"
        for _ in range(5):
            self.assertTrue(self.poll_reconcile(age=3600))
        self.assert_blocked("POSITION_UNKNOWN")

    def test_intent_from_an_earlier_session_is_not_cleared_by_todays_order_list(self):
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.trigger()
        dashboard.BOT_STATE["pending_exit_orders"][self.intent()["key"]]["intent_market_date"] = "2000-01-01"
        for _ in range(5):
            self.assertTrue(self.poll_reconcile(age=3600))
        self.assert_blocked("OUTSIDE_ORDER_LIST_WINDOW")

    def test_tagged_order_for_another_contract_is_not_adopted(self):
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.trigger()
        self.broker.add_order(1, self.broker.tags[0], symbol="SPY261120P00600000")
        self.assertTrue(self.poll_reconcile(age=3600))
        self.assert_blocked("ORDER_MISMATCH")

    def test_earlier_finished_intent_does_not_block_a_later_one(self):
        self.broker.add_order(1, "hkexit-earlier", status="cancelled", exec_quantity=1)
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.trigger()
        self.assertTrue(self.poll_reconcile(age=60))
        self.assertEqual(self.intent()["unresolved_code"], "NO_ORDER_FOUND_YET")

    def test_order_list_requests_tags_and_handles_one_order_as_object(self):
        self.broker.submit_script.append(self.broker.accept_then(TimeoutError("read timed out")))
        self.trigger()
        self.assertTrue(self.poll_reconcile())
        self.assertEqual(self.broker.list_params[-1]["includeTags"], "true")
        self.assertEqual(self.intent()["order_id"], str(self.broker.next_id))

    def test_missing_tag_support_stays_unresolved(self):
        self.broker.tags_supported = False
        self.broker.submit_script.append(self.broker.accept_then(TimeoutError("read timed out")))
        self.trigger()
        self.assertTrue(self.poll_reconcile(age=3600))
        self.assert_blocked("POSSIBLE_MATCHING_ORDERS")

    # 8
    def test_state_save_failure_sends_zero_orders(self):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        with patch.object(dashboard.os, "fsync", side_effect=OSError("disk full")):
            self.assertTrue(self.poll())
        self.assertEqual(self.broker.submitted, [])
        self.assertIsNone(self.intent())
        self.assertIn("ZERO orders sent", dashboard.BOT_STATE["last_error"])
        dashboard.BOT_STATE["exit_retry_after"][self.key()] = 0
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)

    # 9
    def test_repeated_reconciliation_and_restart_log_each_fill_once(self):
        self.trigger()
        snapshot = self.state_file + ".snapshot"
        shutil.copy(self.state_file, snapshot)
        self.broker.fill(0.87)
        self.assertTrue(self.poll())
        self.assertEqual(len(self.sell_rows()), 1)

        for _ in range(3):  # crash before the intent's removal reached disk, then recover again
            shutil.copy(snapshot, self.state_file)
            self.restart()
            self.assertTrue(self.poll())
        self.assertEqual(len(self.sell_rows()), 1)
        self.assertIsNone(self.intent())
        self.assertTrue(self.poll())  # position feed lags the confirmed fill
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(len(self.sell_rows()), 1)

    def test_partial_terminal_fill_is_logged_once_across_restarts(self):
        self.trigger(qty=2)
        snapshot = self.state_file + ".snapshot"
        shutil.copy(self.state_file, snapshot)
        self.broker.fill(0.88, quantity=1, status="cancelled")
        self.assertTrue(self.poll(qty=2))
        shutil.copy(snapshot, self.state_file)
        self.restart()
        self.assertTrue(self.poll(qty=2))
        self.assertEqual([row["Qty"] for row in self.sell_rows()], ["1"])

    def test_unreadable_trade_log_keeps_fill_unresolved(self):
        self.trigger()
        self.broker.fill(0.87)
        with patch.object(dashboard, "exit_fill_already_logged", side_effect=PermissionError("locked")):
            self.assertTrue(self.poll())
        self.assert_blocked("TRADE_LOG_UNREADABLE")

    # 10
    def test_concurrent_manual_and_automatic_exits_send_one_order(self):
        entered, release = threading.Event(), threading.Event()

        def hold(symbol, qty, tag):
            entered.set()
            release.wait(5)

        self.broker.submit_script.append(hold)
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        results = {}
        with patch.object(dashboard, "get_position", return_value=[self.position()]):
            automatic = threading.Thread(target=lambda: results.update(auto=self.poll()))
            manual = threading.Thread(target=lambda: results.update(manual=dashboard.sell_all_positions()))
            automatic.start()
            self.assertTrue(entered.wait(5))
            manual.start()
            threading.Event().wait(0.1)
            release.set()
            automatic.join(5)
            manual.join(5)
        self.assertFalse(automatic.is_alive() or manual.is_alive())
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertIn("already", results["manual"][1])

    # scope
    def test_intent_is_scoped_to_account_and_environment(self):
        self.trigger()
        original = self.connection
        for other in (Connection("sandbox", "OTHER", "tok"), Connection("live", original.account, "tok")):
            with self.subTest(other=other.environment):
                self.connection = other
                self.assertIsNone(dashboard.get_pending_exit(SYMBOL))
                self.assertFalse(dashboard.has_pending_exit_orders())
        self.connection = original
        self.assertIsNotNone(dashboard.get_pending_exit(SYMBOL))
        self.assertNotIn(original.account, json.dumps(self.intent()), "account number is not stored")

    def test_token_rotation_keeps_the_same_scope(self):
        self.trigger()
        self.connection = Connection(self.connection.environment, self.connection.account, "rotated")
        self.assertIsNotNone(dashboard.get_pending_exit(SYMBOL))

    def test_unverifiable_account_sends_nothing_and_fails_closed(self):
        self.trigger()
        self.connection = None
        self.assertIsNotNone(dashboard.get_pending_exit(SYMBOL))
        self.assertTrue(dashboard.has_pending_exit_orders())
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(
            dashboard.submit_tracked_exit("SPY261120P00600000", 1, 100, 1.0, "EXIT", "BOT", {}, {}, {}, "x", [], self.config),
            "SCOPE_UNAVAILABLE",
        )
        self.assertEqual(len(self.broker.submitted), 1)

    def test_old_format_pending_record_stays_blocked(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            json.dump({"pending_exit_orders": {SYMBOL: {"symbol": SYMBOL, "qty": 1, "order_id": "5", "submitted_epoch": 1}}}, handle)
        dashboard.load_exit_state()
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        self.assertEqual(self.broker.submitted, [])
        self.assertIn("ACCOUNT_UNVERIFIED", dashboard.BOT_STATE["last_error"])

    def test_exit_order_carries_tag_but_entries_do_not(self):
        with patch.object(dashboard, "broker_request", return_value=Mock(status_code=200, text="{}")) as post:
            REAL_SUBMIT_ORDER(SYMBOL, 1, "sell_to_close", tag="hkexit-abc")
            self.assertEqual(post.call_args.kwargs["data"]["tag"], "hkexit-abc")
            REAL_SUBMIT_ORDER(SYMBOL, 1, "buy_to_open")
            self.assertNotIn("tag", post.call_args.kwargs["data"])

    def test_validated_bid_gate_still_applies_before_any_intent(self):
        self.quote = {"ask": 0.95, "last": 0.50, "bid_date": self.now().isoformat()}
        self.assertFalse(self.poll())
        self.assertIsNone(self.intent())
        self.assertEqual(self.broker.submitted, [])


class ManualExitIntentClearTests(BidExitCase):
    def stuck_intent(self, age=3600, qty=1):
        self.broker.submit_script.append(TimeoutError("read timed out"))
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll(qty=qty))
        self.broker.list_error = ConnectionError("down")
        stored = dashboard.BOT_STATE["pending_exit_orders"][self.state("pending_exit_orders")["key"]]
        stored["submitted_epoch"] -= age
        stored["last_reconcile_epoch"] = 0
        self.assertTrue(self.poll(qty=qty))
        self.assertEqual(self.state("pending_exit_orders")["unresolved_code"], "ORDER_LOOKUP_FAILED")
        self.broker.list_error = None  # the broker can now verify; OperatorHoldTests covers when it cannot
        return self.state("pending_exit_orders")

    def clear(self, intent, confirmation=SYMBOL):
        return dashboard.clear_stuck_exit_intent(intent["intent_id"], confirmation)

    def assert_refused(self, result, text):
        self.assertFalse(result[0])
        self.assertIn(text, result[1])
        self.assertIsNotNone(self.state("pending_exit_orders"))

    def test_clear_removes_intent_durably_without_touching_orders(self):
        intent = self.stuck_intent()
        self.set_peak(1.60)
        self.broker.tags.clear()
        ok, message = self.clear(intent)
        self.assertTrue(ok)
        self.assertIn("NOT in the bot's trade log", message)
        self.assertIsNone(self.state("pending_exit_orders"))
        with open(self.state_file, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["pending_exit_orders"], {})
        self.assertEqual(len(self.broker.submitted), 1, "clearing sends nothing")
        self.assertEqual(self.broker.unexpected, [], "clearing cancels nothing")
        self.assertEqual(self.state("position_peaks"), 1.60, "stop state survives")
        self.assertEqual(self.sell_rows(), [], "no fill is invented")
        self.assertEqual(self.audit_rows()[-1]["action"], "MANUAL_EXIT_CLEAR")

    def test_exit_resumes_only_after_retry_delay_with_new_intent(self):
        intent = self.stuck_intent()
        self.assertTrue(self.clear(intent)[0])
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1, "retry delay applies")
        dashboard.BOT_STATE["exit_retry_after"][self.key()] = 0
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 2)
        self.assertNotEqual(self.state("pending_exit_orders")["intent_id"], intent["intent_id"])

    def test_cleared_flat_position_state_is_pruned(self):
        intent = self.stuck_intent()
        self.set_peak(1.60)
        self.assertTrue(self.clear(intent)[0])
        dashboard.BOT_STATE["last_position_status"] = "OK_NO_POSITION"
        for _ in range(3):
            dashboard.reconcile_exit_state_with_broker(None)
        self.assertIsNone(self.state("position_peaks"))

    def test_wrong_confirmation_is_refused(self):
        intent = self.stuck_intent()
        for wrong in ("", "yes", SYMBOL.lower()):
            with self.subTest(wrong=wrong):
                self.assert_refused(self.clear(intent, wrong), "exact contract symbol")

    def test_stale_or_unknown_intent_id_is_refused(self):
        self.stuck_intent()
        for bad in ("", "not-a-real-intent"):
            self.assert_refused(dashboard.clear_stuck_exit_intent(bad, SYMBOL), "no longer open")

    def test_recent_intent_is_refused(self):
        intent = self.stuck_intent(age=0)
        self.assert_refused(self.clear(intent), "under 30s old")

    def test_working_tagged_order_is_refused(self):
        intent = self.stuck_intent()
        self.broker.list_error = None
        self.broker.add_order(1, intent["tag"], status="partially_filled", exec_quantity=0)
        self.assert_refused(self.clear(intent), "working sell")

    def test_working_untagged_sell_on_contract_is_refused(self):
        intent = self.stuck_intent()
        self.broker.list_error = None
        self.broker.add_order(1, None)
        self.assert_refused(self.clear(intent), "working sell")

    def test_working_known_order_is_refused(self):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        intent = self.state("pending_exit_orders")
        dashboard.BOT_STATE["pending_exit_orders"][intent["key"]]["submitted_epoch"] -= 3600
        self.assert_refused(self.clear(intent), "Cancel it in Tradier first")
        self.broker.last_order()["status"] = "partially_filled"
        self.assert_refused(self.clear(intent), "partially_filled")

    def test_finished_order_completes_automatically_instead(self):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        intent = self.state("pending_exit_orders")
        dashboard.BOT_STATE["pending_exit_orders"][intent["key"]]["submitted_epoch"] -= 3600
        self.broker.fill(0.87)
        self.assert_refused(self.clear(intent), "complete it automatically")
        self.assertTrue(self.poll())
        self.assertEqual(self.sell_rows()[0]["ExitBrokerFillPrice"], "0.87")

    def test_terminal_order_with_missing_quantity_can_be_cleared(self):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll(qty=2))
        order = self.broker.last_order()
        order["status"] = "cancelled"
        del order["exec_quantity"]
        self.assertTrue(self.poll(qty=2))
        intent = self.state("pending_exit_orders")
        dashboard.BOT_STATE["pending_exit_orders"][intent["key"]]["submitted_epoch"] -= 3600
        self.assertEqual(intent["unresolved_code"], "FILL_QUANTITY_MISSING")
        self.assertTrue(self.clear(intent)[0])

    def test_save_failure_keeps_intent(self):
        intent = self.stuck_intent()
        with patch.object(dashboard.os, "fsync", side_effect=OSError("disk full")):
            self.assert_refused(self.clear(intent), "could not be saved")

    def test_other_account_intent_cannot_be_cleared(self):
        intent = self.stuck_intent()
        original = self.connection
        self.connection = Connection("sandbox", "OTHER", "tok")
        self.assertFalse(self.clear(intent)[0])
        self.connection = original
        self.assertIsNotNone(self.state("pending_exit_orders"))

    def test_unverifiable_account_is_refused(self):
        intent = self.stuck_intent()
        self.connection = None
        result = self.clear(intent)
        self.connection = Connection("sandbox", "TESTACCOUNT1", "dummy-token")
        self.assert_refused(result, "could not be verified")

    def test_old_format_intent_can_be_cleared(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            json.dump({"pending_exit_orders": {SYMBOL: {"symbol": SYMBOL, "qty": 1, "order_id": "5", "submitted_epoch": 1}}}, handle)
        dashboard.load_exit_state()
        record = next(iter(dashboard.BOT_STATE["pending_exit_orders"].values()))
        self.assertTrue(record["intent_id"])
        self.assertTrue(dashboard.clear_stuck_exit_intent(record["intent_id"], SYMBOL)[0])
        self.assertFalse(dashboard.has_pending_exit_orders())

    def test_route_clears_only_on_post_with_confirmation(self):
        intent = self.stuck_intent()
        client = dashboard.app.test_client()
        self.assertEqual(client.get("/resolve-exit-intent").status_code, 405)
        response = client.post("/resolve-exit-intent", data={"intent_id": intent["intent_id"], "confirm_symbol": "no"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.state("pending_exit_orders"))
        client.post("/resolve-exit-intent", data={"intent_id": intent["intent_id"], "confirm_symbol": SYMBOL})
        self.assertIsNone(self.state("pending_exit_orders"))

    def test_panel_lists_stuck_intent_and_escapes_text(self):
        intent = self.stuck_intent()
        dashboard.BOT_STATE["pending_exit_orders"][intent["key"]]["status_message"] = "<script>x</script>"
        html = dashboard.render_pending_exit_panel()
        self.assertIn(intent["intent_id"], html)
        self.assertIn('action="/resolve-exit-intent"', html)
        self.assertNotIn("<script>", html)

    def test_panel_is_hidden_when_nothing_to_show(self):
        self.assertEqual(dashboard.render_pending_exit_panel(), "")

    @unittest.skipUnless(ISOLATED, "full page render touches runtime files; use tests/run_isolated.py")
    def test_page_shows_panel_when_broker_position_is_flat(self):
        intent = self.stuck_intent()
        self.config = REAL_LOAD_CONFIG()  # the throwaway copy's config.json
        self.original_config = copy.deepcopy(self.config)
        with patch.object(dashboard, "get_position", return_value=None):
            response = dashboard.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Unresolved Exit Orders", page)
        self.assertIn(intent["intent_id"], page)


class CorruptStateRecoveryTests(BidExitCase):
    def corrupt(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertFalse(dashboard.load_exit_state())

    def allow_check(self, age=60):
        recovery = dashboard.BOT_STATE["exit_recovery"]
        recovery["detected_epoch"] -= age
        for check in recovery.get("checks", {}).values():
            check["last_epoch"] = 0

    def reconcile(self):
        self.allow_check()
        return dashboard.process_exit_recovery([self.position()])

    def assert_no_new_orders(self):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        with patch.object(dashboard, "get_position", return_value=[self.position()]):
            ok, message = dashboard.sell_all_positions()
        self.assertFalse(ok)
        self.assertIn("RECOVERY", message)
        ok, status, _, _ = dashboard.submit_and_parse_option_order(SYMBOL, 1, "buy_to_open", "ENTRY")
        self.assertEqual((ok, status), (False, "RECOVERY_REQUIRED"))
        self.assertEqual(self.broker.submitted, [])

    def test_corrupt_state_with_working_broker_exit_never_duplicates(self):
        self.broker.add_order(1, "hkexit-lostintent")
        self.corrupt()
        self.assert_no_new_orders()

        self.assertTrue(dashboard.persist_exit_state(), "ordinary save succeeds")
        self.assertTrue(os.path.exists(self.state_file))
        self.assert_no_new_orders()

        self.restart()
        self.assertIsNotNone(dashboard.BOT_STATE["exit_recovery"])
        self.assert_no_new_orders()

        self.assertEqual(self.reconcile(), "BLOCKED")
        self.assertIn("working option order", dashboard.BOT_STATE["exit_recovery"]["status"])
        self.restart()
        self.assert_no_new_orders()

    def test_renamed_corrupt_file_cannot_bypass_recovery(self):
        self.corrupt()
        self.assertFalse(os.path.exists(self.state_file), "corrupt file was moved aside")
        self.restart()
        self.assert_no_new_orders()

    def test_clean_broker_checks_release_only_after_settling(self):
        self.corrupt()
        self.assertEqual(dashboard.process_exit_recovery([self.position()]), "BLOCKED")
        self.assertEqual(self.reconcile(), "BLOCKED")
        self.assertEqual(self.reconcile(), "BLOCKED")
        self.assertEqual(self.reconcile(), "RECONCILED")
        self.assertEqual(dashboard.exit_recovery_block_reason(self.config), "")
        self.restart()
        self.assertEqual(dashboard.exit_recovery_block_reason(self.config), "", "release survives restart")
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)

    def test_failed_lookup_or_unconfirmed_position_cannot_reconcile(self):
        self.corrupt()
        self.broker.list_error = ConnectionError("down")
        for _ in range(5):
            self.assertEqual(self.reconcile(), "BLOCKED")
        self.broker.list_error = None
        dashboard.BOT_STATE["last_position_status"] = "ERROR timeout"
        for _ in range(5):
            self.assertEqual(self.reconcile(), "BLOCKED")
        self.assertNotEqual(dashboard.exit_recovery_block_reason(self.config), "")

    def test_each_account_must_be_reconciled(self):
        self.corrupt()
        for _ in range(3):
            self.reconcile()
        self.assertEqual(dashboard.exit_recovery_block_reason(self.config), "")
        self.connection = Connection("live", "OTHERACCOUNT", "tok")
        self.assertNotEqual(dashboard.exit_recovery_block_reason(self.config), "")

    def test_real_order_post_is_blocked_at_the_broker_layer(self):
        self.corrupt()
        gateway = Mock()
        with patch.object(dashboard, "BROKER", gateway), self.assertRaises(BrokerConnectionError):
            REAL_BROKER_REQUEST("POST", "/accounts/{account}/orders", data={"side": "sell_to_close"})
        gateway.request.assert_not_called()
        gateway.request.return_value = Mock(status_code=200)
        with patch.object(dashboard, "BROKER", gateway):
            REAL_BROKER_REQUEST("GET", "/accounts/{account}/orders")
        gateway.request.assert_called_once()

    def test_monitoring_and_reconciliation_keep_running(self):
        self.corrupt()
        self.set_quote(bid=1.60, ask=1.70, last=1.65)
        self.assertFalse(self.poll())
        self.assertEqual(self.state("position_peaks"), 1.60)
        self.reconcile()
        self.assertTrue(self.broker.list_params)

    def test_invalid_records_in_a_readable_file_require_recovery(self):
        cases = {
            "peak": {"positions": {"sandbox:x|" + SYMBOL: {"peak_price": -1}}},
            "intent": {"pending_exit_orders": {"sandbox:x|" + SYMBOL: {"symbol": SYMBOL}}},
            "remaining": {"exit_confirmed_remaining": {"sandbox:x|" + SYMBOL: "lots"}},
            "closed": {"exit_closed_awaiting": {"sandbox:x|" + SYMBOL: {"symbol": SYMBOL}}},
        }
        for name, payload in cases.items():
            with self.subTest(name):
                dashboard.BOT_STATE["exit_recovery"] = None
                with open(self.state_file, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                dashboard.load_exit_state()
                self.assertNotEqual(dashboard.exit_recovery_block_reason(self.config), "")

    def test_newer_state_version_requires_recovery(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            json.dump({"version": 99}, handle)
        self.assertFalse(dashboard.load_exit_state())
        self.assertNotEqual(dashboard.exit_recovery_block_reason(self.config), "")

    def test_unreadable_marker_fails_closed(self):
        with open(self.recovery_file, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        dashboard.load_exit_state()
        self.assertNotEqual(dashboard.exit_recovery_block_reason(self.config), "")

    def test_marker_write_failure_preserves_corrupt_file(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with patch.object(dashboard, "write_exit_recovery_marker", return_value=False):
            dashboard.load_exit_state()
            self.assertTrue(os.path.exists(self.state_file))
            self.assertFalse(dashboard.persist_exit_state())
        with open(self.state_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not json")

    def test_tagged_fill_found_in_recovery_keeps_stale_position_protected(self):
        order_id = self.broker.add_order(1, "hkexit-lostintent", status="filled", exec_quantity=1)
        self.broker.orders[str(order_id)]["transaction_date"] = self.now().isoformat()
        self.corrupt()
        for _ in range(3):
            self.reconcile()
        self.assertEqual(dashboard.exit_recovery_block_reason(self.config), "")
        self.assertIn(str(order_id), dashboard.BOT_STATE["exit_recovery"]["status"])
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        self.assertEqual(self.broker.submitted, [])


class ClosedAwaitingPositionTests(BidExitCase):
    def fully_filled(self, when=None):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        self.broker.fill(0.87, when=when or (self.now() - timedelta(seconds=120)).isoformat())
        self.assertTrue(self.poll())
        self.assertIsNotNone(self.closed())
        self.assertEqual(len(self.sell_rows()), 1)

    def closed(self):
        return self.state("exit_closed_awaiting")

    def expire_retry(self):
        dashboard.BOT_STATE["exit_retry_after"].clear()

    def assert_single_sell(self):
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(len(self.sell_rows()), 1)

    def flat(self):
        dashboard.BOT_STATE["last_position_status"] = "OK_NO_POSITION"
        dashboard.process_all_pending_exits(None)

    def test_stale_position_beyond_retry_delay_sends_no_sell(self):
        self.fully_filled()
        for _ in range(5):
            self.expire_retry()
            self.assertTrue(self.poll())
        self.assert_single_sell()
        self.assertIsNotNone(self.closed())

    def test_restart_during_delay_keeps_protection(self):
        self.fully_filled()
        self.restart()
        self.assertIsNotNone(self.closed())
        self.expire_retry()
        self.assertTrue(self.poll())
        self.assert_single_sell()

    def test_flat_confirmation_resolves_record(self):
        self.fully_filled()
        self.flat()
        self.assertIsNotNone(self.closed(), "one flat reading is not enough")
        self.flat()
        self.assertIsNone(self.closed())
        self.assert_single_sell()

    def test_flat_glitch_followed_by_old_position_restarts_count(self):
        self.fully_filled()
        self.flat()
        dashboard.BOT_STATE["last_position_status"] = "OK_POSITION"
        self.assertTrue(self.poll())
        self.assertEqual(self.closed()["flat_checks"], 0)
        self.assert_single_sell()

    def test_broker_error_is_not_flat_confirmation(self):
        self.fully_filled()
        dashboard.BOT_STATE["last_position_status"] = "ERROR timeout"
        for _ in range(4):
            dashboard.process_all_pending_exits(None)
        self.assertIsNotNone(self.closed())

    def test_submission_chokepoint_and_manual_sell_are_blocked(self):
        self.fully_filled()
        result = dashboard.submit_tracked_exit(SYMBOL, 1, 125, 1.25, "EXIT", "BOT", self.quote, {}, {}, "x", [], self.config, position_snapshot=self.position())
        self.assertEqual(result, "AWAITING_POSITION_CONFIRMATION")
        with patch.object(dashboard, "get_position", return_value=[self.position()]):
            dashboard.sell_all_positions()
        self.assert_single_sell()

    def assert_reentry_released(self, position):
        self.set_quote(bid=1.25, ask=1.30, last=1.27)
        self.assertFalse(dashboard.fast_exit_poll(self.config, [position]))
        self.assertIsNone(self.closed())
        self.expire_retry()
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(dashboard.fast_exit_poll(self.config, [position]))
        self.assertEqual(len(self.broker.submitted), 2, "the new position gets its own exit")
        self.assertNotEqual(self.broker.tags[0], self.broker.tags[1])

    def test_later_position_acquired_after_fill_is_genuinely_new(self):
        self.fully_filled()
        self.assert_reentry_released(dict(self.position(), date_acquired=self.now().isoformat()))

    def test_later_position_with_different_cost_is_genuinely_new(self):
        self.fully_filled()
        self.assert_reentry_released(self.position(entry=1.05))

    def test_later_buy_order_identifies_a_reentry_at_the_same_price(self):
        self.fully_filled()
        self.broker.add_order(1, None, status="filled", exec_quantity=1, side="buy_to_open")
        self.assert_reentry_released(self.position())

    def test_earlier_buy_order_is_not_a_reentry(self):
        order_id = self.broker.add_order(1, None, status="filled", exec_quantity=1, side="buy_to_open")
        self.broker.orders[str(order_id)]["create_date"] = (self.now() - timedelta(hours=2)).isoformat()
        self.fully_filled()
        self.expire_retry()
        self.assertTrue(self.poll())
        self.assert_single_sell()

    def test_closed_record_is_saved_together_with_intent_removal(self):
        self.fully_filled()
        with open(self.state_file, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["pending_exit_orders"], {})
        self.assertEqual(len(saved["exit_closed_awaiting"]), 1)


class OperatorHoldTests(BidExitCase):
    def intent(self):
        return self.state("pending_exit_orders")

    def known_order(self):
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        self.broker.order_lookup_error = ConnectionError("down")
        self.broker.list_error = ConnectionError("down")
        dashboard.BOT_STATE["pending_exit_orders"][self.intent()["key"]]["submitted_epoch"] -= 3600

    def unknown_order(self):
        self.broker.submit_script.append(self.broker.accept_then(TimeoutError("read timed out")))
        self.set_quote(bid=0.90, ask=0.95, last=1.10)
        self.assertTrue(self.poll())
        self.broker.list_error = ConnectionError("down")
        dashboard.BOT_STATE["pending_exit_orders"][self.intent()["key"]]["submitted_epoch"] -= 3600

    def assert_hold_blocks_resubmission(self):
        ok, message = dashboard.clear_stuck_exit_intent(self.intent()["intent_id"], SYMBOL)
        self.assertFalse(ok)
        self.assertIn("AUTOMATED RESUBMISSION REMAINS PAUSED", message)
        for restart in (False, True):
            if restart:
                self.restart()
            dashboard.BOT_STATE["exit_retry_after"].clear()
            self.allow()
            self.assertTrue(self.poll())
            self.assertEqual(len(self.broker.submitted), 1, "no sell while unverified")
            self.assertTrue(self.intent()["operator_hold"])
        self.assertIn("AUTOMATED RESUBMISSION PAUSED", self.intent()["status_message"])
        html = dashboard.render_pending_exit_panel()
        self.assertIn("AUTOMATED RESUBMISSION PAUSED", html)
        self.assertNotIn('action="/resolve-exit-intent"', html)

    def allow(self):
        dashboard.BOT_STATE["pending_exit_orders"][self.intent()["key"]]["last_reconcile_epoch"] = 0

    def test_known_order_lookup_failure_keeps_hold(self):
        self.known_order()
        self.assert_hold_blocks_resubmission()

    def test_order_list_failure_keeps_hold(self):
        self.unknown_order()
        self.assert_hold_blocks_resubmission()

    def test_discoverable_working_order_blocks_the_clear(self):
        self.known_order()
        self.broker.order_lookup_error = None
        ok, message = dashboard.clear_stuck_exit_intent(self.intent()["intent_id"], SYMBOL)
        self.assertFalse(ok)
        self.assertIn("Cancel it in Tradier first", message)
        self.assertFalse(self.intent().get("operator_hold"))

    def test_working_order_found_in_list_blocks_the_clear_when_single_lookup_fails(self):
        self.known_order()
        self.broker.list_error = None
        ok, message = dashboard.clear_stuck_exit_intent(self.intent()["intent_id"], SYMBOL)
        self.assertFalse(ok)
        self.assertIn("working sell", message)

    def test_hold_releases_only_after_repeated_broker_evidence(self):
        self.known_order()
        dashboard.clear_stuck_exit_intent(self.intent()["intent_id"], SYMBOL)
        self.broker.orders.clear()
        self.broker.list_error = None
        for expected in (1, 2):
            self.allow()
            self.assertTrue(self.poll())
            self.assertEqual(self.intent()["absent_checks"], expected)
            self.assertEqual(len(self.broker.submitted), 1)
        self.allow()
        self.assertTrue(self.poll())
        self.assertIsNone(self.intent())
        self.assertEqual(len(self.broker.submitted), 1)
        dashboard.BOT_STATE["exit_retry_after"].clear()
        self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 2, "resumes only after verified release")

    def test_hold_stays_when_recovered_lookup_shows_working_order(self):
        self.unknown_order()
        dashboard.clear_stuck_exit_intent(self.intent()["intent_id"], SYMBOL)
        self.broker.list_error = None
        for _ in range(5):
            self.allow()
            self.assertTrue(self.poll())
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertTrue(self.intent()["operator_hold"])
        self.assertEqual(self.intent()["order_id"], str(self.broker.next_id))

    def test_held_order_found_filled_is_logged_once(self):
        self.known_order()
        dashboard.clear_stuck_exit_intent(self.intent()["intent_id"], SYMBOL)
        self.broker.order_lookup_error = None
        self.broker.fill(0.87)
        self.assertTrue(self.poll())
        self.assertEqual(self.sell_rows()[0]["ExitBrokerFillPrice"], "0.87")
        self.assertIsNone(self.intent())


class ScopedStopStateTests(BidExitCase):
    ACCOUNT_A = Connection("sandbox", "ACCOUNTA", "token-a")
    ACCOUNT_B = Connection("live", "ACCOUNTB", "token-b")

    def seed_two_accounts(self):
        self.connection = self.ACCOUNT_A
        self.set_quote(bid=1.60, ask=1.70, last=1.65)
        self.assertFalse(self.poll(entry=1.25))
        self.connection = self.ACCOUNT_B
        self.set_quote(bid=3.00, ask=3.10, last=3.05)
        self.assertFalse(self.poll(entry=2.00))

    def assert_both_intact(self):
        self.connection = self.ACCOUNT_A
        self.assertEqual(self.state("position_peaks"), 1.60)
        self.assertEqual(self.state("position_exit_entry_prices"), 1.25)
        self.connection = self.ACCOUNT_B
        self.assertEqual(self.state("position_peaks"), 3.00)
        self.assertEqual(self.state("position_exit_entry_prices"), 2.00)

    def test_accounts_keep_independent_stops_across_switch_and_restart(self):
        self.seed_two_accounts()
        self.assert_both_intact()
        self.restart()
        self.assert_both_intact()
        self.assertFalse(any("EXIT STATE RESET" in line for line in dashboard.BOT_STATE["reason_log"]))

        self.connection = self.ACCOUNT_A
        self.set_quote(bid=1.37, ask=1.40, last=1.38)
        self.assertFalse(self.poll(entry=1.25), "account A stop is 1.36")
        self.connection = self.ACCOUNT_B
        self.set_quote(bid=2.55, ask=2.60, last=2.58)
        self.assertTrue(self.poll(entry=2.00), "account B stop is 2.55")
        self.assertEqual(len(self.broker.submitted), 1)
        self.connection = self.ACCOUNT_A
        self.assertEqual(self.state("position_peaks"), 1.60)

    def test_profit_floor_and_effective_stop_are_per_account(self):
        self.config["strategy"].update(enable_profit_floor_trailing_stop=True, locked_profit_dollars=20.0)
        self.original_config = copy.deepcopy(self.config)
        self.connection = self.ACCOUNT_A
        self.set_quote(bid=1.25, ask=1.30, last=1.28)
        self.poll(entry=1.00)
        self.assertTrue(self.state("position_profit_floor_active"))
        self.connection = self.ACCOUNT_B
        self.assertFalse(self.state("position_profit_floor_active"))
        self.assertIsNone(self.state("position_effective_stops"))

    def test_clearing_one_account_leaves_the_other(self):
        self.seed_two_accounts()
        dashboard.clear_position_exit_state(SYMBOL, "test")
        self.assertIsNone(self.state("position_peaks"))
        self.connection = self.ACCOUNT_A
        self.assertEqual(self.state("position_peaks"), 1.60)

    def test_flat_reconciliation_only_touches_selected_account(self):
        self.seed_two_accounts()
        dashboard.BOT_STATE["last_position_status"] = "OK_NO_POSITION"
        for _ in range(3):
            dashboard.reconcile_exit_state_with_broker(None)
        self.assertIsNone(self.state("position_peaks"))
        self.connection = self.ACCOUNT_A
        self.assertEqual(self.state("position_peaks"), 1.60)

    def test_unverifiable_account_cannot_write_stop_state(self):
        self.connection = None
        self.set_quote(bid=1.60, ask=1.70, last=1.65)
        self.poll()
        self.assertEqual(dashboard.BOT_STATE["position_peaks"], {})

    def write_old_state(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            json.dump({"version": 2, "positions": {SYMBOL: {"peak_price": 1.80, "entry_price": 1.25, "peak_basis": "BID"}}}, handle)
        dashboard.load_exit_state()
        return dashboard.BOT_STATE["unscoped_exit_positions"][SYMBOL]

    def test_old_unscoped_state_is_not_assigned_to_selected_account(self):
        self.write_old_state()
        self.assertIsNone(self.state("position_peaks"))
        self.assertEqual(dashboard.exit_recovery_block_reason(self.config), "", "migration is not corruption")
        self.assertIn("has no account", dashboard.render_pending_exit_panel())
        self.set_quote(bid=1.53, ask=1.60, last=1.70)
        self.assertFalse(self.poll(), "the unassigned peak of 1.80 is not used")

    def test_unscoped_state_survives_save_and_restart(self):
        record = self.write_old_state()
        self.assertTrue(dashboard.persist_exit_state())
        self.restart()
        self.assertEqual(dashboard.BOT_STATE["unscoped_exit_positions"][SYMBOL]["migration_id"], record["migration_id"])

    def test_operator_can_apply_unscoped_state_explicitly(self):
        record = self.write_old_state()
        self.assertFalse(dashboard.resolve_unscoped_exit_state(record["migration_id"], "apply", "wrong")[0])
        self.assertTrue(dashboard.resolve_unscoped_exit_state(record["migration_id"], "apply", SYMBOL)[0])
        self.assertEqual(self.state("position_peaks"), 1.80)
        self.assertEqual(self.state("position_peak_basis"), "LEGACY_UNVERIFIED")
        self.assertEqual(dashboard.BOT_STATE["unscoped_exit_positions"], {})

    def test_operator_can_discard_unscoped_state(self):
        record = self.write_old_state()
        self.assertTrue(dashboard.resolve_unscoped_exit_state(record["migration_id"], "discard", SYMBOL)[0])
        self.assertIsNone(self.state("position_peaks"))
        self.assertEqual(dashboard.BOT_STATE["unscoped_exit_positions"], {})

    def test_apply_refuses_to_overwrite_account_state(self):
        self.set_peak(1.40)
        record = self.write_old_state()
        self.assertFalse(dashboard.resolve_unscoped_exit_state(record["migration_id"], "apply", SYMBOL)[0])
        self.assertEqual(self.state("position_peaks"), 1.40)

    def test_migration_route(self):
        record = self.write_old_state()
        client = dashboard.app.test_client()
        self.assertEqual(client.post("/resolve-unscoped-exit-state", data={
            "migration_id": record["migration_id"], "action": "discard", "confirm_symbol": SYMBOL}).status_code, 302)
        self.assertEqual(dashboard.BOT_STATE["unscoped_exit_positions"], {})


class StopLoop(BaseException):
    pass


class BotLoopIntegrationTests(BidExitCase):
    """Runs the real surfer_bot_loop over mocked broker responses, one step per iteration."""

    def run_loop(self, steps):
        self.config.update(strategy_mode="SURFER", bot_enabled=True, scanner={"interval_seconds": 3600})
        self.original_config = copy.deepcopy(self.config)
        loop_sleeps = []

        def sleep(seconds):
            if seconds == 0.25:  # order-status retry delay inside one iteration
                return
            self.assertNotEqual(seconds, 10, f"bot loop raised: {dashboard.BOT_STATE['last_error']}")
            loop_sleeps.append(seconds)
            if len(loop_sleeps) >= len(steps):
                raise StopLoop()
            steps[len(loop_sleeps)]()

        steps[0]()
        with patch.object(dashboard.time, "sleep", side_effect=sleep), self.assertRaises(StopLoop):
            dashboard.surfer_bot_loop()

    def held(self, qty=1):
        def step():
            self.broker.positions = [self.broker_position(qty)]
        return step

    def broker_position(self, qty=1, entry=1.25):
        return {"id": 1, "symbol": SYMBOL, "quantity": qty, "cost_basis": entry * 100 * qty,
                "date_acquired": "2026-11-01T14:00:00.000Z"}

    def stale_with_retry_expired(self):
        self.broker.positions = [self.broker_position()]
        dashboard.BOT_STATE["exit_retry_after"].clear()

    def test_stale_positions_after_confirmed_fill_send_one_sell(self):
        def first():
            self.set_quote(bid=0.90, ask=0.95, last=1.10)
            self.held()()

        def filled():
            self.broker.fill(0.87, when=(self.now() - timedelta(seconds=120)).isoformat())
            self.stale_with_retry_expired()

        def flat():
            self.broker.positions = []

        self.run_loop([first, self.held(), filled, self.stale_with_retry_expired,
                       self.stale_with_retry_expired, self.stale_with_retry_expired, flat, flat, flat])
        self.assertEqual(self.broker.submitted, [(SYMBOL, 1, "sell_to_close")])
        self.assertEqual(len(self.sell_rows()), 1)
        self.assertIsNone(self.state("exit_closed_awaiting"))
        self.assertIsNone(self.state("pending_exit_orders"))

    def test_lost_submission_response_reconciles_across_iterations(self):
        self.broker.submit_script.append(self.broker.accept_then(TimeoutError("read timed out")))

        def first():
            self.set_quote(bid=0.90, ask=0.95, last=1.10)
            self.held()()

        def age():
            record = dashboard.BOT_STATE["pending_exit_orders"][self.state("pending_exit_orders")["key"]]
            record["submitted_epoch"] -= 3600
            record["last_reconcile_epoch"] = 0
            self.held()()

        def fill():
            self.broker.fill(0.87, when=(self.now() - timedelta(seconds=120)).isoformat())
            self.stale_with_retry_expired()

        self.run_loop([first, age, age, age, fill, self.stale_with_retry_expired, self.stale_with_retry_expired])
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(len(self.sell_rows()), 1)
        self.assertIsNotNone(self.state("exit_closed_awaiting"))

    def test_partial_fill_then_cancel_retries_remainder_in_loop(self):
        def first():
            self.set_quote(bid=0.90, ask=0.95, last=1.10)
            self.held(2)()

        def cancelled():
            self.broker.fill(0.88, quantity=1, status="cancelled", when=self.now().isoformat())
            self.held(2)()  # position feed still shows the old quantity

        def expired():
            dashboard.BOT_STATE["exit_retry_after"].clear()
            self.held(2)()

        self.run_loop([first, cancelled, expired, expired])
        self.assertEqual([order[1] for order in self.broker.submitted], [2, 1])
        self.assertEqual([row["Qty"] for row in self.sell_rows()], ["1"])

    def test_corrupt_state_reconciles_in_loop_before_any_sell(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        dashboard.load_exit_state()
        lost = str(self.broker.add_order(1, "hkexit-lostintent"))
        scope = dashboard.exit_broker_scope(self.config)

        def open_quote():
            self.set_quote(bid=0.90, ask=0.95, last=1.10)
            self.held()()

        def allow():
            recovery = dashboard.BOT_STATE["exit_recovery"]
            recovery["detected_epoch"] = 0
            recovery.setdefault("checks", {}).setdefault(scope, {"count": 0, "last_epoch": 0})["last_epoch"] = 0
            self.held()()

        def cancelled():
            self.broker.orders[lost].update(status="cancelled", exec_quantity=0)
            allow()

        def assert_still_none():
            self.assertEqual(self.broker.submitted, [])
            allow()

        self.run_loop([open_quote, allow, allow, assert_still_none, cancelled, allow, allow, allow, allow])
        self.assertEqual(dashboard.exit_recovery_block_reason(self.config), "")
        self.assertEqual(len(self.broker.submitted), 1, "exactly one exit after reconciliation")

    def test_unsettled_exit_blocks_new_entry_branch(self):
        def first():
            self.set_quote(bid=0.90, ask=0.95, last=1.10)
            self.held()()

        def filled_and_flat():
            self.broker.fill(0.87)
            self.broker.positions = []

        with patch.object(dashboard, "try_surfer_entry") as entry:
            self.run_loop([first, filled_and_flat])
        entry.assert_not_called()


class PendingEntryLoopCadenceTests(BidExitCase):
    """Real surfer_bot_loop and pending-entry code on a fake clock; only the scan and buy are stubbed."""

    CONTRACT = {"symbol": SYMBOL, "bid": 0.99, "ask": 1.01}

    def setUp(self):
        super().setUp()
        directory = self.directory.name
        patches = [
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_FILE", os.path.join(directory, "pending.json")),
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_VISIBLE_FILE", os.path.join(directory, "pending_visible.json")),
            patch.object(dashboard, "PENDING_ENTRY_HISTORY_BACKUP_FILE", os.path.join(directory, "pending_backup.json")),
            patch.object(dashboard, "record_shadow_gate_state_from_pending"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.config.update(strategy_mode="SURFER", bot_enabled=True, auto_entries_enabled=True,
                           scanner={"interval_seconds": 60}, option_momentum_percent=1.0,
                           two_candle_or_confirmation_enabled=False, confirmation_timeout_seconds=60)
        self.original_config = copy.deepcopy(self.config)
        dashboard.clear_pending_entry()
        self.clock = 1_800_000_000.0
        self.sleeps = []
        self.scans = []
        self.checks = []
        self.candidate_ids = set()

    def scan_creates_candidate(self, allow_entry=True):
        if not allow_entry:  # the position branch scans for exits only
            return
        self.scans.append(self.clock)
        dashboard.create_pending_entry(self.config, "BUY CALL", "CALL", dict(self.CONTRACT), 1, 1.00, {}, "MIDPOINT")
        self.candidate_ids.add(dashboard.get_pending_entry()["id"])

    def scan_finds_nothing(self, allow_entry=True):
        self.scans.append(self.clock)

    def run_loop(self, scan, iterations, on_sleep=None):
        real_process = dashboard.process_pending_entry

        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            self.clock += seconds
            if len(self.sleeps) >= iterations:
                raise StopLoop()
            if on_sleep:
                on_sleep(len(self.sleeps))

        def checked(config):
            self.checks.append(self.clock)
            return real_process(config)

        self.buy = Mock(return_value=True)
        with patch.object(dashboard.time, "time", side_effect=lambda: self.clock), \
             patch.object(dashboard.time, "sleep", side_effect=fake_sleep), \
             patch.object(dashboard, "surfer_bot_tick", side_effect=scan), \
             patch.object(dashboard, "process_pending_entry", side_effect=checked), \
             patch.object(dashboard, "execute_entry_buy", self.buy), \
             self.assertRaises(StopLoop):
            dashboard.surfer_bot_loop()
        self.assertNotIn(10, self.sleeps, f"bot loop raised: {dashboard.BOT_STATE['last_error']}")

    def waiting_quote(self):
        self.set_quote(bid=0.99, ask=1.01, last=1.00, age=0.0)

    def confirming_quote(self):
        self.set_quote(bid=1.02, ask=1.04, last=1.03, age=0.0)

    def test_new_candidate_is_checked_on_short_cadence(self):
        self.waiting_quote()
        self.run_loop(self.scan_creates_candidate, 2)
        self.assertEqual(self.sleeps[0], 1.0)
        self.assertEqual(self.checks[0] - self.scans[0], 1.0, "first check one poll after the scan, not 60s")

    def test_first_check_uses_the_configured_poll_interval(self):
        self.config["strategy"]["exit_poll_interval_ms"] = 500
        self.original_config = copy.deepcopy(self.config)
        self.waiting_quote()
        self.run_loop(self.scan_creates_candidate, 2)
        self.assertEqual(self.checks[0] - self.scans[0], 0.5)

    def test_scan_without_candidate_keeps_the_scanner_interval(self):
        self.waiting_quote()
        self.run_loop(self.scan_finds_nothing, 3)
        self.assertEqual(self.sleeps, [60, 60, 60])
        self.assertEqual([scan - self.scans[0] for scan in self.scans], [0.0, 60.0, 120.0])
        self.assertEqual(self.checks, [])

    def test_repeated_polling_creates_one_candidate_and_one_buy(self):
        self.waiting_quote()

        def after_sleep(count):
            if count == 5:
                self.confirming_quote()
            if count == 6:
                self.broker.positions = [{"symbol": SYMBOL, "quantity": 1, "cost_basis": 100.0, "date_acquired": self.now().isoformat()}]

        self.run_loop(self.scan_creates_candidate, 9, after_sleep)
        self.assertEqual(len(self.scans), 1, "no rescan while a candidate is active")
        self.assertEqual(len(self.candidate_ids), 1)
        self.assertEqual(len(self.checks), 5, "polled on every short iteration until confirmed")
        self.assertEqual(self.buy.call_count, 1)
        self.assertFalse(dashboard.get_pending_entry()["active"])

    def test_position_appearing_after_scan_cancels_candidate_without_monitoring(self):
        self.waiting_quote()

        def after_sleep(count):
            self.broker.positions = [{"symbol": SYMBOL, "quantity": 1, "cost_basis": 100.0, "date_acquired": self.now().isoformat()}]

        with patch.object(dashboard, "fast_exit_poll"):
            self.run_loop(self.scan_creates_candidate, 2, after_sleep)
        self.assertEqual(self.checks, [])
        self.assertEqual(dashboard.get_pending_entry()["status"], "CANCELLED")
        self.buy.assert_not_called()

    def test_unsettled_exit_still_blocks_candidate_monitoring(self):
        self.waiting_quote()

        def after_sleep(count):
            dashboard.BOT_STATE["exit_closed_awaiting"][self.key()] = {
                "key": self.key(), "scope": dashboard.exit_broker_scope(self.config), "symbol": SYMBOL,
                "closed_epoch": self.clock, "position_before": {}, "flat_checks": 0,
            }

        self.run_loop(self.scan_creates_candidate, 3, after_sleep)
        self.assertEqual(self.checks, [])
        self.buy.assert_not_called()


class SimulatedProcedureTest(BidExitCase):
    """The end-to-end sequence quoted in the hand-off notes; every number is asserted."""

    def test_full_sequence(self):
        entry = 1.25
        self.set_quote(bid=1.60, ask=1.65, last=1.62)
        self.assertFalse(self.poll(entry))
        self.assertEqual(self.state("position_peaks"), 1.60)

        self.set_quote(bid=1.37, ask=1.42, last=1.30)
        self.assertFalse(self.poll(entry))
        self.assertEqual(self.broker.submitted, [])

        self.set_quote(bid=None, ask=1.42, last=1.40)
        self.assertFalse(self.poll(entry))
        self.assertEqual(self.broker.submitted, [])
        self.assertIn("MISSING_BID", dashboard.BOT_STATE["last_error"])

        self.set_quote(bid=1.35, ask=1.41, last=1.50)
        self.assertTrue(self.poll(entry))
        self.assertEqual(self.broker.submitted, [(SYMBOL, 1, "sell_to_close")])
        self.assertEqual(self.sell_rows(), [])

        self.assertTrue(self.poll(entry))
        self.assertEqual(len(self.broker.submitted), 1)

        self.broker.fill(1.33)
        self.assertTrue(self.poll(entry))
        row = self.sell_rows()[0]
        self.assertEqual(row["ExitTriggerPrice"], "1.35")
        self.assertAlmostEqual(float(row["ExitEffectiveStopPrice"]), 1.36)
        self.assertEqual(row["ExitTriggerOptionLast"], "1.5")
        self.assertEqual(row["ExitBrokerFillPrice"], "1.33")
        self.assertAlmostEqual(float(row["PnL"]), 8.0)
        audit = next(row for row in self.audit_rows() if row["action"] == "SELL")
        self.assertIn("source=BID", audit["reason_log"])
        self.assertIn("bid=1.3500 ask=1.4100 last=1.5000 stop=1.3600 exit_reason=TRAILING_STOP", audit["reason_log"])


if __name__ == "__main__":
    unittest.main()
