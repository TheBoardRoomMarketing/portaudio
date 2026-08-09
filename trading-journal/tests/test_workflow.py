"""Tests for the real workflow: scaling, multi-account copying, bias, grouping.

The two that matter most are `test_ten_accounts_are_one_observation` and
`test_a_missing_follower_fill_is_reported_not_repaired`. Everything else is
arithmetic; those two are the reason this layer exists.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import adapters, bias, db, repo, trades  # noqa: E402
from journal.db import utcnow  # noqa: E402
from tests.test_journal import JournalTestCase  # noqa: E402

DAY = "2026-08-10"
TZ = "America/New_York"


def at(hhmm: str, second: int = 0) -> str:
    return repo.local_to_utc(f"{DAY}T{hhmm}:{second:02d}", TZ)


class WorkflowTestCase(JournalTestCase):
    """A lead account, three followers, an NQ instrument and a trading day."""

    def setUp(self):
        super().setUp()
        self.instrument_id = repo.ensure_instrument(
            self.conn, "NQ", tick_size=0.25, tick_value=5.0, point_value=20.0)

        self.lead_id = repo.ensure_account(self.conn, "Lead", mode="live")
        self.conn.execute("UPDATE account SET role='LEAD', platform='TradeSea' WHERE id=?",
                          (self.lead_id,))
        self.followers = {}
        for name in ("F1", "F2", "F3"):
            aid = repo.ensure_account(self.conn, name, mode="live")
            self.conn.execute(
                "UPDATE account SET role='FOLLOWER', copy_source_account_id=?, "
                "size_multiplier=1.0 WHERE id=?", (self.lead_id, aid))
            self.followers[name] = aid

        now = utcnow()
        self.day_id = self.conn.execute(
            "INSERT INTO trading_day(day_date,tz,status,iso_week,iso_month,created_at,"
            "updated_at,is_demo) VALUES (?,?,'PLANNED',?,?,?,?,0)",
            (DAY, TZ, repo.iso_week_of(DAY), DAY[:7], now, now)).lastrowid
        self.conn.commit()

    def event(self, account_id, kind, hhmm, qty, price, side, second=0, stop=None):
        return trades.record_event(
            self.conn, account_id=account_id, instrument_id=self.instrument_id,
            event_type=kind, occurred_at=at(hhmm, second), quantity=qty, price=price,
            side=side, stop_price=stop, source="manual",
            fill_external_id=f"{account_id}-{kind}-{hhmm}")

    def scaled_trade(self, account_id, *, second=0, stop=23162.0):
        """The brief's exact sequence: open, two adds, two reductions, close."""
        self.event(account_id, "OPEN", "10:04", 2, 23180.25, "BUY", second, stop)
        self.event(account_id, "ADD", "10:07", 1, 23188.50, "BUY", second)
        self.event(account_id, "ADD", "10:12", 2, 23195.75, "BUY", second)
        self.event(account_id, "REDUCE", "10:19", 2, 23214.00, "SELL", second)
        self.event(account_id, "REDUCE", "10:25", 1, 23221.50, "SELL", second)
        self.event(account_id, "CLOSE", "10:31", 2, 23208.75, "SELL", second)

    def group(self):
        self.conn.commit()
        return trades.group_events(self.conn, self.day_id)

    def only_trade(self):
        return self.conn.execute(
            "SELECT * FROM logical_trade WHERE trading_day_id=?", (self.day_id,)).fetchone()

    def metrics(self):
        return self.conn.execute(
            "SELECT * FROM logical_trade_metrics WHERE logical_trade_id=?",
            (self.only_trade()["id"],)).fetchone()


