import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import dashboard
from broker_connection import BrokerGateway, BrokerConnectionError, Connection, parse_balances, resolve_connection


class BrokerConnectionTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.now = 1000
        self.gateway = BrokerGateway(self.transport, lambda: self.now)
        self.connection = Connection("sandbox", "VA123456", "private-test-token")
        self.balances = {"account_number": "VA123456", "account_type": "margin", "total_equity": 500,
                         "total_cash": 200, "market_value": 300, "open_pl": -10,
                         "pending_orders_count": 0, "margin": {"option_buying_power": 100}}
        self.transport.get.return_value = Mock(status_code=200, json=lambda: {"balances": self.balances})

    def test_legacy_credentials_only_route_to_declared_environment(self):
        env = {"TRADIER_TOKEN": "paper", "TRADIER_ACCOUNT": "PA1", "TRADIER_BASE_URL": "https://sandbox.tradier.com/v1"}
        self.assertEqual(resolve_connection({}, env).environment, "sandbox")
        with self.assertRaises(BrokerConnectionError):
            resolve_connection({"broker_environment": "live"}, env)

    def test_explicit_matching_pairs_never_mix_or_fallback(self):
        env = {"TRADIER_SANDBOX_TOKEN": "paper", "TRADIER_SANDBOX_ACCOUNT": "PA1",
               "TRADIER_LIVE_TOKEN": "real", "TRADIER_LIVE_ACCOUNT": "RE1"}
        for mode, token, account in (("sandbox", "paper", "PA1"), ("live", "real", "RE1")):
            connection = resolve_connection({"broker_environment": mode}, env)
            self.assertEqual((connection.token, connection.account), (token, account))
        del env["TRADIER_LIVE_ACCOUNT"]
        with self.assertRaises(BrokerConnectionError):
            resolve_connection({"broker_environment": "live"}, env)

    def test_all_paths_share_one_connection_and_no_redirects(self):
        for mode in ("sandbox", "live"):
            connection = Connection(mode, "ACCT", "TOKEN")
            for path in ("/markets/quotes", "/markets/options/chains", "/accounts/{account}/positions", "/accounts/{account}/balances", "/accounts/{account}/orders"):
                self.gateway.request(connection, "GET", path)
                call = self.transport.get.call_args
                self.assertEqual(call.args[0], connection.url + path.replace("{account}", "ACCT"))
                self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer TOKEN")
                self.assertFalse(call.kwargs["allow_redirects"])
                self.assertEqual(call.kwargs["timeout"], 10)

    def test_balance_mapping_missing_fields_not_zero(self):
        values = parse_balances(self.balances)
        self.assertEqual(values["total_account_value"], 500)
        self.assertEqual(values["options_buying_power"], 100)
        self.assertEqual(values["unrealized_pnl"], -10)
        self.assertIsNone(values["settled_cash"])
        self.assertIsNone(parse_balances({})["cash"])

    def test_cash_available_not_assumed_settled_or_options_power(self):
        values = parse_balances({"account_type": "cash", "cash": {"cash_available": 150, "unsettled_funds": 50}})
        self.assertEqual(values["cash_available"], 150)
        self.assertIsNone(values["settled_cash"])
        self.assertIsNone(values["options_buying_power"])

    def test_pdt_and_explicit_settled_cash(self):
        values = parse_balances({"account_type": "pdt", "settled_cash": 12, "pdt": {"option_buying_power": 35}})
        self.assertEqual(values["options_buying_power"], 35)
        self.assertEqual(values["settled_cash"], 12)

    def test_verified_paper_label_and_mask(self):
        result = self.gateway.account(self.connection)
        self.assertEqual(result["environment_label"], "SANDBOX — PAPER MONEY")
        self.assertEqual(result["masked_account"], "****3456")
        self.assertNotIn(self.connection.token, str(result))
        self.assertNotIn(self.connection.account, str(result))

    def test_url_alone_is_not_account_validation(self):
        self.transport.get.return_value = Mock(status_code=401)
        result = self.gateway.account(Connection("live", "ACCT", "TOKEN"))
        self.assertEqual(result["environment_label"], "UNKNOWN / CONNECTION ERROR")
        self.assertFalse(result["validated"])

    def test_identity_mismatch_is_not_validated(self):
        self.balances["account_number"] = "WRONG"
        self.assertFalse(self.gateway.account(self.connection)["validated"])

    def test_cache_and_errors_preserve_last_values(self):
        self.gateway.account(self.connection)
        self.now += 20
        self.gateway.account(self.connection)
        self.assertEqual(self.transport.get.call_count, 1)
        self.now += 41
        self.transport.get.return_value = Mock(status_code=429)
        result = self.gateway.account(self.connection)
        self.assertTrue(result["stale"])
        self.assertEqual(result["balances"]["cash"], 200)
        self.assertIn("429", result["error"])

    def test_errors_do_not_echo_transport_secrets(self):
        self.transport.get.side_effect = RuntimeError(self.connection.token)
        result = self.gateway.account(self.connection)
        self.assertNotIn(self.connection.token, str(result))
        self.assertTrue(result["stale"])

    def test_environment_caches_are_separate(self):
        self.gateway.account(self.connection)
        self.transport.get.return_value = Mock(status_code=401)
        result = self.gateway.account(Connection("live", self.connection.account, self.connection.token))
        self.assertEqual(result["balances"], {})

    def test_switch_refused_with_positions(self):
        responses = [Mock(status_code=200, json=lambda: {"balances": self.balances}),
                     Mock(status_code=200, json=lambda: {"positions": {"position": {"quantity": 1}}})]
        self.transport.get.side_effect = responses
        with self.assertRaisesRegex(BrokerConnectionError, "open broker positions"):
            self.gateway.assert_switch_safe(self.connection)

    def test_switch_refused_with_pending_orders(self):
        self.balances["pending_orders_count"] = 1
        with self.assertRaises(BrokerConnectionError):
            self.gateway.assert_switch_safe(self.connection)

    def test_switch_allowed_only_verified_empty_account(self):
        self.transport.get.side_effect = [Mock(status_code=200, json=lambda: {"balances": self.balances}),
                                         Mock(status_code=200, json=lambda: {"positions": "null"})]
        self.gateway.assert_switch_safe(self.connection)

    def test_account_endpoint_works_closed_without_market_requests(self):
        with patch.object(dashboard, "BROKER", self.gateway), patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "load_config", return_value={"bot_budget": 100}), \
             patch.object(dashboard, "get_market_quote", side_effect=AssertionError("No ticker polling")):
            response = dashboard.app.test_client().get("/api/account")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["account"]["bot_budget"], 100)
        self.assertEqual(response.json["account"]["market_data"]["status"], "STALE / UNVERIFIED")
        self.transport.post.assert_not_called()

    def test_production_delayed_quote_not_reported_fresh(self):
        connection = Connection("live", "VA123456", "TOKEN")
        now = datetime.now(dashboard.MARKET_TZ)
        self.gateway.clock = lambda: now.timestamp()
        self.gateway.observe_quote(connection, {"symbol": "SPY", "last": 500, "trade_date": (now - timedelta(seconds=900)).isoformat()})
        with patch.object(dashboard, "BROKER", self.gateway), patch.object(dashboard, "resolve_connection", return_value=connection), \
             patch.object(dashboard, "load_config", return_value={}):
            result = dashboard.connected_account_snapshot()
        self.assertEqual(result["environment_label"], "LIVE — REAL MONEY")
        self.assertEqual(result["market_data"]["status"], "STALE / UNVERIFIED")

    def test_order_routing_keeps_params_using_mock_transport(self):
        with patch.object(dashboard, "BROKER", self.gateway), patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "load_config", return_value={}):
            data = {"side": "buy_to_open", "quantity": "1", "type": "market"}
            dashboard.broker_request("POST", "/accounts/{account}/orders", data=data)
        self.assertEqual(self.transport.post.call_args.kwargs["data"], data)
        self.assertIn("sandbox.tradier.com/v1/accounts/VA123456/orders", self.transport.post.call_args.args[0])

    def test_switch_disables_bot_even_when_form_enabled_then_requires_validation(self):
        config = {"broker_environment": "sandbox", "bot_enabled": True, "strategy": {}, "entry_rules": {}, "scanner": {}}
        with patch.object(dashboard, "BROKER", self.gateway), patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "load_config", side_effect=lambda: copy.deepcopy(config)), \
             patch.object(self.gateway, "assert_switch_safe"), patch.object(dashboard, "clear_pending_entry"), \
             patch.object(dashboard, "save_config") as save, patch.dict(dashboard.BOT_STATE, {}, clear=False):
            response = dashboard.app.test_client().post("/save-settings", data={"mode": "live", "bot_enabled": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(save.call_args.args[0]["bot_enabled"])
        self.assertEqual(save.call_args.args[0]["broker_environment"], "live")

    def test_stale_auto_entry_cannot_cross_accounts(self):
        with patch.object(dashboard, "load_config", return_value={"bot_enabled": True, "broker_selection_version": 2}), \
             patch.object(dashboard, "add_bot_reason"), patch.object(dashboard, "submit_option_order") as order:
            result = dashboard.execute_entry_buy({"broker_selection_version": 1}, "BUY CALL", "call", {}, 1, 1, {})
        self.assertFalse(result)
        order.assert_not_called()

    def test_explicit_enable_requires_verified_account(self):
        config = {"broker_environment": "sandbox", "bot_enabled": False, "strategy": {}, "entry_rules": {}, "scanner": {}}
        for valid in (False, True):
            with self.subTest(valid=valid), patch.object(dashboard, "BROKER", self.gateway), \
                 patch.object(dashboard, "resolve_connection", return_value=self.connection), \
                 patch.object(dashboard, "load_config", side_effect=lambda: copy.deepcopy(config)), \
                 patch.object(self.gateway, "account", return_value={"validated": valid}), \
                 patch.object(dashboard, "save_config") as save:
                response = dashboard.app.test_client().post("/save-settings", data={"mode": "sandbox", "bot_enabled": "on"})
                self.assertEqual(response.status_code, 302 if valid else 400)
                self.assertEqual(save.call_count, 1 if valid else 0)

    def test_balance_failure_does_not_block_exit_order(self):
        self.transport.get.return_value = Mock(status_code=503)
        with patch.object(dashboard, "BROKER", self.gateway), patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "load_config", return_value={}):
            dashboard.broker_request("POST", "/accounts/{account}/orders", data={"side": "sell_to_close"})
        self.transport.get.assert_not_called()
        self.transport.post.assert_called_once()

    def test_bad_account_blocks_buy_before_transport_post(self):
        self.transport.get.return_value = Mock(status_code=401)
        with patch.object(dashboard, "BROKER", self.gateway), patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "load_config", return_value={}):
            with self.assertRaises(BrokerConnectionError):
                dashboard.broker_request("POST", "/accounts/{account}/orders", data={"side": "buy_to_open"})
        self.transport.post.assert_not_called()

    def test_incorrect_quote_and_missing_timestamps_are_not_fresh(self):
        self.gateway.observe_quote(self.connection, {"symbol": "SPY", "last": 500})
        self.gateway.quote_failed(self.connection, "Quote request failed")
        with patch.object(dashboard, "BROKER", self.gateway), patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "load_config", return_value={}):
            result = dashboard.connected_account_snapshot()
        self.assertEqual(result["market_data"]["status"], "CONNECTION ERROR")
        self.assertEqual(result["market_data"]["timestamp"], "")


