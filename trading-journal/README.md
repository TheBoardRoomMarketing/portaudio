# Trading Journal v1 — spec and prototype

**Phase 1 deliverable: specification + working visual prototype. Not the production build.**

This directory holds the product specification, the canonical data schema, and a
running human-first prototype populated entirely with synthetic placeholder data.
No broker connection, no live data, no order routing — the journal is read-only
infrastructure by design.

Intended home on the local machine: `~/AI-Projects/projects/trading-journal/`.
This repository directory mirrors that layout so it can be copied across whole.

---

## Look at it first

```
python3 scripts/build_synthetic.py      # builds build/journal.db and prototype/data.js
open prototype/index.html               # no server, no build step, no dependencies
```

Or open the single-file build: `dist/trading-journal-prototype.html`.

Eight screens: **Today**, **Session Review**, **Trade Detail**, **History**
(calendar), **Weekly**, **Monthly**, **Strategies**, and a locked **Research
Mode**, plus a **Capture** screen showing the mobile check-in and check-out
flows. Desktop and mobile layouts, light and dark.

---

## What's here

```
docs/          the specification — read 00 first
schema/        SQLite DDL; 0001 is the canonical schema, 0002 the research views
scripts/       build_synthetic.py — creates the demo DB and exports the dataset
prototype/     the app: index.html + styles.css + app.js + generated data.js
dist/          single-file build of the prototype
build/         generated, git-ignored: journal.db, CSV/JSON exports
```

| Document | Covers |
|---|---|
| [`docs/00-product-requirements.md`](docs/00-product-requirements.md) | what this is for, principles, scope, non-goals |
| [`docs/01-architecture.md`](docs/01-architecture.md) | stack, storage, data layers, safety, backup |
| [`docs/02-data-model.md`](docs/02-data-model.md) | schema walkthrough, field taxonomy, derived metrics |
| [`docs/03-capture-design.md`](docs/03-capture-design.md) | check-in, check-out, mistake taxonomy, voice notes |
| [`docs/04-ingestion-and-enrichment.md`](docs/04-ingestion-and-enrichment.md) | imports, market context, chart capture |
| [`docs/05-research-and-blinding.md`](docs/05-research-and-blinding.md) | blinded layer, preregistration, research integrity |
| [`docs/06-ux-and-design-system.md`](docs/06-ux-and-design-system.md) | sitemap, screens, design system, wireframes |
| [`docs/07-automation-and-reminders.md`](docs/07-automation-and-reminders.md) | scheduling, prompts, intake without a live agent |
| [`docs/08-implementation-plan.md`](docs/08-implementation-plan.md) | stack choice, phases, dependencies, blockers |
| [`docs/09-test-plan.md`](docs/09-test-plan.md) | what gets tested and how |
| [`docs/10-open-decisions.md`](docs/10-open-decisions.md) | decisions that need the director, not the builder |

---

## The three ideas that drive everything

**1. The source of truth is a database, not prose.** SQLite, local-first,
append-only RAW layer, reproducible DERIVED layer, full CSV/JSON export. The
Markdown you might have written is a *report* generated from the data, never
the record itself.

**2. Setups you didn't trade are data.** Every qualified opportunity is logged
with a status — taken, missed, skipped by rule, skipped by choice, invalidated —
alongside a mechanical reference result computed from the strategy version in
force at the time. That makes `USER_VALUE_ADDED = your R − the rules' R`
computable, which is the journal's most important research output.

**3. The research layer is blinded from the trading layer.** Personal and market
research features are computed and stored for every session but never surfaced
during check-in, check-out, or daily review. Values are stored under opaque
keys; their meanings live in a separate dictionary table joined only under a
locked preregistration, and entering Research Mode is logged.

---

## Design position

The backend is strict. The interface is not an admin panel.

One deliberate rule runs through the visual design: **P&L is never rendered in
green or red.** Positive R takes the page's single accent colour, negative takes
a muted clay, and both always carry an explicit sign and a word, so colour is
redundant rather than load-bearing. The only warm alert colour in the system is
reserved for *process* — rule flags and missed setups. The result is an
interface that pulls the eye toward behaviour rather than outcome, which is the
product thesis rendered in CSS.

---

## Status

Phase 1 complete and awaiting director review. Nothing here connects to a
broker, places an order, or reads a real account. See
[`docs/10-open-decisions.md`](docs/10-open-decisions.md) for the questions that
need answering before Phase 2 begins.
