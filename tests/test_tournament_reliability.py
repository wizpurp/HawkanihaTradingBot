import json
import os
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tournament.execution import try_open_virtual_position
from tournament.exits import process_virtual_exits
from tournament.models import ProfileDecision
from tournament.profiles import build_tournament_profiles
from tournament.recovery import (
    EXIT_RECOVERY_DUPLICATE_OPEN_POSITION,
    EXIT_RECOVERY_INVALID_STATE,
    REJECT_INVALID_QUOTE_TIMESTAMP,
    REJECT_MARKET_CLOSED,
    REJECT_QUOTE_STALE,
    RECOVERY_SYNTHETIC_PROOF_CLEARED,
    TEST_TYPE_PIPELINE_PROOF,
    apply_daily_reset,
    quote_is_fresh,
    reconcile_tournament_state,
    tournament_market_is_open,
)
from tournament.snapshot import MarketSnapshot
from tournament.state import create_all_initial_states, load_tournament_state, save_tournament_state
from tournament.trades import append_tournament_trade, list_tournament_trades, load_tournament_trades, save_tournament_trades


MARKET_TZ = ZoneInfo("America/New_York")


def epoch_at(year=2026, month=7, day=28, hour=10, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MARKET_TZ).timestamp()


def runtime_config():
    return {
        "symbol": "SPY",
        "bot_enabled": True,
        "minimum_confidence": 1,
        "minimum_dominance_percent": 50,
        "maximum_position_cost_dollars": 1000.0,
        "contracts": 1,
        "max_quote_age_seconds": 10,
        "strategy": {
            "hard_stop_percent": 20,
            "trailing_stop_percent": 10,
            "enable_profit_floor_trailing_stop": True,
            "locked_profit_dollars": 5.0,
            "exit_poll_interval_ms": 500,
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
        timestamp="2026-07-28T10:00:00-04:00",
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


def snapshot(symbol="SPYRELCALL", ask=1.0, quote_time="2026-07-28T10:00:00-04:00", direction="CALL"):
    return MarketSnapshot(
        timestamp=quote_time,
        symbol="SPY",
        current_price=500.0,
        bullish_score=5 if direction == "CALL" else 0,
        bearish_score=5 if direction == "PUT" else 0,
        confidence=5,
        dominance_percent=100.0,
        market_state="BULLISH" if direction == "CALL" else "BEARISH",
        suggested_direction=direction,
        signal=direction,
        ema_bullish=direction == "CALL",
        ema_bearish=direction == "PUT",
        macd_bullish=direction == "CALL",
        macd_bearish=direction == "PUT",
        above_vwap=direction == "CALL",
        below_vwap=direction == "PUT",
        volume_confirmed=True,
        opening_range_ready=True,
        opening_range_high=499.0,
        opening_range_low=497.0,
        completed_closes=(500.0, 501.0),
        option_symbol=symbol,
        option_bid=ask - 0.05,
        option_ask=ask,
        option_last=ask,
        option_midpoint=ask,
        call_option_symbol=symbol if direction == "CALL" else None,
        call_option_bid=ask - 0.05 if direction == "CALL" else None,
        call_option_ask=ask if direction == "CALL" else None,
        call_option_last=ask if direction == "CALL" else None,
        call_option_midpoint=ask if direction == "CALL" else None,
        put_option_symbol=symbol if direction == "PUT" else None,
        put_option_bid=ask - 0.05 if direction == "PUT" else None,
        put_option_ask=ask if direction == "PUT" else None,
        put_option_last=ask if direction == "PUT" else None,
        put_option_midpoint=ask if direction == "PUT" else None,
        option_quote_timestamp=quote_time,
    )


class TournamentReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.trades_path = os.path.join(self.tempdir.name, "tournament_trades.json")
        self.state_path = os.path.join(self.tempdir.name, "tournament_state.json")
        self.execution_patch = patch("tournament.execution.TOURNAMENT_TRADES_FILE", self.trades_path)
        self.exit_patch = patch("tournament.exits.TOURNAMENT_TRADES_FILE", self.trades_path)
        self.execution_patch.start()
        self.exit_patch.start()
        self.profiles = build_tournament_profiles(runtime_config(), settings_path=os.path.join(self.tempdir.name, "missing_profiles.json"))
        self.states = create_all_initial_states(self.profiles)

    def tearDown(self):
        self.exit_patch.stop()
        self.execution_patch.stop()
        self.tempdir.cleanup()

    def open_position(self, profile_id="BOT_A_BASELINE", option_symbol="SPYRELCALL", now_epoch=None):
        now_epoch = epoch_at() if now_epoch is None else now_epoch
        trade = try_open_virtual_position(
            self.profiles[profile_id],
            self.states[profile_id],
            accepted_decision(profile_id),
            snapshot(option_symbol),
            now_epoch,
        )
        return trade, self.states[profile_id].virtual_position

    def test_valid_open_position_resumes_after_restart(self):
        trade, _ = self.open_position()
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, list_tournament_trades(path=self.trades_path))
        self.assertEqual(states["BOT_A_BASELINE"].virtual_position.trade_id, trade.trade_id)
        self.assertEqual(summary["statuses"]["BOT_A_BASELINE"], "POSITION_RESUMED")

    def test_missing_open_trade_is_reconstructed(self):
        _, position = self.open_position()
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [])
        self.assertEqual(trades[0].trade_id, position.trade_id)
        self.assertEqual(summary["statuses"]["BOT_A_BASELINE"], "TRADE_RECONSTRUCTED")

    def test_missing_position_is_reconstructed_from_open_trade(self):
        trade, _ = self.open_position()
        self.states["BOT_A_BASELINE"].virtual_position = None
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [trade])
        self.assertEqual(states["BOT_A_BASELINE"].virtual_position.trade_id, trade.trade_id)
        self.assertEqual(summary["statuses"]["BOT_A_BASELINE"], "POSITION_RECONSTRUCTED")

    def test_closed_trade_clears_stale_position(self):
        trade, _ = self.open_position()
        closed = asdict(trade)
        closed.update({"status": "CLOSED", "exit_time": "2026-07-28T10:01:00-04:00", "exit_epoch": epoch_at(minute=1), "exit_price": 1.1, "exit_price_source": "BID", "exit_value": 110, "pnl_dollars": 10, "pnl_percent": 10})
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [type(trade)(**closed)])
        self.assertIsNone(states["BOT_A_BASELINE"].virtual_position)
        self.assertEqual(summary["statuses"]["BOT_A_BASELINE"], "CLOSED_POSITION_CLEARED")

    def test_duplicate_open_trades_are_cleaned(self):
        first, _ = self.open_position(option_symbol="ONECALL", now_epoch=epoch_at(minute=0))
        second = replace(first, trade_id=first.trade_id + "-DUP", option_symbol="TWOCALL", entry_epoch=epoch_at(minute=1))
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [first, second])
        cancelled = [trade for trade in trades if trade.status == "CANCELLED"]
        self.assertEqual(cancelled[0].exit_reason, EXIT_RECOVERY_DUPLICATE_OPEN_POSITION)
        self.assertEqual(summary["duplicates_cleaned"], 1)

    def test_invalid_orphan_trade_is_cancelled(self):
        trade, _ = self.open_position()
        trade.entry_price = 0
        self.states["BOT_A_BASELINE"].virtual_position = None
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [trade])
        self.assertEqual(trades[0].status, "CANCELLED")
        self.assertEqual(trades[0].exit_reason, EXIT_RECOVERY_INVALID_STATE)

    def test_invalid_orphan_position_is_handled_safely(self):
        _, position = self.open_position()
        position.entry_price = 0
        self.states["BOT_A_BASELINE"].virtual_position = position
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [])
        self.assertIsNone(states["BOT_A_BASELINE"].virtual_position)
        self.assertEqual(summary["cancelled_invalid"], 1)

    def test_old_spytest_position_is_detected_as_synthetic_and_cleared(self):
        _, position = self.open_position("BOT_B_MOMENTUM", "SPYTESTCALL")
        self.states["BOT_B_MOMENTUM"].virtual_position = position
        self.states["BOT_B_MOMENTUM"].virtual_trades_today = 1
        self.states["BOT_B_MOMENTUM"].virtual_entry_cooldown_until_epoch = epoch_at(minute=10)
        self.states["BOT_B_MOMENTUM"].last_entry_fingerprint = "synthetic"

        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [])

        self.assertIsNone(states["BOT_B_MOMENTUM"].virtual_position)
        self.assertEqual(states["BOT_B_MOMENTUM"].virtual_trades_today, 0)
        self.assertIsNone(states["BOT_B_MOMENTUM"].virtual_entry_cooldown_until_epoch)
        self.assertIsNone(states["BOT_B_MOMENTUM"].last_entry_fingerprint)
        self.assertEqual(summary["synthetic_positions_cleared"], 1)
        self.assertEqual(summary["statuses"]["BOT_B_MOMENTUM"], RECOVERY_SYNTHETIC_PROOF_CLEARED)

    def test_recovery_clears_explicit_pipeline_proof_position_metadata(self):
        _, position = self.open_position("BOT_D_COMBINED", "SPY260101C00500000")
        position.is_test_position = True
        position.test_type = TEST_TYPE_PIPELINE_PROOF
        self.states["BOT_D_COMBINED"].virtual_position = position

        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [])

        self.assertIsNone(states["BOT_D_COMBINED"].virtual_position)
        self.assertEqual(summary["synthetic_positions_cleared"], 1)

    def test_synthetic_cleanup_preserves_real_historical_trades(self):
        real_trade, _ = self.open_position("BOT_A_BASELINE", "SPY260101C00500000")
        synthetic_trade = replace(
            real_trade,
            trade_id=real_trade.trade_id + "-TEST",
            profile_id="BOT_B_MOMENTUM",
            profile_display_name="Bot B Momentum",
            option_symbol="SPYTESTCALL",
            signal="TEST_PIPELINE",
            is_test_position=True,
            test_type=TEST_TYPE_PIPELINE_PROOF,
        )

        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [real_trade, synthetic_trade])

        self.assertEqual([trade.trade_id for trade in trades], [real_trade.trade_id])
        self.assertEqual(summary["synthetic_trades_removed"], 1)
        self.assertEqual(states["BOT_A_BASELINE"].virtual_position.trade_id, real_trade.trade_id)

    def test_bot_b_can_open_real_call_after_fake_position_removed(self):
        _, fake_position = self.open_position("BOT_B_MOMENTUM", "SPYTESTCALL")
        self.states["BOT_B_MOMENTUM"].virtual_position = fake_position
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [])

        trade = try_open_virtual_position(
            self.profiles["BOT_B_MOMENTUM"],
            states["BOT_B_MOMENTUM"],
            accepted_decision("BOT_B_MOMENTUM"),
            snapshot("SPY260101C00500000", quote_time="2026-07-28T10:02:00-04:00"),
            epoch_at(minute=2),
        )

        self.assertEqual(summary["synthetic_positions_cleared"], 1)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.direction, "CALL")
        self.assertEqual(trade.option_symbol, "SPY260101C00500000")

    def test_bot_b_can_open_real_put_after_fake_position_removed(self):
        _, fake_position = self.open_position("BOT_B_MOMENTUM", "SPYTESTCALL")
        self.states["BOT_B_MOMENTUM"].virtual_position = fake_position
        states, trades, summary = reconcile_tournament_state(self.profiles, self.states, [])
        put_decision = accepted_decision("BOT_B_MOMENTUM")
        put_decision.direction = "PUT"
        put_decision.bullish_score = 0
        put_decision.bearish_score = 5

        trade = try_open_virtual_position(
            self.profiles["BOT_B_MOMENTUM"],
            states["BOT_B_MOMENTUM"],
            put_decision,
            snapshot("SPY260101P00500000", quote_time="2026-07-28T10:02:00-04:00", direction="PUT"),
            epoch_at(minute=2),
        )

        self.assertEqual(summary["synthetic_positions_cleared"], 1)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.direction, "PUT")
        self.assertEqual(trade.option_symbol, "SPY260101P00500000")

    def test_main_state_corrupt_loads_backup(self):
        save_tournament_state(self.state_path, self.states)
        os.replace(self.state_path, self.state_path.replace(".json", ".backup.json"))
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write("{bad json")
        loaded = load_tournament_state(self.state_path, self.profiles)
        self.assertIn("BOT_A_BASELINE", loaded)

    def test_main_trades_corrupt_loads_backup(self):
        trade, _ = self.open_position()
        save_tournament_trades([trade], self.trades_path)
        os.replace(self.trades_path, self.trades_path.replace(".json", ".backup.json"))
        with open(self.trades_path, "w", encoding="utf-8") as handle:
            handle.write("{bad json")
        self.assertEqual(len(load_tournament_trades(self.trades_path)), 1)

    def test_both_invalid_fall_back_to_empty_safe_state(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write("{bad json")
        with open(self.state_path.replace(".json", ".backup.json"), "w", encoding="utf-8") as handle:
            handle.write("{bad json")
        loaded = load_tournament_state(self.state_path, self.profiles)
        self.assertIsNone(loaded["BOT_A_BASELINE"].virtual_position)

    def test_daily_reset_resets_trade_counter(self):
        state = self.states["BOT_A_BASELINE"]
        state.virtual_trading_date = "2026-07-27"
        state.virtual_trades_today = 7
        self.assertTrue(apply_daily_reset(self.profiles["BOT_A_BASELINE"], state, epoch_at()))
        self.assertEqual(state.virtual_trades_today, 0)

    def test_daily_reset_preserves_open_position(self):
        _, position = self.open_position()
        self.states["BOT_A_BASELINE"].virtual_trading_date = "2026-07-27"
        apply_daily_reset(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], epoch_at())
        self.assertEqual(self.states["BOT_A_BASELINE"].virtual_position.trade_id, position.trade_id)

    def test_daily_reset_clears_expired_cooldown(self):
        state = self.states["BOT_A_BASELINE"]
        state.virtual_trading_date = "2026-07-27"
        state.virtual_entry_cooldown_until_epoch = epoch_at() - 1
        apply_daily_reset(self.profiles["BOT_A_BASELINE"], state, epoch_at())
        self.assertIsNone(state.virtual_entry_cooldown_until_epoch)

    def test_daily_reset_does_not_delete_history(self):
        trade, _ = self.open_position()
        apply_daily_reset(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], epoch_at(day=29))
        self.assertEqual(len(list_tournament_trades(path=self.trades_path)), 1)
        self.assertEqual(list_tournament_trades(path=self.trades_path)[0].trade_id, trade.trade_id)

    def test_weekend_blocks_new_entries(self):
        self.assertFalse(tournament_market_is_open(epoch_at(day=25)))

    def test_before_930_et_blocks_new_entries(self):
        self.assertFalse(tournament_market_is_open(epoch_at(hour=9, minute=29)))

    def test_after_4pm_et_blocks_new_entries(self):
        self.assertFalse(tournament_market_is_open(epoch_at(hour=16, minute=1)))

    def test_valid_market_hours_allow_entry(self):
        self.assertTrue(tournament_market_is_open(epoch_at(hour=10)))

    def test_missing_quote_does_not_close(self):
        self.open_position()
        closed = process_virtual_exits(self.profiles, self.states, {}, epoch_at())
        self.assertEqual(closed, [])
        self.assertIsNotNone(self.states["BOT_A_BASELINE"].virtual_position)

    def test_zero_bid_does_not_close(self):
        self.open_position()
        closed = process_virtual_exits(self.profiles, self.states, {"SPYRELCALL": {"bid": 0, "_fetched_at": "2026-07-28T10:00:00-04:00"}}, epoch_at())
        self.assertEqual(closed, [])

    def test_stale_quote_does_not_open(self):
        decision = accepted_decision()
        trade = try_open_virtual_position(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], decision, snapshot(quote_time="2026-07-28T09:59:00-04:00"), epoch_at())
        self.assertIsNone(trade)
        self.assertEqual(decision.rejection_reason, REJECT_QUOTE_STALE)

    def test_stale_quote_does_not_close(self):
        self.open_position()
        closed = process_virtual_exits(self.profiles, self.states, {"SPYRELCALL": {"bid": 0.5, "_fetched_at": "2026-07-28T09:59:00-04:00"}}, epoch_at())
        self.assertEqual(closed, [])
        self.assertIsNotNone(self.states["BOT_A_BASELINE"].virtual_position)

    def test_fresh_quote_works(self):
        self.assertEqual(quote_is_fresh({"_fetched_at": "2026-07-28T10:00:00-04:00"}, 10, epoch_at()), (True, None))

    def test_missing_quote_timestamp_is_invalid(self):
        self.assertEqual(quote_is_fresh({"bid": 1.0}, 10, epoch_at())[1], REJECT_INVALID_QUOTE_TIMESTAMP)

    def test_market_closed_sets_rejection_reason(self):
        decision = accepted_decision()
        trade = try_open_virtual_position(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], decision, snapshot(), epoch_at(hour=16, minute=1))
        self.assertIsNone(trade)
        self.assertEqual(decision.rejection_reason, REJECT_MARKET_CLOSED)

    def test_atomic_save_creates_valid_backup(self):
        save_tournament_state(self.state_path, self.states)
        self.states["BOT_A_BASELINE"].virtual_trades_today = 3
        save_tournament_state(self.state_path, self.states)
        self.assertTrue(os.path.exists(self.state_path.replace(".json", ".backup.json")))
        loaded = load_tournament_state(self.state_path, self.profiles)
        self.assertEqual(loaded["BOT_A_BASELINE"].virtual_trades_today, 3)

    def test_api_health_reports_correct_state(self):
        import dashboard
        dashboard.TOURNAMENT_RUNTIME_STATES = self.states
        dashboard.TOURNAMENT_RUNTIME_STATES_LOADED = True
        with patch("dashboard.load_config", return_value=runtime_config()), patch("dashboard.TOURNAMENT_PROFILES_FILE", os.path.join(self.tempdir.name, "missing_profiles.json")):
            response = dashboard.app.test_client().get("/api/tournament/health")
        self.assertEqual(response.status_code, 200)
        self.assertIn("overall_status", response.get_json())

    def test_recovery_endpoint_never_calls_tradier(self):
        import dashboard
        with patch("dashboard.requests.post") as post_mock, patch("dashboard.requests.get") as get_mock, patch("dashboard.TOURNAMENT_STATE_FILE", self.state_path), patch("dashboard.TOURNAMENT_TRADES_FILE", self.trades_path), patch("dashboard.TOURNAMENT_PROFILES_FILE", os.path.join(self.tempdir.name, "missing_profiles.json")):
            response = dashboard.app.test_client().post("/api/tournament/recovery/run")
        self.assertEqual(response.status_code, 200)
        post_mock.assert_not_called()
        get_mock.assert_not_called()

    def test_force_save_endpoint_never_calls_tradier(self):
        import dashboard
        dashboard.TOURNAMENT_RUNTIME_STATES = self.states
        dashboard.TOURNAMENT_RUNTIME_STATES_LOADED = True
        with patch("dashboard.requests.post") as post_mock, patch("dashboard.requests.get") as get_mock, patch("dashboard.TOURNAMENT_STATE_FILE", self.state_path), patch("dashboard.TOURNAMENT_PROFILES_FILE", os.path.join(self.tempdir.name, "missing_profiles.json")):
            response = dashboard.app.test_client().post("/api/tournament/state/save")
        self.assertEqual(response.status_code, 200)
        post_mock.assert_not_called()
        get_mock.assert_not_called()

    def test_watchdog_restart_starts_dead_exit_thread(self):
        import dashboard

        class DeadThread:
            def is_alive(self):
                return False

        class FakeThread:
            started = False

            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                FakeThread.started = True

            def is_alive(self):
                return FakeThread.started

        original_thread = dashboard.TOURNAMENT_EXIT_THREAD
        try:
            dashboard.TOURNAMENT_EXIT_THREAD = DeadThread()
            with patch("dashboard.threading.Thread", FakeThread):
                thread = dashboard.start_tournament_exit_monitor()
            self.assertTrue(thread.is_alive())
            self.assertTrue(FakeThread.started)
        finally:
            dashboard.TOURNAMENT_EXIT_THREAD = original_thread

    def test_watchdog_does_not_create_duplicate_exit_threads(self):
        import dashboard

        class AliveThread:
            def is_alive(self):
                return True

        original_thread = dashboard.TOURNAMENT_EXIT_THREAD
        existing = AliveThread()
        try:
            dashboard.TOURNAMENT_EXIT_THREAD = existing
            with patch("dashboard.threading.Thread") as thread_mock:
                thread = dashboard.start_tournament_exit_monitor()
            self.assertIs(thread, existing)
            thread_mock.assert_not_called()
        finally:
            dashboard.TOURNAMENT_EXIT_THREAD = original_thread

    def test_original_bot_trade_history_remains_unchanged(self):
        import dashboard
        before = dashboard.get_recent_trades()
        self.open_position()
        after = dashboard.get_recent_trades()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
