"""Tests for the two guarantees real capture depends on.

Both failures these cover are quiet and permanent. Synthetic rows averaged into
a real distribution cannot be separated out afterwards by looking at the
numbers, and a backup encrypted with a key nobody can retrieve is not a backup.
Everything here is about proving those two things before Zack starts recording
days that will not happen again.
"""

from __future__ import annotations

import http.cookiejar
import sqlite3
import sys
import tarfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import (access, backup, bias, bias_outcome, config, crypto, demo,  # noqa: E402
                     reports, repo, trades)
from tests.test_workflow import WorkflowTestCase, at  # noqa: E402


# =============================================================================
# Real / synthetic separation
# =============================================================================
class DemoTestCase(WorkflowTestCase):
    """One real trade on the lead account, one synthetic trade beside it."""

    def demo_event(self, kind, hhmm, qty, price, side):
        return trades.record_event(
            self.conn, account_id=self.lead_id, instrument_id=self.instrument_id,
            event_type=kind, occurred_at=at(hhmm, 0), quantity=qty, price=price,
            side=side, source="demo", fill_external_id=f"demo-{kind}-{hhmm}",
            is_demo=True)

    def add_demo_rows(self):
        self.demo_event("OPEN", "11:00", 1, 23300.00, "BUY")
        self.demo_event("CLOSE", "11:10", 1, 23320.00, "SELL")
        self.conn.commit()