# =============================================================================
# Scale-in / scale-out
# =============================================================================
class TestScaling(WorkflowTestCase):
    def test_a_scaled_trade_is_one_logical_trade(self):
        self.scaled_trade(self.lead_id)
        result = self.group()
        self.assertEqual(result["logical_trades"], 1,
                         "adds and reductions must not create separate trades")
        self.assertEqual(result["events_grouped"], 6)

    def test_position_is_reconstructed_step_by_step(self):
        self.scaled_trade(self.lead_id)
        self.group()
        timeline = trades.position_timeline(self.conn, self.only_trade()["id"])
        self.assertEqual([e["position_after"] for e in timeline], [2, 3, 5, 3, 2, 0])
        self.assertEqual([e["event_type"] for e in timeline],
                         ["OPEN", "ADD", "ADD", "REDUCE", "REDUCE", "CLOSE"])

    def test_adds_and_reductions_are_counted(self):
        self.scaled_trade(self.lead_id)
        self.group()
        m = self.metrics()
        self.assertEqual(m["adds"], 2)
        self.assertEqual(m["reductions"], 2)
        self.assertEqual(m["max_position"], 5)

    def test_average_entry_is_size_weighted(self):
        self.scaled_trade(self.lead_id)
        self.group()
        expected = (23180.25 * 2 + 23188.50 * 1 + 23195.75 * 2) / 5
        self.assertAlmostEqual(self.metrics()["avg_entry_price"], round(expected, 4), places=3)

    def test_average_exit_is_size_weighted(self):
        self.scaled_trade(self.lead_id)
        self.group()
        expected = (23214.00 * 2 + 23221.50 * 1 + 23208.75 * 2) / 5
        self.assertAlmostEqual(self.metrics()["avg_exit_price"], round(expected, 4), places=3)

    def test_time_to_maximum_size_is_measured(self):
        self.scaled_trade(self.lead_id)
        self.group()
        self.assertEqual(self.metrics()["seconds_to_max_size"], 8 * 60)   # 10:04 -> 10:12

    def test_returning_to_flat_closes_the_trade(self):
        self.scaled_trade(self.lead_id)
        self.group()
        trade = self.only_trade()
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["closed_at"], at("10:31"))

    def test_a_new_position_after_a_gap_is_a_new_trade(self):
        self.scaled_trade(self.lead_id)
        self.event(self.lead_id, "OPEN", "13:15", 2, 23300.00, "BUY")
        self.event(self.lead_id, "CLOSE", "13:40", 2, 23320.00, "SELL")
        result = self.group()
        self.assertEqual(result["logical_trades"], 2)

    def test_an_immediate_re_entry_is_flagged_rather_than_guessed(self):
        """Flat and straight back in is genuinely ambiguous. The journal says so
        instead of silently splitting or silently merging."""
        self.scaled_trade(self.lead_id)
        self.event(self.lead_id, "OPEN", "10:32", 2, 23210.00, "BUY")
        self.event(self.lead_id, "CLOSE", "10:40", 2, 23230.00, "SELL")
        self.group()
        flagged = self.conn.execute(
            "SELECT * FROM logical_trade WHERE status='NEEDS_GROUPING_REVIEW'").fetchall()
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["grouping_confidence"], "low")
        self.assertIn("re-entered", flagged[0]["grouping_note"])

    def test_grouping_can_be_confirmed_by_hand(self):
        self.scaled_trade(self.lead_id)
        self.event(self.lead_id, "OPEN", "10:32", 2, 23210.00, "BUY")
        self.event(self.lead_id, "CLOSE", "10:40", 2, 23230.00, "SELL")
        self.group()
        flagged = self.conn.execute(
            "SELECT id FROM logical_trade WHERE status='NEEDS_GROUPING_REVIEW'").fetchone()
        trades.regroup(self.conn, flagged["id"], "confirm", "checked against the platform")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM logical_trade WHERE id=?", (flagged["id"],)
        ).fetchone()["status"], "CLOSED")


