# Phase 2 return packet — production foundation

**For:** GPT-06 (director review)
**Status:** Phase 2 complete. Stopped before broker integration, reference
implementation, astro analysis, Research Mode, narrative generation and
correlation analysis, as instructed.
**Date:** 2026-08-09

All ten director rulings are implemented. The journal is usable manually today.

---

## Confirmations

```
LIVE_TRADING_ACTIONS:                  NONE
BROKER_ORDER_CAPABILITY_ADDED:         FALSE
PERSONAL_ASTRO_ANALYSIS_EXECUTED:      FALSE
RESEARCH_MODE_ENABLED:                 FALSE
REAL_PERFORMANCE_CORRELATION_ANALYSIS: FALSE
```

These are not assertions in prose. `journal status` reads them from the running
system, and the tests enforce them: no adapter may report READY, the reference
implementation raises rather than returning a number, and the daily database
connection is refused access to the research layer by the database itself.

---

## 1–4. Location, repository, commits

| | |
|---|---|
| **Starting commit** | `f1a15d4` — Phase 1 spec, schema and prototype |
| **Ending commit** | see the PR head on `claude/trading-journal-v1-spec-yw7w8r` |
| **Project location** | `trading-journal/` in the working repository |
| **Final location** | **not reachable from this session** — see below |
| **Standalone repo** | prepared, not created; the installer does it |

**The move to `~/AI-Projects/projects/trading-journal/` could not be performed.**
This phase was built in a cloud container whose `HOME` is `/root`; that path does
not exist here and creating it would be meaningless. The destination is on your
machine and no cloud session can reach it.

What was delivered instead is `scripts/install_local.sh`, which performs the move
with the verification the ruling requires:

- refuses to proceed if the destination holds anything that is not this project,
  and lists what it found;
- refuses to overwrite an existing install that already has a `journal.db`,
  pointing at `git pull` instead — a database is longitudinal data, not a build
  artifact;
- checks Python and SQLite versions before copying anything;
- `git init` + first commit, making it standalone;
- runs `journal init` and prints the next commands.

Verified in this container: the dry run reports correctly, and pointed at an
occupied directory it stops and names the conflicting contents.

```sh
scripts/install_local.sh --dry-run       # inspect
scripts/install_local.sh                 # move, init, make standalone
```

## 5. Files created and modified

32 files in this phase. 4,078 lines of Python across the application, fixtures
and tests; 2,503 lines of interface.

```
journal/          config, db, repo, metrics, importers, contracts, backup, api, cli
migrations/       0003_phase2_foundation.sql (0001 and 0002 unchanged)
web/              app.js rewired to live data; capture forms; Research Mode locked
tests/            test_journal.py — 66 tests
ops/              install_schedule.sh — launchd agents, cron equivalent documented
scripts/          install_local.sh, seed_synthetic.py, build_demo_bundle.py
bin/journal       entry point
```

## 6. Migration and schema status

Three migrations, all applied, current `0003_phase2_foundation.sql`.
**38 tables, 9 views, 6 immutability triggers.**

`0003` implements four rulings:

- **D9** — `session` rebuilt with `session_kind ∈ (NY_AM, NY_PM, OVERNIGHT, OTHER)`
  and uniqueness on `(date, kind, account)`. A date holds as many sessions as it
  needs, with no future migration. `v_trading_day` aggregates them for daily
  review. The rebuild was necessary because the old `UNIQUE(date, account)` table
  constraint owns an implicit index SQLite cannot drop — which made this a real
  exercise of the migration path rather than a trivial one.
- **D1** — `process_score` renamed to `self_reported_process_index`;
  `mechanical_conformance_index` added as a separate nullable column. The two are
  never merged.
- **D4** — `trade.entry_source ∈ (manual, file_import, api_import, bot_log, backfilled)`
  plus `import_batch_id`, so hand-keyed data cannot pass as imported.
- **CSV mapping profiles** — `import_profile` stores one broker's column mapping
  as data, so a new broker is a row, not a code change.

Applied migrations are immutable: the runner stores each file's hash and refuses
to run if an applied migration has been edited since.

## 7. Tests

**66 tests, 66 passing, 0 failing, 2.2 seconds.**

