import unittest
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from tournament.evaluator import evaluate_all_profiles, evaluate_profile
from tournament.execution import try_open_virtual_position
from tournament.momentum import (
    MOMENTUM_CONFIRMED,
    MOMENTUM_DIRECTION_CHANGED,
    MOMENTUM_DRAWDOWN_FAILED,
    MOMENTUM_TIMEOUT,
    MOMENTUM_TRACKING,
    apply_tournament_momentum,
)
from tournament.profiles import build_tournament_profiles
from tournament.snapshot import MarketSnapshot
from tournament.state import create_all_initial_states


MARKET_TZ = ZoneInfo("America/New_York")


def epoch_at(hour=10, minute=0, second=0):
    return datetime(2026, 7, 28, hour, minute, second, tzinfo=MARKET_TZ).timestamp()


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
            "direction_threshold_percent": 50,
            "hard_stop_percent": 20,
            "trailing_stop_percent": 10,
            "enable_profit_floor_trailing_stop": True,
            "locked_profit_dollars": 5.0,
        },
        "entry_rules": {
            "minimum_signals": 1,
            "allow_calls": True,
            "allow_puts": True,
            "cooldown_minutes": 0,
            "max_trades_per_day": 10,
        },
    }


def snapshot(direction="CALL", call_mid=1.0, put_mid=1.0, completed_closes=None, momentum_confirmed=False, quote_time="2026-07-28T10:00:00-04:00"):
    bullish_score = 5 if direction == "CALL" else 0
    bearish_score = 5 if direction == "PUT" else 0
    completed_closes = completed_closes or ((500.0, 501.0) if direction == "CALL" else (496.0, 495.0))
    return MarketSnapshot(
        timestamp=quote_time,
        symbol="SPY",
        current_price=500.0,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        confidence=5,
        dominance_percent=100.0,
        market_state="BULLISH" if direction == "CALL" else "BEARISH",
        suggested_direction=None,
        signal=None,
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
        completed_closes=completed_closes,
        option_quote_timestamp=quote_time,
        call_option_symbol="SPYTESTCALL",
        call_option_bid=call_mid - 0.01,
        call_option_ask=call_mid + 0.01,
        call_option_last=call_mid,
        call_option_midpoint=call_mid,
        put_option_symbol="SPYTESTPUT",
        put_option_bid=put_mid - 0.01,
        put_option_ask=put_mid + 0.01,
        put_option_last=put_mid,
        put_option_midpoint=put_mid,
        momentum_confirmed=momentum_confirmed,
    )