# =============================================================================
# Multi-account
# =============================================================================
class TestMultiAccount(WorkflowTestCase):
    def copied_day(self, skip=None):
        self.scaled_trade(self.lead_id)
        for name, aid in self.followers.items():
            for i, (kind, hhmm, qty, price, side) in enumerate([
                    ("OPEN", "10:04", 2, 23180.50, "BUY"),
                    ("ADD", "10:07", 1, 23188.75, "BUY"),
                    ("ADD", "10:12", 2, 23196.00, "BUY"),
                    ("REDUCE", "10:19", 2, 23213.75, "SELL"),
                    ("REDUCE", "10:25", 1, 23221.25, "SELL"),
                    ("CLOSE", "10:31", 2, 23208.50, "SELL")]):
                if skip and name == skip[0] and hhmm == skip[1]:
                    continue
                self.event(aid, kind, hhmm, qty, price, side, second=2)
        return self.group()

    def test_ten_accounts_are_one_observation(self):
        """The defect this whole layer exists to prevent: a copier turning one
        decision into N independent trades."""
        self.copied_day()
        count = self.conn.execute(
            "SELECT COUNT(*) c FROM logical_trade WHERE trading_day_id=?",
            (self.day_id,)).fetchone()["c"]
        self.assertEqual(count, 1, "four accounts produced more than one logical trade")

        executions = self.conn.execute(
            "SELECT COUNT(*) c FROM account_execution WHERE logical_trade_id=?",
            (self.only_trade()["id"],)).fetchone()["c"]
        self.assertEqual(executions, 4, "each account should still have its own rollup")

    def test_normalized_pnl_is_the_lead_not_the_sum(self):
        self.copied_day()
        m = self.metrics()
        self.assertAlmostEqual(m["normalized_pnl"], m["lead_pnl"])
        self.assertGreater(m["total_pnl"], m["lead_pnl"] * 3,
                           "total across accounts should be much larger than one account")
        self.assertNotEqual(m["normalized_pnl"], m["total_pnl"])

    def test_a_missing_follower_fill_is_reported_not_repaired(self):
        self.copied_day(skip=("F2", "10:12"))
        rows = self.conn.execute(
            "SELECT a.label, ae.discrepancies, ae.max_position, ae.event_count "
            "FROM account_execution ae JOIN account a ON a.id=ae.account_id "
            "WHERE ae.logical_trade_id=?", (self.only_trade()["id"],)).fetchall()
        by_label = {r["label"]: r for r in rows}

        off = json.loads(by_label["F2"]["discrepancies"])
        self.assertTrue(off, "the missed add should be reported")
        kinds = {d["kind"] for d in off}
        self.assertIn("missed_events", kinds)
        self.assertIn("missed_add", kinds)
        self.assertIn("size_mismatch", kinds)
        self.assertEqual(by_label["F2"]["max_position"], 3,
                         "the missing fill must not be invented to make the copy look complete")
        self.assertEqual(json.loads(by_label["F1"]["discrepancies"]), [])

    def test_follower_slippage_is_measured_against_the_lead(self):
        self.copied_day()
        row = self.conn.execute(
            "SELECT ae.entry_slippage_points FROM account_execution ae "
            "JOIN account a ON a.id=ae.account_id WHERE a.label='F1' AND ae.logical_trade_id=?",
            (self.only_trade()["id"],)).fetchone()
        self.assertIsNotNone(row["entry_slippage_points"])
        self.assertGreater(row["entry_slippage_points"], 0,
                           "followers filled higher on a long, which is adverse")

    def test_the_lead_has_no_slippage_against_itself(self):
        self.copied_day()
        row = self.conn.execute(
            "SELECT ae.entry_slippage_points FROM account_execution ae "
            "JOIN account a ON a.id=ae.account_id WHERE a.label='Lead' AND ae.logical_trade_id=?",
            (self.only_trade()["id"],)).fetchone()
        self.assertIsNone(row["entry_slippage_points"])

    def test_account_configuration_history_is_preserved(self):
        self.conn.execute(
            "INSERT INTO account_snapshot(account_id,effective_from,effective_to,role,status,"
            "account_size,size_multiplier,reason,captured_at) "
            "VALUES (?,'2026-01-01','2026-06-30','FOLLOWER','ACTIVE',50000,1.0,'eval',?)",
            (self.followers["F1"], utcnow()))
        self.conn.execute(
            "INSERT INTO account_snapshot(account_id,effective_from,role,status,account_size,"
            "size_multiplier,reason,captured_at) "
            "VALUES (?,'2026-07-01','FOLLOWER','ACTIVE',150000,2.0,'passed and resized',?)",
            (self.followers["F1"], utcnow()))
        self.conn.commit()
        self.event(self.followers["F1"], "OPEN", "10:04", 2, 23180.0, "BUY")
        snapshot = self.conn.execute(
            "SELECT s.account_size FROM execution_event e "
            "JOIN account_snapshot s ON s.id=e.account_snapshot_id "
            "WHERE e.account_id=?", (self.followers["F1"],)).fetchone()
        self.assertEqual(snapshot["account_size"], 150000,
                         "the event should carry the configuration in force at the time")


