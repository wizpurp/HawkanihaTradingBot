import os
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from tournament.execution import try_open_virtual_position
from tournament.models import ProfileDecision
from tournament.profiles import build_tournament_profiles
from tournament.snapshot import MarketSnapshot
from tournament.state import create_all_initial_states, load_tournament_state, save_tournament_state
from tournament.trades import list_tournament_trades


def runtime_config():
    return {
        "symbol": "SPY",
        "bot_enabled": True,
        "minimum_confidence": 1,
        "minimum_dominance_percent": 50,
        "max_contract_price": 2.0,
        "contracts": 1,
        "strategy": {
            "hard_stop_percent": 12,
            "trailing_stop_percent": 12,
            "enable_profit_floor_trailing_stop": True,
            "locked_profit_amount": 0.05,
        },
        "entry_rules": {
            "minimum_signals": 1,
            "allow_calls": True,
            "allow_puts": True,
            "cooldown_minutes": 0,
            "max_trades_per_day": 10,
        },
    }


def accepted_decision(profile_id="BOT_A_BASELINE", direction="CALL", timestamp="2026-07-28T09:35:00-04:00"):
    return ProfileDecision(
        profile_id=profile_id,
        timestamp=timestamp,
        accepted=True,
        direction=direction,
        status="ACCEPTED",
        rejection_reason=None,
        bullish_score=5 if direction == "CALL" else 0,
        bearish_score=5 if direction == "PUT" else 0,
        confidence=5,
        dominance_percent=100.0,
        momentum_required=False,
        momentum_status="NOT_REQUIRED",
        or_confirmation_required=False,
        or_confirmation_status="NOT_REQUIRED",
    )


def rejected_decision(profile_id="BOT_A_BASELINE"):
    decision = accepted_decision(profile_id)
    decision.accepted = False
    decision.status = "REJECTED"
    decision.rejection_reason = "TEST_REJECTED"
    return decision


def snapshot(option_symbol="SPYTESTCALL", option_ask=1.0, direction="CALL"):
    return MarketSnapshot(
        timestamp="2026-07-28T09:35:00-04:00",
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
        option_symbol=option_symbol,
        option_bid=0.95 if option_ask else None,
        option_ask=option_ask,
        option_last=option_ask,
        option_midpoint=option_ask,
        option_quote_timestamp="2026-07-28T09:35:00-04:00",
    )


class TournamentVirtualEntriesTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.trades_path = os.path.join(self.tempdir.name, "tournament_trades.json")
        self.state_path = os.path.join(self.tempdir.name, "tournament_state.json")
        self.trade_path_patch = patch("tournament.execution.TOURNAMENT_TRADES_FILE", self.trades_path)
        self.trade_path_patch.start()
        self.profiles = build_tournament_profiles(runtime_config(), settings_path=os.path.join(self.tempdir.name, "missing_profiles.json"))
        self.states = create_all_initial_states(self.profiles)

    def tearDown(self):
        self.trade_path_patch.stop()
        self.tempdir.cleanup()

    def open_position(self, profile_id="BOT_A_BASELINE", direction="CALL", snap=None, now_epoch=1000.0):
        snap = snap or snapshot(direction=direction)
        return try_open_virtual_position(
            self.profiles[profile_id],
            self.states[profile_id],
            accepted_decision(profile_id, direction, snap.timestamp),
            snap,
            now_epoch,
        )

    def test_accepted_baseline_opens_virtual_position(self):
        trade = self.open_position("BOT_A_BASELINE")
        self.assertIsNotNone(trade)
        self.assertEqual(self.states["BOT_A_BASELINE"].virtual_position.status, "OPEN")

    def test_momentum_profile_opens_after_accepted_decision(self):
        trade = self.open_position("BOT_B_MOMENTUM")
        self.assertIsNotNone(trade)
        self.assertEqual(trade.profile_id, "BOT_B_MOMENTUM")

    def test_rejected_decision_does_not_open(self):
        trade = try_open_virtual_position(
            self.profiles["BOT_A_BASELINE"],
            self.states["BOT_A_BASELINE"],
            rejected_decision(),
            snapshot(),
            1000.0,
        )
        self.assertIsNone(trade)
        self.assertIsNone(self.states["BOT_A_BASELINE"].virtual_position)

    def test_missing_option_symbol_does_not_open(self):
        self.assertIsNone(self.open_position(snap=snapshot(option_symbol=None)))

    def test_missing_or_zero_ask_does_not_open(self):
        self.assertIsNone(self.open_position(snap=snapshot(option_ask=None)))
        self.assertIsNone(self.open_position(snap=snapshot(option_ask=0)))

    def test_ask_above_max_contract_price_does_not_open(self):
        self.profiles["BOT_A_BASELINE"].config["max_contract_price"] = 0.50
        self.assertIsNone(self.open_position(snap=snapshot(option_ask=1.0)))

    def test_existing_open_position_blocks_same_profile(self):
        self.open_position("BOT_A_BASELINE")
        second = self.open_position("BOT_A_BASELINE", snap=snapshot(option_symbol="SPYTESTCALL2"))
        self.assertIsNone(second)

    def test_another_profile_may_open_same_contract_independently(self):
        first = self.open_position("BOT_A_BASELINE", snap=snapshot(option_symbol="SPYSAME"))
        second = self.open_position("BOT_B_MOMENTUM", snap=snapshot(option_symbol="SPYSAME"))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.trade_id, second.trade_id)

    def test_cooldown_blocks_reentry(self):
        state = self.states["BOT_A_BASELINE"]
        state.virtual_entry_cooldown_until_epoch = 2000.0
        self.assertIsNone(self.open_position(now_epoch=1000.0))

    def test_daily_trade_limit_blocks_entry(self):
        self.profiles["BOT_A_BASELINE"].config["entry_rules"]["max_trades_per_day"] = 1
        self.states["BOT_A_BASELINE"].virtual_trades_today = 1
        self.assertIsNone(self.open_position())

    def test_duplicate_fingerprint_blocks_repeated_entry(self):
        trade = self.open_position()
        self.states["BOT_A_BASELINE"].virtual_position = None
        duplicate = self.open_position()
        self.assertIsNotNone(trade)
        self.assertIsNone(duplicate)

    def test_entry_uses_ask_price(self):
        trade = self.open_position(snap=snapshot(option_ask=1.23))
        self.assertEqual(trade.entry_price, 1.23)
        self.assertEqual(trade.entry_price_source, "ASK")

    def test_entry_cost_is_correct(self):
        trade = self.open_position(snap=snapshot(option_ask=1.25))
        self.assertEqual(trade.entry_cost, 125.0)

    def test_contracts_are_respected(self):
        self.profiles["BOT_A_BASELINE"].config["contracts"] = 3
        trade = self.open_position(snap=snapshot(option_ask=1.0))
        self.assertEqual(trade.contracts, 3)
        self.assertEqual(trade.entry_cost, 300.0)

    def test_trade_and_position_share_same_trade_id(self):
        trade = self.open_position()
        position = self.states["BOT_A_BASELINE"].virtual_position
        self.assertEqual(trade.trade_id, position.trade_id)

    def test_call_decision_opens_only_call_contract(self):
        snap = replace(
            snapshot(),
            call_option_symbol="SPYTESTCALL",
            call_option_ask=1.1,
            put_option_symbol="SPYTESTPUT",
            put_option_ask=0.9,
        )
        trade = self.open_position(direction="CALL", snap=snap)
        self.assertEqual(trade.option_symbol, "SPYTESTCALL")
        self.assertEqual(trade.direction, "CALL")

    def test_put_decision_opens_only_put_contract(self):
        snap = replace(
            snapshot(direction="PUT", option_symbol=None),
            call_option_symbol="SPYTESTCALL",
            call_option_ask=1.1,
            put_option_symbol="SPYTESTPUT",
            put_option_ask=0.9,
        )
        trade = self.open_position(direction="PUT", snap=snap)
        self.assertEqual(trade.option_symbol, "SPYTESTPUT")
        self.assertEqual(trade.direction, "PUT")

    def test_none_decision_never_opens_position(self):
        decision = accepted_decision("BOT_A_BASELINE", None)
        decision.accepted = False
        decision.direction = None
        trade = try_open_virtual_position(
            self.profiles["BOT_A_BASELINE"],
            self.states["BOT_A_BASELINE"],
            decision,
            snapshot(),
            1000.0,
        )
        self.assertIsNone(trade)
        self.assertIsNone(self.states["BOT_A_BASELINE"].virtual_position)

    def test_call_decision_cannot_open_put_contract(self):
        snap = replace(snapshot(option_symbol="SPYTESTPUT"), call_option_symbol="SPYTESTPUT", call_option_ask=1.0)
        decision = accepted_decision("BOT_A_BASELINE", "CALL", snap.timestamp)
        trade = try_open_virtual_position(self.profiles["BOT_A_BASELINE"], self.states["BOT_A_BASELINE"], decision, snap, 1000.0)
        self.assertIsNone(trade)
        self.assertEqual(decision.entry_block_reason, "OPTION_DIRECTION_MISMATCH")

    def test_risk_settings_are_captured_at_entry(self):
        self.profiles["BOT_A_BASELINE"].config["strategy"]["hard_stop_percent"] = 9
        self.profiles["BOT_A_BASELINE"].config["strategy"]["trailing_stop_percent"] = 11
        self.profiles["BOT_A_BASELINE"].config["strategy"]["locked_profit_amount"] = 0.25
        self.open_position()
        position = self.states["BOT_A_BASELINE"].virtual_position
        self.assertEqual(position.hard_stop_percent, 9)
        self.assertEqual(position.trailing_stop_percent, 11)
        self.assertEqual(position.locked_profit_amount, 0.25)

    def test_tournament_trade_is_stored_separately_from_original_trades(self):
        self.open_position(snap=snapshot(option_symbol="TOURNAMENT_ONLY"))
        rows = list_tournament_trades(path=self.trades_path)
        self.assertEqual(rows[0].option_symbol, "TOURNAMENT_ONLY")

    def test_no_tradier_or_broker_function_is_called(self):
        before = set(sys.modules)
        self.open_position()
        imported = set(sys.modules) - before
        forbidden = {"dashboard", "option_order", "order_filer", "paper_buy", "paper_sell", "positions"}
        self.assertTrue(forbidden.isdisjoint(imported))

    def test_old_tournament_state_still_loads(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write('{"BOT_A_BASELINE": {"profile_id": "BOT_A_BASELINE", "virtual_balance": 1000, "starting_balance": 1000}}')
        loaded = load_tournament_state(self.state_path, self.profiles)
        self.assertIsNone(loaded["BOT_A_BASELINE"].virtual_position)
        self.assertEqual(loaded["BOT_A_BASELINE"].virtual_trades_today, 0)

    def test_four_profiles_maintain_independent_state(self):
        self.open_position("BOT_A_BASELINE", snap=snapshot(option_symbol="A"))
        self.open_position("BOT_B_MOMENTUM", snap=snapshot(option_symbol="B"))
        self.states["BOT_A_BASELINE"].virtual_trades_today = 9
        self.assertEqual(self.states["BOT_B_MOMENTUM"].virtual_trades_today, 1)
        self.assertEqual(self.states["BOT_A_BASELINE"].virtual_position.option_symbol, "A")
        self.assertEqual(self.states["BOT_B_MOMENTUM"].virtual_position.option_symbol, "B")


if __name__ == "__main__":
    unittest.main()
