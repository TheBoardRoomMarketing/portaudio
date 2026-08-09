# Handoff — Trading Journal v1, Phase 1

**For:** GPT-06 (director review)
**From:** the builder
**Status:** Phase 1 complete. Stopped before production implementation, as instructed.
**Date:** 2026-08-09

This document is self-contained. You do not need repository or prototype access
to review it. Where a link appears it is for the human, not for you — the
prototype is a private artifact and the repository is private.

---

## 1. What was asked, and what was delivered

The directive asked for a product spec plus a human-first visual prototype for a
trading journal that would become shared infrastructure across manual trading,
the 10AM model, DORB, future algo strategies, execution-quality research,
personal-astro research, and behavioural analysis. Twenty-nine deliverables,
explicitly not a Markdown journal, explicitly stopping before production build.

Delivered:

| Thing | Form | Where |
|---|---|---|
| Canonical schema | 2 SQL migrations, 36 tables, 8 views, 5 immutability triggers | `schema/` |
| Working data pipeline | Python; builds DB, generates 6 weeks of synthetic sessions, computes derived layer, exports CSV/JSON | `scripts/build_synthetic.py` |
| Visual prototype | 8 screens + mobile capture flows; vanilla HTML/CSS/JS, no dependencies, no build step | `prototype/`, `dist/` |
| Specification | 11 documents, indexed against the 29 deliverables | `docs/00`–`docs/10` |

All 29 deliverables are addressed. The index mapping each one to its location is
in `docs/00-product-requirements.md`.

**Location caveat:** this was built in a cloud container, inside a fork of an
unrelated C library repository that was the only repository attached to the
session. The `trading-journal/` directory is self-contained and designed to be
lifted whole into `~/AI-Projects/projects/trading-journal/`. It should become its
own repository before Phase 2. No other trading lane was touched.

---

## 2. The architecture, in brief

**Storage: SQLite, local-first, single file.** Rejected Postgres (adds a daemon
that must be running before the 09:10 check-in can be submitted — exactly the
failure mode to avoid), DuckDB as primary (weaker for small frequent constrained
writes; correct as an *analysis* engine pointed at the same file, no migration
needed), and any document store (the core queries are joins).

**Six layers, with separation enforced by the database rather than convention:**

- **RAW** — `raw_import_batch`, `raw_record`, `trade_fill`. `BEFORE UPDATE` and
  `BEFORE DELETE` triggers `RAISE(ABORT)`. A bug in the importer cannot rewrite a
  fill. Corrections happen by importing a new batch.
- **DERIVED** — `trade_metrics`, `session_metrics`, `market_context_*`,
  `mechanical_reference`. Every row carries `calc_version` and resolves to a
  `derived_run` with a code SHA. Wholly rebuildable from RAW.
- **HUMAN_REPORTED** — `checkin_pre`, `checkin_post`, `voice_note`, `human_tag`.
  Amended via `checkin_amendment` (old value preserved), never edited in place.
  Voice transcripts frozen by trigger once written.
- **AI_ANNOTATED** — `ai_annotation`, `ai_narrative`. A suggestion carries
  `status ∈ (suggested, accepted, rejected, expired)`. Promotion to a label is an
  explicit human action writing a `human_tag` with `from_annotation_id`. Every
  statistic counts `human_tag` only.
- **BLINDED_RESEARCH** — `blinded_feature` (opaque keys), `blinded_feature_dict`
  (labels, separate table), `preregistration`, `research_unblind_log`.
- **REPORTS** — `report`, regenerable artifacts.

**Strategy versioning is the mechanism that prevents history being rewritten.**
`trade`, `opportunity` and `signal` reference `strategy_version.id`, never
`strategy.id`. Each version carries a frozen `rule_hash`, a JSON rule spec, an
`automation_level`, a `reference_impl` naming the module that evaluates its
mechanical counterfactual, and active dates. A trigger blocks UPDATE of `rules`,
`rule_hash` or `version`. Editing a strategy publishes v1.1; trades taken under
v1.0 point at v1.0 forever.

---

## 3. The qualified-opportunity model

This is the schema's most important table, because it holds what did not happen.

Eight statuses, kept deliberately distinct:

| Status | Meaning | Counts as a mistake |
|---|---|---|
| `TAKEN` | traded by the human | no |
| `BOT_EXECUTED` | traded by automation | no |
| `MISSED` | qualified, nobody was watching | **yes** (`MISSED_SETUP`) |
| `SKIPPED_BY_RULE` | a written rule excluded it | no — the system working |
| `SKIPPED_DISCRETIONARY` | seen, considered, declined | no, but this is the discretion question |
| `INVALIDATED` | conditions reversed before entry was possible | no |
| `BOT_FAILED` | automation should have traded and did not | **yes** (`BOT_ROUTING_ERROR`) |
| `NO_ACTION` | qualified outside the trading window | no |

