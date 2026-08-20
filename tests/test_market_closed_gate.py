import unittest
from unittest.mock import patch

import dashboard


class StopLoop(BaseException):
    pass


def surfer_config(bot_enabled=False):
    return {
        "symbol": "SPY",
        "strategy_mode": "SURFER",
        "bot_enabled": bot_enabled,
        "contracts": 1,
        "scanner": {"interval_seconds": 15},
        "strategy": {
            "exit_poll_interval_ms": 1000,
            "hard_stop_percent": 20,
            "trailing_stop_percent": 15,
        },
        "entry_rules": {
            "minimum_signals": 1,
            "allow_calls": True,
            "allow_puts": True,
            "max_trades_per_day": 10,
        },
    }


def reset_market_session_state(value=None):
    with dashboard.BOT_LOCK:
        dashboard.BOT_STATE["market_session_open"] = value
        dashboard.BOT_STATE["market_session_status"] = "UNKNOWN"
        dashboard.BOT_STATE["market_session_last_transition"] = ""
        dashboard.BOT_STATE["running"] = False


class MarketClosedGateTest(unittest.TestCase):
    def tearDown(self):
        reset_market_session_state(None)

    def test_closed_market_skips_ticker_fetch_in_bot_tick(self):
        reset_market_session_state(True)
        with patch.object(dashboard, "option_market_is_open", return_value=False), \
            patch.object(dashboard, "load_config", return_value=surfer_config()), \
            patch.object(dashboard, "sync_trade_limits_from_file"), \
            patch.object(dashboard, "get_market_quote") as quote_mock, \
            patch.object(dashboard, "select_atm_contract") as option_mock, \
            patch.object(dashboard, "evaluate_tournament_decisions") as tournament_mock:
            dashboard.surfer_bot_tick()

        quote_mock.assert_not_called()
        option_mock.assert_not_called()
        tournament_mock.assert_not_called()

    def test_closed_market_skips_option_chain_fetch(self):
        reset_market_session_state(True)
        with patch.object(dashboard, "option_market_is_open", return_value=False), \
            patch.object(dashboard.requests, "get") as request_mock:
            self.assertEqual(dashboard.get_option_chain("SPY", "2026-08-19"), [])

        request_mock.assert_not_called()

    def test_closed_market_skips_tournament_evaluation_and_momentum(self):
        reset_market_session_state(True)
        signal = {"bullish_score": 5, "bearish_score": 0, "confidence": 5, "dominance_percent": 100}
        with patch.object(dashboard, "option_market_is_open", return_value=False), \
            patch.object(dashboard, "evaluate_all_profiles") as evaluate_mock, \
            patch.object(dashboard, "apply_tournament_momentum") as momentum_mock:
            result = dashboard.evaluate_tournament_decisions(surfer_config(), signal)

        self.assertEqual(result, (None, {}))
        evaluate_mock.assert_not_called()
        momentum_mock.assert_not_called()

    def test_repeated_closed_checks_do_not_spam_logs(self):
        reset_market_session_state(True)
        with patch.object(dashboard, "option_market_is_open", return_value=False), \
            patch("builtins.print") as print_mock:
            dashboard.scanner_market_is_open()
            dashboard.scanner_market_is_open()
            dashboard.scanner_market_is_open()

        print_mock.assert_called_once_with("Market closed — scanner sleeping")

    def test_open_to_closed_logs_once(self):
        reset_market_session_state(True)
        with patch.object(dashboard, "option_market_is_open", return_value=False), \
            patch("builtins.print") as print_mock:
            dashboard.scanner_market_is_open()
            dashboard.scanner_market_is_open()

        print_mock.assert_called_once_with("Market closed — scanner sleeping")

    def test_closed_to_open_logs_once(self):
        reset_market_session_state(False)
        with patch.object(dashboard, "option_market_is_open", return_value=True), \
            patch("builtins.print") as print_mock:
            dashboard.scanner_market_is_open()
            dashboard.scanner_market_is_open()

        print_mock.assert_called_once_with("Market open — scanner resumed")

    def test_scanner_loop_sleeps_300_seconds_while_closed(self):
        reset_market_session_state(True)

        def stop_after_sleep(seconds):
            self.assertEqual(seconds, dashboard.MARKET_CLOSED_SLEEP_SECONDS)
            raise StopLoop()

        with patch.object(dashboard, "option_market_is_open", return_value=False), \
            patch.object(dashboard, "load_config", return_value=surfer_config()), \
            patch.object(dashboard, "get_position") as position_mock, \
            patch.object(dashboard, "get_market_quote") as quote_mock, \
            patch.object(dashboard.time, "sleep", side_effect=stop_after_sleep):
            with self.assertRaises(StopLoop):
                dashboard.surfer_bot_loop()

        position_mock.assert_not_called()
        quote_mock.assert_not_called()

    def test_scanner_resumes_normally_after_reopen(self):
        reset_market_session_state(False)
        signal = {
            "price": 500,
            "bullish_score": 0,
            "bearish_score": 0,
            "confidence": 0,
            "dominance_percent": 0,
            "current_signal": "NONE",
            "market_state": "NEUTRAL",
            "decision": "DO NOTHING",
            "reasons": [],
        }
        with patch.object(dashboard, "option_market_is_open", return_value=True), \
            patch.object(dashboard, "load_config", return_value=surfer_config()), \
            patch.object(dashboard, "sync_trade_limits_from_file"), \
            patch.object(dashboard, "get_market_quote", return_value={"last": 500, "volume": 100}) as quote_mock, \
            patch.object(dashboard, "select_atm_contract", return_value=None), \
            patch.object(dashboard, "get_position", return_value=None), \
            patch.object(dashboard, "calculate_surfer_signal", return_value=signal), \
            patch.object(dashboard, "evaluate_tournament_decisions") as tournament_mock, \
            patch.object(dashboard, "log_bot_audit"):
            dashboard.surfer_bot_tick()

        quote_mock.assert_called()
        tournament_mock.assert_called_once()

    def test_flask_endpoint_remains_available_while_closed(self):
        reset_market_session_state(True)
        client = dashboard.app.test_client()
        with patch.object(dashboard, "option_market_is_open", return_value=False), \
            patch.object(dashboard, "get_market_quote") as quote_mock:
            response = client.get("/api/quote?symbol=SPY")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "MARKET_CLOSED")
        quote_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