class EntryFundsLimitTests(unittest.TestCase):
    """New bot entries must fit BOTH the bot budget and broker buying power."""

    def setUp(self):
        self.transport = Mock()
        self.gateway = BrokerGateway(self.transport, lambda: 1000)
        self.connection = Connection("live", "AC6180", "private-test-token")
        # Mirrors the live account: cash account, $50 cash available, $1,000 bot budget.
        self.balances = {"account_number": "AC6180", "account_type": "cash", "total_equity": 50,
                         "total_cash": 50, "market_value": 0, "open_pl": 0, "pending_orders_count": 0,
                         "cash": {"cash_available": 50, "unsettled_funds": 0}}
        self.transport.get.return_value = Mock(status_code=200, json=lambda: {"balances": self.balances})

    def check(self, ask, contracts=1, budget=1000.0, spent=0.0):
        config = {"bot_budget": budget}
        with patch.object(dashboard, "BROKER", self.gateway), \
             patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "sync_trade_limits_from_file", return_value=(0, spent)):
            return dashboard.entry_funds_check(config, {"ask": ask}, contracts, ask)

    def test_cash_account_buying_power_is_cash_available(self):
        self.assertEqual(parse_balances(self.balances)["entry_buying_power"], 50)

    def test_margin_uses_option_buying_power_and_unknown_is_none(self):
        self.assertEqual(parse_balances({"account_type": "margin", "margin": {"option_buying_power": 70}})["entry_buying_power"], 70)
        self.assertIsNone(parse_balances({"account_type": "margin", "margin": {}})["entry_buying_power"])

    def test_limit_is_lower_of_budget_and_buying_power(self):
        from broker_connection import entry_funds_limit
        self.assertEqual(entry_funds_limit(1000, 0, 50), (50, "broker buying power"))
        self.assertEqual(entry_funds_limit(1000, 980, 50), (20, "bot budget remaining today"))
        self.assertEqual(entry_funds_limit(100, 200, 50), (0.0, "bot budget remaining today"))
        self.assertEqual(entry_funds_limit(1000, 0, None)[0], None)

    def test_50_dollar_account_blocks_entry_over_buying_power_despite_1000_budget(self):
        ok, reason = self.check(ask=0.60)  # $60 > $50 buying power, well under $1,000 budget
        self.assertFalse(ok)
        self.assertIn("broker buying power", reason)

    def test_entry_within_both_limits_passes(self):
        self.assertEqual(self.check(ask=0.45), (True, ""))

    def test_budget_still_binds_when_buying_power_is_larger(self):
        self.balances["cash"]["cash_available"] = 5000
        ok, reason = self.check(ask=0.45, budget=100, spent=80)  # $45 > $20 budget left
        self.assertFalse(ok)
        self.assertIn("bot budget", reason)

    def test_unverified_or_unknown_balances_block_entry(self):
        self.transport.get.return_value = Mock(status_code=503)
        self.assertFalse(self.check(ask=0.10)[0])
        self.transport.get.return_value = Mock(status_code=200, json=lambda: {"balances": dict(self.balances, account_type="margin", cash=None)})
        self.gateway.accounts.clear()
        ok, reason = self.check(ask=0.10)
        self.assertFalse(ok)
        self.assertIn("unavailable", reason)

    def test_execute_entry_buy_never_submits_when_funds_insufficient(self):
        config = {"bot_enabled": True, "auto_entries_enabled": True, "bot_budget": 1000.0, "symbol": "SPY"}
        with patch.object(dashboard, "BROKER", self.gateway), \
             patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "load_config", return_value=config), \
             patch.object(dashboard, "option_market_is_open", return_value=True), \
             patch.object(dashboard, "log_direction_safety_result", return_value={"passed": True}), \
             patch.object(dashboard, "entry_execution_diagnostics", return_value={}), \
             patch.object(dashboard, "entry_quote_freshness", return_value=(True, "")), \
             patch.object(dashboard, "sync_trade_limits_from_file", return_value=(0, 0.0)), \
             patch.object(dashboard, "log_bot_audit"), \
             patch.object(dashboard, "submit_and_parse_option_order") as submit:
            result = dashboard.execute_entry_buy(config, "BUY CALL", "CALL", {"symbol": "SPY261106C00735000", "ask": 0.60}, 1, 0.60, {})
        self.assertFalse(result)
        submit.assert_not_called()
        self.transport.post.assert_not_called()

    def test_account_panel_shows_balances_separately_from_budget(self):
        with patch.object(dashboard, "BROKER", self.gateway), \
             patch.object(dashboard, "resolve_connection", return_value=self.connection), \
             patch.object(dashboard, "load_config", return_value={"bot_budget": 1000.0}):
            status = dashboard.connected_account_snapshot()
            html = dashboard.render_connected_account(status)
        self.assertEqual(status["environment_label"], "LIVE — REAL MONEY")
        self.assertEqual(status["masked_account"], "****6180")
        self.assertEqual(status["balances"]["total_account_value"], 50)
        self.assertEqual(status["bot_budget"], 1000.0)
        self.assertEqual(status["max_new_entry_cost"], 50)
        self.assertIn("Configured Bot Budget (not account funds)", html)
        self.assertNotIn("private-test-token", html)
        self.assertNotIn("AC6180", html.replace("****6180", ""))