The `MISSED` / `SKIPPED_DISCRETIONARY` split carries most of the behavioural
weight: "I wasn't there" and "I looked and said no" are different failures, and
only the second is a judgement worth evaluating.

`detection_source ∈ (engine, alert, human_logged, backfilled)` records coverage
honesty — engine-detected opportunities are complete, human-logged ones are not,
and research queries can exclude the incomplete kind.

Every opportunity gets a `mechanical_reference` row: entry, stop, target, exit,
R, MFE, MAE, computed by the version's `reference_impl`, stamped with
`calc_version` and `inputs_hash`. That yields:

```
USER_VALUE_ADDED = Σ actual R over trades linked to opportunities
                 − Σ mechanical R over ALL qualified opportunities
```

Missed setups therefore count against the trader, which is the correct
accounting — a setup missed because nobody was at the desk is a real cost.

---

## 4. Derived metric definitions

Stated explicitly because two of them are easy to get wrong:

| Metric | Definition |
|---|---|
| `risk_per_unit` | `abs(entry − initial_stop)`; NULL if no stop was set |
| `r_multiple` | `(exit − entry) · direction / risk_per_unit` |
| `net_pnl` | `gross_pnl − Σ(commission + exchange_fees)` over fills |
| `entry_slippage_ticks` | `(intended − fill) · direction / tick_size` — **negative is always adverse, for longs and shorts alike** |
| `r_captured_pct` | `r_multiple / mfe_r × 100`, **NULL for losers** — a −1R trade with a +0.6R MFE otherwise yields a meaningless −164% |
| `user_value_added_r` | `total_r − mechanical_r` over the period |

**Process score**, the journal's process headline, contains no P&L term:

```
process_score = 100 × ( 0.35·rule_adherence/5 + 0.20·execution_quality/5
                      + 0.15·patience/5 + 0.10·emotional_control/5
                      + 0.20·plan_conformance )

plan_conformance = 1 − min(1, rule_flags / 3)
```

Four of five inputs are self-reported. **This is a consistency instrument, not an
objective one**, and the docs say so explicitly. See open decision D1.

---

## 5. Capture design (the compliance-critical part)

**Morning, 7 fields, target under 60 seconds:** sleep hours, energy, focus,
stress, desire to trade (five sliders), bias (3-way segmented), and one required
sentence — *"what would make today a well-traded day if P&L were hidden?"*

Everything else from the directive's candidate list (sleep quality, irritability,
impulsivity, confidence, money pressure, physical state, caffeine, life stress,
planned max risk, free note) is collapsed behind "More (optional)", and expanded
by default on Mondays for a richer weekly observation (~50/year).

Sliders prefill from yesterday: a state that changed is one drag, a state that
did not is zero interaction. Ratings are never otherwise prefilled — prefilled
ratings accepted unthinkingly are worse than missing ones. Missing stays NULL,
never imputed, never back-filled from memory.

`fill_seconds` is recorded on both forms as friction telemetry, so the form gets
cut or grown on measured evidence rather than opinion.

**Evening, four cards, target under 2 minutes:** four ratings (execution quality,
rule adherence, patience, emotional control) → six mistake toggles writing
straight into the taxonomy → the anchor question *"Would this still count as a
well-traded day if P&L were hidden?"* (yes / mixed / no), displayed directly
beneath the morning's own definition → optional voice note or two text fields.

Ratings first, taxonomy second, narrative last, so bailing after 40 seconds still
saves the structured data.

**Mistake taxonomy: 15 stable codes in 5 categories** (discipline, entry, exit,
risk, process, system). Categories not severities — severity is contextual and
would need re-judging; category is stable. `OTHER` requires a note, and
accumulating notes under `OTHER` is the signal that a new code is needed. Codes
are added at version boundaries; retired codes get `active = 0` and stay
queryable. System errors are separated from human errors so platform failures do
not depress the process score.

**Voice notes:** audio written first with its sha256, before transcription runs,
so a failed transcription still leaves a recording. Transcript and audio become
immutable by trigger. Model summaries and suggested tags land in `ai_annotation`,
never in the transcript. The interface shows the transcript as a quotation with a
provenance line reading *"Your words, transcribed verbatim — edited never."*

