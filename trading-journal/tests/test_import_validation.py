"""Import validation, written before the export exists.

§14 requires these to pass before any production import. Writing them now means
the day a real TradeSea file arrives, the question is only "does the mapping
profile parse this file" — not "does the engine underneath handle a partial
fill". Every case here runs against synthetic fills shaped like the real thing.

The one that matters most is `test_reimporting_the_same_file_changes_nothing`.
A journal that double-counts on re-import produces a P&L that is wrong in a way
nobody notices, because every individual number looks plausible.
"""

from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import adapters, repo, trades  # noqa: E402
from tests.test_workflow import DAY, WorkflowTestCase, at  # noqa: E402

# The lifecycle §14 names, as a broker would emit it: raw fills, no event types.
LIFECYCLE = [
    ("10:04:00", "Buy", 2, 23180.25, "O-1", "F-1"),
    ("10:07:00", "Buy", 1, 23188.50, "O-2", "F-2"),
    ("10:12:00", "Buy", 2, 23195.75, "O-3", "F-3"),
    ("10:19:00", "Sell", 2, 23214.00, "O-4", "F-4"),
    ("10:25:00", "Sell", 1, 23221.50, "O-5", "F-5"),
    ("10:31:00", "Sell", 2, 23208.75, "O-6", "F-6"),
]

PROFILE = {
    "column_map": {"account": "Account", "symbol": "Symbol", "side": "Side",
                   "quantity": "Qty", "price": "Price", "occurred_at": "Time",
                   "order_id": "OrderId", "fill_id": "FillId"},
    "datetime_format": "%Y-%m-%d %H:%M:%S",
    "source_tz": "America/New_York",
}


class ImportTestCase(WorkflowTestCase):
    def write_csv(self, rows, name="export.csv", account="Lead", symbol="NQ"):
        path = Path(self._tmp.name) / name
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Account", "Symbol", "Side", "Qty", "Price", "Time",
                             "OrderId", "FillId"])
            for time, side, qty, price, order, fill in rows:
                writer.writerow([account, symbol, side, qty, price,
                                 f"{DAY} {time}", order, fill])
        return path

    def ingest(self, path, profile=None):
        events = adapters.CsvAdapter(profile or PROFILE).parse(path)
        result = adapters.ingest(
            self.conn, events,
            account_ids={"Lead": self.lead_id, **{k: v for k, v in self.followers.items()}},
            instrument_ids={"NQ": self.instrument_id})
        trades.group_events(self.conn, self.day_id)
        return result

    def logical_trades(self):
        return self.conn.execute(
            "SELECT * FROM logical_trade WHERE trading_day_id=? ORDER BY id",
            (self.day_id,)).fetchall()

    def events(self):
        return self.conn.execute(
            "SELECT * FROM execution_event ORDER BY occurred_at, id").fetchall()


class TestScaleReconstruction(ImportTestCase):
    def test_open_add_add_reduce_close_becomes_one_logical_trade(self):
        """§14's named case. Six fills, one decision."""
        self.ingest(self.write_csv(LIFECYCLE))

        rows = self.logical_trades()
        self.assertEqual(len(rows), 1, "a scaled trade was split into several")
        self.assertEqual(rows[0]["status"], "CLOSED")

    def test_the_event_types_are_derived_from_position_not_guessed_per_row(self):
        """A lone fill cannot know what it did to the position. The sequence can."""
        self.ingest(self.write_csv(LIFECYCLE))
        self.assertEqual([e["event_type"] for e in self.events()],
                         ["OPEN", "ADD", "ADD", "REDUCE", "REDUCE", "CLOSE"])

    def test_the_position_path_is_reconstructed(self):
        self.ingest(self.write_csv(LIFECYCLE))
        trade_id = self.logical_trades()[0]["id"]
        path = [step["position_after"]
                for step in trades.position_timeline(self.conn, trade_id)]
        self.assertEqual(path, [2, 3, 5, 3, 2, 0])

    def test_metrics_count_adds_and_reductions_separately(self):
        self.ingest(self.write_csv(LIFECYCLE))
        metrics = self.metrics()
        self.assertEqual(metrics["adds"], 2)
        self.assertEqual(metrics["reductions"], 2)
        self.assertEqual(metrics["max_position"], 5)