# =============================================================================
# Import identity
# =============================================================================
class TestImportIdempotency(WorkflowTestCase):
    def test_the_same_event_twice_is_stored_once(self):
        first = self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        second = self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.assertIsNotNone(first)
        self.assertIsNone(second, "a repeated fill id must not create a second event")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM execution_event").fetchone()["c"], 1)

    def test_events_without_a_venue_id_get_a_deterministic_fingerprint(self):
        a = trades.fingerprint(1, 1, at("10:04"), "OPEN", 2, 23180.25)
        b = trades.fingerprint(1, 1, at("10:04"), "OPEN", 2, 23180.25)
        c = trades.fingerprint(1, 1, at("10:05"), "OPEN", 2, 23180.25)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_a_venue_fill_id_wins_over_the_fingerprint(self):
        self.assertEqual(trades.fingerprint(1, 1, at("10:04"), "OPEN", 2, 1.0, "ABC"),
                         "fill:ABC")

    def test_reimporting_an_export_adds_only_the_new_rows(self):
        rows = [
            {"account_ref": "Lead", "symbol": "NQ", "side": "BUY", "quantity": 2,
             "price": 23180.25, "occurred_at": at("10:04"), "fill_external_id": "E1",
             "source": "csv_import", "event_type": None},
            {"account_ref": "Lead", "symbol": "NQ", "side": "SELL", "quantity": 2,
             "price": 23208.75, "occurred_at": at("10:31"), "fill_external_id": "E2",
             "source": "csv_import", "event_type": None},
        ]
        ids = {"Lead": self.lead_id}
        instruments = {"NQ": self.instrument_id}
        first = adapters.ingest(self.conn, rows, account_ids=ids, instrument_ids=instruments)
        self.assertEqual(first["events_written"], 2)

        extended = rows + [
            {"account_ref": "Lead", "symbol": "NQ", "side": "BUY", "quantity": 1,
             "price": 23300.00, "occurred_at": at("13:15"), "fill_external_id": "E3",
             "source": "csv_import", "event_type": None},
            {"account_ref": "Lead", "symbol": "NQ", "side": "SELL", "quantity": 1,
             "price": 23320.00, "occurred_at": at("13:40"), "fill_external_id": "E4",
             "source": "csv_import", "event_type": None},
        ]
        second = adapters.ingest(self.conn, extended, account_ids=ids,
                                 instrument_ids=instruments)
        self.assertEqual(second["events_written"], 2)
        self.assertEqual(second["already_known"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM execution_event").fetchone()["c"], 4)

    def test_raw_fills_are_classified_by_position_context(self):
        """A fill alone cannot know whether it opened, added to or closed."""
        rows = [
            {"account_ref": "L", "symbol": "NQ", "side": "BUY", "quantity": 2,
             "price": 1.0, "occurred_at": at("10:04")},
            {"account_ref": "L", "symbol": "NQ", "side": "BUY", "quantity": 1,
             "price": 1.0, "occurred_at": at("10:07")},
            {"account_ref": "L", "symbol": "NQ", "side": "SELL", "quantity": 1,
             "price": 1.0, "occurred_at": at("10:19")},
            {"account_ref": "L", "symbol": "NQ", "side": "SELL", "quantity": 2,
             "price": 1.0, "occurred_at": at("10:31")},
        ]
        classified = adapters.classify_fills(rows)
        self.assertEqual([r["event_type"] for r in classified],
                         ["OPEN", "ADD", "REDUCE", "CLOSE"])
        self.assertEqual([r["position_after"] for r in classified], [2, 3, 2, 0])

    def test_execution_events_are_append_only(self):
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        row = self.conn.execute("SELECT id FROM execution_event LIMIT 1").fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE execution_event SET quantity=99 WHERE id=?", (row["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM execution_event WHERE id=?", (row["id"],))


# =============================================================================
# Risk
# =============================================================================
class TestRisk(WorkflowTestCase):
    def test_r_is_computed_when_a_real_initial_stop_exists(self):
        self.scaled_trade(self.lead_id, stop=23162.0)
        self.group()
        m = self.metrics()
        self.assertEqual(m["r_status"], "COMPUTED")
        self.assertAlmostEqual(m["initial_risk_points"], 23180.25 - 23162.0, places=2)
        self.assertIsNotNone(m["r_multiple"])

    def test_r_is_unknown_without_an_initial_stop(self):
        self.scaled_trade(self.lead_id, stop=None)
        self.group()
        m = self.metrics()
        self.assertEqual(m["r_status"], "UNKNOWN")
        self.assertIsNone(m["r_multiple"],
                          "R must never be invented from hindsight or excursion")
        self.assertIsNone(m["initial_risk_points"])


# =============================================================================
# Bias
# =============================================================================
class TestDailyBias(WorkflowTestCase):
    def test_a_recorded_bias_cannot_be_edited_directly(self):
        bias.record(self.conn, self.day_id, direction="BULLISH", strength=4,
                    thesis="Expecting the sweep to hold.")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.conn.execute("UPDATE daily_bias SET thesis='Actually I was bearish' "
                              "WHERE trading_day_id=?", (self.day_id,))
        self.assertIn("immutable", str(ctx.exception))

    def test_amending_preserves_the_original(self):
        bias.record(self.conn, self.day_id, direction="BULLISH", strength=4,
                    thesis="Expecting the sweep to hold.")
        bias.amend(self.conn, self.day_id, {"strength": 2},
                   "conviction was lower than I first wrote")
        current = bias.get(self.conn, self.day_id)
        self.assertEqual(current["strength"], 2)
        self.assertEqual(len(current["amendments"]), 1)
        self.assertEqual(current["amendments"][0]["old_value"], "4")
        self.assertEqual(current["amendments"][0]["reason"],
                         "conviction was lower than I first wrote")

    def test_an_amendment_requires_a_reason(self):
        bias.record(self.conn, self.day_id, direction="NEUTRAL")
        with self.assertRaises(ValueError):
            bias.amend(self.conn, self.day_id, {"direction": "BULLISH"}, "")

    def test_the_immutability_trigger_survives_an_amendment(self):
        bias.record(self.conn, self.day_id, direction="BULLISH", strength=3)
        bias.amend(self.conn, self.day_id, {"strength": 4}, "reconsidered")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE daily_bias SET thesis='rewritten' WHERE trading_day_id=?",
                              (self.day_id,))

    def test_only_one_bias_per_day(self):
        bias.record(self.conn, self.day_id, direction="BULLISH")
        with self.assertRaises(ValueError):
            bias.record(self.conn, self.day_id, direction="BEARISH")

    def test_bias_outcome_refuses_a_model_as_its_methodology(self):
        bias.record(self.conn, self.day_id, direction="BULLISH")
        for method in ("llm", "AI", "claude", ""):
            with self.subTest(method=method):
                with self.assertRaises(ValueError):
                    bias.set_outcome(self.conn, self.day_id, "CORRECT", method)

    def test_bias_outcome_is_unset_until_a_methodology_exists(self):
        bias.record(self.conn, self.day_id, direction="BULLISH")
        self.assertIsNone(bias.get(self.conn, self.day_id)["outcome"])

    def test_all_four_directions_are_accepted(self):
        for i, direction in enumerate(bias.DIRECTIONS):
            day = self.conn.execute(
                "INSERT INTO trading_day(day_date,tz,status,created_at,updated_at,is_demo) "
                "VALUES (?,?,'PLANNED',?,?,0)",
                (f"2026-09-{i + 1:02d}", TZ, utcnow(), utcnow())).lastrowid
            bias.record(self.conn, day, direction=direction)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM daily_bias").fetchone()["c"], len(bias.DIRECTIONS))


# =============================================================================
# Days without trades, and demo isolation
# =============================================================================
class TestDaysAndIsolation(WorkflowTestCase):
    def test_a_day_with_a_bias_and_no_trades_is_still_a_day(self):
        bias.record(self.conn, self.day_id, direction="NEUTRAL", strength=2,
                    thesis="Nothing clean here. Staying out.")
        trades.rebuild_day(self.conn, self.day_id)
        row = self.conn.execute("SELECT * FROM v_day_summary WHERE trading_day_id=?",
                                (self.day_id,)).fetchone()
        self.assertEqual(row["trades"], 0)
        self.assertEqual(row["bias_direction"], "NEUTRAL")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM trading_day WHERE id=?", (self.day_id,)
        ).fetchone()["status"], "NO_TRADE")

    def test_demo_rows_are_marked_and_countable(self):
        demo_day = self.conn.execute(
            "INSERT INTO trading_day(day_date,tz,status,created_at,updated_at,is_demo) "
            "VALUES ('2026-08-11',?,'PLANNED',?,?,1)", (TZ, utcnow(), utcnow())).lastrowid
        bias.record(self.conn, demo_day, direction="BULLISH", is_demo=True)
        self.conn.commit()
        isolation = {r["table_name"]: r["demo_rows"] for r in
                     self.conn.execute("SELECT * FROM v_demo_isolation")}
        self.assertEqual(isolation["trading_day"], 1)
        self.assertEqual(isolation["daily_bias"], 1)

    def test_demo_events_never_group_into_real_trades(self):
        self.scaled_trade(self.lead_id)
        trades.record_event(self.conn, account_id=self.lead_id,
                            instrument_id=self.instrument_id, event_type="OPEN",
                            occurred_at=at("11:00"), quantity=1, price=1.0, side="BUY",
                            source="demo", is_demo=True)
        self.group()
        grouped_demo = self.conn.execute(
            "SELECT COUNT(*) c FROM execution_event WHERE is_demo=1 "
            "AND logical_trade_id IS NOT NULL").fetchone()["c"]
        self.assertEqual(grouped_demo, 0,
                         "a demo event must not attach to a real trading day")

    def test_orphan_events_are_visible_rather_than_dropped(self):
        follower = self.followers["F1"]
        self.event(follower, "OPEN", "14:00", 1, 23400.0, "BUY")
        self.group()
        orphans = trades.orphan_events(self.conn)
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["account_label"], "F1")