---

## 6. Blinding

Four independent mechanisms, so no single mistake unblinds anything:

1. **Separate table** — no daily view joins `blinded_feature`.
2. **Opaque keys** — values stored against `pa_f17`, not "Part of Fortune aspect
   state". Labels live in `blinded_feature_dict`, joined only under a locked
   preregistration. A leaked value is uninterpretable.
3. **Connection allow-list** — research views are prefixed `r_` in a separate
   migration; the daily connection's view allow-list excludes that prefix.
4. **Logged entry** — opening Research Mode writes `research_unblind_log`
   (actor, scope, feature sets, reason), append-only, displayed inside Research
   Mode itself.

**Preregistration** stores question, hypothesis, feature keys, outcome metric,
`min_n`, analysis plan, and a `locked_hash` of the plan taken at lock time — so
the plan cannot be edited after the result is seen. The default hypothesis for
every astro question is recorded as *no effect*.

**Sample-size gating:** a view returns per-feature observation counts banded
`insufficient` (<60) / `provisional` (<150) / `reportable`. Research Mode reads
that view rather than recounting, shows a standing warning band naming current n,
and marks every candidate analysis runnable or under-powered. At the prototype's
33 sessions, every analysis reads under-powered — which is the honest answer.

**The weekly report contains no correlation analysis at all.** Fixed sections
only: performance, process, strategies, missed opportunities, mistakes, manual
value add, behaviour, next-week focus. Nothing runs on a schedule inside the
research room.

---

## 7. Interface

Eight screens — Today, Session Review, Trade Detail, History (calendar), Weekly,
Monthly, Strategies, Research Mode — plus a Capture screen with interactive phone
mockups of both forms. Vanilla HTML/CSS/JS, no framework, no build step, desktop
and mobile, light and dark.

**Design direction: "the session log"** — a logbook rather than a terminal. Time
is the organising spine (the Session Review is built around a vertical timeline
with monospaced times and expandable events). Prose is set in a serif because a
journal is read; the interface chrome is sans and mono. No webfont is loaded —
font CDNs are blocked in sandboxed hosts and fail silently, so the stacks resolve
natively on macOS, Windows and Linux.

**The one rule that shapes everything: P&L is never green or red.** Positive R
takes the page's single accent (a deep cerulean), negative takes a muted clay,
and both always carry an explicit sign and an outcome word — so colour is
redundant rather than load-bearing. The only warm alert colour in the system is
reserved for *process*: rule flags, missed setups, low sample sizes. The eye is
pulled toward behaviour rather than outcome.

Today carries a three-state control — **before open / mid-session / after close** —
showing the same day at three points in time. Before the open it is the plan and
the check-in; mid-session it is risk-against-plan; after close it is the review.
One screen, three states, no separate pre-market page.

Every generated or transcribed block carries a **provenance strip** naming its
source, model and version, so the trader can always tell their own words from a
model's at a glance.

Colour tokens were validated with a colour-vision-deficiency checker in both
themes: lightness band, chroma floor, adjacent-pair CVD separation (ΔE ≥ 8 under
protan/deutan/tritan), normal-vision separation and surface contrast all pass.
The first candidate palette (sage green / clay) **failed** deutan separation at
ΔE 5.1 and was replaced.

---

## 8. Deviations from the directive, and why

Listed because these are the things a reviewer should push back on.

1. **Morning check-in cut to 7 daily fields.** The directive listed ~20
   candidates and said not to make all of them mandatory. I went further and made
   13 of them optional-and-collapsed, with a fuller Monday check-in. Rationale:
   compliance risk compounds, statistical power does not.
2. **P&L carries no outcome hue at all.** The directive said avoid casino
   green/red. I removed outcome colouring entirely rather than muting it. This is
   the most reversible of the deviations and the most likely to be contentious.
3. **Weekly report has zero inferential content.** The directive asked for
   research integrity and sample-size warnings. I made the separation absolute:
   descriptive in reports, inferential only under a locked preregistration.
4. **Research Mode is prototyped now but implemented in Phase 7.** Blinded
   feature *storage* starts in Phase 2 so data accumulates from day one. Store
   early, look late.
5. **No frontend framework.** The directive said "project-appropriate web stack".
   For a single-user local app expected to last a decade, I judged
   zero-dependency to be more appropriate than React plus a toolchain requiring
   maintenance. Flagged as revisitable (D7).
