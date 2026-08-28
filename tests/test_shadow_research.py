import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import dashboard
from shadow_research import recorder


def signal(direction="CALL"):
    bullish_score = 5 if direction == "CALL" else 1
    bearish_score = 1 if direction == "CALL" else 5
    decision = "BUY CALL" if direction == "CALL" else "BUY PUT"
    return {
        "symbol": "SPY",
        "decision": decision,
        "current_signal": direction,
        "price": 500.0,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "confidence": abs(bullish_score - bearish_score),
        "dominance_percent": 83.3,
        "market_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "ema_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "ema_value": 499.5,
        "ema_slope": 0.2,
        "ma_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "ma_value": 499.0,
        "macd_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "macd_value": 0.4,
        "macd_signal_value": 0.3,
        "macd_histogram": 0.1,
        "macd_histogram_slope": 0.02,
        "vwap": 498.0,
        "volume_state": "BULLISH",
        "tick_statistics": {
            "green_ticks": 7,
            "red_ticks": 3,
            "green_percent": 70.0,
            "red_percent": 30.0,
        },
        "levels": {
            "opening_range_high": 499.0,
            "opening_range_low": 497.0,
            "previous_day_high": 501.0,
            "previous_day_low": 496.0,
            "previous_week_high": 505.0,
            "previous_week_low": 490.0,
        },
    }


def contract(symbol="SPY260820C00500000", bid=1.0, ask=1.1, last=1.05):
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "last": last,
    }


class ShadowResearchRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "shadow_candidates.csv")
        self.quote_path = os.path.join(self.tempdir.name, "shadow_candidate_quotes.csv")
        self.now = datetime(2026, 8, 20, 10, 0, 0)

    def tearDown(self):
        self.tempdir.cleanup()

    def quote_provider(self, price):
        return lambda symbol: {"symbol": symbol, "bid": price - 0.05, "ask": price + 0.05, "last": price}

    def test_candidate_creation_does_not_place_tradier_orders(self):
        with patch.object(dashboard, "scanner_market_is_open", return_value=True), \
             patch.object(dashboard.requests, "post") as post_mock, \
             patch.object(dashboard, "SHADOW_CANDIDATES_FILE", self.path), \
             patch.object(dashboard, "SHADOW_CANDIDATE_QUOTES_FILE", self.quote_path):
            result = dashboard.update_shadow_research(signal(), contract(), 500.0)

        self.assertTrue(result["created"])
        post_mock.assert_not_called()
        self.assertEqual(len(recorder.load_candidates(self.path)), 1)

    def test_candidate_creation_does_not_alter_real_entry_decision(self):
        original = signal()
        copied = dict(original)
        recorder.record_shadow_research(
            copied,
            {"CALL": contract()},
            self.quote_provider(1.05),
            500.0,
            True,
            1000,
            self.now,
            self.path,
            self.quote_path,
        )
        self.assertEqual(copied["decision"], original["decision"])
        self.assertEqual(copied["current_signal"], original["current_signal"])

    def test_candidate_creation_does_not_alter_tournament_decisions(self):
        original_decisions = {"BOT_A_BASELINE": {"decision": "BUY CALL"}}
        with patch.object(dashboard, "BOT_STATE", {**dashboard.BOT_STATE, "tournament_decisions": original_decisions}):
            recorder.record_shadow_research(
                signal(),
                {"CALL": contract()},
                self.quote_provider(1.05),
                500.0,
                True,
                1000,
                self.now,
                self.path,
            )
            self.assertEqual(dashboard.BOT_STATE["tournament_decisions"], original_decisions)

    def test_same_setup_is_deduplicated(self):
        first = recorder.record_shadow_research(signal(), {"CALL": contract()}, self.quote_provider(1.05), 500.0, True, 1000, self.now, self.path, self.quote_path)
        second = recorder.record_shadow_research(signal(), {"CALL": contract()}, self.quote_provider(1.06), 500.1, True, 1001, self.now, self.path, self.quote_path)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(len(recorder.load_candidates(self.path)), 1)

    def test_opposite_setup_can_create_new_candidate(self):
        recorder.record_shadow_research(signal("CALL"), {"CALL": contract()}, self.quote_provider(1.05), 500.0, True, 1000, self.now, self.path, self.quote_path)
        result = recorder.record_shadow_research(
            signal("PUT"),
            {"PUT": contract("SPY260820P00500000")},
            self.quote_provider(1.05),
            499.0,
            True,
            1001,
            self.now,
            self.path,
            self.quote_path,
        )
        self.assertTrue(result["created"])
        self.assertEqual(len(recorder.load_candidates(self.path)), 2)

    def test_exact_option_contract_remains_locked(self):
        recorder.record_shadow_research(signal(), {"CALL": contract("SPY260820C00500000")}, self.quote_provider(1.05), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research(signal(), {"CALL": contract("SPY260820C00501000")}, self.quote_provider(1.10), 501.0, True, 1010, self.now, self.path, self.quote_path)
        rows = recorder.load_candidates(self.path)
        self.assertEqual(rows[0]["option_symbol"], "SPY260820C00500000")

    def test_mfe_and_mae_calculation_is_correct(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.10), 501.0, True, 1010, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(0.95), 499.0, True, 1020, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        self.assertAlmostEqual(float(row["mfe_percent"]), 10.0, places=3)
        self.assertAlmostEqual(float(row["mae_percent"]), 5.0, places=3)

    def test_checkpoints_work(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        for offset in (15, 30, 60, 120, 300):
            recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.02), 500.0 + offset, True, 1000 + offset, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        for offset in (15, 30, 60, 120, 300):
            self.assertNotEqual(row[f"checkpoint_{offset}_option_price"], "")
        self.assertEqual(row["status"], "COMPLETED")

    def test_plus_5_before_minus_5_labeling_is_correct(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.06), 501.0, True, 1010, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(0.94), 499.0, True, 1300, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        self.assertEqual(str(row["hit_plus_5_before_minus_5"]).lower(), "true")

    def test_plus_8_before_minus_4_labeling_is_correct(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(0.95), 499.0, True, 1010, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.09), 501.0, True, 1300, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        self.assertEqual(str(row["hit_plus_8_before_minus_4"]).lower(), "false")

    def test_adverse_first_then_plus_15_classifies_recovery(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(0.94), 499.0, True, 1019, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.156), 502.0, True, 1238, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.156), 502.0, True, 1300, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        self.assertEqual(row["classification"], "ADVERSE_FIRST_RECOVERY")
        self.assertEqual(str(row["hit_plus_5_before_minus_5"]).lower(), "false")

    def test_plus_10_before_minus_5_classifies_clean_strong(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.10), 502.0, True, 1030, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.10), 502.0, True, 1300, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        self.assertEqual(row["classification"], "CLEAN_STRONG_CONTINUATION")
        self.assertEqual(str(row["hit_plus_5_before_minus_5"]).lower(), "true")

    def test_mae_dashboard_display_uses_negative_sign(self):
        self.assertEqual(dashboard.fmt_adverse_percent(14.2), "-14.20%")

    def test_first_threshold_hit_timestamps_are_preserved(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.06), 501.0, True, 1010, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.08), 502.0, True, 1030, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        self.assertEqual(int(float(row["time_to_plus_5_seconds"])), 10)
        self.assertEqual(float(row["first_plus_5_epoch"]), 1010)

    def test_mfe_timestamp_updates_only_on_new_favorable_extreme(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.05), 501.0, True, 1010, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.04), 501.0, True, 1020, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(1.08), 502.0, True, 1030, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        self.assertEqual(int(float(row["time_to_maximum_favorable_excursion_seconds"])), 30)

    def test_mae_timestamp_updates_only_on_new_adverse_extreme(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(0.98), 499.0, True, 1010, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(0.99), 499.0, True, 1020, self.now, self.path, self.quote_path)
        recorder.record_shadow_research({"decision": "DO NOTHING"}, {}, self.quote_provider(0.95), 498.0, True, 1030, self.now, self.path, self.quote_path)
        row = recorder.load_candidates(self.path)[0]
        self.assertEqual(int(float(row["time_to_maximum_adverse_excursion_seconds"])), 30)

    def test_quote_path_remains_locked_to_candidate_contract(self):
        recorder.record_shadow_research(signal(), {"CALL": contract("SPY260820C00500000", bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        recorder.record_shadow_research(signal(), {"CALL": contract("SPY260820C00501000", bid=1.2, ask=1.2)}, self.quote_provider(1.1), 501.0, True, 1010, self.now, self.path, self.quote_path)
        quote_rows = recorder.load_quote_path(self.quote_path)
        self.assertTrue(quote_rows)
        self.assertTrue(all(row["option_symbol"] == "SPY260820C00500000" for row in quote_rows))

    def test_quote_path_does_not_trigger_broker_orders(self):
        with patch.object(dashboard.requests, "post") as post_mock:
            recorder.record_shadow_research(signal(), {"CALL": contract()}, self.quote_provider(1.05), 500.0, True, 1000, self.now, self.path, self.quote_path)
        post_mock.assert_not_called()

    def test_restart_recovery_finalizes_old_candidate(self):
        recorder.record_shadow_research(signal(), {"CALL": contract(bid=1.0, ask=1.0)}, self.quote_provider(1.0), 500.0, True, 1000, self.now, self.path, self.quote_path)
        changed = recorder.recover_shadow_candidates(
            self.quote_provider(1.04),
            lambda symbol: 501.0,
            True,
            1301,
            self.path,
            self.quote_path,
        )
        row = recorder.load_candidates(self.path)[0]
        self.assertTrue(changed)
        self.assertEqual(row["status"], "COMPLETED")

    def test_market_closed_creates_no_candidates(self):
        result = recorder.record_shadow_research(signal(), {"CALL": contract()}, self.quote_provider(1.05), 500.0, False, 1000, self.now, self.path, self.quote_path)
        self.assertFalse(result["created"])
        self.assertEqual(recorder.load_candidates(self.path), [])


if __name__ == "__main__":
    unittest.main()
