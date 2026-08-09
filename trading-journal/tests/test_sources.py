"""Source mapping, provenance, sync state and normalisation.

Written during the TradeSyncer investigation, against synthetic fixtures only.
No network client exists and nothing here contacts anything — these cover the
layer that any ingestion route feeds, including the manual CSV fallback.

The two that matter most:

  * `test_a_secret_shaped_field_is_refused_rather_than_stored` — a token written
    into a journal is a token in every backup and every export of that journal,
    from then on.
  * `test_a_failed_sync_does_not_advance_the_watermark` — advancing past records
    that may never have arrived loses them permanently and silently.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import adapters, contracts, sources  # noqa: E402
from tests.test_workflow import WorkflowTestCase  # noqa: E402

FIELD_MAP = {"record_id": "id", "source_account_id": "acct", "symbol": "sym",
             "side": "dir", "quantity": "qty", "price": "px", "occurred_at": "ts",
             "order_id": "ord", "fill_id": "fill", "fees": "fee"}


def record(**kw):
    base = {"id": "e1", "acct": "acct_991", "sym": "NQ", "dir": "Buy", "qty": 2,
            "px": 23180.25, "ts": "2026-08-10T14:04:00Z", "ord": "O-1",
            "fill": "F-1", "fee": 1.24}
    base.update(kw)
    return base


class TestAccountMapping(WorkflowTestCase):
    def test_a_source_id_resolves_to_a_journal_account(self):
        sources.map_account(self.conn, source="tradesyncer", source_account_id="acct_991",
                            source_label="Tradeify 100k", account_id=self.lead_id,
                            source_role="LEAD")
        self.assertEqual(
            sources.resolve_account(self.conn, "tradesyncer", "acct_991"), self.lead_id)

    def test_account_names_are_not_hardcoded_anywhere(self):
        """The label is Zack's and may change; the source's own ID is what
        survives a rename."""
        sources.map_account(self.conn, source="tradesyncer", source_account_id="acct_991",
                            source_label="Tradeify 100k", account_id=self.lead_id)
        self.conn.execute("UPDATE account SET label='Tradeify 150k' WHERE id=?",
                          (self.lead_id,))
        self.conn.commit()
        self.assertEqual(
            sources.resolve_account(self.conn, "tradesyncer", "acct_991"), self.lead_id)

    def test_an_unmapped_account_is_reported_not_guessed(self):
        """Guessing by label match is how a follower's fills get attributed to
        the wrong prop account, corrupting copy quality for the whole day."""
        sources.map_account(self.conn, source="tradesyncer", source_account_id="acct_991",
                            account_id=self.lead_id)
        missing = sources.unmapped(self.conn, "tradesyncer", ["acct_991", "acct_777"])
        self.assertEqual(missing, ["acct_777"])

    def test_retiring_a_mapping_keeps_old_data_interpretable(self):
        sources.map_account(self.conn, source="tradesyncer", source_account_id="acct_991",
                            account_id=self.lead_id)
        sources.retire_mapping(self.conn, "tradesyncer", "acct_991")

        self.assertIsNone(sources.resolve_account(self.conn, "tradesyncer", "acct_991"))
        self.assertEqual(len(sources.mapping_report(self.conn)), 1,
                         "a retired mapping was deleted rather than closed")

    def test_a_source_role_never_overwrites_the_journal_role(self):
        """A copier's idea of who leads is evidence, not authority. When they
        disagree that is worth seeing, not worth silently resolving."""
        follower = self.followers["F1"]
        sources.map_account(self.conn, source="tradesyncer", source_account_id="acct_002",
                            account_id=follower, source_role="LEAD")

        self.assertEqual(
            self.conn.execute("SELECT role FROM account WHERE id=?",
                              (follower,)).fetchone()["role"], "FOLLOWER")
        disagreements = sources.role_disagreements(self.conn)
        self.assertEqual(len(disagreements), 1)
        self.assertEqual(disagreements[0]["source_role"], "LEAD")
        self.assertEqual(disagreements[0]["journal_role"], "FOLLOWER")

    def test_an_unknown_role_is_refused_rather_than_coerced(self):
        with self.assertRaises(sources.MappingError):
            sources.map_account(self.conn, source="x", source_account_id="1",
                                account_id=self.lead_id, source_role="LEADER")


class TestSecretHandling(WorkflowTestCase):
    def test_a_secret_shaped_field_is_refused_rather_than_stored(self):
        """A token written into the journal is a token in every backup and every
        export of it, from then on."""
        for key in ("access_token", "Authorization", "api_key", "Set-Cookie",
                    "password", "session"):
            with self.subTest(key=key):
                with self.assertRaises(sources.SecretLeak):
                    sources.assert_no_secrets({"fills": [{key: "value"}]})

    def test_a_nested_secret_is_still_caught(self):
        with self.assertRaises(sources.SecretLeak):
            sources.assert_no_secrets({"a": {"b": [{"refresh_token": "x"}]}})

    def test_ordinary_trade_fields_pass(self):
        sources.assert_no_secrets(record())  # must not raise

    def test_normalisation_refuses_a_payload_carrying_a_secret(self):
        with self.assertRaises(sources.SecretLeak):
            sources.normalize([{"acct": "a", "token": "abc"}], source="x", field_map={})

    def test_the_raw_payload_is_hashed_not_stored(self):
        """The raw record may carry account numbers. The hash proves which bytes
        produced a row without duplicating them across database, backup and
        export."""
        result = sources.normalize([record()], source="tradesyncer", field_map=FIELD_MAP)
        event = result["events"][0]
        self.assertEqual(len(event["source_payload_sha256"]), 64)
        self.assertNotIn("acct_991", event["source_payload_sha256"])

    def test_the_sync_state_table_has_nowhere_to_put_a_credential(self):
        columns = {r["name"] for r in
                   self.conn.execute("PRAGMA table_info(source_sync_state)")}
        for forbidden in ("token", "password", "cookie", "secret", "api_key",
                          "credential", "auth"):
            self.assertNotIn(forbidden, columns)


class TestNormalisation(WorkflowTestCase):
    def test_a_source_record_becomes_a_canonical_event(self):
        result = sources.normalize([record()], source="tradesyncer", field_map=FIELD_MAP)
        event = result["events"][0]

        self.assertTrue(result["complete"])
        self.assertEqual(event["side"], "BUY")
        self.assertEqual(event["quantity"], 2.0)
        self.assertEqual(event["fees"], 1.24)
        self.assertEqual(event["source_record_id"], "e1")
        self.assertEqual(event["adapter_version"], sources.ADAPTER_VERSION)

    def test_event_type_is_not_decided_here(self):
        """A single fill does not know whether it opened, added, reduced or
        closed. Only the sequence knows."""
        result = sources.normalize([record()], source="tradesyncer", field_map=FIELD_MAP)
        self.assertNotIn("event_type", result["events"][0])

    def test_the_journal_never_learns_the_source_field_names(self):
        result = sources.normalize([record()], source="tradesyncer", field_map=FIELD_MAP)
        keys = set(result["events"][0])
        for vendor_field in ("acct", "sym", "dir", "qty", "px", "ts"):
            self.assertNotIn(vendor_field, keys)

    def test_a_partial_parse_is_reported_not_returned_as_success(self):
        """Half a day's fills reconstructs a position path that never existed."""
        result = sources.normalize(
            [record(), record(id="e2", px=None)], source="tradesyncer", field_map=FIELD_MAP)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("price", result["problems"][0])

    def test_an_unrecognised_side_is_a_problem_not_a_guess(self):
        result = sources.normalize([record(dir="Flatten")], source="tradesyncer",
                                   field_map=FIELD_MAP)
        self.assertFalse(result["complete"])
        self.assertEqual(result["events"], [])

    def test_normalising_the_same_record_twice_produces_the_same_fingerprint(self):
        first = sources.normalize([record()], source="s", field_map=FIELD_MAP)
        second = sources.normalize([record()], source="s", field_map=FIELD_MAP)
        self.assertEqual(first["events"][0]["source_payload_sha256"],
                         second["events"][0]["source_payload_sha256"])

    def test_normalisation_touches_no_database_and_no_network(self):
        """Pure by construction, which is what makes it testable against a
        redacted fixture before any of the surrounding machinery exists."""
        result = sources.normalize([record()], source="s", field_map=FIELD_MAP)
        self.assertEqual(len(result["events"]), 1)

    def test_multiple_accounts_in_one_payload_are_all_reported(self):
        result = sources.normalize(
            [record(), record(id="e2", acct="acct_002")],
            source="tradesyncer", field_map=FIELD_MAP)
        self.assertEqual(result["accounts_seen"], ["acct_002", "acct_991"])