6. **Chart screenshots are simulated in the prototype.** The screenshot capture
   design is fully specified (four phases, overlays, headless capture, local
   renderer fallback, `capture_status` so failures are visible). The prototype
   draws synthetic price paths from entry/stop/target/MFE/MAE geometry, labelled
   as standing in for captured images, because no real charts exist yet.
7. **Additions not requested:** `checkin_amendment` (correction without erasure),
   `fill_seconds` friction telemetry, `detection_source` coverage honesty, the
   provenance strips, the three-state Today control, and a proposed mechanical
   conformance index to sit beside the self-reported process score.

---

## 9. What was verified, and what was not

Stated precisely, because "tested" is doing no work here.

**Actually run and passing:**
- Both migrations execute on an empty database; `PRAGMA foreign_key_check`
  returns nothing.
- The full pipeline runs end to end: 33 sessions, 65 trades, 90 opportunities,
  +12.4R cumulative with a visible mid-period drawdown.
- Every prototype route renders headless with no console errors.
- No horizontal overflow at 390px on any screen; light and dark both verified.
- Palette validated for CVD separation in both themes.
- Timeline disclosure and chart tooltip interactions verified programmatically.

**Written but not yet implemented:** the test plan itself. `docs/09-test-plan.md`
specifies 12 categories and roughly 60 assertions, ordered by severity and
weighted toward raw immutability, strategy version preservation and blinding
isolation. Three of them are currently satisfied by the checks above; the rest
are Phase 2+ work.

**Not built at all** (correctly, per the stop instruction): ingestion code,
reference implementations for any strategy, astro engine integration, chart
capture, transcription, narrative generation, scheduling jobs, backup automation.

---

## 10. Open decisions

Ten are documented with recommendations. The four that most need a director:

**D1 — Is the process score the right instrument?** It is four self-reported
ratings plus a mistake count. Consistent but not objective; it drifts with
self-perception. *Recommendation:* keep it as the headline, add a mechanical
conformance index from countable facts (rule violations, size deviation, entry
slippage, missed setups) in Phase 4, track both. Persistent divergence between
them is itself the finding.

**D3 — Which strategy gets a reference implementation first?** `USER_VALUE_ADDED`
requires an evaluator precise enough to execute, and this is the hardest
engineering in the project. A hand-waved reference produces a confidently wrong
number, which is worse than no number. *Recommendation:* 10AM Model only, in
Phase 4, validated against sessions where the trade *was* taken mechanically.
DORB follows once the pattern is proven. Discretionary X gets no reference
implementation and is excluded from discretion analysis rather than approximated.

**D4 — Broker API or file exports?** Many futures brokers issue only
full-permission keys, and a key that *could* place an order violates the safety
design even if no code calls that endpoint. *Recommendation:* file exports by
default; API only if a genuinely scoped read-only key exists.

**D8 — When is Research Mode actually built?** *Recommendation:* store blinded
features from Phase 2, build the interface in Phase 7. The risk of building early
is not wasted work — it is that having the room built creates the temptation to
walk into it at n = 30 and see something. This is the single most important
sequencing decision in the project.

The remaining six: D2 (how much of the morning battery is daily), D5 (where
`journal.db` sits relative to cloud sync), D6 (laptop-only vs always-on host),
D7 (framework-free frontend), D9 (session grain across regions), D10 (whether the
journal ever speaks during a session — recommended firmly no).

---

## 11. Blocking Phase 2

Five answers required:

1. **10AM Model rule specification**, precise enough to hash and to execute. If a
   rule cannot be written to that standard, that is a finding about the strategy,
   not an obstacle to the journal.
2. **Which broker**, and whether it issues scoped read-only keys.
3. **Sync arrangement** for `journal.db` (recommendation: `raw/`, `media/` and
   `backups/` synced; the database itself on local disk, reconstructible from
   synced material).
4. **Confirmation** that `~/AI-Projects/projects/trading-journal/` is free of
   other lanes.
5. **A real broker export sample**, redacted.

---

## 12. What I would like reviewed

In priority order:

1. **Is the discretion metric worth the engineering it demands?** It is the most
   expensive thing in the plan and the most abusable output. If the answer is
   "not yet", Phase 4 shrinks dramatically and the journal is still valuable.
2. **Is the process score acceptable as a self-reported instrument**, or does the
   mechanical conformance index need to exist from the start?
3. **Is the 7-field morning form the right cut**, and is the required
   well-traded-day sentence the right single mandatory free-text field?
4. **Is removing outcome colour entirely correct**, or is it austerity that will
   make the journal less pleasant to open?
5. **Is the phase ordering right** — behavioural capture first, research last?
   Everything else follows from that sequencing.
