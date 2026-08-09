# Phase 3 return packet — local deployment and real-use hardening

**For:** GPT-06 (director review)
**Status:** the buildable half of Phase 3 is complete. The half that requires
Zack's machine is prepared, documented and blocked on him, not on engineering.
**Date:** 2026-08-09

---

## Confirmations

```
LIVE_TRADING_ACTIONS:                  NONE
BROKER_ORDER_CAPABILITY_ADDED:         FALSE
RESEARCH_MODE_ENABLED:                 FALSE
PERSONAL_ASTRO_ANALYSIS_EXECUTED:      FALSE
REAL_PERFORMANCE_CORRELATION_ANALYSIS: FALSE
```

Research Mode has no unlock control, no hidden route and no admin bypass. The
weekly report generator refuses at generation time to emit anything containing
the words correlation, significance, regression, astro, lunar, transit or
optimal — that is a test, not a convention.

---

## The shape of this phase

The ruling split cleanly into work that could be done here and work that cannot.

| Item | Where it must happen | Status |
|---|---|---|
| 1 Local installation | Zack's Mac | **prepared, blocked on him** |
| 2 Real daily check-ins | Zack's Mac | blocked on installation |
| 3 Scheduled reminders | Zack's Mac | installer written, blocked on installation |
| 4 One broker-import path | needs a redacted sample | **blocked on sample** |
| 5 Blinded astro enrichment | needs the engine | **storage path built and tested** |
| 6 Mechanical conformance index | here | **built** |
| 7 Friction telemetry | here | **built** |

Everything in the first group is documented step by step in
`docs/PHASE-3-LOCAL-RUNBOOK.md`, which is the actual handover artefact.

## 1–4. Local repo, commit, tests, serve URL

| | |
|---|---|
| Local repo path | `~/AI-Projects/projects/trading-journal` — **not yet created** |
| Local commit | none yet; `install_local.sh` makes the first one |
| Tests | **95 passing** (66 from Phase 2, 29 new), 4.6 s |
| Serve URL | `http://127.0.0.1:8765/` once installed |

The cloud container's `HOME` is `/root`. The destination is on a different
machine. `scripts/install_local.sh` performs the move with the verification the
ruling requires, and was re-tested this phase: pointed at an occupied directory
it stops and names the conflicting contents; pointed at an existing install with
a `journal.db` it refuses and directs to `git pull`.

## 5. Backup status

Unchanged from Phase 2 and still verified: archives contain a consistent
snapshot, raw imports, media, migration state and a manifest of per-file hashes
and per-table row counts, and `journal backup` verifies each archive by
restoring it into scratch before reporting success. A nightly agent is in the
scheduler installer. Restore is tested, including that a tampered archive is
refused and that the replaced database is moved aside rather than deleted.

Not yet done: **encryption at rest**. The ruling asked for
"encrypted/versioned" snapshots and only versioned is implemented. Doing it
properly needs a key that lives somewhere — Keychain is the obvious answer and
is a local decision. Flagged rather than half-done, since a backup encrypted
with a key nobody can find is a backup that does not exist.

## 6. Reminder status

`ops/install_schedule.sh` installs launchd agents for 09:10 check-in, 16:05
check-out, 18:00 verified backup and Friday 16:30 weekly review, and
`--uninstall` removes them. The OS is the scheduler; nothing depends on a
terminal or an assistant. Every firing writes to `job_run` with status and
detail, so a prompt that never fired is visible rather than merely absent.

Reminders carry no trading advice, no P&L, and no research state — the job
creates the session row if needed and hands over a URL.

**Operational only after step 7 of the runbook.**

## 7–8. Morning and evening workflows

Both work and both save, unchanged in shape from the accepted design. Morning:
five sliders, a bias control, one required sentence, thirteen fields behind
"More". Evening: four ratings, six mistake toggles, the well-traded verdict, two
narrative fields. Editing a saved check-in writes an amendment and says so.

New this phase: `optional_opened` is recorded on both forms, so the
optional-section open rate is measured rather than guessed.

## 9. Broker import status

`BLOCKED_ON_SAMPLE`, and deliberately still one profile rather than five.

The generic CSV path is implemented and tested: duplicate files refused,
partially-overlapping re-imports adding only genuinely new round turns, parse
errors reported without writing, fills grouped into trades when the position
returns to flat, and per-execution uniqueness at the RAW layer. What is missing
is exactly one thing: a real export to map against.

**The ask, per item 10:** one redacted export from the platform used for the
sessions being journaled. Keep headers, dates and times, instrument, side,
quantity, price, fees and order IDs. Remove account number, name, email, keys.
A handful of trades including one partial fill is ideal — a large history is not
useful and adds redaction risk.

## 10. Mechanical conformance index

**`MECHANICAL_CONFORMANCE_INDEX_V1` is built, stored and displayed.**

Six components, all from objectively observable fields:

| Component | Weight | Measure |
|---|---|---|
| `qualified_setups_taken` | 0.25 | 1 − missed / qualified |
| `rule_violations` | 0.25 | 1 − confirmed violations / 3 |
| `size_discipline` | 0.15 | mean oversize against plan |
| `session_window` | 0.15 | share of entries inside the declared window |
| `override_discipline` | 0.10 | 1 − overrides / trades |
| `stop_discipline` | 0.10 | 1 − stops moved / 2 |
| `entry_deviation` | — | **NOT_AVAILABLE**, declared on every session |

The governing rule, and the reason the module is careful rather than short:
**a component that cannot be measured is never scored as perfect.** Unavailable
components are excluded from the normalisation and named in
`components_missing`; the score is normalised over available weight only. Each
session stores `index_version`, `components_available`, `components_missing` and
per-component detail, because an index of 78 built from six components and one
built from three are different numbers and the record must say which.

Two design decisions worth your eye:

- **System errors do not count as indiscipline.** `TECHNICAL_ERROR` and
  `BOT_ROUTING_ERROR` are excluded from the violation set. A platform failure
  depressing a discipline score would teach the trader to distrust the metric.
- **A session where nothing happened scores nothing.** This was caught by test:
  the first implementation gave an empty session 100, because "no violations
  recorded" read as perfect compliance. It now returns no score at all. That is
  the same failure mode as scoring a missing component as perfect, one level up.

Undersizing does not count against the score; only oversizing does.

## 11. Blinded astro enrichment status

`BLOCKED_ON_ENGINE` — **the storage path is implemented and tested end to end.**

`journal enrich-blinded --engine pkg.mod:Engine --hypothesis <name>` loads any
object satisfying `BlindedEnricher`, and stores each value with session id,
feature set, opaque key, engine name, engine version, **engine hash**, source
hypothesis and computation timestamp. The engine hash is taken from the class
source, not just the declared version, because a version string somebody forgot
to bump is the classic way two different calculations end up pooled as one
variable.

Two things the loader enforces rather than trusts:

- **Readable feature keys are refused at the door.** A key must match `pa_f01`
  form; anything containing `good`, `money`, `moon`, `transit`, `fortune`,
  `bradley`, `aspect` and similar is rejected with an error. The blind only
  works if a leaked value is uninterpretable.
- **One failing session does not stop the run**, and failures are reported
  rather than silently skipped.

Tested against a stub engine: provenance stored, readable keys refused, failures
survived, re-runs skipped, values unreadable from the daily connection, and the
daily payload scanned for leakage.

Nothing has been analysed. No feature meaning exists anywhere the interface can
reach.

## 12. Real data captured so far

**None, and that is correct.** The database currently holds synthetic fixtures
only. Step 6 of the runbook clears them before real capture begins — a step
worth reading twice, because mixing invented sessions into the real record would
quietly poison the longitudinal dataset this whole phase exists to start.

## 13. Friction telemetry status

Built and exercised. `journal friction` reports completion rate for both forms,
median and worst fill times against the 60 s / 120 s targets, how many sessions
ran over, optional-section open rate, voice-note usage, manual trade burden, and
`OTHER` tag usage — the signal that the taxonomy needs a new code.

`journal review` produces the real-use checkpoint from item 23: distributions of
both process views, disagreement bands, missing-data rates, manual burden. It
reports whether the 20-session / four-week threshold has been reached and says
so when it has not. Descriptive only; it computes no relationship between any of
these and P&L.

## 14. Blockers

1. **Installation** — needs Zack at his machine for about ten minutes.
2. **A redacted broker export** — one file, a few trades, one partial fill.
3. **The astro engine's import path and feature-set name** — the storage side
   is done and waiting.
4. **The 10AM rule specification** — unchanged, still `BLOCKED_ON_STRATEGY_SPEC`,
   still not being reconstructed from memory.
5. **Backup encryption decision** — which key store, or explicitly not yet.

None of these blocks daily capture. Items 2–5 can all arrive after real check-ins
have started.

## 15. Next user action

Run the first three steps of `docs/PHASE-3-LOCAL-RUNBOOK.md` — clone, dry run,
install — and report what the dry run said. Everything else follows from that.

---

## Also in this phase

- **Both process views are displayed and never merged.** Session Review and
  Today show "How I felt I traded" beside "What the record shows", with the
  component breakdown behind a disclosure and neutral language for a gap
  ("The record counted more conformance than your own rating. Worth a look at
  which."). A difference is never presented as an error in either view.
- **Weekly reports generate descriptively** from an allowlist of seven sections,
  with a generation-time guard that refuses inferential or blinded content and
  stores which sections were used.
- **Migration 0004** adds the conformance columns, the declared session window,
  friction telemetry fields and blinded provenance fields.
- **The two integrity rules from your ruling are now tests**, so they cannot
  regress: `USER_VALUE_ADDED` stays NULL without a reference implementation and
  a missing counterfactual never becomes mechanical R = 0; `r_captured_pct`
  stays NULL on losing trades.
- **No visual redesign.** The only interface change is the second process view,
  which item 12 required.

## Test count, stated precisely

**95 executable tests, 95 passing.** 66 from Phase 2, 29 new: conformance
components and normalisation, the two preserved integrity rules, descriptive
report guards, friction telemetry, and blinded enrichment provenance and key
validation. No Phase-1 planned-but-unimplemented test is counted here.