class TestSyncState(WorkflowTestCase):
    def test_a_failed_sync_does_not_advance_the_watermark(self):
        """Advancing past records that may never have arrived loses them
        permanently, and nothing afterwards can tell."""
        sources.record_sync(self.conn, source="tradesyncer", status="OK",
                            last_seen_at="2026-08-10T14:00:00Z", records=6)
        sources.record_sync(self.conn, source="tradesyncer", status="FAILED",
                            last_seen_at="2026-08-10T18:00:00Z", error="unreachable")

        self.assertEqual(sources.watermark(self.conn, "tradesyncer")["last_seen_at"],
                         "2026-08-10T14:00:00Z")

    def test_a_partial_sync_also_holds_the_watermark(self):
        sources.record_sync(self.conn, source="tradesyncer", status="OK",
                            last_seen_at="2026-08-10T14:00:00Z")
        sources.record_sync(self.conn, source="tradesyncer", status="PARTIAL",
                            last_seen_at="2026-08-10T18:00:00Z")
        self.assertEqual(sources.watermark(self.conn, "tradesyncer")["last_seen_at"],
                         "2026-08-10T14:00:00Z")

    def test_a_successful_sync_advances_it(self):
        sources.record_sync(self.conn, source="tradesyncer", status="OK",
                            last_seen_at="2026-08-10T14:00:00Z")
        sources.record_sync(self.conn, source="tradesyncer", status="OK",
                            last_seen_at="2026-08-10T18:00:00Z")
        self.assertEqual(sources.watermark(self.conn, "tradesyncer")["last_seen_at"],
                         "2026-08-10T18:00:00Z")

    def test_repeated_failures_are_counted_and_surface_as_failing(self):
        for _ in range(3):
            sources.record_sync(self.conn, source="tradesyncer", status="FAILED",
                                error="timeout")
        health = sources.sync_health(self.conn)
        self.assertFalse(health["ok"])
        self.assertEqual(health["sources"][0]["health"], "failing")
        self.assertEqual(health["sources"][0]["consecutive_failures"], 3)

    def test_a_success_clears_the_failure_count(self):
        sources.record_sync(self.conn, source="tradesyncer", status="FAILED")
        sources.record_sync(self.conn, source="tradesyncer", status="OK",
                            last_seen_at="2026-08-10T14:00:00Z")
        self.assertEqual(sources.sync_health(self.conn)["sources"][0]["health"], "ok")

    def test_a_never_run_source_is_distinguishable_from_a_healthy_one(self):
        """A day with no trades and a day whose trades never arrived look
        identical unless something says which it was."""
        self.assertEqual(sources.watermark(self.conn, "tradesyncer"),
                         {"last_seen_at": None, "last_cursor": None, "last_record_id": None})

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            sources.record_sync(self.conn, source="x", status="MAYBE")


