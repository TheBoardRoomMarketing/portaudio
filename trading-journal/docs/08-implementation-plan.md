# 08 — Implementation plan

## 1. Stack

| Layer | Choice | Why |
|---|---|---|
| Storage | SQLite + WAL | see [01 §3](01-architecture.md) |
| Backend | Python 3.11+, stdlib `sqlite3`, FastAPI for the local app | the ingest/enrich/derive work is data wrangling; the analysis later is pandas/DuckDB; one language across the whole pipeline |
| Migrations | numbered `.sql` files, applied in order, recorded in `schema_migration` | no ORM, no migration framework, readable in ten years |
| Frontend | vanilla HTML/CSS/JS, no build step | the prototype is already the real thing; a framework would add a toolchain that must keep working for a decade to protect a single-user app |
| Charts | hand-rolled inline SVG | eight chart shapes total; a charting library is more code than the charts |
| Transcription | local Whisper | audio never leaves the machine |
| Narratives | Claude API, or a local model behind the same interface | pluggable, and the journal works with it switched off |
| Chart capture | Playwright headless | already the standard tool for this |
| Analysis | DuckDB pointed at `journal.db`, notebooks | zero migration cost, proper analytics engine |
| Scheduling | `launchd` (macOS) | runs missed jobs after wake |
| Tests | `pytest` | see [09](09-test-plan.md) |

**On the no-framework decision.** The prototype is ~1,400 lines of JS and CSS,
has no dependencies, and renders eight screens in both themes at every viewport.
A React/Next build would add a dependency tree needing maintenance for the life
of the journal, in exchange for conveniences a single-user app does not need. If
the interface later grows past what plain JS handles comfortably, the migration
is a rewrite of the view layer against an unchanged database — a contained
decision, deliberately deferred. This is [10](10-open-decisions.md) D7.

## 2. Phases

Each phase ends with something usable. No phase depends on a later one existing.

### Phase 1 — spec and prototype ✅ complete

Schema, synthetic data pipeline, eight-screen prototype, this documentation.
Awaiting director review.

### Phase 2 — capture works, for real (≈1 week)

The narrowest thing that produces real value: `journal.db` on the real machine,
migrations applied, the local app serving the two capture forms, `launchd`
prompts at 09:10 and 16:05, manual trade entry, nightly backup.

*Done when:* five consecutive sessions have a real check-in and check-out, and a
restore drill has been performed successfully.

*Why first:* behavioural data cannot be back-filled. Every day without capture is
a day permanently missing from the research set. Trades can be imported
retroactively; how the trader felt on 12 August cannot.

### Phase 3 — ingestion (≈1–2 weeks)

Broker export importer with the three duplicate defences; trade assembly from
fills; session linking; `derive` with `calc_version`; the real Session Review and
Trade Detail screens reading real trades.

*Done when:* a month of history imports cleanly, re-importing changes nothing,
and `derive --rebuild` reproduces every metric.

### Phase 4 — opportunities and the mechanical reference (≈2 weeks)

Alert/log ingestion into `signal`; strategy version registry populated with real
rules and hashes; `reference_impl` for 10AM Model and DORB; opportunity detection
producing rows for setups nobody saw; `mechanical_reference` computation;
`USER_VALUE_ADDED`.

*Done when:* a session shows a qualified setup that the trader did not know had
occurred, with its mechanical result.

*This is the highest-value and highest-risk phase* — see §4.

### Phase 5 — enrichment and media (≈1 week)

Market context from local bars; chart capture; voice notes with local
transcription; media retention policy.

### Phase 6 — reports and narratives (≈1 week)

Weekly and monthly generation on schedule; AI narrative with provenance; model
tag suggestions with the confirm/reject flow.

### Phase 7 — research mode (≈1 week, deliberately last)

Blinded feature computation from the astro engine; the dictionary/value split;
the allow-list enforcement; preregistration; sample-size gating; unblinding log.

*Why last:* it is worthless until there is data, and building it early creates
the temptation to look early. The interface is prototyped now so the design is
settled; the implementation waits until n justifies it.

**Rough total: 7–9 weeks of part-time work**, front-loaded so that value arrives
in week one and the research capability arrives when it can actually be used.

## 3. Dependencies

| Dependency | Needed by | Status |
|---|---|---|
| Broker CSV export format (a real sample) | Phase 3 | **needed from the director** |
| Read-only broker API availability | Phase 3 | unknown — [10](10-open-decisions.md) D4 |
| 10AM Model and DORB rule specifications, precise enough to hash | Phase 4 | **needed** — the version registry is meaningless without them |
| A callable reference implementation for each strategy | Phase 4 | **needed** — this is the hard one |
| Local bar data, 1-minute or finer, with history | Phase 4, 5 | assumed present in the hub |
| TradersPost log/webhook access | Phase 4 | to confirm |
| Astro engine interface (input: date/time/location; output: feature vector) | Phase 7 | exists; needs a stable calling contract and a version string |
| Whisper model weights locally | Phase 5 | trivial |
| Model API key or local model | Phase 6 | optional; journal works without |

## 4. Blockers and risks

**The mechanical reference is the real engineering problem.** Everything
downstream of the discretion question depends on being able to say, precisely,
what the rules would have done. That requires each strategy to be specified to
the point of executability — entry trigger, invalidation, stop placement, target,
time filters, position sizing — and a fill model honest about what was actually
available. A hand-waved reference produces a confidently wrong
`USER_VALUE_ADDED`, which is worse than not having the metric.

Mitigation: build the reference implementation for **one** strategy first (10AM
Model), validate it against sessions where the trade *was* taken mechanically,
and only then extend. If a strategy cannot be specified to that standard, it is
marked `qualification_status = 'research'` and excluded from discretion analysis
rather than approximated.

**Retroactive opportunity detection needs history.** Opportunities for past
sessions can only be reconstructed if bar data exists for them. Check the depth
of local history early; the answer determines whether Phase 4 can back-fill or
only runs forward.

**Chart capture is the flakiest component.** Third-party chart pages change
without notice. Mitigation: the local renderer fallback drawing the same geometry
from bar data — already demonstrated in the prototype — and `capture_status =
'failed'` so a gap is visible rather than silent.

**Compliance is the real risk to the whole project.** The most likely failure is
not technical: it is that the morning check-in gets skipped for a fortnight in
month three. Mitigations are all in the design — under 60 seconds, one nudge
only, prefilled sliders, `fill_seconds` telemetry to catch drift — but the
director should expect to review compliance at week 4 and cut fields if the
median has risen.

**Timezone and DST.** Every date bug in a trading journal is a timezone bug. The
convention is fixed ([02 §1](02-data-model.md)) and tested explicitly across a
DST boundary ([09](09-test-plan.md) T5).

**Scope creep toward analysis.** The temptation, once data exists, is to build
the correlation dashboard. The research-integrity design exists to resist that;
the director's job is to hold the line on Phase 7 timing.