class TestDemoSeparation(DemoTestCase):
    def test_demo_rows_are_counted_separately_from_real_ones(self):
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.event(self.lead_id, "CLOSE", "10:31", 2, 23208.75, "SELL")
        self.conn.commit()
        self.add_demo_rows()

        state = demo.status(self.conn)
        self.assertEqual(state["demo_by_table"]["execution_event"], 2)
        self.assertEqual(state["real_by_table"]["execution_event"], 2)
        self.assertTrue(state["mixed"])
        self.assertFalse(state["clean_for_real_use"])

    def test_a_clean_database_says_so(self):
        self.assertTrue(demo.status(self.conn)["clean_for_real_use"])
        demo.assert_clean(self.conn)  # must not raise
        self.assertEqual(demo.guard_report(self.conn), [])

    def test_assert_clean_refuses_when_synthetic_rows_are_present(self):
        self.add_demo_rows()
        with self.assertRaises(demo.DemoContaminationError) as ctx:
            demo.assert_clean(self.conn)
        # The message has to name the tables, or it is not actionable.
        self.assertIn("execution_event", str(ctx.exception))

    def test_mixed_data_is_warned_about_in_plain_words(self):
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.conn.commit()
        self.add_demo_rows()
        warnings = demo.guard_report(self.conn)
        self.assertEqual(len(warnings), 1)
        self.assertIn("synthetic", warnings[0])

    def test_purge_removes_synthetic_rows_and_leaves_real_ones(self):
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.event(self.lead_id, "CLOSE", "10:31", 2, 23208.75, "SELL")
        self.conn.commit()
        self.add_demo_rows()

        result = demo.purge(self.conn)

        self.assertEqual(result["removed"]["execution_event"], 2)
        self.assertTrue(result["now"]["clean_for_real_use"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM execution_event").fetchone()["c"], 2)

    def test_purge_restores_the_append_only_trigger(self):
        """A purge lifts the delete guard. If it stays lifted, RAW is no longer
        append-only and nobody would notice until evidence went missing."""
        self.add_demo_rows()
        demo.purge(self.conn)

        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.conn.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM execution_event")

    def test_purge_on_a_clean_database_is_a_no_op(self):
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.conn.commit()
        result = demo.purge(self.conn)
        self.assertEqual(result["removed"], {})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM execution_event").fetchone()["c"], 1)

    def test_the_journal_payload_is_never_both_modes_at_once(self):
        """The single most dangerous view would be one that shows real and
        synthetic days in the same list, because averages taken from it look
        entirely normal."""
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.event(self.lead_id, "CLOSE", "10:31", 2, 23208.75, "SELL")
        self.conn.commit()
        self.add_demo_rows()
        trades.group_events(self.conn, self.day_id)

        real = repo.day_journal_payload(self.conn, demo=False)
        synthetic = repo.day_journal_payload(self.conn, demo=True)

        self.assertEqual(real["meta"]["mode"], "real")
        self.assertEqual(synthetic["meta"]["mode"], "demo")
        # The interface is told data is mixed even while showing only one side.
        self.assertTrue(real["meta"]["mixed"])
        for payload in (real, synthetic):
            flags = {t.get("is_demo") for day in payload["days"] for t in day.get("trades", [])}
            self.assertLessEqual(len(flags), 1, "a payload mixed real and demo trades")

    def test_every_demo_marked_table_actually_has_the_column(self):
        """The table list is the thing that goes stale. If a table loses or
        never had is_demo, the guard silently under-counts."""
        for table in demo.DEMO_MARKED_TABLES:
            with self.subTest(table=table):
                columns = {r["name"] for r in
                           self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
                self.assertIn("is_demo", columns)


# =============================================================================
# Backup encryption
# =============================================================================
class TestBackupEncryption(WorkflowTestCase):
    def test_the_cipher_round_trips_and_detects_tampering(self):
        result = crypto.self_test()
        self.assertTrue(result["round_trip"])
        self.assertTrue(result["tamper_detected"])
        self.assertTrue(result["wrong_key_rejected"])
        self.assertIn(result["cipher"], ("aes-256-gcm", "chacha20-poly1305"))

    def test_a_key_can_be_read_back_after_it_is_created(self):
        created = crypto.get_key(create=True)
        self.assertEqual(len(created), 32)
        self.assertEqual(crypto.get_key(create=False), created)

    def test_the_key_file_is_not_readable_by_others(self):
        crypto.get_key(create=True)
        mode = crypto._key_file_path().stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, f"key file is mode {mode:o}")

    def test_tests_never_touch_the_real_keychain(self):
        """The Keychain is scoped to the user, not to the data directory. Left on
        "auto", the first test to create a key would write into the developer's
        real login keychain and stay there — and every later test expecting no
        key would then find one. That is exactly how this suite passed on Linux
        and failed on macOS.
        """
        self.assertEqual(config.KEY_BACKEND, "file")
        self.assertEqual(crypto.key_source(), "file")

        crypto.get_key(create=True)
        # The key landed inside this test's temporary directory and nowhere else.
        self.assertTrue(crypto._key_file_path().exists())
        self.assertEqual(crypto._key_file_path().parent, Path(config.DATA_HOME))

    def test_a_fresh_data_home_starts_with_no_key(self):
        """The property the macOS failure violated: a brand new journal has no
        key, so its first backup is plain until someone opts in."""
        self.assertIsNone(crypto.get_key(create=False))
        self.assertFalse(backup.is_encrypted(backup.create(self.conn, label="first")))

    def test_key_status_never_reveals_the_key(self):
        key = crypto.get_key(create=True)
        rendered = repr(crypto.key_status())
        self.assertNotIn(key.hex(), rendered)
        self.assertNotIn(key.decode("latin-1"), rendered)

    def test_an_encrypted_archive_is_not_readable_without_the_key(self):
        archive = backup.create(self.conn, label="sealed", encrypt=True)
        self.assertTrue(backup.is_encrypted(archive))
        with self.assertRaises(tarfile.TarError):
            tarfile.open(archive, "r:gz")

    def test_an_encrypted_archive_verifies_and_restores(self):
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.conn.commit()

        archive = backup.create(self.conn, label="sealed", encrypt=True)
        check = backup.verify(archive)
        self.assertTrue(check["ok"], check["problems"])
        self.assertTrue(check["encrypted"])

        self.conn.close()
        result = backup.restore(archive)
        self.assertEqual(Path(result["restored_to"]), config.DB_PATH)

        self.conn = sqlite3.connect(str(config.DB_PATH))
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM execution_event").fetchone()["c"], 1)

    def test_a_tampered_archive_refuses_to_open(self):
        archive = Path(backup.create(self.conn, label="sealed", encrypt=True))
        blob = bytearray(archive.read_bytes())
        blob[-1] ^= 0x01
        archive.write_bytes(bytes(blob))
        with self.assertRaises(crypto.DecryptError):
            backup.verify(archive)

    def test_the_wrong_key_does_not_silently_produce_rubbish(self):
        key = crypto.get_key(create=True)
        blob = crypto.encrypt(b"a day of evidence", key)
        with self.assertRaises(crypto.DecryptError):
            crypto.decrypt(blob, bytes(32))

    def test_the_key_is_never_written_into_the_archive(self):
        key = crypto.get_key(create=True)
        archive = Path(backup.create(self.conn, label="sealed", encrypt=True))
        self.assertNotIn(key, archive.read_bytes())

    def test_encryption_is_sticky_once_a_key_exists(self):
        """Opting in is a one-time act. Nobody should have to remember a flag
        on every later backup for their data to stay encrypted."""
        plain = backup.create(self.conn, label="before")
        self.assertFalse(backup.is_encrypted(plain))

        crypto.get_key(create=True)
        after = backup.create(self.conn, label="after")
        self.assertTrue(backup.is_encrypted(after))

    def test_encryption_can_be_declined_explicitly(self):
        crypto.get_key(create=True)
        archive = backup.create(self.conn, label="plain", encrypt=False)
        self.assertFalse(backup.is_encrypted(archive))
        self.assertTrue(backup.verify(archive)["ok"])

    def test_plain_archives_still_work_unchanged(self):
        archive = backup.create(self.conn, label="plain")
        self.assertFalse(backup.is_encrypted(archive))
        self.assertTrue(backup.verify(archive)["ok"])

    def test_prune_counts_encrypted_and_plain_archives_together(self):
        backup.create(self.conn, label="a", encrypt=False)
        backup.create(self.conn, label="b", encrypt=True)
        backup.create(self.conn, label="c", encrypt=True)
        removed = backup.prune(keep=1)
        self.assertEqual(len(removed), 2)


# =============================================================================
# Phone access
# =============================================================================
class TestAccessPolicy(WorkflowTestCase):
    def test_loopback_needs_no_token_and_nothing_changed(self):
        plan = access.resolve_bind("127.0.0.1")
        self.assertFalse(plan["require_token"])
        self.assertIsNone(plan["token"])

    def test_a_wider_bind_switches_authentication_on_by_itself(self):
        """The property that matters: there is no flag combination that yields
        an unauthenticated service beyond loopback."""
        for host in ("100.101.102.103", "192.168.1.20", "10.0.0.5", "172.16.4.4"):
            with self.subTest(host=host):
                plan = access.resolve_bind(host)
                self.assertTrue(plan["require_token"])
                self.assertTrue(plan["token"])

    def test_a_publicly_routable_address_is_refused(self):
        for host in ("8.8.8.8", "93.184.216.34", "2606:4700::1111"):
            with self.subTest(host=host):
                with self.assertRaises(access.AccessError):
                    access.resolve_bind(host)

    def test_binding_every_interface_is_refused(self):
        for host in ("0.0.0.0", "::"):  # noqa: S104 — asserting the refusal
            with self.subTest(host=host):
                with self.assertRaises(access.AccessError):
                    access.resolve_bind(host)

    def test_a_hostname_is_refused_rather_than_resolved(self):
        """Resolving a name would make the bind depend on DNS at start-up, which
        is how a service ends up on an address nobody chose."""
        with self.assertRaises(access.AccessError):
            access.resolve_bind("example.com")

    def test_tailscale_addresses_are_recognised(self):
        self.assertEqual(access.classify("100.101.102.103"), "tailscale")
        self.assertEqual(access.classify("127.0.0.1"), "loopback")
        self.assertEqual(access.classify("8.8.8.8"), "public")

    def test_the_token_file_is_not_readable_by_others(self):
        access.rotate_token()
        mode = access._token_path().stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, f"token file is mode {mode:o}")

    def test_rotating_invalidates_the_previous_token(self):
        first = access.get_token(create=True)
        second = access.rotate_token()
        self.assertNotEqual(first, second)
        self.assertFalse(access.token_matches(first, access.get_token()))

    def test_token_comparison_rejects_empty_and_partial_values(self):
        token = access.get_token(create=True)
        for supplied in (None, "", token[:-1], token + "x"):
            with self.subTest(supplied=supplied):
                self.assertFalse(access.token_matches(supplied, token))
        self.assertTrue(access.token_matches(token, token))

    def test_token_status_does_not_reveal_the_token(self):
        token = access.get_token(create=True)
        self.assertNotIn(token, repr(access.token_status()))

    def test_the_guidance_never_proposes_exposing_anything(self):
        text = repr(access.guidance()).lower()
        for forbidden in ("port forward", "ngrok", "public url", "0.0.0.0"):
            self.assertNotIn(forbidden, text.replace("no port forwarding", "")
                             .replace("no bind to 0.0.0.0", ""))


class TestAccessEnforcement(WorkflowTestCase):
    """The policy is only worth as much as the server's enforcement of it."""

    def setUp(self):
        super().setUp()
        from journal import api

        self.token = access.rotate_token()
        self.server = api.JournalServer(
            ("127.0.0.1", 0), api.JournalHandler, db_path=config.DB_PATH,
            token=self.token, require_token=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, headers=None, opener=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                         headers=headers or {})
        try:
            with (opener or urllib.request.urlopen)(request) as response:
                return response.status, response.headers.get("Set-Cookie")
        except urllib.error.HTTPError as exc:
            # An HTTPError is itself an open response. Closing it keeps the test
            # output free of ResourceWarnings, which otherwise bury real failures.
            try:
                return exc.code, None
            finally:
                exc.close()

    def test_an_unauthenticated_request_is_refused(self):
        self.assertEqual(self.get("/api/health")[0], 401)

    def test_a_wrong_token_is_refused(self):
        self.assertEqual(
            self.get("/api/health", {"X-Journal-Token": "x" * 43})[0], 401)

    def test_static_files_are_gated_too(self):
        """An unauthenticated interface shell would be a small leak on its own
        and an invitation to look for an unguarded endpoint."""
        self.assertEqual(self.get("/")[0], 401)
        self.assertEqual(self.get("/app.js")[0], 401)

    def test_writes_are_gated(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/recompute", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        with ctx.exception as error:
            self.assertEqual(error.code, 401)

    def test_a_valid_token_works_and_is_swapped_for_a_cookie(self):
        status, cookie = self.get(f"/api/health?t={self.token}")
        self.assertEqual(status, 200)
        self.assertIn(access.COOKIE_NAME, cookie or "")
        self.assertIn("HttpOnly", cookie)

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)).open
        self.get(f"/api/health?t={self.token}", opener=opener)
        # The URL no longer carries the token; the cookie does.
        self.assertEqual(self.get("/api/health", opener=opener)[0], 200)

    def test_the_error_gives_no_hint_about_the_token(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/health")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        with ctx.exception as error:
            body = error.read().decode()
        self.assertNotIn(self.token, body)
        self.assertNotIn(str(len(self.token)), body)


# =============================================================================
# Bias outcome methodologies — dormant
# =============================================================================
class TestBiasOutcomeCandidates(unittest.TestCase):
    """Three candidates, none approved. The first two tests are the binding ones."""

    def price(self, **kw):
        base = {"open": 23180.0, "high": 23260.0, "low": 23150.0, "close": 23240.0,
                "atr": 100.0}
        base.update(kw)
        return bias_outcome.SessionPrice(**base)

    def test_no_methodology_is_active(self):
        self.assertIsNone(bias_outcome.ACTIVE_METHODOLOGY)
        self.assertTrue(all(v == "DORMANT_AWAITING_APPROVAL"
                            for v in bias_outcome.STATUS.values()))

    def test_evaluate_refuses_every_candidate(self):
        for name in bias_outcome.METHODOLOGIES:
            with self.subTest(name=name):
                with self.assertRaises(bias_outcome.NotApproved):
                    bias_outcome.evaluate(name, {"direction": "BULLISH"}, self.price())

    def test_a_dry_run_writes_nothing_and_says_so(self):
        result = bias_outcome.dry_run({"direction": "BULLISH"}, self.price())
        self.assertIn("Nothing was written", result["note"])
        self.assertIn("close_vs_open", result["verdicts"])

    def test_missing_price_yields_no_verdict_rather_than_a_guess(self):
        incomplete = bias_outcome.SessionPrice(open=23180.0)
        for name, fn in bias_outcome.METHODOLOGIES.items():
            with self.subTest(name=name):
                self.assertIsNone(fn({"direction": "BULLISH"}, incomplete))

    def test_a_non_directional_bias_is_not_scored(self):
        for direction in ("NEUTRAL", "UNSURE"):
            with self.subTest(direction=direction):
                self.assertIsNone(
                    bias_outcome.close_vs_open({"direction": direction}, self.price()))

    def test_close_vs_open_reads_the_direction_called(self):
        up = self.price(close=23240.0)
        self.assertEqual(
            bias_outcome.close_vs_open({"direction": "BULLISH"}, up).outcome, "CORRECT")
        self.assertEqual(
            bias_outcome.close_vs_open({"direction": "BEARISH"}, up).outcome, "INCORRECT")

    def test_a_flat_close_is_indeterminate_not_a_marginal_win(self):
        flat = self.price(close=23181.0)  # one point, well inside 0.15 ATR
        self.assertEqual(
            bias_outcome.close_vs_open({"direction": "BULLISH"}, flat).outcome,
            "INDETERMINATE")

    def test_excursion_can_disagree_with_the_close(self):
        """The reason there is more than one candidate: a day that ran the called
        way and gave it all back is a different fact depending on the question."""
        gave_it_back = self.price(high=23400.0, low=23170.0, close=23180.0)
        self.assertEqual(
            bias_outcome.close_vs_open({"direction": "BULLISH"}, gave_it_back).outcome,
            "INDETERMINATE")
        self.assertEqual(
            bias_outcome.favourable_excursion({"direction": "BULLISH"},
                                              gave_it_back).outcome, "CORRECT")

    def test_excursion_calls_a_two_sided_day_partial_or_indeterminate(self):
        even = self.price(high=23230.0, low=23130.0, close=23180.0)
        self.assertIn(
            bias_outcome.favourable_excursion({"direction": "BULLISH"}, even).outcome,
            ("PARTIALLY_CORRECT", "INDETERMINATE"))

    def test_invalidation_needs_a_level_and_says_nothing_without_one(self):
        self.assertIsNone(bias_outcome.invalidation_respected(
            {"direction": "BULLISH", "invalidation": "wrong below the low"}, self.price()))

    def test_a_breached_invalidation_level_is_incorrect(self):
        verdict = bias_outcome.invalidation_respected(
            {"direction": "BULLISH", "invalidation_level": 23160.0}, self.price())
        self.assertEqual(verdict.outcome, "INCORRECT")

    def test_a_respected_level_with_no_follow_through_is_partial(self):
        verdict = bias_outcome.invalidation_respected(
            {"direction": "BULLISH", "invalidation_level": 23100.0},
            self.price(high=23190.0, close=23185.0))
        self.assertEqual(verdict.outcome, "PARTIALLY_CORRECT")

    def test_every_verdict_carries_the_numbers_that_produced_it(self):
        for name, fn in bias_outcome.METHODOLOGIES.items():
            verdict = fn({"direction": "BULLISH", "invalidation_level": 23100.0},
                         self.price())
            with self.subTest(name=name):
                self.assertTrue(verdict.inputs, f"{name} returned a bare label")
                self.assertTrue(verdict.detail)

    def test_no_candidate_can_see_profit_and_loss(self):
        """A correct read traded badly must stay a correct read. The cheapest
        guarantee of that is that the input type has nowhere to put P&L."""
        fields = set(bias_outcome.SessionPrice.__dataclass_fields__)
        self.assertEqual(fields, {"open", "high", "low", "close", "atr"})

    def test_the_comparison_recommends_no_approval_yet(self):
        comparison = bias_outcome.compare()
        self.assertIsNone(comparison["active"])
        self.assertEqual(len(comparison["candidates"]), 3)
        self.assertIn("Do not approve one yet", comparison["recommendation"])


# =============================================================================
# Capture telemetry on the day model
# =============================================================================
class TestDayFriction(DemoTestCase):
    def drop_the_real_day(self):
        """The base fixture creates one real day. These two cases are about what
        happens without one."""
        self.conn.execute("DELETE FROM trading_day WHERE id=?", (self.day_id,))
        self.conn.commit()

    def test_no_days_reports_honestly_rather_than_zero(self):
        self.drop_the_real_day()
        result = reports.day_friction(self.conn)
        self.assertEqual(result["days"], 0)
        self.assertIn("no real trading days", result["note"])

    def test_synthetic_days_never_reach_the_friction_numbers(self):
        """Demo days would flatter every median. The view excludes them, and this
        is the test that notices if that ever stops being true."""
        self.drop_the_real_day()
        self.conn.execute(
            "INSERT INTO trading_day(day_date,tz,status,iso_week,iso_month,created_at,"
            "updated_at,is_demo) VALUES ('2026-08-11','America/New_York','PLANNED',"
            "'2026-W33','2026-08','x','x',1)")
        self.conn.commit()
        self.assertEqual(reports.day_friction(self.conn)["days"], 0)

    def test_a_missing_morning_counts_against_completion(self):
        """MISSING EVIDENCE IS NOT PERFECT PERFORMANCE: a day with no bias must
        lower the morning rate, not be excluded from the denominator."""
        result = reports.day_friction(self.conn)  # the real day from setUp, no bias
        self.assertEqual(result["days"], 1)
        self.assertEqual(result["completion"]["morning_bias"], 0.0)

    def test_capture_seconds_are_measured_and_unmeasured_days_are_named(self):
        bias.record(self.conn, self.day_id, direction="BULLISH", thesis="up",
                    capture_seconds=42)
        result = reports.day_friction(self.conn)
        self.assertEqual(result["completion"]["morning_bias"], 1.0)
        self.assertEqual(result["seconds"]["bias_median"], 42)
        self.assertEqual(result["seconds"]["unmeasured_days"], 0)

    def test_an_unmeasured_capture_is_not_counted_as_a_fast_one(self):
        bias.record(self.conn, self.day_id, direction="BULLISH", thesis="up")
        result = reports.day_friction(self.conn)
        self.assertIsNone(result["seconds"]["bias_median"])
        self.assertEqual(result["seconds"]["unmeasured_days"], 1)

    def test_the_unreviewed_backlog_is_visible(self):
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.event(self.lead_id, "CLOSE", "10:31", 2, 23208.75, "SELL")
        self.group()
        result = reports.day_friction(self.conn)
        self.assertEqual(result["backlog"]["unreviewed_trades"], 1)
        self.assertEqual(result["completion"]["trades_reviewed"], 0.0)

    def test_manual_burden_is_measured_because_it_is_what_an_export_would_fix(self):
        self.event(self.lead_id, "OPEN", "10:04", 2, 23180.25, "BUY")
        self.event(self.lead_id, "CLOSE", "10:31", 2, 23208.75, "SELL")
        self.group()
        burden = reports.day_friction(self.conn)["manual_burden"]
        self.assertEqual(burden["manual_events"], 2)
        self.assertEqual(burden["manual_share"], 1.0)

    def test_a_numeric_invalidation_level_is_captured_and_counted(self):
        bias.record(self.conn, self.day_id, direction="BULLISH",
                    invalidation="wrong below 23140", invalidation_level=23140.0)
        depth = reports.day_friction(self.conn)["bias_capture_depth"]
        self.assertEqual(depth["with_invalidation_level"], 1.0)
        self.assertEqual(depth["with_invalidation_text"], 1.0)

    def test_prose_invalidation_without_a_number_is_not_counted_as_one(self):
        bias.record(self.conn, self.day_id, direction="BULLISH",
                    invalidation="wrong below the overnight low")
        depth = reports.day_friction(self.conn)["bias_capture_depth"]
        self.assertEqual(depth["with_invalidation_text"], 1.0)
        self.assertEqual(depth["with_invalidation_level"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