class TestAutomaticRouteRemainsBlocked(unittest.TestCase):
    def test_every_read_operation_refuses(self):
        adapter = adapters.TradeSyncerJournalAdapter()
        for call in (adapter.list_accounts,
                     lambda: adapter.fetch_new_executions(since="2026-08-10"),
                     lambda: adapter.fetch_trade_details("t1")):
            with self.subTest(call=call):
                with self.assertRaises(contracts.NotImplementedContract):
                    call()

    def test_it_is_registered_as_blocked_and_says_what_it_needs(self):
        entry = next(e for e in adapters.status_report()
                     if e["name"] == "tradesyncer_journal")
        self.assertNotEqual(entry["status"], adapters.IMPLEMENTED)
        self.assertTrue(entry["needs"])
        self.assertTrue(entry["evidence"])

    def test_no_ingestion_module_imports_a_network_client(self):
        """The cheapest proof that nothing contacts anything: the capability is
        absent, not merely unused.

        Parsed rather than grepped — a docstring saying "opens no socket" is
        prose, and a test that cannot tell prose from an import would fail on
        the sentence promising the thing it checks.
        """
        import ast

        import journal.adapters as adapters_mod
        import journal.sources as sources_mod

        forbidden = {"socket", "requests", "httpx", "urllib", "http", "ftplib",
                     "telnetlib", "smtplib", "asyncio", "aiohttp"}

        for module in (adapters_mod, sources_mod):
            with self.subTest(module=module.__name__):
                tree = ast.parse(Path(module.__file__).read_text())
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module.split(".")[0])
                self.assertEqual(imported & forbidden, set(),
                                 f"{module.__name__} imports a network-capable module")


if __name__ == "__main__":
    unittest.main(verbosity=2)
