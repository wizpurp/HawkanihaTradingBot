import os
import tempfile
import unittest
from unittest.mock import patch

import dashboard
from tournament.profiles import default_tournament_profile_settings, save_tournament_profile_settings
from tournament.trades import list_tournament_trades


class TournamentPipelineProofTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.trades_path = os.path.join(self.tempdir.name, "tournament_trades.json")
        self.profiles_path = os.path.join(self.tempdir.name, "missing_profiles.json")
        self.patches = [
            patch.object(dashboard, "TOURNAMENT_TRADES_FILE", self.trades_path),
            patch.object(dashboard, "TOURNAMENT_PROFILES_FILE", self.profiles_path),
            patch("tournament.execution.TOURNAMENT_TRADES_FILE", self.trades_path),
        ]
        for item in self.patches:
            item.start()
        self.client = dashboard.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tempdir.cleanup()

    def test_bot_b_and_bot_d_reach_position_opened_through_real_pipeline(self):
        response = self.client.post("/api/tournament/pipeline-proof/run", json={})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload["ok"])
        for profile_id in ("BOT_B_MOMENTUM", "BOT_D_COMBINED"):
            for direction, expected_marker, expected_type in (("CALL", "C00500000", "CALL"), ("PUT", "P00500000", "PUT")):
                row = payload["results"][profile_id][direction]
                self.assertEqual(row["preliminary_direction"], direction)
                self.assertEqual(row["decision_direction"], direction)
                self.assertIn(expected_marker, row["candidate_symbol"])
                self.assertEqual(row["candidate_type"], expected_type)
                self.assertTrue(row["contract_direction_match"])
                self.assertTrue(row["candidate_created"])
                self.assertTrue(row["momentum_confirmed"])
                self.assertTrue(row["or_confirmed"])
                self.assertTrue(row["decision_accepted"])
                self.assertTrue(row["entry_attempted"])
                self.assertTrue(row["entry_opened"])
                self.assertEqual(row["status"], "POSITION_OPENED")
                self.assertIsNotNone(row["trade_id"])

        trades = list_tournament_trades(path=self.trades_path)
        self.assertEqual(len(trades), 4)
        self.assertEqual({trade.profile_id for trade in trades}, {"BOT_B_MOMENTUM", "BOT_D_COMBINED"})
        self.assertEqual({trade.signal for trade in trades}, {"TEST_PIPELINE"})
        for trade in trades:
            self.assertIn("C00500000" if trade.direction == "CALL" else "P00500000", trade.option_symbol)

    def test_delete_pipeline_proof_trades_removes_only_test_pipeline_records(self):
        self.client.post("/api/tournament/pipeline-proof/run", json={})
        response = self.client.post("/api/tournament/pipeline-proof/delete", json={})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deleted"], 4)
        self.assertEqual(list_tournament_trades(path=self.trades_path), [])

    def test_bot_b_and_bot_d_zero_percent_momentum_proof_opens(self):
        settings = default_tournament_profile_settings()
        settings["BOT_B_MOMENTUM"]["option_momentum_percent"] = 0.0
        settings["BOT_D_COMBINED"]["option_momentum_percent"] = 0.0
        save_tournament_profile_settings(settings, self.profiles_path)

        response = self.client.post("/api/tournament/pipeline-proof/run", json={})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200, payload)
        for profile_id in ("BOT_B_MOMENTUM", "BOT_D_COMBINED"):
            self.assertEqual(payload["results"][profile_id]["CALL"]["status"], "POSITION_OPENED")
            self.assertTrue(payload["results"][profile_id]["CALL"]["momentum_confirmed"])
            self.assertEqual(payload["results"][profile_id]["PUT"]["status"], "POSITION_OPENED")
            self.assertTrue(payload["results"][profile_id]["PUT"]["momentum_confirmed"])


if __name__ == "__main__":
    unittest.main()
