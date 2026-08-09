"""Phase 5: manual logical-trade capture, and the bias observation period.

The most important case here is `test_followers_imported_later_still_attach`.
It covers a defect found while writing the import-validation suite: follower
fills that arrived after the lead trade had already been grouped attached to
nothing, so copy quality reported a clean day while holding no follower data at
all. That is the normal sequence in real use — lead export first, follower
exports later — and the failure was invisible from the interface.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import bias, bias_observation as obs, bias_outcome, db, trades  # noqa: E402
from tests.test_workflow import DAY, WorkflowTestCase, at  # noqa: E402

LEGS = [
    {"action": "OPEN", "time": "10:04", "quantity": 2, "price": 23180.25, "side": "BUY"},
    {"action": "ADD", "time": "10:07", "quantity": 1, "price": 23188.50},
    {"action": "ADD", "time": "10:12", "quantity": 2, "price": 23195.75},
    {"action": "REDUCE", "time": "10:19", "quantity": 2, "price": 23214.00},
    {"action": "REDUCE", "time": "10:25", "quantity": 1, "price": 23221.50},
    {"action": "CLOSE", "time": "10:31", "quantity": 2, "price": 23208.75},
]


# =============================================================================
# Manual capture at the logical-trade level
# =============================================================================
class TestManualCapture(WorkflowTestCase):
    def log(self, legs=None, **kw):
        return trades.log_manual_trade(
            self.conn, day_date=DAY, symbol="NQ", legs=legs or LEGS, **kw)

    def test_a_scaled_trade_entered_by_hand_is_one_logical_trade(self):
        result = self.log(stop_price=23162.0)
        self.assertEqual(result["events_written"], 6)
        self.assertEqual(len(self.conn.execute(
            "SELECT id FROM logical_trade").fetchall()), 1)

    def test_it_reproduces_the_same_numbers_as_an_import(self):
        self.log(stop_price=23162.0)
        metrics = self.metrics()
        self.assertEqual(metrics["lead_pnl"], 2530.0)
        self.assertEqual(metrics["normalized_pnl"], 2530.0)
        self.assertEqual(metrics["r_status"], "COMPUTED")
        self.assertEqual(metrics["max_position"], 5)

    def test_the_position_path_is_reconstructed_from_the_legs(self):
        self.log()
        trade_id = self.only_trade()["id"]
        self.assertEqual(
            [s["position_after"] for s in trades.position_timeline(self.conn, trade_id)],
            [2, 3, 5, 3, 2, 0])

    def test_submitting_the_same_trade_twice_does_not_double_it(self):
        """A phone loses signal mid-save and the trader taps again. That must
        cost nothing."""
        self.log()
        again = self.log()
        self.assertEqual(again["events_written"], 0)
        self.assertEqual(again["already_known"], 6)
        self.assertEqual(len(self.conn.execute(
            "SELECT id FROM logical_trade").fetchall()), 1)

    def test_no_follower_fills_are_invented_from_the_lead(self):
        """The one thing manual entry must never do. Fanning the lead out by
        multiplier would be one line, and every fabricated fill would be
        indistinguishable from a real one afterwards."""
        result = self.log()
        self.assertEqual(result["followers_captured"], 0)
        follower_events = self.conn.execute(
            "SELECT COUNT(*) c FROM execution_event WHERE account_id != ?",
            (self.lead_id,)).fetchone()["c"]
        self.assertEqual(follower_events, 0)

    def test_without_a_stop_r_stays_unknown_rather_than_being_invented(self):
        self.log()
        self.assertEqual(self.metrics()["r_status"], "UNKNOWN")
        self.assertIsNone(self.metrics()["r_multiple"])

    def test_a_short_trade_infers_the_side_of_every_later_leg(self):
        """Direction is stated once. On a phone, saying "sell" six times is six
        chances to say it wrong."""
        legs = [{"action": "OPEN", "time": "10:04", "quantity": 2, "price": 23200.0,
                 "side": "SELL"},
                {"action": "ADD", "time": "10:08", "quantity": 1, "price": 23210.0},
                {"action": "CLOSE", "time": "10:20", "quantity": 3, "price": 23180.0}]
        self.log(legs=legs)
        self.assertEqual(self.only_trade()["direction"], "SHORT")
        self.assertGreater(self.metrics()["lead_pnl"], 0)

    def test_a_sequence_that_does_not_start_with_an_open_is_refused(self):
        with self.assertRaises(ValueError):
            self.log(legs=[{"action": "ADD", "time": "10:04", "quantity": 1,
                            "price": 23180.0}])

    def test_a_leg_without_a_price_is_refused_rather_than_stored_empty(self):
        broken = [dict(LEGS[0]), {"action": "ADD", "time": "10:07", "quantity": 1}]
        with self.assertRaises(ValueError):
            self.log(legs=broken)

    def test_capture_time_is_recorded_for_the_friction_report(self):
        self.log(capture_seconds=95)
        row = self.conn.execute(
            "SELECT review_seconds FROM trade_annotation").fetchone()
        self.assertEqual(row["review_seconds"], 95)


# =============================================================================
# Late-arriving follower data — the regression this phase found
# =============================================================================
class TestLateFollowerAttachment(WorkflowTestCase):
    def test_followers_imported_later_still_attach(self):
        """Lead grouped first, followers arriving afterwards. Before the fix
        these stayed orphaned and copy quality silently held nothing."""
        self.scaled_trade(self.lead_id)
        self.group()
        self.assertEqual(len(self.conn.execute(
            "SELECT id FROM logical_trade").fetchall()), 1)

        self.scaled_trade(self.followers["F1"], second=2)
        self.group()

        self.assertEqual(len(self.conn.execute(
            "SELECT id FROM logical_trade").fetchall()), 1,
            "a late follower import created a trade of its own")
        orphans = self.conn.execute(
            "SELECT COUNT(*) c FROM execution_event WHERE logical_trade_id IS NULL"
        ).fetchone()["c"]
        self.assertEqual(orphans, 0, "late follower fills were left unattached")

    def test_the_rollup_is_rebuilt_when_a_follower_arrives_late(self):
        """Attaching the events is not enough — the account rollup and copy
        quality have to be recomputed, or the trade still reads as lead-only."""
        self.scaled_trade(self.lead_id)
        self.group()
        self.assertEqual(self.metrics()["accounts_participating"], 1)

        self.scaled_trade(self.followers["F1"], second=2)
        self.group()

        self.assertEqual(self.metrics()["accounts_participating"], 2)
        rows = self.conn.execute(
            "SELECT a.label FROM account_execution ae JOIN account a ON a.id=ae.account_id"
        ).fetchall()
        self.assertIn("F1", [r["label"] for r in rows])

    def test_a_discrepancy_is_still_detected_on_a_late_follower(self):
        self.scaled_trade(self.lead_id)
        self.group()

        # F1 misses the second add entirely.
        self.event(self.followers["F1"], "OPEN", "10:04", 2, 23180.25, "BUY", 2)
        self.event(self.followers["F1"], "ADD", "10:07", 1, 23188.50, "BUY", 2)
        self.event(self.followers["F1"], "REDUCE", "10:19", 2, 23214.00, "SELL", 2)
        self.event(self.followers["F1"], "CLOSE", "10:25", 1, 23221.50, "SELL", 2)
        self.group()

        row = self.conn.execute(
            "SELECT ae.discrepancies FROM account_execution ae "
            "JOIN account a ON a.id=ae.account_id WHERE a.label='F1'").fetchone()
        self.assertTrue(row and row["discrepancies"] not in (None, "[]"),
                        "a late follower's missed add went unreported")


# =============================================================================
# Bias observation period
# =============================================================================
class TestBiasObservation(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        bias.record(self.conn, self.day_id, direction="BULLISH", thesis="sweep and reclaim",
                    invalidation="below 23140", invalidation_level=23140.0)
        self.admin = db.connect(restricted=False)

    def tearDown(self):
        self.admin.close()
        super().tearDown()

    def price(self, **kw):
        args = {"open": 23180.0, "high": 23260.0, "low": 23150.0, "close": 23240.0,
                "atr": 100.0, "source": "manual_chart_read"}
        args.update(kw)
        obs.record_price(self.admin, self.day_id, DAY, **args)

    def test_the_daily_connection_cannot_reach_the_observation_layer(self):
        """The isolation is structural: the authorizer refuses `r_` objects, so
        no view, report or screen can join to candidate labels by accident."""
        with self.assertRaises(obs.NotIsolated):
            obs.progress(self.conn)
        with self.assertRaises(obs.NotIsolated):
            obs.comparison(self.conn)

    def test_a_dry_run_stores_verdicts_without_writing_a_bias_outcome(self):
        self.price()
        result = obs.run_day(self.admin, self.day_id)

        self.assertTrue(result["ran"])
        self.assertEqual(len(result["verdicts"]), 3)
        self.assertIsNone(self.conn.execute(
            "SELECT outcome FROM daily_bias WHERE trading_day_id=?",
            (self.day_id,)).fetchone()["outcome"])
        self.assertEqual(obs.leak_check(self.admin), [])

    def test_a_day_without_a_price_is_not_run(self):
        result = obs.run_day(self.admin, self.day_id)
        self.assertFalse(result["ran"])
        self.assertIn("no session price", result["reason"])

    def test_all_three_candidates_judge_the_same_recorded_price(self):
        """A comparison where the candidates saw different prices compares
        nothing."""
        self.price()
        obs.run_day(self.admin, self.day_id)
        hashes = {r["price_hash"] for r in self.admin.execute(
            "SELECT price_hash FROM r_bias_candidate_run")}
        self.assertEqual(len(hashes), 1)

    def test_rerunning_a_day_updates_rather_than_duplicating(self):
        self.price()
        obs.run_day(self.admin, self.day_id)
        obs.run_day(self.admin, self.day_id)
        count = self.admin.execute(
            "SELECT COUNT(*) c FROM r_bias_candidate_run").fetchone()["c"]
        self.assertEqual(count, 3)

    def test_silence_is_recorded_with_its_reason_rather_than_skipped(self):
        """How often a candidate declines to judge is one of the things being
        compared, so it has to be in the data."""
        self.conn.execute("DROP TRIGGER IF EXISTS daily_bias_no_update")
        self.conn.execute("UPDATE daily_bias SET invalidation_level=NULL WHERE trading_day_id=?",
                          (self.day_id,))
        self.conn.commit()
        self.price()
        obs.run_day(self.admin, self.day_id)

        row = self.admin.execute(
            "SELECT outcome, unavailable_reason FROM r_bias_candidate_run "
            "WHERE methodology='invalidation_respected'").fetchone()
        self.assertIsNone(row["outcome"])
        self.assertIn("invalidation level", row["unavailable_reason"])

    def test_progress_counts_only_days_with_both_a_bias_and_a_price(self):
        self.assertEqual(obs.progress(self.admin)["complete_days"], 0)
        self.price()
        state = obs.progress(self.admin)
        self.assertEqual(state["complete_days"], 1)
        self.assertEqual(state["target_days"], 20)
        self.assertFalse(state["ready_for_selection"])

    def test_the_comparison_ranks_nothing_and_names_no_favourite(self):
        self.price()
        obs.run_day(self.admin, self.day_id)
        report = obs.comparison(self.admin)

        self.assertEqual(report["ranking"],
                         "NONE — the director selects; this report does not recommend")
        self.assertIn("profit and loss", report["not_used"])
        self.assertFalse(report["confirmations"]["BIAS_OUTCOMES_ASSIGNED"])
        self.assertIsNone(report["confirmations"]["ACTIVE_METHODOLOGY"])

    def test_silence_is_never_counted_as_disagreement(self):
        """A candidate that declined to judge did not disagree with one that
        did. Conflating them would make the quietest candidate look the most
        contrarian."""
        self.price(high=None, low=None)   # excursion and invalidation go silent
        obs.run_day(self.admin, self.day_id)
        report = obs.comparison(self.admin)

        for pair, stats in report["pairwise"].items():
            with self.subTest(pair=pair):
                self.assertEqual(stats["disagree"], 0)

    def test_no_methodology_is_approved_during_the_observation_period(self):
        self.assertIsNone(bias_outcome.ACTIVE_METHODOLOGY)
        self.price()
        obs.run_day(self.admin, self.day_id)
        self.assertEqual(obs.comparison(self.admin)["status"], "OBSERVING")

    def test_candidate_labels_never_reach_the_day_payload(self):
        """The evidence stays uncontaminated only if Zack never sees a label
        during the observation period — a label he has seen can change how he
        reads tomorrow."""
        from journal import repo

        self.price()
        obs.run_day(self.admin, self.day_id)
        payload = repr(repo.day_journal_payload(self.conn, demo=False))

        for name in bias_outcome.METHODOLOGIES:
            self.assertNotIn(name, payload)
        self.assertNotIn("PARTIALLY_CORRECT", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
