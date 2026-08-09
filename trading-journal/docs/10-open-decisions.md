# 10 — Open decisions

Decisions that belong to the director, not the builder. Each carries a
recommendation, because "your call" without a recommendation is not useful.

---

### D1 — Is the process score the right instrument?

It is four self-reported ratings plus a count of confirmed mistake tags, with no
P&L term ([00](00-product-requirements.md)). That makes it consistent but not
objective: a trader in a good mood rates themselves higher, and the score drifts
with self-perception rather than conduct.

The alternative is a fully mechanical score built only from countable facts —
rule violations, size deviation, entry slippage against signal, missed setups.
Objective, but it measures a much narrower thing and misses everything the trader
knows and the data does not.

**Recommendation:** keep the self-reported score as the headline, and add a
mechanical "conformance index" from countable facts in Phase 4. Track both. If
they diverge persistently, that divergence is itself the interesting finding.

---

### D2 — How much of the morning battery is asked daily?

The brief lists ~20 candidate fields. The design asks 7 daily and collapses the
rest, with a fuller Monday check-in ([03 §1](03-capture-design.md)).

The tension is real: every field dropped is statistical power lost, and every
field added is compliance risk. Compliance risk compounds — a fortnight of skips
in month three costs more than a narrower daily form ever would.

**Recommendation:** ship the 7-field version. Review `fill_seconds` at week 4. If
the median is under 45 seconds, promote one field from the optional block; if it
is over 60, cut one. Let the telemetry decide rather than the wish-list.

---

### D3 — Which strategies get a reference implementation first?

`USER_VALUE_ADDED` requires a callable mechanical evaluator per strategy version,
and that is the hardest engineering in the project
([08 §4](08-implementation-plan.md)).

**Recommendation:** 10AM Model only, in Phase 4. It is semi-automated already, so
its rules are closest to executable, and there are taken trades to validate the
evaluator against. DORB follows once the pattern is proven. Discretionary X gets
no reference implementation and is excluded from discretion analysis — labelled
as such rather than approximated.

**Needed from the director:** rule specifications precise enough to hash. If a
rule cannot be written to that standard, that is a finding about the strategy,
not an obstacle to the journal.

---

### D4 — Broker API, or file exports?

An API gives same-day fills without a manual download. But many futures brokers
issue only full-permission keys, and a key that *could* place an order violates
the safety design even if the code never calls that endpoint.

**Recommendation:** file exports by default. Use an API only if a genuinely
read-only, scope-limited key is available. The cost is one download a day, which
can be automated as a folder watch. It is a small price for a system that is
structurally incapable of trading.

**Needed from the director:** which broker, and whether it issues scoped keys.

---

### D5 — Where does `journal.db` live relative to cloud sync?

Putting the project directory in iCloud or Dropbox gives off-machine redundancy
for free, but a sync conflict on a WAL database can fork it, and the fork may not
be obvious for days ([01 §7](01-architecture.md)).

**Recommendation:** `raw/`, `media/` and `backups/` in the synced folder — all
append-only and safe. `journal.db` outside it, on local disk, protected by the
nightly `.backup` which *does* land in the synced folder. The database is then
always reconstructible from synced material, and never itself subject to a
conflict.

---

### D6 — Laptop-only, or a small always-on host?

The design assumes the laptop is the single writer, with the phone reaching it
over the LAN ([07 §4](07-automation-and-reminders.md)). That means capture is
unavailable when the laptop is closed or away from home.

The alternative is a Mac mini or a small Linux box on the LAN running the journal
permanently: prompts always fire, the phone always reaches it, ingestion runs on
schedule regardless.

**Recommendation:** start laptop-only with the offline fallback form. If skipped
check-ins cluster around "laptop was closed" after a month, move to an always-on
host — the move is a directory copy, since nothing about the design is
laptop-specific.

---

### D7 — Does the interface stay framework-free?

The prototype is dependency-free vanilla JS and renders all eight screens
([08 §1](08-implementation-plan.md)). Adding React would bring a toolchain that
must keep working for as long as the journal is worth keeping.

**Recommendation:** stay framework-free through Phase 6. Revisit only if the
capture forms need genuine client-side state management beyond the offline
fallback. The database is the asset; the view layer is replaceable at any time
without touching it.

---

### D8 — When is Research Mode actually built?

Phase 7, deliberately last, when n justifies it
([08 §2](08-implementation-plan.md)).

The risk in building it early is not wasted work — it is that having the room
built creates the temptation to walk into it at n = 30 and see something.

**Recommendation:** implement blinded feature *computation and storage* in Phase
2, so the data accumulates from day one, and build the Research Mode *interface*
in Phase 7. Store early, look late. This is the single most important sequencing
decision in the project.

---

### D9 — What counts as a "session" when trading spans regions?

The model assumes one session per local trading day per account
([02 §3](02-data-model.md)). An overnight or multi-region day would need either
multiple sessions per date or a session that spans midnight.

**Recommendation:** one session per local trading date, as designed. If overnight
trading becomes routine, add a `session_segment` table rather than changing the
session's grain — the daily check-in is a daily ritual and should not fragment.

---

### D10 — Does the journal ever surface a nudge during a session?

Everything here is post-hoc by design: nothing about state or research reaches
the trader while positions are open.

There is a plausible exception — a hard limit breach ("you are at your daily loss
limit") — which is arguably risk management rather than journaling.

**Recommendation:** no. The platform enforces hard limits; the journal observes.
The moment the journal starts talking during a session, it becomes part of the
behaviour it is supposed to be measuring, and the record stops being clean.

---

## Answers needed before Phase 2 starts

1. **D3** — 10AM Model rule specification, precise enough to hash.
2. **D4** — broker, and whether scoped read-only keys are available.
3. **D5** — sync arrangement confirmed.
4. Confirmation that `~/AI-Projects/projects/trading-journal/` is the right home,
   and that no existing trading lane writes there.
5. A real broker export sample, with anything sensitive redacted.