class TestIdempotency(ImportTestCase):
    def test_reimporting_the_same_file_changes_nothing(self):
        """The failure this guards against is silent: every number stays
        plausible while the P&L doubles."""
        path = self.write_csv(LIFECYCLE)
        first = self.ingest(path)
        before = self.metrics()["lead_pnl"]

        second = self.ingest(path)

        self.assertEqual(first["events_written"], 6)
        self.assertEqual(second["events_written"], 0)
        self.assertEqual(second["already_known"], 6)
        self.assertEqual(len(self.events()), 6)
        self.assertEqual(len(self.logical_trades()), 1)
        self.assertEqual(self.metrics()["lead_pnl"], before)

    def test_an_overlapping_export_imports_only_the_new_rows(self):
        """Exports usually overlap — a fresh download covers yesterday too."""
        self.ingest(self.write_csv(LIFECYCLE[:3], name="first.csv"))
        result = self.ingest(self.write_csv(LIFECYCLE, name="second.csv"))

        self.assertEqual(result["events_written"], 3)
        self.assertEqual(result["already_known"], 3)
        self.assertEqual(len(self.events()), 6)

    def test_identical_fills_at_different_times_are_both_kept(self):
        """Two 1-lot fills at the same price a minute apart are two fills. The
        fingerprint must not collapse them."""
        rows = [("10:04:00", "Buy", 1, 23180.25, "O-1", "F-1"),
                ("10:05:00", "Buy", 1, 23180.25, "O-2", "F-2"),
                ("10:10:00", "Sell", 2, 23190.00, "O-3", "F-3")]
        self.ingest(self.write_csv(rows))
        self.assertEqual(len(self.events()), 3)

    def test_rows_without_fill_ids_still_import_and_still_deduplicate(self):
        """Not every export carries IDs. The fingerprint falls back to the
        fill's own facts, which is weaker but must not be wrong."""
        rows = [(t, s, q, p, "", "") for t, s, q, p, _o, _f in LIFECYCLE]
        path = self.write_csv(rows)
        self.ingest(path)
        again = self.ingest(path)

        self.assertEqual(len(self.events()), 6)
        self.assertEqual(again["events_written"], 0)


class TestPositionEdges(ImportTestCase):
    def test_a_partial_fill_sequence_is_one_open_not_several(self):
        """A 5-lot order filling in three pieces is one entry decision."""
        rows = [("10:04:00", "Buy", 2, 23180.00, "O-1", "F-1"),
                ("10:04:01", "Buy", 2, 23180.25, "O-1", "F-2"),
                ("10:04:02", "Buy", 1, 23180.50, "O-1", "F-3"),
                ("10:30:00", "Sell", 5, 23200.00, "O-2", "F-4")]
        self.ingest(self.write_csv(rows))

        self.assertEqual(len(self.logical_trades()), 1)
        self.assertEqual(self.metrics()["max_position"], 5)

    def test_returning_to_flat_closes_the_trade(self):
        self.ingest(self.write_csv(LIFECYCLE))
        trade = self.logical_trades()[0]
        self.assertEqual(trade["status"], "CLOSED")
        self.assertIsNotNone(trade["closed_at"])

    def test_a_clear_re_entry_after_flat_is_a_second_trade(self):
        """Two decisions an hour apart are two observations, not one."""
        rows = LIFECYCLE + [
            ("13:00:00", "Buy", 2, 23150.00, "O-7", "F-7"),
            ("13:20:00", "Sell", 2, 23170.00, "O-8", "F-8"),
        ]
        self.ingest(self.write_csv(rows))
        self.assertEqual(len(self.logical_trades()), 2)

    def test_an_immediate_re_entry_is_flagged_rather_than_decided(self):
        """Back in 30 seconds could be one plan or two. The engine refuses to
        pick and says so, which is the honest answer."""
        rows = LIFECYCLE + [
            ("10:31:30", "Buy", 2, 23209.00, "O-7", "F-7"),
            ("10:45:00", "Sell", 2, 23220.00, "O-8", "F-8"),
        ]
        self.ingest(self.write_csv(rows))
        flagged = [t for t in self.logical_trades()
                   if t["status"] == "NEEDS_GROUPING_REVIEW"
                   or t["grouping_confidence"] in ("low", "NEEDS_GROUPING_REVIEW")]
        self.assertTrue(flagged, "an ambiguous re-entry was silently decided")

    def test_a_short_trade_reconstructs_the_same_way(self):
        rows = [("10:04:00", "Sell", 2, 23200.00, "O-1", "F-1"),
                ("10:10:00", "Sell", 1, 23210.00, "O-2", "F-2"),
                ("10:20:00", "Buy", 3, 23180.00, "O-3", "F-3")]
        self.ingest(self.write_csv(rows))

        self.assertEqual(len(self.logical_trades()), 1)
        self.assertEqual(self.logical_trades()[0]["direction"], "SHORT")
        self.assertGreater(self.metrics()["lead_pnl"], 0)