```sh
python3 -m unittest discover -s tests -v
```

Every case the ruling named is executable. Coverage by group:

| Group | Cases |
|---|---|
| RAW immutability | 4 — update and delete refused on `raw_record`, `trade_fill`, `raw_import_batch` |
| Foreign keys | 3 — enforcement on, orphan rejected, integrity clean after normal use |
| Strategy version immutability | 3 — rules frozen; a v2.0 publish does not rewrite v1.0 trades |
| Check-in amendments | 2 — old value preserved per field; unchanged fields write no amendment |
| Voice transcript immutability | 2 — rewrite refused; first attachment allowed |
| AI annotation non-canonical | 4 — suggestion creates no label; accept records its origin; reject creates none; **an unconfirmed suggestion never reaches a statistic** |
| Blinded isolation | 5 — daily connection refused; all four research views refused; payload and routine export both scanned for leakage |
| Timezone and session linking | 6 — DST correctness both directions, round trip, multiple kinds per date |
| R calculation | 6 — long, short, full stop, breakeven, size-weighted partials, NULL without a stop |
| Fee aggregation | 2 — across every fill, and split across synthesised legs |
| Slippage sign | 3 — adverse is negative for **both** long and short; favourable positive |
| Capture percentage | 3 — NULL on losers, correct on winners, NULL when nothing was measured |
| Process index | 2 — **no P&L term**, and the mechanical index stays NULL |
| Import provenance | 7 — manual vs imported, coverage split, duplicate refused, partial re-import adds only new rows, parse errors write nothing |
| Integration contracts | 3 — the placeholder raises rather than inventing; nothing claims READY |
| Export round trip | 2 | Backup and restore | 5 | Migration upgrade path | 3 |

The tests found three real bugs, all fixed:

1. **`backup.create` deadlocked** against a connection holding an uncommitted
   transaction. `conn.backup()` blocks indefinitely there. A backup now commits
   first, on the reasoning that taking a backup is a natural commit point and a
   snapshot silently missing recent work is worse than an implicit commit.
2. **`save_profile` had a parameter-binding error** — nine placeholders, eight
   values. Every import profile save would have failed.
3. **`journal status` tried to count blinded rows on the daily connection** and
   was refused by its own authorizer. The count now comes from a separate
   connection: how many rows exist is operational, what they contain is not.

Two further defects were found by inspection rather than by test, and are worth
recording because both are the failure mode the ruling warned about:

4. **`user_value_added_r` was reporting a confident wrong number.** With no
   reference implementation there are no `mechanical_reference` rows, so the sum
   was zero and the metric read "discretion cost you 2.00R" when nothing had been
   compared against anything. It is now NULL when no reference exists, and the
   interface says *"not computable until a strategy has one"*.
5. **`r_captured_pct` on losers** — carried over from Phase 1; a −1R trade that
   saw +0.6R produced −164%. NULL, and tested.

## 8. Database initialisation

```sh
python3 bin/journal init --account "Futures Eval 50k"
```

Creates `data/`, applies all migrations, creates the account. Idempotent.
`journal serve` also applies pending migrations on start, so an upgrade cannot
be forgotten. `journal verify` runs physical integrity, foreign key check, and
six logical invariants the schema cannot express as constraints.

## 9. Today screen — three states

Implemented as specified, as one screen with a segmented control.

- **Before open** — check-in summary, session plan, planned strategies, risk plan,
  the well-traded-day definition, objective market context, scheduled prompts.
- **Mid-session** — trades so far, qualified setups so far, **risk used against
  planned risk**. No process score, no ratings, no research, no judgement of any
  kind. This is D10 in the interface: capture, observe, review.
- **After close** — summary, self-reported process index, trade cards, qualified
  and missed setups, reflection, review link.

## 10–11. Mobile capture

Both forms work and save. Morning: five sliders, a bias control and one required
sentence, with thirteen further fields behind "More". Evening: four ratings, six
mistake toggles writing straight into the taxonomy, the well-traded verdict, and
two narrative fields.

Verified at 390 px: no horizontal overflow on any screen, bottom tab bar, forms
usable one-handed. Editing a saved check-in produces an amendment and the
interface says so — *"the previous answer was kept as an amendment"*.

