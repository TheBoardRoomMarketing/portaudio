# 00 — Product requirements

## What this is

A single, durable record of how trading actually goes: what the market did, what
setups appeared, which ones were taken, how well they were executed, and what
state the trader was in. It serves manual trading, the 10AM model, DORB, and
whatever algorithmic strategies follow, without being rebuilt for each one.

It is not a Markdown file. It is a local SQLite database with an interface on
top and exports out the side.

## Who it is for

One trader, reviewing alone, mostly at 09:10 and 16:05 on a phone, and on a
laptop at the weekend. Everything in the design assumes an audience of one who
already knows the material and does not need to be sold to.

## The problem it solves

Trading records normally answer *what happened to my account*. This one has to
answer four harder questions:

1. **Was that well traded?** — independent of whether it made money.
2. **What did I not do?** — the setups that qualified and were never taken.
3. **Is my discretion worth anything?** — measured against what the rules alone
   would have produced, not against a feeling.
4. **Does my state predict my behaviour?** — sleep, stress, focus versus
   violations and execution quality, tested honestly rather than noticed
   selectively.

None of these are answerable from a P&L export, and none survive being written
in prose.

## Principles

**The record is structured; the prose is generated.** Weekly narratives, daily
summaries, and reports are artifacts derived from data. Delete every one and
nothing is lost.

**Raw is sacred.** Broker fills, alert payloads and bot logs land byte-faithful
and are never rewritten. Corrections happen by importing a new batch, not by
editing history. This is enforced by database triggers, not by discipline.

**Derived is disposable.** Every computed number — R, MFE, MAE, slippage,
process score — can be dropped and recomputed from raw with a pinned
`calc_version`. If a metric definition changes, history is recomputed, and the
version says which definition produced a given number.

**Human words are never edited by a model.** A voice transcript and a written
reflection are the trader's own record. Models may summarise and suggest
alongside them; they may not rewrite them, and a suggestion never becomes a
label without an explicit confirmation.

**Objective and subjective never share a column.** "ATR was 51" and "it felt
choppy" are different kinds of fact and live in different tables.

**Blinded stays blinded.** Research features are computed daily and hidden from
the daily interface, so expectations cannot contaminate behaviour or reporting.

**Capture must be fast or it will not happen.** Under 60 seconds in the morning,
under two minutes in the evening. Every field beyond that budget is optional or
collapsed.

**Read-only.** The journal never places, modifies, or cancels an order, and
holds no credential that could.

## What "well traded" means here

The journal scores process separately from outcome, and the process score
contains no P&L term at all:

```
process_score = 100 × ( 0.35 · rule_adherence/5
                      + 0.20 · execution_quality/5
                      + 0.15 · patience/5
                      + 0.10 · emotional_control/5
                      + 0.20 · plan_conformance )

plan_conformance = 1 − min(1, rule_flags / 3)
```

Four of the five inputs are self-reported and one is counted from confirmed
mistake tags. That is deliberate: this is a measure of how the trader judged
their own conduct, anchored by a fixed question asked before the session
("what would make today a well-traded day if P&L were hidden?") and again after
it ("would this still count as a well-traded day if P&L were hidden?").

It is not an objective score and must never be presented as one. Its value is
that it is *consistent* — the same question, asked the same way, every day, so
the trend is meaningful even where the level is not.

## Scope for v1

**In:** session model, trade model, qualified opportunity model with mechanical
reference, pre/post check-in, mistake taxonomy, voice notes, market context
enrichment, chart capture, blinded research layer, daily/weekly/monthly reports,
the eight-screen interface, scheduled prompts, CSV/JSON export, local backup.

**Out:** any order routing or trade action; multi-user or cloud sync; a hosted
service; live P&L streaming; automatic statistical slicing; strategy
optimisation; anything that writes to a trading-control system.

**Deferred:** options and equities beyond futures (the schema supports them, the
importers do not); multi-account aggregation beyond one account per session;
tax reporting.

## Success criteria

Phase 1 is successful if the director can open the prototype and, within a
minute, understand what happened on 7 August 2026 without asking a question. It
is successful in six months if the trader has used it every session, and the
data supports the discretion question with enough observations to be worth
asking.

It has failed if morning check-in gets skipped, if the interface is only opened
to check P&L, or if any research finding gets acted on before it was
preregistered.

---

## Deliverables index

| # | Deliverable | Where |
|---|---|---|
| 1 | Recommended project architecture | [01](01-architecture.md) §1–2 |
| 2 | Database choice and reasoning | [01](01-architecture.md) §3 |
| 3 | Full schema proposal | [02](02-data-model.md), `schema/0001_init.sql` |
| 4 | Field taxonomy | [02](02-data-model.md) §3–7 |
| 5 | Morning check-in design | [03](03-capture-design.md) §1 |
| 6 | Post-session design | [03](03-capture-design.md) §2 |
| 7 | Mistake taxonomy | [03](03-capture-design.md) §3 |
| 8 | Qualified-opportunity model | [02](02-data-model.md) §6 |
| 9 | Raw / derived / blinded architecture | [01](01-architecture.md) §4, [05](05-research-and-blinding.md) |
| 10 | Automatic import plan | [04](04-ingestion-and-enrichment.md) §1–2 |
| 11 | Voice-note design | [03](03-capture-design.md) §4 |
| 12 | Screenshot design | [04](04-ingestion-and-enrichment.md) §4 |
| 13 | Automation / reminder plan | [07](07-automation-and-reminders.md) |
| 14 | Privacy / safety design | [01](01-architecture.md) §6 |
| 15 | Backup / recovery plan | [01](01-architecture.md) §7 |
| 16 | Visual UX sitemap | [06](06-ux-and-design-system.md) §1 |
| 17 | Desktop wireframes / mockups | [06](06-ux-and-design-system.md) §2, prototype |
| 18 | Mobile wireframes / mockups | [06](06-ux-and-design-system.md) §3, prototype `#/capture` |
| 19 | Today prototype | `prototype/` → `#/today` |
| 20 | Session Review prototype | `#/session/2026-08-07` |
| 21 | Trade Detail prototype | `#/trade/…` (via any trade card) |
| 22 | Weekly Review prototype | `#/weekly` |
| 23 | Research Mode prototype | `#/research` |
| 24 | Design-system recommendations | [06](06-ux-and-design-system.md) §4–6 |
| 25 | Build stack recommendation | [08](08-implementation-plan.md) §1 |
| 26 | Implementation phases | [08](08-implementation-plan.md) §2 |
| 27 | Dependencies / blockers | [08](08-implementation-plan.md) §3–4 |
| 28 | Test plan | [09](09-test-plan.md) |
| 29 | Unresolved director decisions | [10](10-open-decisions.md) |