class AutoEntriesSwitchTests(unittest.TestCase):
    """auto_entries_enabled blocks NEW entries only; bot_enabled keeps exits running."""

    def test_default_is_disabled_for_existing_configs(self):
        import inspect
        self.assertIn('config.setdefault("auto_entries_enabled", False)', inspect.getsource(dashboard.load_config))

    def test_execute_entry_buy_blocked_before_any_broker_call(self):
        config = {"bot_enabled": True, "auto_entries_enabled": False}
        broker = Mock()
        with patch.object(dashboard, "load_config", return_value=config), \
             patch.object(dashboard, "BROKER", broker), \
             patch.object(dashboard, "log_bot_audit"), \
             patch.object(dashboard, "submit_and_parse_option_order") as submit:
            self.assertFalse(dashboard.execute_entry_buy(config, "BUY CALL", "CALL", {"symbol": "X"}, 1, 0.1, {}))
        submit.assert_not_called()
        broker.request.assert_not_called()

    def test_try_surfer_entry_stops_before_selecting_contract(self):
        with patch.object(dashboard, "log_bot_audit"), \
             patch.object(dashboard, "select_entry_contract") as select, \
             patch.object(dashboard, "create_pending_entry") as pending, \
             patch.object(dashboard, "execute_entry_buy") as buy:
            dashboard.try_surfer_entry({"bot_enabled": True, "auto_entries_enabled": False}, [], {"decision": "BUY CALL"}, {}, {})
        select.assert_not_called(); pending.assert_not_called(); buy.assert_not_called()

    def test_active_pending_entry_is_cancelled_not_bought(self):
        pending = {"active": True, "decision": "BUY CALL", "direction": "CALL", "option_symbol": "X",
                   "contracts": 1, "market_context": {"decision": "BUY CALL"}}
        with patch.object(dashboard, "get_pending_entry", return_value=pending), \
             patch.object(dashboard, "scanner_market_is_open", return_value=True), \
             patch.object(dashboard, "set_pending_entry") as saved, \
             patch.object(dashboard, "record_shadow_gate_state_from_pending"), \
             patch.object(dashboard, "upsert_pending_history"), \
             patch.object(dashboard, "refresh_pending_time_remaining"), \
             patch.object(dashboard, "log_bot_audit"), \
             patch.object(dashboard, "execute_entry_buy") as buy:
            dashboard.process_pending_entry({"bot_enabled": True, "auto_entries_enabled": False})
        buy.assert_not_called()
        self.assertEqual(saved.call_args.args[0]["status"], "CANCELLED")
        self.assertEqual(saved.call_args.args[0]["reason"], "automatic entries disabled")

    def test_exit_paths_do_not_depend_on_entry_switch(self):
        import inspect
        for fn in (dashboard.try_surfer_exit, dashboard.fast_exit_poll, dashboard.surfer_bot_loop, dashboard.sell_all_positions):
            self.assertNotIn("auto_entries_enabled", inspect.getsource(fn), fn.__name__)

    def test_settings_checkbox_saves_and_switch_forces_off(self):
        config = {"broker_environment": "live", "bot_enabled": True, "strategy": {}, "entry_rules": {}, "scanner": {}}
        gateway = Mock(); gateway.account.return_value = {"validated": True}
        for form, expected in (({"mode": "live", "bot_enabled": "on"}, False),
                               ({"mode": "live", "bot_enabled": "on", "auto_entries_enabled": "on"}, True)):
            with self.subTest(form=form), patch.object(dashboard, "BROKER", gateway), \
                 patch.object(dashboard, "resolve_connection", return_value=Connection("live", "A1", "t")), \
                 patch.object(dashboard, "load_config", side_effect=lambda: copy.deepcopy(config)), \
                 patch.object(dashboard, "save_config") as save:
                self.assertEqual(dashboard.app.test_client().post("/save-settings", data=form).status_code, 302)
                self.assertIs(save.call_args.args[0]["auto_entries_enabled"], expected)
                self.assertTrue(save.call_args.args[0]["bot_enabled"])
