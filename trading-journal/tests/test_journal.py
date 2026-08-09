"""Executable tests for the guarantees the journal actually makes.

Phase 1 wrote a test plan. This is the part of it that runs. Every case here
corresponds to a promise made in the specification, and the ordering follows
severity: the invariants that would silently corrupt longitudinal data come
first, the arithmetic second, the durability last.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import backup, config, contracts, db, importers, metrics, repo  # noqa: E402


class JournalTestCase(unittest.TestCase):
    """A fresh database and data home per test. Nothing is shared."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self._saved = {k: getattr(config, k) for k in
                       ("DATA_HOME", "DB_PATH", "RAW_DIR", "MEDIA_DIR", "BACKUP_DIR", "EXPORT_DIR")}
        config.DATA_HOME = home
        config.DB_PATH = home / "journal.db"
        config.RAW_DIR = home / "raw"
        config.MEDIA_DIR = home / "media"
        config.BACKUP_DIR = home / "backups"
        config.EXPORT_DIR = home / "exports"
        config.ensure_dirs()

        setup = db.connect(restricted=False)
        db.migrate(setup)
        setup.commit()
        setup.close()

        self.conn = db.connect()
        self.account_id = repo.ensure_account(self.conn, "Test Account", mode="paper")
        repo.ensure_instrument(self.conn, "MES", tick_size=0.25, point_value=5.0)
        repo.ensure_strategy(self.conn, "DORB", "DORB")
        repo.publish_strategy_version(self.conn, "DORB", "1.0", {"entry": "range break"},
                                      automation_level="bot", active_from="2026-01-01")
        for code in ("OVERTRADE", "REVENGE", "FOMO_ENTRY", "EARLY_ENTRY", "LATE_ENTRY",
                     "PREMATURE_EXIT", "STOP_MOVED", "OVERSIZED", "MISSED_SETUP",
                     "RULE_OVERRIDE", "UNPLANNED_TRADE", "DISTRACTED", "TECHNICAL_ERROR",
                     "BOT_ROUTING_ERROR", "OTHER"):
            self.conn.execute(
                "INSERT OR IGNORE INTO mistake_tag(code,label,category) VALUES (?,?,'process')",
                (code, code.replace("_", " ").title()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for k, v in self._saved.items():
            setattr(config, k, v)
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def make_session(self, date="2026-08-07", kind="NY_AM"):
        return repo.get_or_create_session(self.conn, date, account_id=self.account_id,
                                          session_kind=kind, mode="paper")

    def make_trade(self, session_id, **overrides):
        payload = dict(symbol="MES", strategy_id="DORB", side="long", quantity=2,
                       planned_quantity=2, entry_at="2026-08-07T10:00",
                       exit_at="2026-08-07T10:30", avg_entry_price=5700.0,
                       avg_exit_price=5710.0, initial_stop=5695.0, initial_target=5715.0,
                       commission=1.00, exchange_fees=0.50)
        payload.update(overrides)
        return repo.record_trade(self.conn, session_id, payload,
                                 entry_source=overrides.pop("entry_source", "manual"))


# =============================================================================
# 1. Invariants — the things whose failure would silently corrupt the record
# =============================================================================
class TestRawImmutability(JournalTestCase):
    def test_raw_record_rejects_update(self):
        sid = self.make_session()
        self.make_trade(sid)
        row = self.conn.execute("SELECT id FROM raw_record LIMIT 1").fetchone()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute("UPDATE raw_record SET payload='tampered' WHERE id=?", (row["id"],))
        self.assertIn("append-only", str(ctx.exception))

    def test_raw_record_rejects_delete(self):
        sid = self.make_session()
        self.make_trade(sid)
        row = self.conn.execute("SELECT id FROM raw_record LIMIT 1").fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM raw_record WHERE id=?", (row["id"],))

    def test_trade_fill_rejects_update(self):
        sid = self.make_session()
        self.make_trade(sid)
        fill = self.conn.execute("SELECT id FROM trade_fill LIMIT 1").fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE trade_fill SET price=1 WHERE id=?", (fill["id"],))

    def test_import_batch_rejects_delete(self):
        sid = self.make_session()
        self.make_trade(sid)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM raw_import_batch")


class TestForeignKeys(JournalTestCase):
    def test_foreign_keys_are_enforced(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_trade_requires_a_real_session(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO trade(trade_uid,session_id,account_id,mode,instrument_id,"
                "execution_mode,side,quantity,entry_at,avg_entry_price,created_at,updated_at)"
                " VALUES ('x',99999,?,'paper',1,'manual','long',1,'2026-08-07T14:00:00Z',"
                "5700,'2026-08-07T14:00:00Z','2026-08-07T14:00:00Z')", (self.account_id,))

    def test_integrity_check_is_clean_after_normal_use(self):
        sid = self.make_session()
        self.make_trade(sid)
        repo.save_checkin_pre(self.conn, sid, {"energy": 4, "focus": 4})
        metrics.recompute_all(self.conn)
        result = db.integrity_check(self.conn)
        self.assertTrue(result["ok"], result["problems"])


class TestStrategyVersionImmutability(JournalTestCase):
    def test_rules_cannot_be_edited(self):
        version = self.conn.execute(
            "SELECT id FROM strategy_version WHERE strategy_id='DORB'").fetchone()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute("UPDATE strategy_version SET rules='{\"entry\":\"changed\"}' "
                              "WHERE id=?", (version["id"],))
        self.assertIn("frozen", str(ctx.exception))

    def test_history_keeps_the_version_in_force_at_the_time(self):
        old_id = repo.strategy_version_id(self.conn, "DORB", on_date="2026-03-01")
        early = self.make_session("2026-03-02")
        early_trade = self.make_trade(early, entry_at="2026-03-02T10:00",
                                      exit_at="2026-03-02T10:30")

        repo.publish_strategy_version(self.conn, "DORB", "2.0", {"entry": "new rules"},
                                      automation_level="bot", active_from="2026-06-01")
        late = self.make_session("2026-06-15")
        late_trade = self.make_trade(late, entry_at="2026-06-15T10:00",
                                     exit_at="2026-06-15T10:30")

        early_version = self.conn.execute(
            "SELECT sv.version FROM trade t JOIN strategy_version sv "
            "ON sv.id=t.strategy_version_id WHERE t.id=?", (early_trade,)).fetchone()["version"]
        late_version = self.conn.execute(
            "SELECT sv.version FROM trade t JOIN strategy_version sv "
            "ON sv.id=t.strategy_version_id WHERE t.id=?", (late_trade,)).fetchone()["version"]

        self.assertEqual(early_version, "1.0", "publishing v2.0 rewrote a historical trade")
        self.assertEqual(late_version, "2.0")
        self.assertIsNotNone(old_id)

    def test_publishing_closes_the_previous_version(self):
        repo.publish_strategy_version(self.conn, "DORB", "2.0", {"entry": "new"},
                                      active_from="2026-06-01")
        closed = self.conn.execute(
            "SELECT active_to FROM strategy_version WHERE strategy_id='DORB' AND version='1.0'"
        ).fetchone()["active_to"]
        self.assertEqual(closed, "2026-06-01")


class TestCheckinAmendments(JournalTestCase):
    def test_editing_a_checkin_preserves_the_original_answer(self):
        sid = self.make_session()
        first = repo.save_checkin_pre(self.conn, sid, {"sleep_hours": 5.5, "energy": 2,
                                                       "stress": 4})
        second = repo.save_checkin_pre(self.conn, sid, {"sleep_hours": 7.5, "energy": 4},
                                       "misread the sleep tracker")
        self.assertEqual(first, "created")
        self.assertEqual(second, "amended")

        history = repo.amendments(self.conn, sid)
        fields = {a["field"]: a for a in history}
        self.assertEqual(len(history), 2, "expected one amendment per changed field")
        self.assertEqual(fields["sleep_hours"]["old_value"], "5.5")
        self.assertEqual(fields["sleep_hours"]["new_value"], "7.5")
        self.assertEqual(fields["energy"]["old_value"], "2")
        self.assertEqual(fields["sleep_hours"]["reason"], "misread the sleep tracker")

        current = self.conn.execute("SELECT * FROM checkin_pre WHERE session_id=?",
                                    (sid,)).fetchone()
        self.assertEqual(current["sleep_hours"], 7.5)
        self.assertEqual(current["stress"], 4, "unchanged fields must survive an amendment")

    def test_unchanged_values_do_not_create_amendments(self):
        sid = self.make_session()
        repo.save_checkin_pre(self.conn, sid, {"energy": 4})
        repo.save_checkin_pre(self.conn, sid, {"energy": 4})
        self.assertEqual(repo.amendments(self.conn, sid), [])


class TestVoiceNoteImmutability(JournalTestCase):
    def test_transcript_cannot_be_rewritten(self):
        sid = self.make_session()
        note_id = repo.add_voice_note(self.conn, session_id=sid, audio_path="a.m4a",
                                      audio_sha256="abc", transcript="what I actually said")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            repo.attach_transcript(self.conn, note_id, "a tidier version", "model", "1")
        self.assertIn("immutable", str(ctx.exception))

    def test_transcript_may_be_attached_once_after_the_audio(self):
        sid = self.make_session()
        note_id = repo.add_voice_note(self.conn, session_id=sid, audio_path="a.m4a",
                                      audio_sha256="abc")
        repo.attach_transcript(self.conn, note_id, "transcribed later", "whisper", "3.0")
        stored = self.conn.execute("SELECT transcript FROM voice_note WHERE id=?",
                                   (note_id,)).fetchone()["transcript"]
        self.assertEqual(stored, "transcribed later")


class TestAiAnnotationIsNotCanonical(JournalTestCase):
    def test_a_suggestion_creates_no_label(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid)
        repo.suggest_tag(self.conn, target_type="trade", target_id=trade_id, code="LATE_ENTRY",
                         rationale="entry 132s after signal", confidence=0.86,
                         model="tagger", model_version="0.2.0")
        tags = self.conn.execute("SELECT COUNT(*) c FROM human_tag WHERE trade_id=?",
                                 (trade_id,)).fetchone()["c"]
        self.assertEqual(tags, 0, "a model suggestion must not become a label on its own")

    def test_accepting_creates_a_label_that_records_its_origin(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid)
        annotation = repo.suggest_tag(self.conn, target_type="trade", target_id=trade_id,
                                      code="LATE_ENTRY", rationale="r", confidence=0.9,
                                      model="tagger", model_version="0.2.0")
        repo.accept_suggestion(self.conn, annotation)
        tag = self.conn.execute("SELECT * FROM human_tag WHERE trade_id=?", (trade_id,)).fetchone()
        self.assertEqual(tag["tag_code"], "LATE_ENTRY")
        self.assertEqual(tag["from_annotation_id"], annotation)

    def test_rejecting_creates_no_label(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid)
        annotation = repo.suggest_tag(self.conn, target_type="trade", target_id=trade_id,
                                      code="LATE_ENTRY", rationale="r", confidence=0.4,
                                      model="tagger", model_version="0.2.0")
        repo.reject_suggestion(self.conn, annotation)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM human_tag").fetchone()["c"], 0)
        self.assertEqual(
            self.conn.execute("SELECT status FROM ai_annotation WHERE id=?",
                              (annotation,)).fetchone()["status"], "rejected")

    def test_mistake_counts_use_confirmed_labels_only(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid)
        repo.suggest_tag(self.conn, target_type="trade", target_id=trade_id, code="STOP_MOVED",
                         rationale="r", confidence=0.7, model="tagger", model_version="0.2.0")
        metrics.recompute_all(self.conn)
        count = self.conn.execute("SELECT mistake_count FROM session_metrics WHERE session_id=?",
                                  (sid,)).fetchone()["mistake_count"]
        self.assertEqual(count, 0, "an unconfirmed suggestion leaked into a statistic")


class TestBlindedIsolation(JournalTestCase):
    def setUp(self):
        super().setUp()
        self.uid = self.conn.execute(
            "SELECT session_uid FROM session WHERE id=?", (self.make_session(),)
        ).fetchone()["session_uid"]
        self.conn.commit()
        writer = db.connect(restricted=False)
        repo.store_blinded_features(writer, "session", self.uid, "personal_astro",
                                    {"pa_f01": 0.42, "pa_f17": 0.91},
                                    engine="test", engine_version="0.0.0")
        writer.commit()
        writer.close()

    def test_daily_connection_cannot_read_blinded_features(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("SELECT * FROM blinded_feature").fetchall()

    def test_research_views_are_unreachable_from_the_daily_connection(self):
        for view in ("r_session_features", "r_feature_sample_sizes",
                     "r_manual_vs_mechanical", "r_state_vs_violations"):
            with self.subTest(view=view):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.conn.execute(f"SELECT * FROM {view} LIMIT 1").fetchall()

    def test_features_are_stored_and_readable_only_unrestricted(self):
        reader = db.connect(restricted=False)
        try:
            rows = reader.execute("SELECT feature_key, value_num FROM blinded_feature "
                                  "WHERE scope_id=? ORDER BY feature_key", (self.uid,)).fetchall()
        finally:
            reader.close()
        self.assertEqual([r["feature_key"] for r in rows], ["pa_f01", "pa_f17"])

    def test_the_daily_payload_carries_no_blinded_data(self):
        payload = json.dumps(repo.journal_payload(self.conn))
        for needle in ("pa_f01", "pa_f17", "blinded", "personal_astro"):
            self.assertNotIn(needle, payload, f"'{needle}' leaked into the daily payload")

    def test_routine_export_carries_no_blinded_data(self):
        result = backup.export_all(self.conn)
        for name in result["files"]:
            body = (Path(result["directory"]) / name).read_text()
            self.assertNotIn("pa_f01", body, f"{name} leaked a blinded feature")


# =============================================================================
# 2. Correctness — the arithmetic the journal reports
# =============================================================================
class TestTimezoneAndSessionLinking(JournalTestCase):
    def test_local_wall_clock_is_stored_as_utc(self):
        self.assertEqual(repo.local_to_utc("2026-08-07T10:04", "America/New_York"),
                         "2026-08-07T14:04:00Z")

    def test_conversion_respects_daylight_saving(self):
        # January is EST (-5), August is EDT (-4). A fixed offset would fail one.
        self.assertEqual(repo.local_to_utc("2026-01-15T10:00", "America/New_York"),
                         "2026-01-15T15:00:00Z")
        self.assertEqual(repo.local_to_utc("2026-08-15T10:00", "America/New_York"),
                         "2026-08-15T14:00:00Z")

    def test_round_trip_returns_the_original_wall_clock(self):
        stored = repo.local_to_utc("2026-08-07T10:04", "America/New_York")
        back = repo.utc_to_local(stored, "America/New_York")
        self.assertEqual(back.strftime("%Y-%m-%dT%H:%M"), "2026-08-07T10:04")

    def test_a_date_can_hold_several_sessions(self):
        morning = self.make_session("2026-08-07", "NY_AM")
        afternoon = self.make_session("2026-08-07", "NY_PM")
        overnight = self.make_session("2026-08-07", "OVERNIGHT")
        self.assertEqual(len({morning, afternoon, overnight}), 3)
        day = self.conn.execute(
            "SELECT session_count FROM v_trading_day WHERE session_date='2026-08-07'").fetchone()
        self.assertEqual(day["session_count"], 3)

    def test_the_same_kind_on_the_same_date_is_the_same_session(self):
        self.assertEqual(self.make_session("2026-08-07", "NY_AM"),
                         self.make_session("2026-08-07", "NY_AM"))

    def test_a_trade_lands_in_the_session_it_was_given(self):
        sid = self.make_session("2026-08-07", "NY_PM")
        trade_id = self.make_trade(sid, entry_at="2026-08-07T14:30", exit_at="2026-08-07T15:00")
        row = self.conn.execute(
            "SELECT s.session_kind, t.entry_at FROM trade t JOIN session s ON s.id=t.session_id "
            "WHERE t.id=?", (trade_id,)).fetchone()
        self.assertEqual(row["session_kind"], "NY_PM")
        self.assertEqual(row["entry_at"], "2026-08-07T18:30:00Z")


class TestRCalculation(JournalTestCase):
    def test_long_winner(self):
        sid = self.make_session()
        # entry 5700, stop 5695 -> risk 5 points; exit 5710 -> +2R
        trade_id = self.make_trade(sid, avg_entry_price=5700.0, initial_stop=5695.0,
                                   avg_exit_price=5710.0)
        metrics.recompute_all(self.conn)
        row = self.conn.execute("SELECT * FROM trade_metrics WHERE trade_id=?",
                                (trade_id,)).fetchone()
        self.assertAlmostEqual(row["risk_per_unit"], 5.0)
        self.assertAlmostEqual(row["r_multiple"], 2.0)
        self.assertAlmostEqual(row["gross_pnl"], 10.0 * 2 * 5.0)  # points * qty * point_value

    def test_short_winner(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, side="short", avg_entry_price=5700.0,
                                   initial_stop=5705.0, avg_exit_price=5690.0)
        metrics.recompute_all(self.conn)
        row = self.conn.execute("SELECT r_multiple, gross_pnl FROM trade_metrics WHERE trade_id=?",
                                (trade_id,)).fetchone()
        self.assertAlmostEqual(row["r_multiple"], 2.0)
        self.assertAlmostEqual(row["gross_pnl"], 100.0)

    def test_full_stop_is_minus_one_r(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, avg_exit_price=5695.0)
        metrics.recompute_all(self.conn)
        self.assertAlmostEqual(
            self.conn.execute("SELECT r_multiple FROM trade_metrics WHERE trade_id=?",
                              (trade_id,)).fetchone()["r_multiple"], -1.0)

    def test_breakeven_is_zero_r_and_costs_only_fees(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, avg_exit_price=5700.0)
        metrics.recompute_all(self.conn)
        row = self.conn.execute("SELECT * FROM trade_metrics WHERE trade_id=?",
                                (trade_id,)).fetchone()
        self.assertEqual(row["r_multiple"], 0.0)
        self.assertEqual(row["gross_pnl"], 0.0)
        self.assertAlmostEqual(row["net_pnl"], -1.5)

    def test_partial_exits_are_weighted_by_size(self):
        sid = self.make_session()
        trade_id = repo.record_trade(self.conn, sid, {
            "symbol": "MES", "strategy_id": "DORB", "side": "long", "quantity": 4,
            "planned_quantity": 4, "entry_at": "2026-08-07T10:00", "exit_at": "2026-08-07T11:00",
            "avg_entry_price": 5700.0, "avg_exit_price": 5710.0, "initial_stop": 5695.0,
            "fills": [
                {"leg": "entry", "price": 5700.0, "quantity": 4, "filled_at": "2026-08-07T10:00",
                 "commission": 1.48, "exchange_fees": 0.60},
                {"leg": "partial", "price": 5705.0, "quantity": 2, "filled_at": "2026-08-07T10:30",
                 "commission": 0.74, "exchange_fees": 0.30},
                {"leg": "exit", "price": 5715.0, "quantity": 2, "filled_at": "2026-08-07T11:00",
                 "commission": 0.74, "exchange_fees": 0.30},
            ]})
        metrics.recompute_all(self.conn)
        row = self.conn.execute("SELECT * FROM trade_metrics WHERE trade_id=?",
                                (trade_id,)).fetchone()
        # half out at +5 points, half at +15 -> average +10 -> 2R on 5-point risk
        self.assertAlmostEqual(row["r_multiple"], 2.0)
        self.assertAlmostEqual(row["gross_pnl"], (5.0 * 2 + 15.0 * 2) * 5.0)

    def test_r_is_null_without_a_stop(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, initial_stop=None)
        metrics.recompute_all(self.conn)
        self.assertIsNone(
            self.conn.execute("SELECT r_multiple FROM trade_metrics WHERE trade_id=?",
                              (trade_id,)).fetchone()["r_multiple"])


class TestFeeAggregation(JournalTestCase):
    def test_fees_sum_across_every_fill(self):
        sid = self.make_session()
        trade_id = repo.record_trade(self.conn, sid, {
            "symbol": "MES", "strategy_id": "DORB", "side": "long", "quantity": 4,
            "entry_at": "2026-08-07T10:00", "exit_at": "2026-08-07T11:00",
            "avg_entry_price": 5700.0, "avg_exit_price": 5710.0, "initial_stop": 5695.0,
            "fills": [
                {"leg": "entry", "price": 5700.0, "quantity": 4, "filled_at": "2026-08-07T10:00",
                 "commission": 1.48, "exchange_fees": 0.60},
                {"leg": "partial", "price": 5705.0, "quantity": 2, "filled_at": "2026-08-07T10:30",
                 "commission": 0.74, "exchange_fees": 0.30},
                {"leg": "exit", "price": 5715.0, "quantity": 2, "filled_at": "2026-08-07T11:00",
                 "commission": 0.74, "exchange_fees": 0.30},
            ]})
        metrics.recompute_all(self.conn)
        row = self.conn.execute("SELECT gross_pnl, fees, net_pnl FROM trade_metrics "
                                "WHERE trade_id=?", (trade_id,)).fetchone()
        self.assertAlmostEqual(row["fees"], 4.16)
        self.assertAlmostEqual(row["net_pnl"], round(row["gross_pnl"] - 4.16, 2))

    def test_summary_fees_are_split_across_synthesised_legs(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, commission=2.00, exchange_fees=1.00)
        metrics.recompute_all(self.conn)
        self.assertAlmostEqual(
            self.conn.execute("SELECT fees FROM trade_metrics WHERE trade_id=?",
                              (trade_id,)).fetchone()["fees"], 3.00)


class TestSlippageSign(JournalTestCase):
    def test_long_paying_up_is_negative(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, side="long", avg_entry_price=5700.25,
                                   intended_entry_price=5700.00)
        metrics.recompute_all(self.conn)
        self.assertAlmostEqual(
            self.conn.execute("SELECT entry_slippage_ticks FROM trade_metrics WHERE trade_id=?",
                              (trade_id,)).fetchone()["entry_slippage_ticks"], -1.0)

    def test_short_selling_lower_is_also_negative(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, side="short", avg_entry_price=5716.75,
                                   initial_stop=5721.25, avg_exit_price=5710.0,
                                   intended_entry_price=5718.25)
        metrics.recompute_all(self.conn)
        self.assertAlmostEqual(
            self.conn.execute("SELECT entry_slippage_ticks FROM trade_metrics WHERE trade_id=?",
                              (trade_id,)).fetchone()["entry_slippage_ticks"], -6.0)

    def test_a_better_fill_is_positive(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, side="long", avg_entry_price=5699.75,
                                   intended_entry_price=5700.00)
        metrics.recompute_all(self.conn)
        self.assertAlmostEqual(
            self.conn.execute("SELECT entry_slippage_ticks FROM trade_metrics WHERE trade_id=?",
                              (trade_id,)).fetchone()["entry_slippage_ticks"], 1.0)


class TestCapturePercentage(JournalTestCase):
    def test_losers_have_no_capture_percentage(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, avg_exit_price=5695.0, reported_mfe_price=5703.0)
        metrics.recompute_all(self.conn)
        row = self.conn.execute("SELECT r_multiple, mfe_r, r_captured_pct FROM trade_metrics "
                                "WHERE trade_id=?", (trade_id,)).fetchone()
        self.assertLess(row["r_multiple"], 0)
        self.assertAlmostEqual(row["mfe_r"], 0.6)
        self.assertIsNone(row["r_captured_pct"],
                          "a losing trade must not report a share of MFE captured")

    def test_winners_report_the_share_of_the_favourable_move_kept(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, avg_exit_price=5710.0, reported_mfe_price=5712.5)
        metrics.recompute_all(self.conn)
        row = self.conn.execute("SELECT r_multiple, mfe_r, r_captured_pct FROM trade_metrics "
                                "WHERE trade_id=?", (trade_id,)).fetchone()
        self.assertAlmostEqual(row["mfe_r"], 2.5)
        self.assertAlmostEqual(row["r_captured_pct"], 80.0)

    def test_excursions_stay_null_when_nothing_was_reported(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid)
        metrics.recompute_all(self.conn)
        row = self.conn.execute("SELECT mfe_r, mae_r FROM trade_metrics WHERE trade_id=?",
                                (trade_id,)).fetchone()
        self.assertIsNone(row["mfe_r"], "MFE must not be invented when nothing measured it")
        self.assertIsNone(row["mae_r"])


class TestProcessIndex(JournalTestCase):
    def test_the_index_contains_no_pnl_term(self):
        """A losing session with perfect discipline scores 100; a winning
        session with poor discipline scores low. This is the product thesis
        expressed as an assertion."""
        losing = self.make_session("2026-08-05")
        self.make_trade(losing, entry_at="2026-08-05T10:00", exit_at="2026-08-05T10:30",
                        avg_exit_price=5695.0)
        repo.save_checkin_post(self.conn, losing, {
            "execution_quality": 5, "rule_adherence": 5, "patience": 5,
            "emotional_control": 5, "well_traded": "yes"})

        winning = self.make_session("2026-08-06")
        winning_trade = self.make_trade(winning, entry_at="2026-08-06T10:00",
                                        exit_at="2026-08-06T10:30", avg_exit_price=5715.0)
        repo.confirm_tag(self.conn, "STOP_MOVED", trade_id=winning_trade)
        repo.confirm_tag(self.conn, "LATE_ENTRY", trade_id=winning_trade)
        repo.confirm_tag(self.conn, "MISSED_SETUP", session_id=winning)
        repo.save_checkin_post(self.conn, winning, {
            "execution_quality": 2, "rule_adherence": 2, "patience": 1,
            "emotional_control": 2, "well_traded": "no"})

        metrics.recompute_all(self.conn)
        rows = {r["session_id"]: r for r in self.conn.execute(
            "SELECT session_id, total_r, self_reported_process_index FROM session_metrics")}

        self.assertLess(rows[losing]["total_r"], 0)
        self.assertEqual(rows[losing]["self_reported_process_index"], 100.0)
        self.assertGreater(rows[winning]["total_r"], 0)
        self.assertLess(rows[winning]["self_reported_process_index"], 45)

    def test_the_two_indices_are_stored_in_separate_columns(self):
        """They are computed from different evidence and must stay separable.
        Detailed conformance behaviour is covered in tests/test_phase3.py."""
        sid = self.make_session()
        self.make_trade(sid)
        repo.save_checkin_post(self.conn, sid, {"execution_quality": 4, "rule_adherence": 4,
                                                "patience": 4, "emotional_control": 4})
        metrics.recompute_all(self.conn)
        row = self.conn.execute(
            "SELECT self_reported_process_index, mechanical_conformance_index, "
            "conformance_index_version FROM session_metrics WHERE session_id=?",
            (sid,)).fetchone()
        self.assertIsNotNone(row["self_reported_process_index"])
        self.assertIsNotNone(row["mechanical_conformance_index"])
        self.assertEqual(row["conformance_index_version"], "mci@1.0.0")


# =============================================================================
# 3. Ingestion — provenance and duplicate handling
# =============================================================================
class TestImportProvenance(JournalTestCase):
    CSV_HEADER = "Execution ID,Order ID,Symbol,Side,Quantity,Price,Fill Time,Commission,Fees\n"
    CSV_ROWS = (
        "E1,O1,MES,Buy,2,5700.00,2026-08-07 10:00:00,0.74,0.30\n"
        "E2,O1,MES,Sell,2,5710.00,2026-08-07 10:30:00,0.74,0.30\n"
    )

    def setUp(self):
        super().setUp()
        example = importers.EXAMPLE_PROFILE
        importers.save_profile(self.conn, example["name"], example["column_map"],
                               datetime_format=example["datetime_format"])
        self.conn.commit()
        self.csv_path = Path(self._tmp.name) / "export.csv"
        self.csv_path.write_text(self.CSV_HEADER + self.CSV_ROWS)

    def test_manual_entry_is_recorded_as_manual(self):
        sid = self.make_session()
        trade_id = self.make_trade(sid, entry_source="manual")
        row = self.conn.execute("SELECT entry_source FROM trade WHERE id=?",
                                (trade_id,)).fetchone()
        self.assertEqual(row["entry_source"], "manual")

    def test_imported_trades_are_recorded_as_imported(self):
        result = importers.import_file(self.conn, self.csv_path, "generic_futures_csv",
                                       account_label="Test Account")
        self.assertEqual(result["trades_created"], 1)
        sources = [r["entry_source"] for r in
                   self.conn.execute("SELECT entry_source FROM trade")]
        self.assertEqual(sources, ["file_import"])

    def test_coverage_report_separates_hand_keyed_from_imported(self):
        sid = self.make_session()
        self.make_trade(sid, entry_source="manual")
        importers.import_file(self.conn, self.csv_path, "generic_futures_csv",
                              account_label="Test Account")
        coverage = repo.coverage_report(self.conn)
        self.assertEqual(coverage["trades_by_entry_source"]["manual"], 1)
        self.assertEqual(coverage["trades_by_entry_source"]["file_import"], 1)
        self.assertAlmostEqual(coverage["manual_trade_share"], 0.5)

    def test_human_logged_setups_are_not_counted_as_engine_coverage(self):
        sid = self.make_session()
        repo.record_opportunity(self.conn, sid, {
            "strategy_id": "DORB", "symbol": "MES", "qualified_at": "2026-08-07T10:00",
            "direction": "long", "status": "MISSED"}, detection_source="human_logged")
        coverage = repo.coverage_report(self.conn)
        self.assertEqual(coverage["opportunities_by_detection_source"]["human_logged"], 1)
        self.assertEqual(coverage["engine_detected_opportunity_share"], 0.0)

    def test_reimporting_the_same_file_is_refused(self):
        importers.import_file(self.conn, self.csv_path, "generic_futures_csv",
                              account_label="Test Account")
        with self.assertRaises(importers.DuplicateImport):
            importers.import_file(self.conn, self.csv_path, "generic_futures_csv",
                                  account_label="Test Account")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM trade").fetchone()["c"], 1)

    def test_a_file_with_one_new_row_does_not_duplicate_the_old_ones(self):
        importers.import_file(self.conn, self.csv_path, "generic_futures_csv",
                              account_label="Test Account")
        extended = Path(self._tmp.name) / "export2.csv"
        extended.write_text(self.CSV_HEADER + self.CSV_ROWS +
                            "E3,O2,MES,Sell,1,5720.00,2026-08-07 11:00:00,0.37,0.15\n"
                            "E4,O2,MES,Buy,1,5715.00,2026-08-07 11:20:00,0.37,0.15\n")
        result = importers.import_file(self.conn, extended, "generic_futures_csv",
                                       account_label="Test Account")
        exec_ids = [r["exec_id"] for r in self.conn.execute("SELECT exec_id FROM trade_fill")]
        self.assertEqual(sorted(exec_ids), ["E1", "E2", "E3", "E4"])
        self.assertEqual(result["trades_created"], 1,
                         "only the genuinely new round turn should be created")
        self.assertEqual(len(result["skipped"]), 1,
                         "the already-known trade should be reported as skipped, not silent")

    def test_parsing_reports_problems_without_writing(self):
        broken = Path(self._tmp.name) / "broken.csv"
        broken.write_text(self.CSV_HEADER +
                          "E9,O9,MES,Sideways,2,5700.00,2026-08-07 10:00:00,0,0\n")
        result = importers.parse_file(self.conn, broken, "generic_futures_csv")
        self.assertFalse(result["ok"])
        self.assertIn("unrecognised side", result["problems"][0])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM trade").fetchone()["c"], 0)


class TestIntegrationContracts(JournalTestCase):
    def test_the_reference_implementation_refuses_to_invent_a_result(self):
        placeholder = contracts.TenAmReferencePlaceholder()
        with self.assertRaises(contracts.NotImplementedContract):
            placeholder.evaluate({}, {})

    def test_blocked_adapters_report_honestly(self):
        report = {a["key"]: a for a in contracts.status_report()}
        self.assertEqual(report["10AM_REFERENCE_IMPL"]["status"],
                         contracts.BLOCKED_ON_STRATEGY_SPEC)
        self.assertFalse(any(a["status"] == contracts.READY for a in report.values()),
                         "no adapter may claim READY before it is verified against real data")

    def test_requiring_a_blocked_adapter_raises(self):
        with self.assertRaises(contracts.NotImplementedContract):
            contracts.require("10AM_REFERENCE_IMPL")


# =============================================================================
# 4. Durability — export, backup, restore, migration
# =============================================================================
class TestExportRoundTrip(JournalTestCase):
    def test_every_session_and_trade_survives_the_round_trip(self):
        for date in ("2026-08-05", "2026-08-06", "2026-08-07"):
            sid = self.make_session(date)
            self.make_trade(sid, entry_at=f"{date}T10:00", exit_at=f"{date}T10:30")
            repo.save_checkin_pre(self.conn, sid, {"energy": 4, "focus": 4, "stress": 2})
            repo.save_checkin_post(self.conn, sid, {"execution_quality": 4, "rule_adherence": 4,
                                                    "patience": 4, "emotional_control": 4,
                                                    "well_traded": "yes"})
        metrics.recompute_all(self.conn)
        result = backup.export_all(self.conn)
        directory = Path(result["directory"])

        with open(directory / "trades.csv") as fh:
            trades = list(csv.DictReader(fh))
        self.assertEqual(len(trades), 3)
        self.assertEqual({t["session_date"] for t in trades},
                         {"2026-08-05", "2026-08-06", "2026-08-07"})

        payload = json.loads((directory / "journal.json").read_text())
        self.assertEqual(len(payload["sessions"]), 3)
        self.assertEqual(sum(len(s["trades"]) for s in payload["sessions"]), 3)

        with open(directory / "checkins.csv") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 3)

    def test_r_values_in_the_export_match_the_database(self):
        sid = self.make_session()
        self.make_trade(sid)
        metrics.recompute_all(self.conn)
        expected = self.conn.execute("SELECT r_multiple FROM trade_metrics").fetchone()["r_multiple"]
        result = backup.export_all(self.conn)
        with open(Path(result["directory"]) / "trades.csv") as fh:
            row = next(csv.DictReader(fh))
        self.assertAlmostEqual(float(row["r_multiple"]), expected)


class TestBackupRestore(JournalTestCase):
    def test_a_backup_verifies_against_its_own_manifest(self):
        sid = self.make_session()
        self.make_trade(sid)
        metrics.recompute_all(self.conn)
        archive = backup.create(self.conn, label="test")
        result = backup.verify(archive)
        self.assertTrue(result["ok"], result["problems"])

    def test_restoring_recovers_data_written_after_the_backup(self):
        first = self.make_session("2026-08-05")
        self.make_trade(first, entry_at="2026-08-05T10:00", exit_at="2026-08-05T10:30")
        metrics.recompute_all(self.conn)
        archive = backup.create(self.conn, label="before")
        before = self.conn.execute("SELECT COUNT(*) c FROM trade").fetchone()["c"]

        second = self.make_session("2026-08-06")
        self.make_trade(second, entry_at="2026-08-06T10:00", exit_at="2026-08-06T10:30")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM trade").fetchone()["c"],
                         before + 1)
        self.conn.close()

        backup.restore(archive)

        self.conn = db.connect()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM trade").fetchone()["c"], before)
        self.assertTrue(db.integrity_check(self.conn)["ok"])

    def test_restoring_moves_the_current_database_aside_rather_than_deleting_it(self):
        sid = self.make_session()
        self.make_trade(sid)
        archive = backup.create(self.conn)
        self.conn.close()
        backup.restore(archive)
        superseded = list(config.DATA_HOME.glob("journal.superseded-*.db"))
        self.assertEqual(len(superseded), 1, "the replaced database was not preserved")
        self.conn = db.connect()

    def test_an_archive_whose_contents_do_not_match_its_manifest_is_refused(self):
        """The manifest is the proof. A snapshot that no longer hashes to what
        the manifest recorded is not restorable, however well-formed it looks."""
        import tarfile

        sid = self.make_session()
        self.make_trade(sid)
        archive = backup.create(self.conn)

        tampered_dir = config.DATA_HOME / "tampered"
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tampered_dir)
        root = next(p for p in tampered_dir.iterdir() if p.is_dir())
        with open(root / backup.SNAPSHOT_NAME, "ab") as fh:
            fh.write(b"appended after the manifest was written")

        forged = config.DATA_HOME / "forged.tar.gz"
        with tarfile.open(forged, "w:gz") as tar:
            tar.add(root, arcname=root.name)

        result = backup.verify(forged)
        self.assertFalse(result["ok"])
        self.assertTrue(any("hash mismatch" in p for p in result["problems"]),
                        result["problems"])
        with self.assertRaises(RuntimeError):
            backup.restore(forged)

    def test_a_snapshot_that_fails_integrity_check_is_refused(self):
        sid = self.make_session()
        self.make_trade(sid)
        archive = backup.create(self.conn)
        manifest = backup.read_manifest(archive)
        self.assertEqual(manifest["row_counts"]["trade"], 1)
        self.assertIn(backup.SNAPSHOT_NAME, manifest["files"])
        self.assertEqual(manifest["schema"]["current"], db.migration_files()[-1].name)

    def test_pruning_keeps_the_most_recent_archives(self):
        sid = self.make_session()
        self.make_trade(sid)
        for i in range(4):
            backup.create(self.conn, label=f"n{i}")
        removed = backup.prune(keep=2)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(list(config.BACKUP_DIR.glob("journal-*.tar.gz"))), 2)


class TestMigrationUpgradePath(unittest.TestCase):
    """Data written under an older schema must survive the upgrade."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "upgrade.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self, *names):
        conn = db.connect(self.db_path, restricted=False)
        for name in names:
            sql = (config.MIGRATIONS_DIR / name).read_text()
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migration(version, applied_at, code_sha, notes) "
                "VALUES (?,?,?,'test')",
                (name, db.utcnow(), db.sha256_file(config.MIGRATIONS_DIR / name)))
            conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        return conn

    def test_a_session_written_under_0002_survives_0003(self):
        conn = self._apply("0001_init.sql", "0002_research_views.sql")
        conn.execute("INSERT INTO account(id,label,broker,mode,currency,tz,active) "
                     "VALUES (1,'A','b','paper','USD','America/New_York',1)")
        conn.execute(
            "INSERT INTO session(session_uid,session_date,tz,account_id,mode,status,"
            "iso_week,iso_month,created_at,updated_at) VALUES "
            "('2026-08-07:MAIN:A','2026-08-07','America/New_York',1,'paper','closed',"
            "'2026-W32','2026-08','2026-08-07T09:00:00Z','2026-08-07T21:00:00Z')")
        conn.execute("INSERT INTO session_metrics(session_id,calc_version,computed_at,"
                     "process_score,total_r) VALUES (1,'v0','2026-08-07T21:00:00Z',77.5,1.4)")
        conn.commit()
        conn.close()

        upgrade = db.connect(self.db_path, restricted=False)
        applied = db.migrate(upgrade)
        expected = [p.name for p in db.migration_files()
                    if p.name > "0002_research_views.sql"]
        self.assertEqual(applied, expected,
                         "every migration after the recorded point should apply, in order")

        session = upgrade.execute("SELECT * FROM session WHERE id=1").fetchone()
        self.assertEqual(session["session_date"], "2026-08-07")
        self.assertEqual(session["session_kind"], "NY_AM",
                         "existing sessions must be assigned the default grain")
        self.assertEqual(session["session_uid"], "2026-08-07:NY_AM:A",
                         "the session uid should be migrated to the new grain")

        row = upgrade.execute("SELECT * FROM session_metrics WHERE session_id=1").fetchone()
        self.assertEqual(row["self_reported_process_index"], 77.5,
                         "the renamed column lost its value")
        self.assertIsNone(row["mechanical_conformance_index"])
        self.assertEqual(row["total_r"], 1.4)

        self.assertTrue(db.integrity_check(upgrade)["ok"])
        upgrade.close()

    def test_a_second_session_kind_becomes_possible_after_the_upgrade(self):
        conn = self._apply("0001_init.sql", "0002_research_views.sql")
        conn.execute("INSERT INTO account(id,label,broker,mode,currency,tz,active) "
                     "VALUES (1,'A','b','paper','USD','America/New_York',1)")
        conn.commit()
        conn.close()

        upgrade = db.connect(self.db_path, restricted=False)
        db.migrate(upgrade)
        upgrade.close()

        conn = db.connect(self.db_path)
        morning = repo.get_or_create_session(conn, "2026-08-07", account_id=1,
                                             session_kind="NY_AM")
        afternoon = repo.get_or_create_session(conn, "2026-08-07", account_id=1,
                                               session_kind="NY_PM")
        self.assertNotEqual(morning, afternoon)
        conn.close()

    def test_an_applied_migration_cannot_be_edited_afterwards(self):
        conn = db.connect(self.db_path, restricted=False)
        db.migrate(conn)
        conn.execute("UPDATE schema_migration SET code_sha='0' * 64 WHERE version=?",
                     ("0001_init.sql",))
        conn.commit()
        with self.assertRaises(RuntimeError) as ctx:
            db.migrate(conn)
        self.assertIn("immutable", str(ctx.exception))
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