## 12. Session Review

The core screen, unchanged in character from the accepted prototype: summary
header, vertical timeline with expandable entries, trade cards, opportunity
events, mistake markers, pre and post state, manual/bot labels, voice transcript
with provenance, narrative placeholder, process review.

The proof screen is **Wednesday 5 August**: −$97.08, −2.00R, self-reported
process index **100**, "well traded: yes". Alongside **Thursday 6 August**:
+$304.80, +4.13R, process index **29**, six flags, "well traded: no". Both are in
the fixtures precisely so that any future change equating P&L with process is
visible immediately.

## 13. Trade Detail

Production shell complete: large chart area with entry, exit, stop, target, MFE
and MAE geometry; R, net, fees; strategy and version; **order and fill history**;
opportunity link; market context; notes; screenshot slots; voice transcript;
mechanical reference placeholder that reads "—" rather than a number.

## 14. History

Monthly calendar, one cell per day, R and markers in words as well as colour,
click through to the session. Month totals above.

## 15–16. Manual capture

**This works before any automation exists, which was the point of the phase.**

- **Trades** — full form: instrument, strategy, direction, execution mode,
  quantity and planned quantity, prices, stop, target, times, fees, optional
  excursion prices and notes. Written through the same `repo.record_trade` the
  importer uses, so a hand-keyed trade still passes through the RAW layer as a
  batch with synthesised fills.
- **Setups** — strategy, instrument, time, direction, one of the eight statuses,
  and why. Recorded as `human_logged`.

Provenance is displayed, not just stored. The Capture screen shows a coverage
card: currently `file_import 32 / manual 1` and `engine 35 / human_logged 1`,
with the standing note that *opportunity coverage is only complete for
engine-detected setups; human-logged ones record what was noticed, not what
occurred.*

## 17. Strategy and version registry

`publish_strategy_version` writes an immutable version with a rule hash, closes
the previous one at the new one's start date, and every trade resolves the
version in force on its own date. The fixtures contain a real transition: DORB
v0.9 holds 3 trades, v1.0 holds 11, and publishing v1.0 did not touch the v0.9
history. Tested directly.

## 18. Mistake taxonomy

15 codes in 5 categories, seeded and enforced by foreign key. The evening
checkboxes write into it; `MISSED` and `BOT_FAILED` opportunities automatically
confirm `MISSED_SETUP` and `BOT_ROUTING_ERROR`.

## 19. Blinded storage

Live and tested. 124 feature rows written across 31 sessions in the fixtures,
against opaque keys (`pa_f01`, `pa_f17`, `ln_f01`), from a separate connection.

Four locks, all verified by test: separate table, opaque keys with the dictionary
in another table, a **connection authorizer that refuses to read any `r_` or
`blinded_` object on the daily connection**, and append-only unblind logging
architecture. Two tests scan the daily payload and every routine export file for
leakage.

Research Mode is **not built**. The screen exists and says why, and there is
deliberately no unlock control on it.

## 20. Voice notes

Storage shell complete: audio path and hash written first, before any
transcription runs, so a failed transcription still leaves the recording.
Transcript is immutable once written, enforced by trigger and tested. Model
summaries would land in `ai_annotation`, never in the transcript.

## 21. Exports

`journal export` writes 12 files: sessions, trading days, trades, fills,
opportunities, check-ins, amendments, tags, voice notes, media, strategy
versions, plus the full JSON payload. Round trip tested. **Routine exports carry
no blinded features**, and that is a test, not a convention.

## 22. Backup and restore

`journal backup` produces a `.tar.gz` containing a consistent snapshot via the
SQLite backup API, the raw imports, the media, the migration state, and a
manifest with per-file hashes and per-table row counts. It then verifies itself
by restoring into scratch and comparing against its own manifest.

`journal restore` refuses an archive that fails verification, and moves the
existing database aside rather than deleting it. Round trip verified in this
container; the archive built for this packet verified `True`.

## 23. Screenshots and preview

`data/shots/` holds 14 captures: Today, Session Review, Capture (all four tabs),
History, Weekly, Strategies, Research Mode, three mobile widths, and dark mode.