class TournamentMomentumTest(unittest.TestCase):
    def setUp(self):
        self.profiles = build_tournament_profiles(runtime_config())
        self.states = create_all_initial_states(self.profiles)

    def decisions_with_momentum(self, snap, now_epoch):
        decisions = evaluate_all_profiles(self.profiles, self.states, snap)
        return apply_tournament_momentum(self.profiles, self.states, decisions, snap, now_epoch)

    def test_bot_b_creates_candidate_after_valid_preliminary_direction(self):
        decisions = self.decisions_with_momentum(snapshot(), epoch_at())
        candidate = self.states["BOT_B_MOMENTUM"].momentum_candidate
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.status, MOMENTUM_TRACKING)
        self.assertEqual(candidate.direction, "CALL")
        self.assertEqual(decisions["BOT_B_MOMENTUM"].momentum_status, MOMENTUM_TRACKING)

    def test_one_percent_setting_means_one_percent_not_one_hundred(self):
        self.decisions_with_momentum(snapshot(call_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(call_mid=1.01), epoch_at(second=1))
        self.assertAlmostEqual(decisions["BOT_B_MOMENTUM"].momentum_observed_percent, 1.0)
        self.assertEqual(decisions["BOT_B_MOMENTUM"].momentum_status, MOMENTUM_CONFIRMED)

    def test_call_candidate_confirms_when_call_option_rises(self):
        self.decisions_with_momentum(snapshot(direction="CALL", call_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(direction="CALL", call_mid=1.02), epoch_at(second=1))
        self.assertTrue(decisions["BOT_B_MOMENTUM"].accepted)
        self.assertEqual(decisions["BOT_B_MOMENTUM"].direction, "CALL")

    def test_put_candidate_confirms_when_put_option_rises(self):
        self.decisions_with_momentum(snapshot(direction="PUT", put_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(direction="PUT", put_mid=1.02), epoch_at(second=1))
        self.assertTrue(decisions["BOT_B_MOMENTUM"].accepted)
        self.assertEqual(decisions["BOT_B_MOMENTUM"].direction, "PUT")

    def test_movement_below_threshold_remains_tracking(self):
        self.decisions_with_momentum(snapshot(call_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(call_mid=1.005), epoch_at(second=1))
        self.assertFalse(decisions["BOT_B_MOMENTUM"].accepted)
        self.assertEqual(decisions["BOT_B_MOMENTUM"].momentum_status, MOMENTUM_TRACKING)

    def test_timeout_cancels_candidate(self):
        self.decisions_with_momentum(snapshot(call_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(call_mid=1.005, quote_time="2026-07-28T10:02:00-04:00"), epoch_at(minute=2))
        self.assertEqual(decisions["BOT_B_MOMENTUM"].momentum_status, MOMENTUM_TIMEOUT)

    def test_direction_change_cancels_candidate(self):
        self.decisions_with_momentum(snapshot(direction="CALL", call_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(direction="PUT", put_mid=1.00), epoch_at(second=1))
        self.assertEqual(decisions["BOT_B_MOMENTUM"].momentum_status, MOMENTUM_DIRECTION_CHANGED)

    def test_excess_drawdown_cancels_candidate(self):
        self.decisions_with_momentum(snapshot(call_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(call_mid=0.94), epoch_at(second=1))
        self.assertEqual(decisions["BOT_B_MOMENTUM"].momentum_status, MOMENTUM_DRAWDOWN_FAILED)

    def test_bot_b_can_enter_after_momentum_confirms(self):
        self.decisions_with_momentum(snapshot(call_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(call_mid=1.02), epoch_at(second=1))
        trade = try_open_virtual_position(self.profiles["BOT_B_MOMENTUM"], self.states["BOT_B_MOMENTUM"], decisions["BOT_B_MOMENTUM"], snapshot(call_mid=1.02), epoch_at(second=1))
        self.assertIsNotNone(trade)
        self.assertEqual(trade.direction, "CALL")

    def test_bot_d_enters_only_after_momentum_and_or_pass(self):
        self.decisions_with_momentum(snapshot(call_mid=1.00, completed_closes=(500.0, 499.0)), epoch_at())
        failed_or = self.decisions_with_momentum(snapshot(call_mid=1.02, completed_closes=(500.0, 499.0)), epoch_at(second=1))
        passed_both = self.decisions_with_momentum(snapshot(call_mid=1.02, completed_closes=(500.0, 501.0)), epoch_at(second=2))
        self.assertFalse(failed_or["BOT_D_COMBINED"].accepted)
        self.assertEqual(failed_or["BOT_D_COMBINED"].or_confirmation_status, "FAIL")
        self.assertTrue(passed_both["BOT_D_COMBINED"].accepted)

    def test_bot_a_remains_unaffected(self):
        decisions = self.decisions_with_momentum(snapshot(), epoch_at())
        self.assertTrue(decisions["BOT_A_BASELINE"].accepted)
        self.assertEqual(decisions["BOT_A_BASELINE"].momentum_status, "NOT_REQUIRED")

    def test_bot_c_remains_unaffected(self):
        decisions = self.decisions_with_momentum(snapshot(completed_closes=(500.0, 501.0)), epoch_at())
        self.assertTrue(decisions["BOT_C_TWO_CANDLE_OR"].accepted)
        self.assertEqual(decisions["BOT_C_TWO_CANDLE_OR"].momentum_status, "NOT_REQUIRED")

    def test_profile_setting_changes_required_threshold(self):
        self.profiles["BOT_B_MOMENTUM"].config["option_momentum_percent"] = 2.0
        self.decisions_with_momentum(snapshot(call_mid=1.00), epoch_at())
        below = self.decisions_with_momentum(snapshot(call_mid=1.01), epoch_at(second=1))
        at_threshold = self.decisions_with_momentum(snapshot(call_mid=1.02), epoch_at(second=2))
        self.assertEqual(below["BOT_B_MOMENTUM"].momentum_status, MOMENTUM_TRACKING)
        self.assertEqual(at_threshold["BOT_B_MOMENTUM"].momentum_status, MOMENTUM_CONFIRMED)

    def test_missing_upstream_momentum_boolean_does_not_permanently_block(self):
        start = snapshot(call_mid=1.00, momentum_confirmed=False)
        confirm = snapshot(call_mid=1.02, momentum_confirmed=False)
        self.decisions_with_momentum(start, epoch_at())
        decisions = self.decisions_with_momentum(confirm, epoch_at(second=1))
        self.assertTrue(decisions["BOT_B_MOMENTUM"].accepted)

    def test_dashboard_exposes_observed_and_required_momentum_values(self):
        self.decisions_with_momentum(snapshot(call_mid=1.00), epoch_at())
        decisions = self.decisions_with_momentum(snapshot(call_mid=1.005), epoch_at(second=1))
        decision = decisions["BOT_B_MOMENTUM"]
        self.assertGreater(decision.momentum_observed_percent, 0)
        self.assertEqual(decision.momentum_required_percent, 1.0)
        self.assertEqual(decision.momentum_candidate_option_symbol, "SPYTESTCALL")


if __name__ == "__main__":
    unittest.main()
