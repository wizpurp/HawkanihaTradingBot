import os
import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import patch

from tournament.profiles import PROFILE_ORDER
from tournament.trades import (
    append_tournament_trade,
    create_synthetic_tournament_trade,
    get_tournament_trade,
    list_tournament_trades,
    load_tournament_trades,
    save_tournament_trades,
    update_tournament_trade,
)


class TournamentTradesTest(unittest.TestCase):
    def temp_path(self, directory):
        return os.path.join(directory, "tournament_trades.json")

    def trade(self, profile_id="BOT_A_BASELINE", direction="CALL", entry_price=1.0, option_symbol=None):
        return create_synthetic_tournament_trade(
            profile_id=profile_id,
            direction=direction,
            entry_price=entry_price,
            option_symbol=option_symbol or f"SPYTEST{profile_id[-1]}{direction}",
        )

    def test_four_valid_profile_ids_are_accepted(self):
        for profile_id in PROFILE_ORDER:
            self.assertEqual(self.trade(profile_id=profile_id).profile_id, profile_id)

    def test_invalid_profile_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.trade(profile_id="BAD_PROFILE")

    def test_duplicate_trade_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            trade = self.trade()
            append_tournament_trade(trade, path)
            with self.assertRaises(ValueError):
                append_tournament_trade(trade, path)

    def test_open_trade_saves_and_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            trade = self.trade()
            append_tournament_trade(trade, path)
            loaded = get_tournament_trade(trade.trade_id, path)

        self.assertEqual(loaded.trade_id, trade.trade_id)
        self.assertEqual(loaded.status, "OPEN")

    def test_closed_trade_saves_with_correct_pnl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            trade = self.trade(entry_price=1.00)
            append_tournament_trade(trade, path)
            updated = update_tournament_trade(
                trade.trade_id,
                {
                    "status": "CLOSED",
                    "exit_time": trade.entry_time,
                    "exit_epoch": trade.entry_epoch + 60,
                    "exit_price": 1.20,
                    "exit_price_source": "SYNTHETIC",
                    "exit_value": 120.0,
                    "exit_reason": "TEST_CLOSE",
                    "pnl_dollars": 20.0,
                    "pnl_percent": 20.0,
                },
                path,
            )

        self.assertEqual(updated.status, "CLOSED")
        self.assertEqual(updated.pnl_dollars, 20.0)
        self.assertEqual(updated.pnl_percent, 20.0)

    def test_corrupt_json_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{bad json")
            self.assertEqual(load_tournament_trades(path), [])

    def test_missing_file_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_tournament_trades(self.temp_path(directory)), [])

    def test_newest_trades_are_listed_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            old_trade = self.trade(option_symbol="OLD")
            new_trade = self.trade(option_symbol="NEW")
            old_trade.entry_epoch = 1
            new_trade.entry_epoch = 2
            save_tournament_trades([old_trade, new_trade], path)

            rows = list_tournament_trades(path=path)

        self.assertEqual(rows[0].option_symbol, "NEW")

    def test_profile_filter_works(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            save_tournament_trades([
                self.trade(profile_id="BOT_A_BASELINE"),
                self.trade(profile_id="BOT_B_MOMENTUM"),
            ], path)
            rows = list_tournament_trades(profile_id="BOT_B_MOMENTUM", path=path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].profile_id, "BOT_B_MOMENTUM")

    def test_status_filter_works(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            open_trade = self.trade(option_symbol="OPEN")
            closed_trade = self.trade(option_symbol="CLOSED")
            closed_payload = asdict(closed_trade)
            closed_payload.update({
                "status": "CLOSED",
                "exit_time": closed_trade.entry_time,
                "exit_epoch": closed_trade.entry_epoch + 1,
                "exit_price": closed_trade.entry_price,
                "exit_price_source": "SYNTHETIC",
                "exit_value": closed_trade.entry_cost,
                "exit_reason": "TEST",
                "pnl_dollars": 0.0,
                "pnl_percent": 0.0,
            })
            save_tournament_trades([open_trade, closed_payload], path)
            rows = list_tournament_trades(status="CLOSED", path=path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "CLOSED")

    def test_limit_works(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            save_tournament_trades([self.trade(option_symbol="ONE"), self.trade(option_symbol="TWO")], path)
            self.assertEqual(len(list_tournament_trades(limit=1, path=path)), 1)

    def test_returned_trades_are_deep_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            trade = self.trade()
            append_tournament_trade(trade, path)
            rows = list_tournament_trades(path=path)
            rows[0].profile_display_name = "MUTATED"
            rows_again = list_tournament_trades(path=path)

        self.assertNotEqual(rows_again[0].profile_display_name, "MUTATED")

    def test_api_returns_profile_labels(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            append_tournament_trade(self.trade(profile_id="BOT_B_MOMENTUM"), path)
            with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", path):
                response = dashboard.app.test_client().get("/api/tournament/trades")

        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["trades"][0]["profile_display_name"], "Bot B Momentum")
        self.assertEqual(data["trades"][0]["profile_id"], "BOT_B_MOMENTUM")

    def test_test_record_endpoint_creates_synthetic_record_only(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", path):
                response = dashboard.app.test_client().post(
                    "/api/tournament/trades/test-record",
                    json={"profile_id": "BOT_A_BASELINE", "direction": "CALL", "entry_price": 1.0},
                )

        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["trade"]["signal"], "TEST_RECORD")
        self.assertEqual(data["trade"]["status"], "OPEN")

    def test_test_record_endpoint_never_calls_tradier(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", path), patch.object(dashboard.requests, "post") as post_mock, patch.object(dashboard.requests, "get") as get_mock:
                response = dashboard.app.test_client().post(
                    "/api/tournament/trades/test-record",
                    json={"profile_id": "BOT_A_BASELINE", "direction": "CALL", "entry_price": 1.0},
                )

        self.assertEqual(response.status_code, 200)
        post_mock.assert_not_called()
        get_mock.assert_not_called()

    def test_tournament_trades_do_not_appear_in_original_bot_trade_history(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            trade = self.trade(option_symbol="TOURNAMENT_ONLY")
            append_tournament_trade(trade, path)
            with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", path):
                original_rows = dashboard.get_recent_trades(limit=None)

        self.assertTrue(all(row.get("Symbol") != "TOURNAMENT_ONLY" for row in original_rows))

    def test_dashboard_table_renders_profile_name_and_profile_id(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            append_tournament_trade(self.trade(profile_id="BOT_C_TWO_CANDLE_OR"), path)
            with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", path), self.dashboard_safe_patches(dashboard):
                response = dashboard.app.test_client().get("/")

        html = response.get_data(as_text=True)
        self.assertIn("Bot C Two-Candle OR", html)
        self.assertIn("BOT_C_TWO_CANDLE_OR", html)

    def test_open_trades_render_blank_exit_fields(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            append_tournament_trade(self.trade(option_symbol="OPEN_RENDER"), path)
            with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", path), self.dashboard_safe_patches(dashboard):
                html = dashboard.app.test_client().get("/").get_data(as_text=True)

        self.assertIn("<td>OPEN_RENDER</td>", html)
        self.assertIn("<td>OPEN</td>", html)
        self.assertIn("<td></td>\n<td>OPEN</td>", html)

    def test_closed_trades_render_pnl_correctly(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            path = self.temp_path(directory)
            trade = self.trade(entry_price=1.0, option_symbol="CLOSED_RENDER")
            append_tournament_trade(trade, path)
            update_tournament_trade(
                trade.trade_id,
                {
                    "status": "CLOSED",
                    "exit_time": trade.entry_time,
                    "exit_epoch": trade.entry_epoch + 1,
                    "exit_price": 1.25,
                    "exit_price_source": "SYNTHETIC",
                    "exit_value": 125.0,
                    "exit_reason": "TEST_WIN",
                    "pnl_dollars": 25.0,
                    "pnl_percent": 25.0,
                },
                path,
            )
            with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", path), self.dashboard_safe_patches(dashboard):
                html = dashboard.app.test_client().get("/").get_data(as_text=True)

        self.assertIn("+$25.00", html)
        self.assertIn("+25.00%", html)

    def dashboard_safe_patches(self, dashboard):
        summary = {
            "starting_balance": 1000,
            "current_balance": 1000,
            "net_profit": 0,
            "today_budget": 100,
            "budget_remaining": 100,
            "spent_today": 0,
            "win_rate": 0,
            "today_pnl": 0,
            "total_pnl": 0,
            "overall_grade": "N/A",
            "number_of_trades": 0,
            "trade_list": [],
        }
        return patch.multiple(
            dashboard,
            get_market_quote=lambda symbol: {"symbol": symbol, "last": 500, "bid": 499.99, "ask": 500.01, "volume": 1},
            get_position=lambda: [],
            select_atm_contract=lambda symbol, option_type: None,
            get_trade_performance=lambda: {"BOT": dict(summary), "HUMAN": dict(summary)},
            load_bot_audit_rows=lambda limit: [],
            pending_entry_history_snapshot=lambda limit=None: [],
            get_trade_history_trades=lambda limit=None: [],
        )


if __name__ == "__main__":
    unittest.main()
