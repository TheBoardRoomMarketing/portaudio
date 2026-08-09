# Trading Journal

A local-first trading journal. Structured, queryable, versioned data underneath;
a calm reading interface on top.

Everything runs on your own machine. There is no account, no cloud service, no
broker connection, and no code path anywhere in this project that can place,
cancel, modify or route an order.

---

## Run it

Requires Python 3.9+ and nothing else. No packages to install.

```sh
python3 bin/journal init --account "Futures Eval 50k"   # create the database
python3 bin/journal serve --open                        # run the journal
```

The interface opens at `http://127.0.0.1:8765/`. To look around before entering
anything real:

```sh
python3 bin/journal seed        # ~6 weeks of synthetic sessions
```

To see the interface without running anything, open `data/trading-journal-demo.html`
in a browser — a single self-contained file with synthetic data, built by
`scripts/build_demo_bundle.py`.

## Everyday commands

| Command | What it does |
|---|---|
| `journal serve` | run the local application |
| `journal status` | integrity, row counts, coverage, integration status |
| `journal import <file> --profile <name> --account <label>` | import a broker export (read only) |
| `journal export` | write CSV + JSON to `data/exports` |
| `journal backup` | create a verified backup archive |
| `journal restore <archive>` | restore from an archive |
| `journal verify [archive]` | check an archive, or the live database |
| `journal recompute` | rebuild every derived metric |
| `journal weekly` | descriptive weekly report |
| `journal friction` | how much the journal costs to use |
| `journal review` | the real-use checkpoint report |
| `journal enrich-blinded` | run a blinded research enricher (storage only) |
| `journal contracts` | what is integrated and what is still blocked |

## Layout

```
journal/          the application: db, repo, metrics, importers, api, cli
migrations/       numbered SQL migrations; applied ones are immutable
web/              the interface — plain HTML, CSS and JavaScript, no build step
tests/            executable tests for the guarantees the journal makes
ops/              scheduled prompts (launchd on macOS, cron elsewhere)
scripts/          installer, synthetic fixtures, demo bundle builder
docs/             specification and design decisions
data/             your data. Not in version control.
```

## Where your data lives

Everything is under `data/`, and `data/journal.db` is a single SQLite file you
can copy, query with any tool, or walk away from.

**Do not put `data/journal.db` inside iCloud, Dropbox or Drive.** SQLite in WAL
mode inside a consumer sync folder is a known corruption path. `data/raw/`,
`data/media/` and `data/backups/` are safe to sync, and a backup archive is
enough to rebuild the database completely.

Move the whole data directory with `JOURNAL_HOME=/some/path`.

## Scheduled prompts

```sh
ops/install_schedule.sh          # macOS launchd agents; --uninstall removes them
```

Installs a 09:10 check-in prompt, a 16:05 check-out prompt, a nightly verified
backup, and a Friday weekly-review reminder. The operating system is the
scheduler — nothing depends on a terminal or an assistant staying alive. Every
run is recorded in `job_run` whether it succeeds or fails, so a prompt that
never fired is visible rather than merely absent.

## What it will not do

- No order routing, and no credential capable of it. If the only API token a
  broker offers carries trading authority, the journal does not use that broker's
  API; it reads file exports instead.
- No coaching, scoring or commentary during a live session. Capture before,
  observe during, review after.
- No research features in the daily interface. They are stored from day one
  against opaque keys and are unreadable from every daily view; Research Mode
  itself is deliberately not built yet.
- No invented numbers. Where something cannot be measured — MFE without market
  data, discretion value without a reference implementation — the journal shows
  a dash and says why, rather than a plausible figure.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

95 tests covering raw immutability, foreign keys, duplicate imports, strategy
version preservation, check-in amendment history, transcript immutability, AI
annotations staying non-canonical, blinded isolation, timezone handling, import
provenance, R and fee arithmetic, slippage sign convention, export round trips,
backup restore, the migration upgrade path, conformance-index composition,
descriptive-report guards and blinded enrichment provenance.

## Two process views

The journal reports process twice and never merges the two:

- **How I felt I traded** — `self_reported_process_index`, from your four
  evening ratings and the plan you set. Consistency, not objective quality.
- **What the record shows** — `mechanical_conformance_index`, counted from
  setups taken, rule violations, size discipline, overrides, stop discipline
  and whether you traded inside your declared window.

A component that cannot be measured is excluded from the score rather than
counted as perfect, and each session records which components were in it. When
the two views disagree the interface says so and nothing more — a difference is
information about the relationship between self-perception and record, not a
verdict on either.

## Documentation

`docs/` holds the specification. Start with `docs/00-product-requirements.md`.
To get this running on your own machine, follow
`docs/PHASE-3-LOCAL-RUNBOOK.md`. `docs/PHASE-3-RETURN.md` describes what the
current build does and does not do.