# =============================================================================
# Review
# =============================================================================
class TestReview(WorkflowTestCase):
    def test_review_stores_the_dimensions_separately(self):
        self.scaled_trade(self.lead_id)
        self.group()
        trade_id = self.only_trade()["id"]
        self.conn.execute("INSERT INTO setup(id,name,created_at) VALUES ('S1','Sweep',?)",
                          (utcnow(),))
        trades.submit_review(self.conn, trade_id, {
            "setup_id": "S1", "why": "swept and reclaimed", "planning_mode": "PLANNED",
            "execution_grade": 4, "process_grade": 5, "setup_quality": 3,
            "process_tags": ["LATE_ENTRY"], "review_seconds": 74})
        row = self.conn.execute("SELECT * FROM trade_annotation WHERE logical_trade_id=?",
                                (trade_id,)).fetchone()
        self.assertEqual(row["execution_grade"], 4)
        self.assertEqual(row["process_grade"], 5)
        self.assertEqual(row["setup_quality"], 3)
        self.assertEqual(row["planning_mode"], "PLANNED")
        self.assertEqual(self.conn.execute(
            "SELECT review_state FROM logical_trade WHERE id=?", (trade_id,)
        ).fetchone()["review_state"], "REVIEWED")

    def test_a_profitable_trade_can_carry_a_poor_process_grade(self):
        self.scaled_trade(self.lead_id)
        self.group()
        trade_id = self.only_trade()["id"]
        trades.submit_review(self.conn, trade_id, {"process_grade": 1, "execution_grade": 2})
        m = self.metrics()
        row = self.conn.execute("SELECT process_grade FROM trade_annotation "
                                "WHERE logical_trade_id=?", (trade_id,)).fetchone()
        self.assertGreater(m["lead_pnl"], 0)
        self.assertEqual(row["process_grade"], 1,
                         "outcome and process must be independently recordable")

    def test_the_day_completes_once_every_trade_is_reviewed(self):
        self.scaled_trade(self.lead_id)
        self.group()
        self.assertEqual(self.conn.execute(
            "SELECT status FROM trading_day WHERE id=?", (self.day_id,)
        ).fetchone()["status"], "REVIEW_PENDING")
        trades.submit_review(self.conn, self.only_trade()["id"], {"process_grade": 4})
        self.assertEqual(self.conn.execute(
            "SELECT status FROM trading_day WHERE id=?", (self.day_id,)
        ).fetchone()["status"], "DAY_COMPLETE")


class TestAdapterHonesty(WorkflowTestCase):
    def test_blocked_adapters_refuse_to_guess_a_format(self):
        for name in ("tradesea", "tradesyncer"):
            with self.subTest(adapter=name):
                adapter = adapters.ADAPTERS[name]
                self.assertNotEqual(adapter.status, adapters.IMPLEMENTED)
                with self.assertRaises(Exception):
                    adapter.parse(Path("/nonexistent.csv"))

    def test_every_adapter_states_what_it_needs(self):
        for entry in adapters.status_report():
            self.assertTrue(entry["needs"], f"{entry['name']} does not say what it needs")

    def test_the_manual_adapter_always_works(self):
        events = adapters.ManualAdapter().parse({
            "account_ref": "Lead", "symbol": "NQ",
            "events": [{"event_type": "OPEN", "occurred_at": at("10:04"),
                        "quantity": 2, "price": 23180.25, "side": "BUY"}]})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "manual")


if __name__ == "__main__":
    unittest.main(verbosity=2)
