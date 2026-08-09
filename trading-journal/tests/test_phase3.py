"""Phase 3 tests: conformance, descriptive reporting, blinded enrichment.

The central case in this file is `test_a_missing_component_is_not_scored_as_perfect`.
Everything else in the conformance index is arithmetic; that one is the design.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import conformance, db, enrich, metrics, reports, repo  # noqa: E402
from tests.test_journal import JournalTestCase  # noqa: E402


class TestConformanceIndex(JournalTestCase):
    def _post(self, sid, **kw):
        payload = dict(execution_quality=4, rule_adherence=4, patience=4,
                       emotional_control=4, well_traded="yes")
        payload.update(kw)
        repo.save_checkin_post(self.conn, sid, payload)

    def test_a_clean_session_scores_high_on_every_measured_component(self):
        sid = self.make_session()
        self.make_trade(sid)
        repo.record_opportunity(self.conn, sid, {
            "strategy_id": "DORB", "symbol": "MES", "qualified_at": "2026-08-07T09:59",
            "direction": "long", "status": "TAKEN"})
        self._post(sid)
        metrics.recompute_all(self.conn)

        result = conformance.compute(self.conn, sid)
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["version"], "mci@1.0.0")
        self.assertEqual(len(result["available"]), 6)

    def test_a_missing_component_is_not_scored_as_perfect(self):
        """A session with no qualified setups logged must not out-score one where
        a setup was logged and taken. Absence of evidence is not compliance."""
        logged = self.make_session("2026-08-06")
        self.make_trade(logged, entry_at="2026-08-06T10:00", exit_at="2026-08-06T10:30")
        repo.record_opportunity(self.conn, logged, {
            "strategy_id": "DORB", "symbol": "MES", "qualified_at": "2026-08-06T09:59",
            "direction": "long", "status": "MISSED",
            "status_reason": "away from the desk"})

        silent = self.make_session("2026-08-07")
        self.make_trade(silent, entry_at="2026-08-07T10:00", exit_at="2026-08-07T10:30")
        metrics.recompute_all(self.conn)

        logged_result = conformance.compute(self.conn, logged)
        silent_result = conformance.compute(self.conn, silent)

        self.assertIn("qualified_setups_taken", logged_result["available"])
        self.assertIn("qualified_setups_taken", silent_result["missing"])
        self.assertEqual(len(silent_result["available"]), 5)
        # The silent session is scored over five components, not six-with-a-free-pass.
        self.assertLess(silent_result["coverage"], logged_result["coverage"])

    def test_entry_deviation_is_always_declared_missing(self):
        sid = self.make_session()
        self.make_trade(sid)
        metrics.recompute_all(self.conn)
        result = conformance.compute(self.conn, sid)
        self.assertIn("entry_deviation", result["missing"])
        self.assertNotIn("entry_deviation", result["available"])
        self.assertEqual(result["detail"]["entry_deviation"]["status"], "not_available")
        self.assertIn("BLOCKED_ON_STRATEGY_SPEC",
                      result["detail"]["entry_deviation"]["blocked_by"])

    def test_an_empty_session_has_no_opinion(self):
        sid = self.make_session()
        metrics.recompute_all(self.conn)
        result = conformance.compute(self.conn, sid)
        self.assertIsNone(result["score"], "an empty session must not score perfectly")

    def test_violations_lower_the_score(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid)
        clean = conformance.compute(self.conn, sid)["score"]
        repo.confirm_tag(self.conn, "LATE_ENTRY", trade_id=trade_id)
        repo.confirm_tag(self.conn, "STOP_MOVED", trade_id=trade_id)
        metrics.recompute_all(self.conn)
        dirty = conformance.compute(self.conn, sid)["score"]
        self.assertLess(dirty, clean)

    def test_system_errors_do_not_count_as_indiscipline(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid)
        metrics.recompute_all(self.conn)
        before = conformance.compute(self.conn, sid)["score"]
        repo.confirm_tag(self.conn, "TECHNICAL_ERROR", trade_id=trade_id)
        metrics.recompute_all(self.conn)
        after = conformance.compute(self.conn, sid)["score"]
        self.assertEqual(before, after,
                         "a platform failure must not depress a discipline score")

    def test_trading_outside_the_declared_window_is_counted(self):
        sid = self.make_session("2026-08-07", "NY_AM")   # window 09:30-12:00
        self.make_trade(sid, entry_at="2026-08-07T10:00", exit_at="2026-08-07T10:30")
        metrics.recompute_all(self.conn)
        inside = conformance.compute(self.conn, sid)["detail"]["session_window"]["value"]
        self.assertEqual(inside, 1.0)

        late = self.make_session("2026-08-06", "NY_AM")
        self.make_trade(late, entry_at="2026-08-06T14:30", exit_at="2026-08-06T15:00")
        metrics.recompute_all(self.conn)
        outside = conformance.compute(self.conn, late)["detail"]["session_window"]["value"]
        self.assertEqual(outside, 0.0)

    def test_an_overnight_window_wraps_midnight(self):
        self.assertTrue(conformance._within_window(23 * 60, "18:00", "09:30"))
        self.assertTrue(conformance._within_window(2 * 60, "18:00", "09:30"))
        self.assertFalse(conformance._within_window(12 * 60, "18:00", "09:30"))

    def test_oversizing_counts_but_trading_smaller_does_not(self):
        big = self.make_session("2026-08-06")
        self.make_trade(big, entry_at="2026-08-06T10:00", exit_at="2026-08-06T10:30",
                        quantity=4, planned_quantity=2)
        small = self.make_session("2026-08-07")
        self.make_trade(small, entry_at="2026-08-07T10:00", exit_at="2026-08-07T10:30",
                        quantity=1, planned_quantity=2)
        metrics.recompute_all(self.conn)
        oversized = conformance.compute(self.conn, big)["detail"]["size_discipline"]["value"]
        undersized = conformance.compute(self.conn, small)["detail"]["size_discipline"]["value"]
        self.assertLess(oversized, 1.0)
        self.assertEqual(undersized, 1.0)

    def test_the_index_is_stored_with_its_composition(self):
        sid = self.make_session()
        self.make_trade(sid)
        self._post(sid)
        metrics.recompute_all(self.conn)
        row = self.conn.execute(
            "SELECT mechanical_conformance_index, conformance_index_version, "
            "conformance_components_available, conformance_components_missing, "
            "conformance_detail FROM session_metrics WHERE session_id=?", (sid,)).fetchone()
        self.assertIsNotNone(row["mechanical_conformance_index"])
        self.assertEqual(row["conformance_index_version"], "mci@1.0.0")
        self.assertIn("rule_violations", json.loads(row["conformance_components_available"]))
        self.assertIn("entry_deviation", json.loads(row["conformance_components_missing"]))
        self.assertIn("weight", json.loads(row["conformance_detail"])["rule_violations"])

    def test_the_two_process_views_stay_separate(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid)
        repo.confirm_tag(self.conn, "LATE_ENTRY", trade_id=trade_id)
        self._post(sid, execution_quality=5, rule_adherence=5, patience=5,
                   emotional_control=5)
        metrics.recompute_all(self.conn)
        row = self.conn.execute(
            "SELECT self_reported_process_index s, mechanical_conformance_index m "
            "FROM session_metrics WHERE session_id=?", (sid,)).fetchone()
        self.assertIsNotNone(row["s"])
        self.assertIsNotNone(row["m"])
        self.assertNotEqual(row["s"], row["m"],
                            "the two views should be able to disagree")

    def test_disagreement_is_described_without_a_verdict(self):
        self.assertIsNone(conformance.describe_disagreement(None, 80))
        self.assertEqual(conformance.describe_disagreement(95, 90)["band"], "aligned")
        self.assertEqual(conformance.describe_disagreement(95, 62)["band"], "large_difference")
        self.assertEqual(conformance.describe_disagreement(60, 90)["direction"], "record_higher")


class TestIntegrityRulesPreserved(JournalTestCase):
    """Director ruling: these two must never regress."""

    def test_user_value_added_is_null_without_a_reference_implementation(self):
        sid = self.make_session()
        self.make_trade(sid, avg_exit_price=5695.0)
        repo.record_opportunity(self.conn, sid, {
            "strategy_id": "DORB", "symbol": "MES", "qualified_at": "2026-08-07T09:59",
            "direction": "long", "status": "MISSED"})
        metrics.recompute_all(self.conn)
        row = self.conn.execute(
            "SELECT mechanical_r, user_value_added_r, total_r FROM session_metrics "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertLess(row["total_r"], 0)
        self.assertIsNone(row["mechanical_r"],
                          "a missing counterfactual must never become mechanical R = 0")
        self.assertIsNone(row["user_value_added_r"])

    def test_capture_percentage_is_null_on_losing_trades(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, avg_exit_price=5695.0, reported_mfe_price=5703.0)
        metrics.recompute_all(self.conn)
        self.assertIsNone(self.conn.execute(
            "SELECT r_captured_pct FROM trade_metrics WHERE trade_id=?",
            (trade_id,)).fetchone()["r_captured_pct"])


class TestDescriptiveReporting(JournalTestCase):
    def _week(self):
        for day in ("2026-08-05", "2026-08-06", "2026-08-07"):
            sid = self.make_session(day)
            self.make_trade(sid, entry_at=f"{day}T10:00", exit_at=f"{day}T10:30")
            repo.save_checkin_pre(self.conn, sid, {
                "energy": 4, "focus": 4, "stress": 2, "desire_to_trade": 3,
                "well_traded_definition": "A-setups only", "fill_seconds": 45})
            repo.save_checkin_post(self.conn, sid, {
                "execution_quality": 4, "rule_adherence": 4, "patience": 4,
                "emotional_control": 4, "well_traded": "yes", "fill_seconds": 100})
        metrics.recompute_all(self.conn)
        return self.conn.execute(
            "SELECT iso_week FROM session LIMIT 1").fetchone()["iso_week"]

    def test_a_weekly_report_contains_only_allowlisted_sections(self):
        payload = reports.weekly(self.conn, self._week())
        self.assertEqual(set(payload["sections"]), set(reports.ALLOWED_SECTIONS))
        for section in reports.ALLOWED_SECTIONS:
            self.assertIn(section, payload)

    def test_a_weekly_report_reports_both_process_views(self):
        payload = reports.weekly(self.conn, self._week())
        self.assertIn("self_reported_median", payload["process"])
        self.assertIn("mechanical_median", payload["process"])
        self.assertIn("aligned", payload["process"])

    def test_generation_refuses_inferential_or_blinded_content(self):
        with self.assertRaises(RuntimeError) as ctx:
            reports._assert_descriptive({"sections": [], "finding": "correlation with lunar phase"})
        self.assertIn("non-descriptive", str(ctx.exception))

    def test_generation_refuses_sections_outside_the_allowlist(self):
        with self.assertRaises(RuntimeError):
            reports._assert_descriptive({"sections": ["performance", "predictive_model"]})

    def test_a_stored_report_records_which_sections_it_used(self):
        week = self._week()
        reports.store_weekly(self.conn, week)
        row = self.conn.execute(
            "SELECT sections, generator_version, metrics FROM report WHERE period_key=?",
            (week,)).fetchone()
        self.assertEqual(set(json.loads(row["sections"])), set(reports.ALLOWED_SECTIONS))
        self.assertEqual(row["generator_version"], reports.GENERATOR_VERSION)
        self.assertNotIn("astro", row["metrics"].lower())

    def test_friction_telemetry_reports_completion_and_fill_times(self):
        self._week()
        result = reports.friction(self.conn)
        self.assertEqual(result["sessions"], 3)
        self.assertEqual(result["completion"]["morning"], 1.0)
        self.assertEqual(result["fill_seconds"]["morning_median"], 45)
        self.assertEqual(result["fill_seconds"]["evening_median"], 100)
        self.assertEqual(result["fill_seconds"]["targets"]["morning"], 60)

    def test_the_real_use_review_is_descriptive_and_knows_it_is_early(self):
        self._week()
        review = reports.real_use_review(self.conn)
        self.assertFalse(review["checkpoint_reached"])
        self.assertEqual(review["sessions"], 3)
        self.assertIn("self_reported_distribution", review)
        self.assertIn("mechanical_distribution", review)
        blob = json.dumps(review).lower()
        for forbidden in ("correlation", "significance", "astro"):
            self.assertNotIn(forbidden, blob)


# --------------------------------------------------------------------- enrichment
class StubEngine:
    """Stands in for the real astro engine, which lives in its own project."""

    feature_set = "personal_astro"
    engine_version = "stub@0.1.0"

    def compute(self, session_uid: str, date: str) -> dict:
        return {"pa_f01": 0.25, "pa_f17": 0.75}


class ReadableKeyEngine(StubEngine):
    def compute(self, session_uid: str, date: str) -> dict:
        return {"money_house_favourable": 1.0}


class ExplodingEngine(StubEngine):
    def compute(self, session_uid: str, date: str) -> dict:
        raise ValueError("ephemeris unavailable")


class TestBlindedEnrichment(JournalTestCase):
    def setUp(self):
        super().setUp()
        for date in ("2026-08-05", "2026-08-06", "2026-08-07"):
            self.make_session(date)
        self.conn.commit()

    def _writer(self):
        return db.connect(restricted=False)

    def test_features_are_stored_with_full_provenance(self):
        writer = self._writer()
        try:
            result = enrich.enrich(writer, StubEngine(),
                                   source_hypothesis="personal-state-v1")
            self.assertEqual(result["rows_written"], 6)
            row = writer.execute(
                "SELECT * FROM blinded_feature WHERE feature_key='pa_f01' LIMIT 1").fetchone()
            self.assertIsNotNone(row["session_id"])
            self.assertEqual(row["engine_version"], "stub@0.1.0")
            self.assertEqual(row["source_hypothesis"], "personal-state-v1")
            self.assertTrue(row["engine_hash"])
            self.assertTrue(row["computed_at"])
        finally:
            writer.close()

    def test_readable_feature_keys_are_refused(self):
        writer = self._writer()
        try:
            with self.assertRaises(enrich.EnrichmentError) as ctx:
                enrich.enrich(writer, ReadableKeyEngine())
            self.assertIn("opaque", str(ctx.exception))
            self.assertEqual(
                writer.execute("SELECT COUNT(*) c FROM blinded_feature").fetchone()["c"], 0)
        finally:
            writer.close()

    def test_a_failing_session_does_not_stop_the_run(self):
        writer = self._writer()
        try:
            result = enrich.enrich(writer, ExplodingEngine())
            self.assertEqual(result["rows_written"], 0)
            self.assertEqual(len(result["failures"]), 3)
        finally:
            writer.close()

    def test_rerunning_skips_sessions_already_enriched_at_that_version(self):
        writer = self._writer()
        try:
            enrich.enrich(writer, StubEngine())
            again = enrich.enrich(writer, StubEngine())
            self.assertEqual(again["rows_written"], 0)
            self.assertEqual(again["sessions_skipped_already_enriched"], 3)
        finally:
            writer.close()

    def test_enriched_features_remain_unreadable_from_the_daily_connection(self):
        writer = self._writer()
        try:
            enrich.enrich(writer, StubEngine())
        finally:
            writer.close()
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("SELECT * FROM blinded_feature").fetchall()
        payload = json.dumps(repo.journal_payload(self.conn))
        self.assertNotIn("pa_f01", payload)

    def test_coverage_reports_counts_never_values(self):
        writer = self._writer()
        try:
            enrich.enrich(writer, StubEngine())
            coverage = enrich.coverage(writer)
        finally:
            writer.close()
        self.assertEqual(coverage[0]["feature_set"], "personal_astro")
        self.assertEqual(coverage[0]["sessions"], 3)
        self.assertNotIn("value_num", json.dumps(coverage))

    def test_loading_a_bad_engine_spec_fails_clearly(self):
        for spec in ("nomodule", "tests.test_phase3:DoesNotExist", "json:dumps"):
            with self.subTest(spec=spec):
                with self.assertRaises(enrich.EnrichmentError):
                    enrich.load_engine(spec)

    def test_a_valid_engine_loads_by_dotted_path(self):
        engine = enrich.load_engine("tests.test_phase3:StubEngine")
        self.assertEqual(engine.feature_set, "personal_astro")


if __name__ == "__main__":
    unittest.main(verbosity=2)
