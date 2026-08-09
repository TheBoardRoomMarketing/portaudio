"""Command line interface.

    journal init                 create the database and apply migrations
    journal serve                run the local app (this is the normal one)
    journal seed                 load synthetic fixtures into a scratch database
    journal status               integrity, counts, coverage, integration status
    journal recompute            rebuild the derived layer
    journal import <file>        import a broker export through a mapping profile
    journal profiles             list / add CSV mapping profiles
    journal export               write CSV + JSON to the export directory
    journal backup               create and verify a backup archive
    journal restore <archive>    restore from an archive
    journal verify <archive>     restore into scratch and check against manifest
    journal keys                 backup encryption key status (never prints the key)
    journal demo                 synthetic-data status, and purge before real capture
    journal access               phone access: addresses, token, what to do
    journal accounts             register accounts, set lead/follower roles
    journal job <name>           run a scheduled job (used by launchd/cron)
    journal weekly [week]        generate and store a descriptive weekly report
    journal friction             capture-friction telemetry
    journal review               the real-use checkpoint report
    journal enrich-blinded       run a blinded research enricher (storage only)
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from . import (access, backup, config, contracts, crypto, db, demo, enrich, importers,
               metrics, reports, repo)
from .db import utcnow


def _out(payload):
    print(json.dumps(payload, indent=2, default=str))


def cmd_init(args) -> int:
    config.ensure_dirs()
    conn = db.connect(restricted=False)
    try:
        applied = db.migrate(conn, verbose=True)
        if args.account:
            account_id = repo.ensure_account(conn, args.account, mode=args.mode)
            print(f"account: {args.account} (id {account_id}, {args.mode})")
        conn.commit()
    finally:
        conn.close()
    print(f"database: {config.DB_PATH}")
    print(f"data home: {config.DATA_HOME}")
    print(f"migrations applied: {len(applied)}")
    return 0


def cmd_serve(args) -> int:
    config.ensure_dirs()
    conn = db.connect(restricted=False)
    try:
        pending = db.migrate(conn)
        if pending:
            print(f"applied pending migrations: {', '.join(pending)}")
        conn.commit()
    finally:
        conn.close()

    host = args.host
    if args.phone and host == config.SERVER_HOST:
        # --phone means "the address the phone should use". Prefer Tailscale;
        # fall back to the LAN address, which is honestly labelled as weaker.
        candidates = [a for a in access.local_addresses() if a["kind"] != "loopback"]
        if not candidates:
            print("no Tailscale or private address found on this machine. "
                  "Run `journal access` for what to do about it.", file=sys.stderr)
            return 1
        host = candidates[0]["address"]

    server = None
    try:
        from . import api
        server = api.serve(host, args.port, verbose=args.verbose)
    except access.AccessError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"could not bind {host}:{args.port} — {exc}", file=sys.stderr)
        return 1

    plan = server.plan
    url = access.phone_url(host, args.port, plan["token"])
    print(f"Trading Journal running at http://{host}:{args.port}/")
    print(f"database {config.DB_PATH}")
    print(plan["transport"])
    if plan["require_token"]:
        # Printed to the terminal because it has to be typed into a phone once.
        # It is not written to the request log, and the first request swaps it
        # for a cookie so it stops travelling in URLs.
        print("\nOpen this once on the phone, then bookmark what the address bar shows:")
        print(f"  {url}")
        print("Rotate it any time with `journal access --rotate`.")
    else:
        print("local only — nothing is exposed to the network.")
    print("\nctrl-c to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def cmd_status(args) -> int:
    from . import api
    conn = db.connect()
    try:
        _out(api.status_report(conn))
    finally:
        conn.close()
    return 0


def cmd_recompute(args) -> int:
    conn = db.connect()
    try:
        _out(metrics.recompute_all(conn, note="cli"))
    finally:
        conn.close()
    return 0


def cmd_import(args) -> int:
    conn = db.connect()
    try:
        if args.dry_run:
            result = importers.parse_file(conn, Path(args.file), args.profile)
            result["rows"] = result["rows"][:5]
            result["note"] = "dry run — first 5 parsed rows shown, nothing written"
            _out(result)
            return 0 if result["ok"] else 1
        result = importers.import_file(conn, Path(args.file), args.profile,
                                       account_label=args.account,
                                       session_kind=args.session_kind)
        metrics.recompute_all(conn, note="import")
        _out(result)
    except importers.DuplicateImport as exc:
        print(f"already imported: {exc}", file=sys.stderr)
        return 2
    except importers.ImportError_ as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def cmd_profiles(args) -> int:
    conn = db.connect()
    try:
        if args.add_example:
            example = importers.EXAMPLE_PROFILE
            pid = importers.save_profile(conn, example["name"], example["column_map"],
                                         datetime_format=example["datetime_format"],
                                         notes=example["notes"])
            conn.commit()
            print(f"added profile '{example['name']}' (id {pid}) — NOT verified against a "
                  "real export; remap before use")
            return 0
        rows = [dict(r) for r in conn.execute(
            "SELECT id,name,kind,source_tz,verified_against_sample,notes FROM import_profile")]
        _out(rows)
    finally:
        conn.close()
    return 0


def cmd_export(args) -> int:
    conn = db.connect()
    try:
        _out(backup.export_all(conn, Path(args.dest) if args.dest else None))
    finally:
        conn.close()
    return 0


def cmd_backup(args) -> int:
    encrypt = True if args.encrypt else (False if args.no_encrypt else None)
    conn = db.connect()
    try:
        archive = backup.create(conn, label=args.label, encrypt=encrypt)
    finally:
        conn.close()
    # Verified by opening it the way a restore would, which for an encrypted
    # archive means the key is exercised too. An archive that verifies is an
    # archive that can actually be restored on this machine.
    check = backup.verify(archive)
    _out({"archive": str(archive), "encrypted": check["encrypted"],
          "cipher": crypto.cipher_name() if check["encrypted"] else None,
          "verification": check, "pruned": backup.prune(args.keep)})
    return 0 if check["ok"] else 1


def cmd_keys(args) -> int:
    """Inspect, create, export or restore the backup encryption key.

    The key value is shown only by --export, only to a terminal, and never
    appears in ordinary output.
    """
    if args.create:
        crypto.get_key(create=True)

    if args.export:
        # Refuse unless a human is looking. Redirecting to a file or a log is
        # exactly the outcome the "never printed to logs" rule exists to prevent,
        # and a TTY check is what distinguishes the two.
        if not sys.stdout.isatty():
            print("refusing to print the key to something that is not a terminal. "
                  "Run this interactively and copy it into a password manager.",
                  file=sys.stderr)
            return 2
        try:
            encoded = crypto.export_key()
        except crypto.NoKey as exc:
            print(str(exc), file=sys.stderr)
            return 1
        status = crypto.key_status()
        print("\nBackup encryption key — store this in a password manager NOW.\n")
        print(f"  {encoded}\n")
        print(f"  fingerprint  {status['fingerprint']}")
        print(f"  stored in    {status['location']}\n")
        print("Without it, every encrypted archive is permanently unopenable — "
              "including copies on another disk.")
        print("Restore it on a new machine with:  journal keys --import\n")
        return 0

    if args.off:
        had = crypto.forget_key()
        stranded = sorted(config.BACKUP_DIR.glob("journal-*.tar.gz.enc")) \
            if config.BACKUP_DIR.exists() else []
        for archive in stranded if args.discard_encrypted else []:
            archive.unlink()
        print("backup encryption is off — every backup from now on is a plain archive"
              if had else "there was no key; backups were already plain")
        if stranded and not args.discard_encrypted:
            print(f"\n{len(stranded)} existing archive(s) were encrypted with that key and "
                  "can no longer be opened.\nRe-run with --discard-encrypted to delete them, "
                  "then take a fresh backup.")
        elif args.discard_encrypted and stranded:
            print(f"deleted {len(stranded)} unopenable archive(s)")
        print("\nTurn it back on any time with: journal backup --encrypt")
        return 0

    if args.rotate:
        result = crypto.rotate_key()
        print(f"new key created (fingerprint {result['fingerprint']}) in {result['source']}")
        print(result["note"])
        return 0

    if getattr(args, "import_key", False):
        # Read from a prompt, not from argv: a command line lands in shell
        # history and is visible in a process listing while it runs.
        import getpass  # noqa: PLC0415 — only needed on this path

        encoded = getpass.getpass("Paste the exported key (input hidden): ")
        try:
            result = crypto.import_key(encoded, force=args.force)
        except (crypto.KeyError_, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"key restored to {result['source']} "
              f"(fingerprint {result['fingerprint']})"
              + (" — previous key replaced" if result["replaced"] else ""))
        print("Verify with:  journal verify <an existing archive>")
        return 0

    payload = {"key": crypto.key_status(), "cipher": crypto.cipher_name()}
    if args.self_test:
        payload["self_test"] = crypto.self_test()
    _out(payload)
    return 0 if payload["key"]["problem"] is None else 1


def cmd_access(args) -> int:
    """Phone access: what this machine offers, and the token. Advice, not action."""
    if args.rotate:
        access.rotate_token()
        print("token rotated — every existing phone bookmark is now invalid")
    if args.clear:
        print("token removed" if access.clear_token() else "no token to remove")
    payload = access.guidance()
    if args.show_token:
        token = access.get_token(create=False)
        payload["token_value"] = token or "(none — created on first non-loopback serve)"
    _out(payload)
    return 0


def cmd_accounts(args) -> int:
    """Register and describe accounts. The setup step before the first real day.

    Grouping is driven by the lead account, so exactly one account has to be
    marked LEAD before manual capture or import will produce logical trades —
    and until this existed, preflight could name that problem without offering
    any way to fix it.
    """
    conn = db.connect()
    try:
        if args.add or args.set:
            label = args.add or args.set
            row = conn.execute("SELECT id, role FROM account WHERE label=?",
                               (label,)).fetchone()
            if args.add and row:
                print(f"an account labelled {label!r} already exists; use --set to change it",
                      file=sys.stderr)
                return 1
            if args.set and not row:
                print(f"no account labelled {label!r}", file=sys.stderr)
                return 1

            account_id = row["id"] if row else repo.ensure_account(
                conn, label, broker=args.broker or "unspecified", mode=args.mode or "paper")

            if args.role == "LEAD":
                # One lead at a time. Two would make grouping ambiguous in a way
                # that produces plausible-looking trades on the wrong account.
                others = conn.execute(
                    "SELECT label FROM account WHERE role='LEAD' AND is_demo=0 AND id!=?",
                    (account_id,)).fetchall()
                if others:
                    print("already led by: " + ", ".join(o["label"] for o in others) +
                          ". Demote it first with --role FOLLOWER or --role STANDALONE.",
                          file=sys.stderr)
                    return 1

            updates, params = [], []
            for column, value in (("role", args.role), ("mode", args.mode),
                                  ("platform", args.platform), ("prop_firm", args.prop_firm),
                                  ("broker", args.broker), ("external_id", args.external_id)):
                if value:
                    updates.append(f"{column}=?")
                    params.append(value)
            if args.account_size:
                updates.append("account_size=?")
                params.append(float(args.account_size))
            if args.size_multiplier:
                updates.append("size_multiplier=?")
                params.append(float(args.size_multiplier))
            if args.copies:
                lead = conn.execute("SELECT id FROM account WHERE label=?",
                                    (args.copies,)).fetchone()
                if not lead:
                    print(f"no account labelled {args.copies!r} to copy from", file=sys.stderr)
                    return 1
                updates.append("copy_source_account_id=?")
                params.append(lead["id"])

            if updates:
                conn.execute(f"UPDATE account SET {', '.join(updates)} WHERE id=?",
                             params + [account_id])
            conn.commit()
            print(f"{'created' if args.add else 'updated'}: {label}")

        rows = [dict(r) for r in conn.execute(
            "SELECT a.id, a.label, a.role, a.mode, a.platform, a.prop_firm, a.account_size, "
            "a.size_multiplier, a.status, l.label AS copies "
            "FROM account a LEFT JOIN account l ON l.id=a.copy_source_account_id "
            "WHERE a.is_demo=0 ORDER BY CASE WHEN a.role='LEAD' THEN 0 ELSE 1 END, a.label")]
        leads = [r for r in rows if r["role"] == "LEAD"]
        _out({
            "accounts": rows,
            "lead": leads[0]["label"] if leads else None,
            "followers": sum(1 for r in rows if r["role"] == "FOLLOWER"),
            "ready_for_grouping": len(leads) == 1,
        })
    finally:
        conn.close()
    return 0 if rows else 1


def cmd_sources(args) -> int:
    """Source account mapping and sync health. Contacts nothing."""
    from . import sources

    conn = db.connect()
    try:
        if args.map:
            account = conn.execute("SELECT id FROM account WHERE label=?",
                                   (args.account,)).fetchone()
            if not account:
                print(f"no account labelled {args.account!r}", file=sys.stderr)
                return 1
            sources.map_account(conn, source=args.source, source_account_id=args.id,
                                source_label=args.label, account_id=account["id"],
                                source_role=args.role)
            print(f"{args.source}:{args.id} → {args.account}")
        elif args.retire:
            sources.retire_mapping(conn, args.source, args.id)
            print(f"{args.source}:{args.id} retired (kept for old data)")

        _out({"mappings": sources.mapping_report(conn),
              "role_disagreements": sources.role_disagreements(conn),
              "sync": sources.sync_health(conn),
              "adapters": [a for a in contracts.status_report()]})
    finally:
        conn.close()
    return 0


def cmd_preflight(args) -> int:
    """Everything that should be true before the first real trading day.

    One command rather than a checklist to remember, and it exits non-zero if
    anything is wrong so it can gate a script. Nothing here is fixed
    automatically — a preflight that quietly repairs things teaches you to stop
    reading it.
    """
    from . import bias_observation as obs

    problems, notes = [], []
    conn = db.connect()
    try:
        state = demo.status(conn)
        if not state["clean_for_real_use"]:
            problems.append(
                f"{state['demo_rows']} demo rows present. Run `journal demo --purge`.")

        integrity = db.integrity_check(conn)
        if not integrity["ok"]:
            problems.extend(integrity["problems"])

        lead = conn.execute(
            "SELECT label FROM account WHERE role='LEAD' AND is_demo=0 LIMIT 1").fetchone()
        if not lead:
            problems.append(
                "no account is marked LEAD. Grouping is driven by the lead account, "
                "so manual capture and import both need one.")
        else:
            notes.append(f"lead account: {lead['label']}")

        followers = conn.execute(
            "SELECT COUNT(*) c FROM account WHERE role='FOLLOWER' AND is_demo=0"
        ).fetchone()["c"]
        notes.append(f"{followers} follower accounts registered")
        if not followers:
            notes.append("no followers registered yet — copy quality stays empty until "
                         "there are some, which is accurate rather than broken")
    finally:
        conn.close()

    key = crypto.key_status()
    if not key["present"]:
        notes.append("backups are not encrypted yet. `journal backup --encrypt` turns it "
                     "on once and every later backup follows.")
    else:
        # The failure this guards against is total and silent: the machine dies,
        # the key dies with it, and archives sitting safely on another disk turn
        # out to be permanently unopenable.
        notes.append(
            f"backup key fingerprint {key['fingerprint']} — it exists only on this "
            "machine. If you have not put a copy in a password manager, run "
            "`journal keys --export` now; without it every archive is unopenable.")
    if key["problem"]:
        problems.append(key["problem"])

    archives = sorted(config.BACKUP_DIR.glob("journal-*.tar.gz*")) \
        if config.BACKUP_DIR.exists() else []
    if not archives:
        notes.append("no backup archive exists yet. Take one before the first real day.")

    admin = db.connect(restricted=False)
    try:
        leaks = obs.leak_check(admin)
    finally:
        admin.close()
    problems.extend(leaks)

    _out({
        "ready": not problems,
        "problems": problems,
        "notes": notes,
        "data_home": str(config.DATA_HOME),
        "confirmations": {
            "LIVE_TRADING_ACTIONS": "NONE",
            "BROKER_ORDER_CAPABILITY_ADDED": False,
            "RESEARCH_MODE_ENABLED": False,
            "PERSONAL_ASTRO_ANALYSIS_EXECUTED": False,
            "BIAS_OUTCOMES_ASSIGNED": False,
            "REAL_PERFORMANCE_CORRELATION_ANALYSIS": False,
        },
    })
    return 0 if not problems else 1


def cmd_demo(args) -> int:
    conn = db.connect()
    try:
        if args.purge:
            _out(demo.purge(conn))
        else:
            state = demo.status(conn)
            state["warnings"] = demo.guard_report(conn)
            _out(state)
            if args.require_clean and not state["clean_for_real_use"]:
                print("demo rows present — not clean for real capture", file=sys.stderr)
                return 1
    finally:
        conn.close()
    return 0


def cmd_restore(args) -> int:
    _out(backup.restore(Path(args.archive)))
    return 0


def cmd_verify(args) -> int:
    if args.archive:
        result = backup.verify(Path(args.archive))
    else:
        conn = db.connect()
        try:
            result = db.integrity_check(conn)
        finally:
            conn.close()
    _out(result)
    return 0 if result["ok"] else 1


def cmd_seed(args) -> int:
    from scripts import seed_synthetic  # noqa: PLC0415 - optional dev dependency
    return seed_synthetic.main(reset=args.reset)


def cmd_job(args) -> int:
    """Run a scheduled job. Invoked by launchd/cron, never by a chat session."""
    conn = db.connect()
    started = utcnow()
    status, detail = "ok", ""
    try:
        if args.name == "backup":
            archive = backup.create(conn, label="scheduled")
            check = backup.verify(archive)
            status = "ok" if check["ok"] else "failed"
            detail = f"{Path(archive).name}; verified={check['ok']}"
            backup.prune(30)
        elif args.name == "recompute":
            detail = json.dumps(metrics.recompute_all(conn, note="scheduled"))
        elif args.name in ("weekly_review", "monthly_review"):
            row = conn.execute(
                "SELECT iso_week FROM session ORDER BY session_date DESC LIMIT 1").fetchone()
            if not row:
                raise RuntimeError("no sessions to report on")
            payload = reports.store_weekly(conn, row["iso_week"])
            detail = f"{row['iso_week']}: {payload.get('sessions', 0)} sessions, descriptive only"
        elif args.name in ("pre_session_checkin", "post_session_checkout"):
            # The job's only duty is to make sure the session row exists and to
            # hand the human a URL. It never fills anything in on their behalf.
            account = conn.execute(
                "SELECT id,label FROM account WHERE active=1 ORDER BY id LIMIT 1").fetchone()
            if not account:
                raise RuntimeError("no active account; run `journal init --account`")
            date = args.date or utcnow()[:10]
            sid = repo.get_or_create_session(conn, date, account_id=account["id"],
                                             session_kind=args.session_kind)
            which = "morning" if args.name == "pre_session_checkin" else "evening"
            url = f"http://{config.SERVER_HOST}:{config.SERVER_PORT}/#/capture/{which}/{date}"
            detail = url
            print(url)
        else:
            raise ValueError(f"unknown job {args.name}")
    except Exception as exc:  # noqa: BLE001 - a failed job must be recorded
        status, detail = "failed", str(exc)
        print(f"job {args.name} failed: {exc}", file=sys.stderr)
    finally:
        conn.execute(
            "INSERT INTO job_run(job,scheduled_for,started_at,finished_at,status,detail)"
            " VALUES (?,?,?,?,?,?)",
            (args.name, args.date, started, utcnow(), status, detail))
        conn.commit()
        conn.close()
    return 0 if status == "ok" else 1


def cmd_weekly(args) -> int:
    conn = db.connect()
    try:
        week = args.week
        if not week:
            row = conn.execute(
                "SELECT iso_week FROM session ORDER BY session_date DESC LIMIT 1").fetchone()
            if not row:
                print("no sessions yet", file=sys.stderr)
                return 1
            week = row["iso_week"]
        _out(reports.store_weekly(conn, week) if args.store else reports.weekly(conn, week))
    finally:
        conn.close()
    return 0


def cmd_friction(args) -> int:
    conn = db.connect()
    try:
        # Two models, reported separately rather than blended: the session-model
        # numbers are real history, and the day-model numbers are the workflow
        # in use now. Averaging them would describe neither.
        _out({"days": reports.day_friction(conn), "sessions": reports.friction(conn)})
    finally:
        conn.close()
    return 0


def cmd_bias_methods(args) -> int:
    """The candidate bias-outcome methodologies. Nothing is approved or run."""
    from . import bias_outcome
    _out(bias_outcome.compare())
    return 0


def cmd_bias_observe(args) -> int:
    """The 20-day dry-run observation period.

    Runs on an unrestricted connection because the observation layer is
    deliberately unreadable from the daily one. Nothing here writes a bias
    outcome.
    """
    from . import bias_observation as obs

    conn = db.connect(restricted=False)
    try:
        if args.price:
            row = conn.execute(
                "SELECT id, day_date FROM trading_day WHERE day_date=? AND is_demo=0",
                (args.date,)).fetchone()
            if not row:
                print(f"no real trading day on {args.date}", file=sys.stderr)
                return 1
            obs.record_price(conn, row["id"], row["day_date"], open=args.open,
                             high=args.high, low=args.low, close=args.close,
                             atr=args.atr, source=args.source)
            print(f"price recorded for {args.date} from {args.source}")

        if args.run:
            _out(obs.run_all(conn))
        elif args.compare:
            _out(obs.comparison(conn))
        elif not args.price:
            _out({"progress": obs.progress(conn), "leaks": obs.leak_check(conn)})
    finally:
        conn.close()
    return 0


def cmd_review(args) -> int:
    conn = db.connect()
    try:
        result = reports.real_use_review(conn)
    finally:
        conn.close()
    _out(result)
    if not result.get("checkpoint_reached"):
        print(f"\nCheckpoint not yet reached: {result['sessions']} sessions "
              "(needs 20 sessions or 4 weeks).", file=sys.stderr)
    return 0


def cmd_enrich_blinded(args) -> int:
    """Storage only. Nothing here reads a feature back or analyses anything."""
    try:
        engine = enrich.load_engine(args.engine)
    except enrich.EnrichmentError as exc:
        print(f"could not load engine: {exc}", file=sys.stderr)
        return 1

    conn = db.connect(restricted=False)
    try:
        result = enrich.enrich(conn, engine, since=args.since, until=args.until,
                               source_hypothesis=args.hypothesis, overwrite=args.overwrite)
        result["coverage"] = enrich.coverage(conn)
    except enrich.EnrichmentError as exc:
        print(f"enrichment refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    _out(result)
    return 0


def cmd_contracts(args) -> int:
    _out(contracts.status_report())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="journal", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create the database and apply migrations")
    s.add_argument("--account", help="create an account with this label")
    s.add_argument("--mode", default="paper", choices=["paper", "live", "sim_eval"])
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("serve", help="run the local application")
    s.add_argument("--host", default=config.SERVER_HOST)
    s.add_argument("--port", type=int, default=config.SERVER_PORT)
    s.add_argument("--open", action="store_true", help="open a browser window")
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--phone", action="store_true",
                   help="bind the address the phone can reach (Tailscale if present, "
                        "otherwise LAN). Token authentication is switched on.")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("access", help="phone access: addresses, token, what to do")
    s.add_argument("--rotate", action="store_true", help="mint a new token")
    s.add_argument("--clear", action="store_true", help="remove the token")
    s.add_argument("--show-token", action="store_true", help="print the token value")
    s.set_defaults(func=cmd_access)

    s = sub.add_parser("status", help="integrity, counts, coverage, integrations")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("recompute", help="rebuild the derived layer")
    s.set_defaults(func=cmd_recompute)

    s = sub.add_parser("import", help="import a broker export (read only)")
    s.add_argument("file")
    s.add_argument("--profile", required=True)
    s.add_argument("--account", required=True)
    s.add_argument("--session-kind", default=config.DEFAULT_SESSION_KIND)
    s.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("profiles", help="CSV mapping profiles")
    s.add_argument("--add-example", action="store_true")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("export", help="write CSV + JSON exports")
    s.add_argument("--dest")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("backup", help="create and verify a backup archive")
    s.add_argument("--label", default="")
    s.add_argument("--keep", type=int, default=30)
    s.add_argument("--encrypt", action="store_true",
                   help="encrypt the archive, creating a key if there is none yet")
    s.add_argument("--no-encrypt", action="store_true",
                   help="write a plain archive even though a key exists")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("keys", help="backup encryption key: status, escrow, restore")
    s.add_argument("--create", action="store_true", help="create the key if absent")
    s.add_argument("--self-test", action="store_true",
                   help="round-trip and tamper-check with an ephemeral key")
    s.add_argument("--export", action="store_true",
                   help="print the key once, to a terminal only, for a password manager")
    s.add_argument("--import", dest="import_key", action="store_true",
                   help="restore an exported key on a new machine (prompts, hidden)")
    s.add_argument("--force", action="store_true",
                   help="with --import, replace a different existing key")
    s.add_argument("--rotate", action="store_true",
                   help="replace the key; reports archives stranded by it")
    s.add_argument("--off", action="store_true",
                   help="delete the key and stop encrypting backups")
    s.add_argument("--discard-encrypted", action="store_true",
                   help="with --off, delete archives that can no longer be opened")
    s.set_defaults(func=cmd_keys)

    s = sub.add_parser("accounts", help="register accounts and set lead/follower roles")
    s.add_argument("--add", metavar="LABEL", help="create an account")
    s.add_argument("--set", metavar="LABEL", help="change an existing account")
    s.add_argument("--role", choices=["LEAD", "FOLLOWER", "STANDALONE"])
    s.add_argument("--mode", choices=["paper", "live", "sim_eval"])
    s.add_argument("--copies", metavar="LEAD_LABEL",
                   help="the lead account this one copies from")
    s.add_argument("--size-multiplier", help="contracts relative to the lead, e.g. 1.0")
    s.add_argument("--platform", help="TradeSea, Tradovate, NinjaTrader…")
    s.add_argument("--prop-firm", help="Tradeify, BluSky, Lucid…")
    s.add_argument("--account-size", help="e.g. 100000")
    s.add_argument("--broker")
    s.add_argument("--external-id", help="the platform's own identifier")
    s.set_defaults(func=cmd_accounts)

    s = sub.add_parser("sources", help="external source mapping and sync health")
    s.add_argument("--map", action="store_true", help="bind a source account to ours")
    s.add_argument("--retire", action="store_true", help="close a mapping")
    s.add_argument("--source", default="tradesyncer")
    s.add_argument("--id", help="the source's own account identifier")
    s.add_argument("--label", default="", help="what the source calls it")
    s.add_argument("--account", help="journal account label")
    s.add_argument("--role", default="UNKNOWN", choices=["LEAD", "FOLLOWER", "UNKNOWN"],
                   help="the role the SOURCE reports; never overwrites ours")
    s.set_defaults(func=cmd_sources)

    s = sub.add_parser("preflight",
                       help="everything that should be true before the first real day")
    s.set_defaults(func=cmd_preflight)

    s = sub.add_parser("demo", help="synthetic-data status, and purge before real capture")
    s.add_argument("--purge", action="store_true", help="delete every demo row")
    s.add_argument("--require-clean", action="store_true",
                   help="exit non-zero if any demo row is present")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("restore", help="restore from an archive")
    s.add_argument("archive")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("verify", help="verify an archive, or the live database")
    s.add_argument("archive", nargs="?")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("seed", help="load synthetic fixtures")
    s.add_argument("--reset", action="store_true", help="delete the database first")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("job", help="run a scheduled job (for launchd/cron)")
    s.add_argument("name", choices=["pre_session_checkin", "post_session_checkout",
                                    "weekly_review", "monthly_review", "backup", "recompute"])
    s.add_argument("--date")
    s.add_argument("--session-kind", default=config.DEFAULT_SESSION_KIND)
    s.set_defaults(func=cmd_job)

    s = sub.add_parser("weekly", help="generate a descriptive weekly report")
    s.add_argument("week", nargs="?", help="ISO week, e.g. 2026-W32 (default: most recent)")
    s.add_argument("--store", action="store_true", help="save it to the report table")
    s.set_defaults(func=cmd_weekly)

    s = sub.add_parser("friction", help="capture-friction telemetry")
    s.set_defaults(func=cmd_friction)

    s = sub.add_parser("bias-methods",
                       help="candidate bias-outcome methodologies (none approved)")
    s.set_defaults(func=cmd_bias_methods)

    s = sub.add_parser("bias-observe",
                       help="20-day dry-run observation (writes no bias outcome)")
    s.add_argument("--price", action="store_true", help="record a day's session price")
    s.add_argument("--date", help="trading day, YYYY-MM-DD")
    s.add_argument("--open", type=float)
    s.add_argument("--high", type=float)
    s.add_argument("--low", type=float)
    s.add_argument("--close", type=float)
    s.add_argument("--atr", type=float)
    s.add_argument("--source", default="", help="where the prices came from")
    s.add_argument("--run", action="store_true", help="dry-run every eligible day")
    s.add_argument("--compare", action="store_true", help="the comparison report")
    s.set_defaults(func=cmd_bias_observe)

    s = sub.add_parser("review", help="the real-use checkpoint report")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("enrich-blinded", help="run a blinded research enricher (storage only)")
    s.add_argument("--engine", required=True, help="package.module:Attribute")
    s.add_argument("--since", help="earliest session date")
    s.add_argument("--until", help="latest session date")
    s.add_argument("--hypothesis", default="unspecified",
                   help="source hypothesis or feature-set revision this run belongs to")
    s.add_argument("--overwrite", action="store_true",
                   help="recompute sessions already enriched at this engine version")
    s.set_defaults(func=cmd_enrich_blinded)

    s = sub.add_parser("contracts", help="integration contract status")
    s.set_defaults(func=cmd_contracts)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
