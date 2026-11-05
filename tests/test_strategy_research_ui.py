import copy
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard
from tournament.profiles import default_tournament_profile_settings


class StrategyResearchUITests(unittest.TestCase):
    def test_metrics_handle_winners_losers_breakeven(self):
        stats = dashboard.reporting_trade_statistics([20, 10, -4, -6, 0, None, float("nan")])
        self.assertEqual(stats["Trades"], 5)
        self.assertEqual(stats["Wins"], 2)
        self.assertEqual(stats["Losses"], 2)
        self.assertEqual(stats["Win Rate"], "40.00%")
        self.assertEqual(stats["Average Winner"], dashboard.fmt_money(15))
        self.assertEqual(stats["Average Loser"], dashboard.fmt_money(-5))
        self.assertEqual(stats["Largest Winner"], dashboard.fmt_money(20))
        self.assertEqual(stats["Largest Loser"], dashboard.fmt_money(-6))

    def test_empty_metrics_do_not_invent_winners(self):
        stats = dashboard.reporting_trade_statistics([])
        self.assertEqual(stats["Average Winner"], "N/A")
        self.assertEqual(stats["Largest Loser"], "N/A")
        self.assertEqual(stats["Trades"], 0)

    def test_comparison_uses_broker_fills_and_preserves_human(self):
        rows = [{"Action": "SELL", "Source": "BOT", "Symbol": "CALL", "Qty": 1,
                 "Price": .42, "PnL": -9, "EntryBrokerFillPrice": .51, "ExitBrokerFillPrice": .51},
                {"Action": "SELL", "Source": "HUMAN", "PnL": 20}]
        original = copy.deepcopy(rows)
        markup = dashboard.render_human_bot_comparison(rows, {})
        self.assertIn("ORIGINAL BOT", markup)
        self.assertIn("$20.00", markup)
        self.assertNotIn("$-9.00", markup)
        self.assertEqual(rows, original)

    def test_four_profiles_describe_actual_filters_and_positions(self):
        settings = default_tournament_profile_settings()
        original = copy.deepcopy(settings)
        state = {"BOT_B_MOMENTUM": {"virtual_position": {"option_symbol": "SPY_REAL_CALL",
                 "direction": "CALL", "contracts": 1, "entry_price": 1.2}}}
        markup = dashboard.render_strategy_research_cards([], settings, state)
        for name in ("BOT A — BASELINE", "BOT B — MOMENTUM", "BOT C — BREAKOUT", "BOT D — COMBINED"):
            self.assertIn(name, markup)
        self.assertIn("SPY_REAL_CALL", markup)
        self.assertIn("Momentum: ON · Opening-range confirmation: OFF", markup)
        self.assertIn("Momentum: OFF · Opening-range confirmation: ON", markup)
        self.assertEqual(settings, original)

    def test_live_settings_override_experiment_label(self):
        settings = default_tournament_profile_settings()
        settings["BOT_B_MOMENTUM"]["option_momentum_confirmation_enabled"] = False
        markup = dashboard.render_strategy_research_cards([], settings, {})
        section = markup.split("BOT B — MOMENTUM")[1].split("</article>")[0]
        self.assertIn("Momentum: OFF", section)

    def test_all_records_counted_but_open_and_synthetic_excluded(self):
        row = {"profile_id": "BOT_A_BASELINE", "status": "CLOSED", "pnl_dollars": 1, "option_symbol": "REAL"}
        rows = [dict(row) for _ in range(60)]
        rows += [dict(row, pnl_dollars=999, is_test_position=True), dict(row, pnl_dollars=999, signal="TEST_RECORD"),
                 dict(row, pnl_dollars=999, status="OPEN")]
        markup = dashboard.render_strategy_research_cards(rows, default_tournament_profile_settings(), {})
        self.assertIn("$60.00", markup)
        self.assertNotIn("$999.00", markup)

    def test_report_endpoint_only_reads_reporting_data(self):
        with patch.object(dashboard, "load_config", return_value={}), \
             patch.object(dashboard, "load_tournament_profile_settings", return_value=default_tournament_profile_settings()), \
             patch.object(dashboard, "get_recent_trades", return_value=[]), \
             patch.object(dashboard, "list_tournament_trades", return_value=[]) as trades, \
             patch.object(dashboard, "submit_option_order", side_effect=AssertionError("Order forbidden")) as order:
            response = dashboard.app.test_client().get("/api/strategy-research-display")
        self.assertEqual(response.status_code, 200)
        self.assertIn("research_html", response.json)
        self.assertNotIn("limit", trades.call_args.kwargs)
        order.assert_not_called()

    def test_layout_order_and_collapsed_debug_are_preserved(self):
        source = Path(dashboard.__file__).read_text(encoding="utf-8")
        self.assertLess(source.index("<h2>HUMAN VS BOT"), source.index("<h2>STRATEGY RESEARCH"))
        self.assertLess(source.index("<h2>STRATEGY RESEARCH"), source.index("<h2>Status"))
        self.assertIn('<details><summary><strong>Advanced Debug — Tournament Decisions', source)
        self.assertIn('<details><summary><strong>Advanced Debug — Momentum Candidate Transitions', source)
        self.assertIn('id="tournament-decisions-raw-body"', source)
        self.assertIn('reporting.research_html', source)

    def test_original_execution_reporting_remains_visible(self):
        markup = dashboard.render_execution_performance({})
        self.assertIn("All Broker Trades = actual broker results", markup)
        self.assertIn("Clean Strategy Trades = strategy evaluation excluding execution anomalies", markup)
