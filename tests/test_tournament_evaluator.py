import json
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from tournament.evaluator import evaluate_all_profiles, evaluate_profile
from tournament.models import ProfileDecision
from tournament.profiles import build_tournament_profiles
from tournament.snapshot import MarketSnapshot, build_market_snapshot_from_signal
from tournament.state import create_all_initial_states, load_tournament_state


class TournamentEvaluatorTest(unittest.TestCase):
    def setUp(self):
        self.runtime_config = {
            "symbol": "SPY",
            "bot_enabled": True,
            "minimum_confidence": 4,
            "minimum_dominance_percent": 60,
            "strategy": {
                "direction_threshold_percent": 60,
            },
            "entry_rules": {
                "minimum_signals": 2,
                "allow_calls": True,
                "allow_puts": True,
            },
            "bot_starting_account_balance": 1000.0,
        }
        self.profiles = build_tournament_profiles(self.runtime_config)
        self.states = create_all_initial_states(self.profiles)

    def snapshot(self, **overrides):
        values = {
            "timestamp": "2026-07-28T09:35:00-04:00",
            "symbol": "SPY",
            "current_price": 500.0,
            "bullish_score": 5,
            "bearish_score": 0,
            "confidence": 5,
            "dominance_percent": 100.0,
            "market_state": "BULLISH",
            "suggested_direction": "CALL",
            "signal": "CALL",
            "ema_bullish": True,
            "ema_bearish": False,
            "macd_bullish": True,
            "macd_bearish": False,
            "above_vwap": True,
            "below_vwap": False,
            "volume_confirmed": True,
            "opening_range_ready": True,
            "opening_range_high": 499.0,
            "opening_range_low": 497.0,
            "completed_closes": (500.0, 501.0),
            "option_symbol": None,
            "option_bid": None,
            "option_ask": None,
            "option_last": None,
            "option_midpoint": None,
            "option_quote_timestamp": None,
        }
        values.update(overrides)
        return MarketSnapshot(**values)

    def profile_decision_stub(self, profile, state, snapshot):
        return ProfileDecision(
            profile_id=profile.profile_id,
            timestamp=snapshot.timestamp,
            accepted=True,
            direction=snapshot.suggested_direction,
            status="ACCEPTED",
            rejection_reason=None,
            bullish_score=snapshot.bullish_score,
            bearish_score=snapshot.bearish_score,
            confidence=snapshot.confidence,
            dominance_percent=snapshot.dominance_percent,
            momentum_required=False,
            momentum_status="NOT_REQUIRED",
            or_confirmation_required=False,
            or_confirmation_status="NOT_REQUIRED",
        )

    def test_all_four_profiles_receive_same_snapshot_object(self):
        snapshot = self.snapshot()
        with patch("tournament.evaluator.evaluate_profile", side_effect=self.profile_decision_stub) as mocked:
            evaluate_all_profiles(self.profiles, self.states, snapshot)

        self.assertEqual(mocked.call_count, 4)
        self.assertTrue(all(call.args[2] is snapshot for call in mocked.call_args_list))

    def test_baseline_accepts_valid_setup_without_momentum_or_or(self):
        decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot())
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.status, "ACCEPTED")
        self.assertEqual(decision.direction, "CALL")
        self.assertEqual(decision.final_direction, "CALL")

    def test_strong_bearish_snapshot_produces_put(self):
        decision = evaluate_profile(
            self.profiles["BOT_A_BASELINE"],
            self.states["BOT_A_BASELINE"],
            self.snapshot(bullish_score=0, bearish_score=5, suggested_direction="CALL", signal="CALL", market_state="BEARISH", completed_closes=(496.0, 495.0)),
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.direction, "PUT")
        self.assertEqual(decision.final_direction, "PUT")

    def test_balanced_signals_produce_none(self):
        decision = evaluate_profile(
            self.profiles["BOT_A_BASELINE"],
            self.states["BOT_A_BASELINE"],
            self.snapshot(bullish_score=2, bearish_score=2, confidence=0, dominance_percent=50.0),
        )
        self.assertFalse(decision.accepted)
        self.assertIsNone(decision.direction)
        self.assertEqual(decision.preliminary_direction, None)
        self.assertEqual(decision.rejection_reason, "NO_DOMINANT_DIRECTION")

    def test_direction_threshold_blocks_weak_wave(self):
        for profile in self.profiles.values():
            profile.config["strategy"]["direction_threshold_percent"] = 75
            profile.config["minimum_dominance_percent"] = 50
        decision = evaluate_profile(
            self.profiles["BOT_A_BASELINE"],
            self.states["BOT_A_BASELINE"],
            self.snapshot(bullish_score=4, bearish_score=2, confidence=4, dominance_percent=66.7),
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.rejection_reason, "DIRECTION_THRESHOLD_NOT_MET")

    def test_momentum_profile_waits_for_momentum(self):
        decision = evaluate_profile(self.profiles["BOT_B_MOMENTUM"], self.states["BOT_B_MOMENTUM"], self.snapshot())
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.status, "WAITING_MOMENTUM")
        self.assertEqual(decision.rejection_reason, "MOMENTUM_CONFIRMATION_REQUIRED")
        self.assertFalse(decision.momentum_passed)

    def test_momentum_profile_accepts_when_momentum_passes(self):
        decision = evaluate_profile(self.profiles["BOT_B_MOMENTUM"], self.states["BOT_B_MOMENTUM"], self.snapshot(momentum_confirmed=True))
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.momentum_status, "PASS")
        self.assertTrue(decision.momentum_passed)

    def test_or_profile_accepts_call_when_required_closes_above_or_high(self):
        decision = evaluate_profile(self.profiles["BOT_C_TWO_CANDLE_OR"], self.states["BOT_C_TWO_CANDLE_OR"], self.snapshot(completed_closes=(500.0, 501.0)))
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.or_confirmation_status, "PASS")

    def test_or_profile_rejects_call_when_required_close_is_at_or_below_high(self):
        decision = evaluate_profile(self.profiles["BOT_C_TWO_CANDLE_OR"], self.states["BOT_C_TWO_CANDLE_OR"], self.snapshot(completed_closes=(500.0, 499.0)))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.status, "WAITING_OR_CONFIRMATION")
        self.assertEqual(decision.rejection_reason, "TWO_CANDLE_OR_NOT_CONFIRMED")

    def test_or_profile_accepts_put_when_required_closes_below_or_low(self):
        snapshot = self.snapshot(
            bullish_score=0,
            bearish_score=5,
            market_state="BEARISH",
            suggested_direction="PUT",
            signal="PUT",
            ema_bullish=False,
            ema_bearish=True,
            macd_bullish=False,
            macd_bearish=True,
            above_vwap=False,
            below_vwap=True,
            completed_closes=(496.0, 495.0),
        )
        decision = evaluate_profile(self.profiles["BOT_C_TWO_CANDLE_OR"], self.states["BOT_C_TWO_CANDLE_OR"], snapshot)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.direction, "PUT")
        self.assertEqual(decision.or_confirmation_status, "PASS")

    def test_combined_profile_waits_for_momentum_after_or_passes(self):
        decision = evaluate_profile(self.profiles["BOT_D_COMBINED"], self.states["BOT_D_COMBINED"], self.snapshot(completed_closes=(500.0, 501.0)))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.status, "WAITING_MOMENTUM")
        self.assertEqual(decision.or_confirmation_status, "PASS")

    def test_combined_profile_requires_both_confirmations(self):
        waiting_or = evaluate_profile(self.profiles["BOT_D_COMBINED"], self.states["BOT_D_COMBINED"], self.snapshot(momentum_confirmed=True, completed_closes=(500.0, 499.0)))
        accepted = evaluate_profile(self.profiles["BOT_D_COMBINED"], self.states["BOT_D_COMBINED"], self.snapshot(momentum_confirmed=True, completed_closes=(500.0, 501.0)))
        self.assertEqual(waiting_or.rejection_reason, "TWO_CANDLE_OR_NOT_CONFIRMED")
        self.assertTrue(accepted.accepted)

    def test_previous_call_does_not_force_next_put_decision(self):
        call_decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot())
        put_decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot(bullish_score=0, bearish_score=5, suggested_direction="CALL", signal="CALL", market_state="BEARISH", completed_closes=(496.0, 495.0)))
        self.assertEqual(call_decision.direction, "CALL")
        self.assertEqual(put_decision.direction, "PUT")

    def test_previous_put_does_not_force_next_call_decision(self):
        put_decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot(bullish_score=0, bearish_score=5, suggested_direction="PUT", signal="PUT", market_state="BEARISH", completed_closes=(496.0, 495.0)))
        call_decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot(suggested_direction="PUT", signal="PUT"))
        self.assertEqual(put_decision.direction, "PUT")
        self.assertEqual(call_decision.direction, "CALL")

    def test_direction_trace_explains_final_direction(self):
        decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot(bullish_score=4, bearish_score=1, confidence=4, dominance_percent=80.0))
        self.assertEqual(decision.total_signals, 5)
        self.assertEqual(decision.bullish_dominance_percent, 80.0)
        self.assertEqual(decision.bearish_dominance_percent, 20.0)
        self.assertEqual(decision.preliminary_direction, "CALL")
        self.assertEqual(decision.final_direction, "CALL")

    def test_dashboard_current_signal_does_not_control_tournament_direction(self):
        snapshot = build_market_snapshot_from_signal(
            {
                "current_signal": "PUT",
                "decision": "BUY PUT",
                "bullish_score": 5,
                "bearish_score": 0,
                "confidence": 5,
                "dominance_percent": 100.0,
                "levels": {"opening_range_high": 499, "opening_range_low": 497},
            },
            "SPY",
            "2026-07-28T09:35:00-04:00",
        )
        decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], snapshot)
        self.assertEqual(snapshot.suggested_direction, None)
        self.assertEqual(decision.direction, "CALL")

    def test_call_snapshot_drops_put_symbol(self):
        snap = build_market_snapshot_from_signal(
            {
                "price": 500,
                "bullish_score": 5,
                "bearish_score": 0,
                "confidence": 5,
                "call_option_symbol": "SPY260810P00773000",
                "call_option_bid": 1.0,
                "call_option_ask": 1.1,
            },
            "SPY",
            "2026-08-10T10:00:00-04:00",
        )
        self.assertIsNone(snap.call_option_symbol)
        self.assertIsNone(snap.call_option_ask)
        self.assertIn("INVALID_CALL_OPTION_SYMBOL", snap.option_diagnostics)

    def test_put_snapshot_drops_call_symbol(self):
        snap = build_market_snapshot_from_signal(
            {
                "price": 500,
                "bullish_score": 0,
                "bearish_score": 5,
                "confidence": 5,
                "put_option_symbol": "SPY260810C00773000",
                "put_option_bid": 1.0,
                "put_option_ask": 1.1,
            },
            "SPY",
            "2026-08-10T10:00:00-04:00",
        )
        self.assertIsNone(snap.put_option_symbol)
        self.assertIsNone(snap.put_option_ask)
        self.assertIn("INVALID_PUT_OPTION_SYMBOL", snap.option_diagnostics)

    def test_confidence_below_four_rejects_all_profiles(self):
        snapshot = self.snapshot(bullish_score=5, bearish_score=2, confidence=3, dominance_percent=71.4)
        decisions = evaluate_all_profiles(self.profiles, self.states, snapshot)
        self.assertTrue(all(decision.rejection_reason == "CONFIDENCE_BELOW_MINIMUM" for decision in decisions.values()))

    def test_dominance_below_configured_minimum_rejects_all_profiles(self):
        snapshot = self.snapshot(bullish_score=5, bearish_score=4, confidence=5, dominance_percent=55.6)
        decisions = evaluate_all_profiles(self.profiles, self.states, snapshot)
        self.assertTrue(all(decision.rejection_reason == "DOMINANCE_BELOW_MINIMUM" for decision in decisions.values()))

    def test_signal_count_below_minimum_rejects_all_profiles(self):
        for profile in self.profiles.values():
            profile.config["entry_rules"]["minimum_signals"] = 6
        decisions = evaluate_all_profiles(self.profiles, self.states, self.snapshot())
        self.assertTrue(all(decision.rejection_reason == "SIGNAL_COUNT_BELOW_MINIMUM" for decision in decisions.values()))

    def test_calls_disabled_rejects_call_only(self):
        for profile in self.profiles.values():
            profile.config["entry_rules"]["allow_calls"] = False
        call_decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot())
        put_snapshot = self.snapshot(
            bullish_score=0,
            bearish_score=5,
            market_state="BEARISH",
            suggested_direction="PUT",
            signal="PUT",
            completed_closes=(496.0, 495.0),
        )
        put_decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], put_snapshot)
        self.assertEqual(call_decision.rejection_reason, "CALLS_DISABLED")
        self.assertEqual(put_decision.status, "ACCEPTED")

    def test_puts_disabled_rejects_put_only(self):
        for profile in self.profiles.values():
            profile.config["entry_rules"]["allow_puts"] = False
        call_decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot())
        put_decision = evaluate_profile(
            self.profiles["BOT_A_BASELINE"],
            self.states["BOT_A_BASELINE"],
            self.snapshot(bullish_score=0, bearish_score=5, market_state="BEARISH", suggested_direction="PUT", signal="PUT"),
        )
        self.assertEqual(call_decision.status, "ACCEPTED")
        self.assertEqual(put_decision.rejection_reason, "PUTS_DISABLED")

    def test_disabled_profile_returns_disabled(self):
        self.profiles["BOT_A_BASELINE"].enabled = False
        decision = evaluate_profile(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], self.snapshot())
        self.assertEqual(decision.status, "DISABLED")
        self.assertEqual(decision.rejection_reason, "BOT_DISABLED")

    def test_no_profile_mutates_market_snapshot(self):
        snapshot = self.snapshot()
        evaluate_all_profiles(self.profiles, self.states, snapshot)
        with self.assertRaises(FrozenInstanceError):
            snapshot.bullish_score = 0

    def test_original_bot_order_functions_are_never_imported_or_called(self):
        forbidden_modules = {
            "dashboard",
            "option_order",
            "order_filer",
            "paper_buy",
            "paper_sell",
            "positions",
        }
        before = set(sys.modules)
        evaluate_all_profiles(self.profiles, self.states, self.snapshot())
        imported = set(sys.modules) - before
        self.assertTrue(forbidden_modules.isdisjoint(imported))

    def test_old_saved_tournament_state_loads_without_decision_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            old_state = {
                "BOT_A_BASELINE": {
                    "profile_id": "BOT_A_BASELINE",
                    "virtual_balance": 1000.0,
                    "starting_balance": 1000.0,
                    "position": {},
                    "pending_entry": {},
                    "metrics": {},
                    "cooldown_until": None,
                    "last_updated_at": "2026-07-28T09:35:00-04:00",
                    "enabled": True,
                }
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(old_state, handle)

            loaded = load_tournament_state(path, self.profiles)

        self.assertIsNone(loaded["BOT_A_BASELINE"].last_decision)
        self.assertEqual(loaded["BOT_A_BASELINE"].decisions_evaluated, 0)
        self.assertEqual(set(loaded), set(self.profiles))


if __name__ == "__main__":
    unittest.main()