class TestTimezones(ImportTestCase):
    def test_local_export_times_are_stored_as_utc(self):
        self.ingest(self.write_csv(LIFECYCLE[:1] + LIFECYCLE[-1:]))
        first = self.events()[0]
        # 10:04 New York in August is 14:04 UTC.
        self.assertTrue(first["occurred_at"].endswith("Z"))
        self.assertIn("T14:04", first["occurred_at"])

    def test_a_profile_in_another_timezone_lands_on_the_same_instant(self):
        """The same fills exported by a venue reporting UTC must not become a
        different trade, or a second copy of one."""
        utc_rows = [("14:04:00", "Buy", 2, 23180.25, "O-1", "F-1"),
                    ("14:31:00", "Sell", 2, 23208.75, "O-6", "F-6")]
        profile = dict(PROFILE, source_tz="UTC")
        self.ingest(self.write_csv(utc_rows, name="utc.csv"), profile=profile)
        ny_events = [e["occurred_at"] for e in self.events()]

        self.ingest(self.write_csv(LIFECYCLE[:1] + LIFECYCLE[-1:], name="ny.csv"))
        self.assertEqual([e["occurred_at"] for e in self.events()], ny_events,
                         "the same instant imported twice under two timezones")


class TestMalformedInput(ImportTestCase):
    def test_a_missing_column_is_named_rather_than_skipped(self):
        path = Path(self._tmp.name) / "bad.csv"
        path.write_text("Account,Symbol,Side,Qty,Time\nLead,NQ,Buy,2,2026-08-10 10:04:00\n")
        with self.assertRaises(adapters.AdapterError) as ctx:
            adapters.CsvAdapter(PROFILE).parse(path)
        self.assertIn("Price", str(ctx.exception))

    def test_an_unrecognised_side_stops_the_import(self):
        """Better a refused file than a file half-imported with a guess in it."""
        rows = [("10:04:00", "Flatten", 2, 23180.25, "O-1", "F-1")]
        with self.assertRaises(adapters.AdapterError):
            adapters.CsvAdapter(PROFILE).parse(self.write_csv(rows))

    def test_an_unknown_account_is_reported_not_invented(self):
        result = self.ingest(self.write_csv(LIFECYCLE[:1]))
        self.assertEqual(result["events_written"], 1)

        events = adapters.CsvAdapter(PROFILE).parse(
            self.write_csv(LIFECYCLE[1:2], name="other.csv", account="Unknown Prop 50k"))
        outcome = adapters.ingest(self.conn, events, account_ids={"Lead": self.lead_id},
                                  instrument_ids={"NQ": self.instrument_id})
        self.assertEqual(outcome["events_written"], 0)
        self.assertIn("Unknown Prop 50k / NQ", outcome["unmapped"])

    def test_a_profile_missing_a_required_field_is_refused_at_construction(self):
        broken = {"column_map": {k: v for k, v in PROFILE["column_map"].items()
                                 if k != "price"}}
        with self.assertRaises(adapters.AdapterError):
            adapters.CsvAdapter(broken)


class TestFollowerImport(ImportTestCase):
    def test_follower_fills_attach_to_the_lead_trade_not_their_own(self):
        self.ingest(self.write_csv(LIFECYCLE))
        follower_rows = [(t, s, q, p, f"{o}-F1", f"{f}-F1")
                         for t, s, q, p, o, f in LIFECYCLE]
        self.ingest(self.write_csv(follower_rows, name="f1.csv", account="F1"))

        self.assertEqual(len(self.logical_trades()), 1,
                         "a follower's copy became a trade of its own")

    def test_a_follower_that_missed_a_fill_is_reported_not_repaired(self):
        self.ingest(self.write_csv(LIFECYCLE))
        missing = [row for row in LIFECYCLE if row[4] != "O-2"]
        self.ingest(self.write_csv(
            [(t, s, q, p, f"{o}-F1", f"{f}-F1") for t, s, q, p, o, f in missing],
            name="f1.csv", account="F1"))

        row = self.conn.execute(
            "SELECT discrepancies FROM account_execution ae JOIN account a ON a.id=ae.account_id "
            "WHERE a.label='F1'").fetchone()
        self.assertTrue(row["discrepancies"] and row["discrepancies"] != "[]",
                        "a missed follower fill was not reported")


if __name__ == "__main__":
    unittest.main(verbosity=2)
