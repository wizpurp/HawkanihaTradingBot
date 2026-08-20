import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from tournament.execution import try_open_virtual_position
from tournament.exits import (
    EXIT_HARD_STOP,
    EXIT_PROFIT_FLOOR_STOP,
    EXIT_TRAILING_STOP,
    close_virtual_position,
    evaluate_virtual_exit,
    hard_stop_price,
    process_virtual_exits,
    profit_floor_price,
    profit_floor_activated,
    trailing_stop_price,
    update_virtual_position_price,
)
from tournament.models import ProfileDecision
from tournament.profiles import build_tournament_profiles
from tournament.snapshot import MarketSnapshot
from tournament.state import create_all_initial_states, load_tournament_state
from tournament.trades import list_tournament_trades


def runtime_config(contracts=1, locked_profit_dollars=5.0):
    return {
        "symbol": "SPY",
        "bot_enabled": True,
        "minimum_confidence": 1,
        "minimum_dominance_percent": 50,
        "maximum_position_cost_dollars": 1000.0,
        "contracts": contracts,
        "strategy": {
            "hard_stop_percent": 20,
            "trailing_stop_percent": 10,
            "enable_profit_floor_trailing_stop": True,
            "locked_profit_dollars": locked_profit_dollars,
        },
        "entry_rules": {
            "minimum_signals": 1,
            "allow_calls": True,
            "allow_puts": True,
            "cooldown_minutes": 0,
            "max_trades_per_day": 10,
        },
    }


def accepted_decision(profile_id="BOT_A_BASELINE"):
    return ProfileDecision(
        profile_id=profile_id,
        timestamp="2026-07-28T09:35:00-04:00",
        accepted=True,
        direction="CALL",
        status="ACCEPTED",
        rejection_reason=None,
        bullish_score=5,
        bearish_score=0,
        confidence=5,
        dominance_percent=100.0,
        momentum_required=False,
        momentum_status="NOT_REQUIRED",
        or_confirmation_required=False,
        or_confirmation_status="NOT_REQUIRED",
    )


def snapshot(option_symbol="SPYEXITCALL", ask=1.0):
    return MarketSnapshot(
        timestamp="2026-07-28T09:35:00-04:00",
        symbol="SPY",
        current_price=500.0,
        bullish_score=5,
        bearish_score=0,
        confidence=5,
        dominance_percent=100.0,
        market_state="BULLISH",
        suggested_direction="CALL",
        signal="CALL",
        ema_bullish=True,
        ema_bearish=False,
        macd_bullish=True,
        macd_bearish=False,
        above_vwap=True,
        below_vwap=False,
        volume_confirmed=True,
        opening_range_ready=True,
        opening_range_high=499.0,
        opening_range_low=497.0,
        completed_closes=(500.0, 501.0),
        option_symbol=option_symbol,
        option_bid=ask - 0.05,
        option_ask=ask,
        option_last=ask,
        option_midpoint=ask,
        option_quote_timestamp="2026-07-28T09:35:00-04:00",
    )


class TournamentVirtualExitsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.trades_path = os.path.join(self.tempdir.name, "tournament_trades.json")
        self.trade_path_patch = patch("tournament.execution.TOURNAMENT_TRADES_FILE", self.trades_path)
        self.exit_trade_path_patch = patch("tournament.exits.TOURNAMENT_TRADES_FILE", self.trades_path)
        self.trade_path_patch.start()
        self.exit_trade_path_patch.start()
        self.profiles = build_tournament_profiles(runtime_config(), settings_path=os.path.join(self.tempdir.name, "missing_profiles.json"))
        self.states = create_all_initial_states(self.profiles)

    def tearDown(self):
        self.exit_trade_path_patch.stop()
        self.trade_path_patch.stop()
        self.tempdir.cleanup()

    def open_position(self, profile_id="BOT_A_BASELINE", ask=1.0, symbol="SPYEXITCALL", contracts=None, locked_profit_dollars=None):
        if contracts is not None or locked_profit_dollars is not None:
            config = runtime_config(
                contracts=contracts or 1,
                locked_profit_dollars=locked_profit_dollars if locked_profit_dollars is not None else 5.0,
            )
            self.profiles = build_tournament_profiles(config, settings_path=os.path.join(self.tempdir.name, "missing_profiles_2.json"))
            self.states = create_all_initial_states(self.profiles)
        trade = try_open_virtual_position(
            self.profiles[profile_id],
            self.states[profile_id],
            accepted_decision(profile_id),
            snapshot(symbol, ask),
            1000.0,
        )
        return trade, self.states[profile_id].virtual_position

    def test_bid_updates_current_price(self):
        _, position = self.open_position()
        updated = update_virtual_position_price(position, 1.10, 1001.0)
        self.assertEqual(updated.current_price, 1.10)
        self.assertEqual(updated.current_price_source, "BID")

    def test_position_pnl_is_correct(self):
        _, position = self.open_position()
        updated = update_virtual_position_price(position, 1.25, 1001.0)
        self.assertEqual(updated.unrealized_pnl_dollars, 25.0)
        self.assertEqual(updated.unrealized_pnl_percent, 25.0)

    def test_peak_price_updates_upward_only(self):
        _, position = self.open_position()
        high = update_virtual_position_price(position, 1.20, 1001.0)
        lower = update_virtual_position_price(high, 1.10, 1002.0)
        self.assertEqual(lower.peak_price, 1.20)

    def test_lowest_price_updates_downward_only(self):
        _, position = self.open_position()
        low = update_virtual_position_price(position, 0.90, 1001.0)
        higher = update_virtual_position_price(low, 0.95, 1002.0)
        self.assertEqual(higher.lowest_price, 0.90)

    def test_hard_stop_closes_correctly(self):
        _, position = self.open_position()
        self.assertEqual(evaluate_virtual_exit(position, 0.79, 1001.0), EXIT_HARD_STOP)

    def test_hard_stop_exact_boundary_closes(self):
        _, position = self.open_position()
        self.assertEqual(evaluate_virtual_exit(position, hard_stop_price(position), 1001.0), EXIT_HARD_STOP)

    def test_standard_trailing_stop_activates_only_above_entry(self):
        _, position = self.open_position()
        self.assertIsNone(evaluate_virtual_exit(position, 0.91, 1001.0))

    def test_standard_trailing_stop_closes_after_retracement(self):
        _, position = self.open_position(locked_profit_dollars=100.0)
        position = update_virtual_position_price(position, 1.20, 1001.0)
        self.assertEqual(evaluate_virtual_exit(position, 1.08, 1002.0), EXIT_TRAILING_STOP)

    def test_trailing_stop_does_not_trigger_before_profit(self):
        _, position = self.open_position()
        self.assertIsNone(evaluate_virtual_exit(position, 0.95, 1001.0))

    def test_profit_floor_activates_at_correct_price(self):
        _, position = self.open_position(locked_profit_dollars=5.0)
        self.assertEqual(profit_floor_price(position), 1.05)
        position = update_virtual_position_price(position, 1.05, 1001.0)
        self.assertTrue(profit_floor_activated(position))

    def test_profit_floor_calculation_for_one_contract(self):
        _, position = self.open_position(contracts=1, locked_profit_dollars=5.0)
        self.assertEqual(profit_floor_price(position), 1.05)

    def test_profit_floor_calculation_for_multiple_contracts(self):
        _, position = self.open_position(contracts=2, locked_profit_dollars=5.0)
        self.assertEqual(profit_floor_price(position), 1.025)

    def test_ui_locked_profit_10_creates_correct_one_contract_floor(self):
        _, position = self.open_position(contracts=1, ask=1.20, locked_profit_dollars=10.0)
        self.assertEqual(profit_floor_price(position), 1.30)

    def test_profit_floor_prevents_normal_retracement_below_locked_profit_when_possible(self):
        _, position = self.open_position(locked_profit_dollars=5.0)
        position = update_virtual_position_price(position, 1.06, 1001.0)
        self.assertEqual(evaluate_virtual_exit(position, 1.05, 1002.0), EXIT_PROFIT_FLOOR_STOP)

    def test_market_gap_below_floor_exits_at_actual_bid(self):
        _, position = self.open_position(locked_profit_dollars=5.0)
        position = update_virtual_position_price(position, 1.06, 1001.0)
        state = self.states["BOT_A_BASELINE"]
        state.virtual_position = position
        trade = close_virtual_position(self.profiles["BOT_A_BASELINE"], state, 0.90, EXIT_PROFIT_FLOOR_STOP, 1002.0)
        self.assertEqual(trade.exit_price, 0.90)

    def test_exit_precedence_is_deterministic(self):
        _, position = self.open_position(locked_profit_dollars=0.0)
        position = update_virtual_position_price(position, 1.20, 1001.0)
        self.assertEqual(evaluate_virtual_exit(position, 0.70, 1002.0), EXIT_HARD_STOP)

    def test_closed_trade_pnl_is_correct(self):
        _, position = self.open_position()
        state = self.states["BOT_A_BASELINE"]
        state.virtual_position = position
        trade = close_virtual_position(self.profiles["BOT_A_BASELINE"], state, 1.25, EXIT_TRAILING_STOP, 1001.0)
        self.assertEqual(trade.pnl_dollars, 25.0)
        self.assertEqual(trade.pnl_percent, 25.0)

    def test_closed_trade_updates_existing_record_instead_of_appending_duplicate(self):
        trade, position = self.open_position()
        self.states["BOT_A_BASELINE"].virtual_position = position
        close_virtual_position(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], 1.25, EXIT_TRAILING_STOP, 1001.0)
        rows = list_tournament_trades(path=self.trades_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].trade_id, trade.trade_id)
        self.assertEqual(rows[0].status, "CLOSED")

    def test_state_virtual_position_clears_after_close(self):
        _, position = self.open_position()
        state = self.states["BOT_A_BASELINE"]
        state.virtual_position = position
        close_virtual_position(self.profiles["BOT_A_BASELINE"], state, 1.25, EXIT_TRAILING_STOP, 1001.0)
        self.assertIsNone(state.virtual_position)

    def test_other_profile_position_remains_open(self):
        self.open_position("BOT_A_BASELINE", symbol="ACALL")
        self.open_position("BOT_B_MOMENTUM", symbol="BCALL")
        close_virtual_position(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], 1.25, EXIT_TRAILING_STOP, 1001.0)
        self.assertIsNone(self.states["BOT_A_BASELINE"].virtual_position)
        self.assertIsNotNone(self.states["BOT_B_MOMENTUM"].virtual_position)

    def test_shared_contract_quote_is_fetched_once_for_multiple_profiles(self):
        import dashboard

        self.open_position("BOT_A_BASELINE", symbol="SAMECALL")
        self.open_position("BOT_B_MOMENTUM", symbol="SAMECALL")
        dashboard.TOURNAMENT_RUNTIME_STATES = self.states
        dashboard.TOURNAMENT_RUNTIME_STATES_LOADED = True
        with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", self.trades_path), patch.object(dashboard, "TOURNAMENT_STATE_FILE", os.path.join(self.tempdir.name, "state.json")), patch.object(dashboard, "option_market_is_open", return_value=True), patch.object(dashboard, "get_market_quote", return_value={"bid": 1.01}) as quote_mock:
            dashboard.process_tournament_virtual_exit_poll()
        self.assertEqual(quote_mock.call_count, 1)

    def test_invalid_bid_does_not_close(self):
        _, position = self.open_position()
        self.assertIsNone(evaluate_virtual_exit(position, 0, 1001.0))

    def test_missing_quote_does_not_close(self):
        self.open_position()
        closed = process_virtual_exits(self.profiles, self.states, {}, 1001.0)
        self.assertEqual(closed, [])
        self.assertIsNotNone(self.states["BOT_A_BASELINE"].virtual_position)

    def test_concurrent_close_attempts_create_only_one_closed_trade(self):
        _, position = self.open_position()
        state = self.states["BOT_A_BASELINE"]
        state.virtual_position = position
        close_virtual_position(self.profiles["BOT_A_BASELINE"], state, 1.25, EXIT_TRAILING_STOP, 1001.0)
        with self.assertRaises(ValueError):
            close_virtual_position(self.profiles["BOT_A_BASELINE"], state, 1.25, EXIT_TRAILING_STOP, 1001.0)
        self.assertEqual(len(list_tournament_trades(path=self.trades_path)), 1)

    def test_old_state_remains_loadable(self):
        state_path = os.path.join(self.tempdir.name, "old_state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            handle.write('{"BOT_A_BASELINE": {"profile_id": "BOT_A_BASELINE", "virtual_balance": 1000, "starting_balance": 1000}}')
        loaded = load_tournament_state(state_path, self.profiles)
        self.assertIsNone(loaded["BOT_A_BASELINE"].virtual_position)

    def test_test_price_endpoint_never_calls_tradier(self):
        import dashboard

        self.open_position()
        dashboard.TOURNAMENT_RUNTIME_STATES = self.states
        dashboard.TOURNAMENT_RUNTIME_STATES_LOADED = True
        with patch.object(dashboard, "TOURNAMENT_TRADES_FILE", self.trades_path), patch.object(dashboard, "TOURNAMENT_STATE_FILE", os.path.join(self.tempdir.name, "state.json")), patch.object(dashboard.requests, "get") as get_mock, patch.object(dashboard.requests, "post") as post_mock:
            response = dashboard.app.test_client().post(
                "/api/tournament/positions/test-price",
                json={"profile_id": "BOT_A_BASELINE", "bid_price": 1.01},
            )
        self.assertEqual(response.status_code, 200)
        get_mock.assert_not_called()
        post_mock.assert_not_called()

    def test_original_bot_trade_history_remains_unchanged(self):
        import dashboard

        self.open_position(symbol="TOURNAMENT_EXIT_ONLY")
        rows = dashboard.get_recent_trades(limit=None)
        self.assertTrue(all(row.get("Symbol") != "TOURNAMENT_EXIT_ONLY" for row in rows))

    def test_all_tournament_trades_retain_profile_labels(self):
        self.open_position("BOT_D_COMBINED")
        rows = list_tournament_trades(path=self.trades_path)
        self.assertEqual(rows[0].profile_id, "BOT_D_COMBINED")
        self.assertEqual(rows[0].profile_display_name, "Bot D Combined")


if __name__ == "__main__":
    unittest.main()