- Live: `python3 bin/journal serve --open`
- No install: open `data/trading-journal-demo.html` — one self-contained file.

## 24. Accessibility and responsive

Automated checks on the running interface: no image without alt text, no
`role="img"` SVG without a label, no button without an accessible name, no input
without a label, landmarks present (`main`, `nav`, `aside`, `header`). No
horizontal overflow at 390 px on any screen. Light and dark both render from the
same tokens; the palette passes colour-vision-deficiency separation, lightness
band, chroma floor and contrast in both modes. Sign and word accompany every
signed number, so no outcome is carried by colour alone.

## 25. Deviations from the Phase 1 prototype

Five, all narrow:

1. **Research Mode replaced, not extended.** The prototype had an unlock button
   and a populated research screen. Per D8 that is gone; the screen now explains
   the deferral and has no way in.
2. **`process_score` renamed** throughout to `self_reported_process_index`, with
   the dial and every caption reworded so nothing implies objectivity.
3. **Mechanical comparisons show "—"** where the prototype showed numbers. The
   prototype's synthetic mechanical references were fixtures; real ones do not
   exist yet, so the honest display is a dash and a reason.
4. **Capture screen is now four working forms** rather than two static phone
   mockups.
5. **The interface reads a live API**, falling back to the bundled payload when
   opened as a file. One codebase serves both.

Everything else is preserved: session-log feel, timeline-based review, serif
reading layer with restrained chrome, generous spacing, progressive disclosure,
provenance strips, light and dark, mobile capture, and P&L free of casino
green/red.

## 26. Integrations still blocked

| Contract | Status | Blocked by |
|---|---|---|
| `10AM_REFERENCE_IMPL` | `BLOCKED_ON_STRATEGY_SPEC` | the exact rule spec, from its owning project |
| `BROKER_IMPORTER` | `BLOCKED_ON_SAMPLE` | broker choice and a real export sample |
| `SIGNAL_IMPORTER` | `NOT_IMPLEMENTED` | signal source format |
| `MARKET_CONTEXT_ENRICHER` | `NOT_IMPLEMENTED` | a local market data source |
| `ASTRO_BLINDED_ENRICHER` | `NOT_IMPLEMENTED` | astro engine interface |

Each has a defined interface. The generic CSV import path **is** implemented and
tested against a synthetic export — what is blocked is the broker-specific
mapping, which needs a sample. `TenAmReferencePlaceholder.evaluate()` raises;
it does not return a plausible number.

## 27. Next blockers

1. **The 10AM rule specification**, precise enough to hash and execute.
2. **Broker choice**, and whether it issues a token that cannot route orders. If
   the only token carries trading authority, the journal uses file exports.
3. **A redacted broker export sample**, to build and verify one mapping profile.
4. **Confirmation the destination path is clear**, then run the installer.
5. **A market data source**, if MFE/MAE should be measured rather than reported.

None of these blocks daily use. The journal is usable now.

## 28. Phase 3 recommendation

**Use it for four weeks before building anything else.**

The foundation is complete and every remaining item is an enrichment. Four weeks
of real check-ins would answer questions no amount of design can: whether the
morning form actually takes under a minute (`fill_seconds` is recorded on every
submission, so this is measurable rather than arguable), whether the seven daily
fields are the right seven, whether the evening verdict gets used honestly, and
whether the self-reported index tracks anything you recognise.

Then, in order:

1. **One mapping profile** against a real export — the cheapest large win, and it
   removes hand-keying.
2. **`MECHANICAL_CONFORMANCE_INDEX`** — six of its seven inputs are already
   countable today (missed setups, rule violations, size deviation, unauthorised
   overrides, stop-rule violations, trading outside the planned window). Only
   entry deviation needs the reference implementation. It could ship as a
   six-input index with the seventh declared absent.
3. **10AM reference implementation**, when the spec exists.
4. **Research Mode**, last, when n justifies it.

A note on sequencing: `USER_VALUE_ADDED` remains the most expensive item in the
plan and the most abusable output. The conformance index gives most of the
behavioural signal for a fraction of the work, and it needs no counterfactual.
If the reference implementation stays blocked, the journal loses less than it
looks.
