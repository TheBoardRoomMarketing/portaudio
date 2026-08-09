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

from . import backup, config, contracts, db, enrich, importers, metrics, reports, repo
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

    server = None
    try:
        from . import api
        server = api.serve(args.host, args.port, verbose=args.verbose)
    except OSError as exc:
        print(f"could not bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.port}/"
    print(f"Trading Journal running at {url}")
    print(f"database {config.DB_PATH}")
    print("local only — nothing is exposed to the network. ctrl-c to stop.")
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
    conn = db.connect()
    try:
        archive = backup.create(conn, label=args.label)
    finally:
        conn.close()
    check = backup.verify(archive)
    _out({"archive": str(archive), "verification": check,
          "pruned": backup.prune(args.keep)})
    return 0 if check["ok"] else 1


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
        _out(reports.friction(conn))
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
    s.set_defaults(func=cmd_serve)

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
    s.set_defaults(func=cmd_backup)

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
