# 05 — Research and blinding

The journal exists partly to answer questions that are extremely easy to answer
wrongly. This document is about the machinery that makes wrong answers harder to
produce and harder to believe.

## 1. Why blinding

If the trader can see that today has a particular personal astro state, that
knowledge changes behaviour — more caution, more boldness, more willingness to
read a bad session as externally caused. The measurement then contains the
expectation, and no amount of later analysis can separate them.

So the features are computed and stored every day, and never shown during
check-in, check-out, or daily review. The trader supplies unbiased behaviour;
the data supplies the state; the join happens much later, deliberately.

This applies to *all* research features, not only astro ones. Any variable whose
visibility could plausibly change behaviour belongs behind the same wall.

## 2. How blinding is enforced

Four independent mechanisms, so no single mistake unblinds anything:

**1 — Separate table.** `blinded_feature` is not joined by any daily view.
`v_session_daily`, `v_trade_full`, `v_strategy_scorecard` and
`v_mistake_frequency` do not reference it. A `SELECT *` on a daily view cannot
leak a feature.

**2 — Opaque keys.** Values are stored against keys like `pa_f17`, not
"Part of Fortune aspect state". Human-readable labels live in
`blinded_feature_dict`, a separate table joined only inside Research Mode under a
locked preregistration. Even a leaked value is uninterpretable without the
dictionary.

**3 — Connection-level allow-list.** Research views live in migration 0002 and
are prefixed `r_`. The application opens its daily connection with a view
allow-list that excludes that prefix. The database-level separation is the
second line of defence, not the first.

**4 — Logged entry.** Entering Research Mode writes a `research_unblind_log` row
with actor, scope, feature sets and reason. The log is append-only and is
displayed inside Research Mode itself, so the trader can see how often they have
gone looking.

The prototype demonstrates all four: `#/research` is locked by default, states
plainly why, and once opened shows opaque keys with the label column visibly
redacted.

## 3. Preregistration

A question must be written down before the data is looked at.

```sql
preregistration(question, hypothesis, feature_keys, outcome_metric,
                min_n, analysis_plan, locked_hash,
                status CHECK IN ('draft','locked','analysed','abandoned'))
```

`locked_hash` is a hash of the analysis plan taken at lock time, so the plan
cannot be quietly edited after the result is seen. `min_n` is the sample size
below which the analysis will not be run at all — not run and greyed out, but not
run.

The default null hypothesis for every astro question is **no effect**. That is
recorded in the `hypothesis` field, as it is in the prototype's example
registration. The purpose is not to prove a relationship; it is to make it
possible to be honestly wrong.

## 4. Sample-size discipline

The failure this is built to prevent is exactly the one the brief names:
"performance is amazing when sleep = 4 and Moon = 127°" after eleven
observations.

`r_feature_sample_sizes` returns, per feature key, the observation count and a
status band — `insufficient` under 60, `provisional` under 150, `reportable`
above. Research Mode reads that view rather than recounting, renders the status
as a chip on every row, and shows a standing warning band naming the current n.

Every candidate analysis is listed with its own required n and marked
**runnable** or **under-powered**. In the prototype, with 33 synthetic sessions,
every analysis reads under-powered — which is the honest answer and the point of
building the screen this way.

Rules of the room, displayed in the interface:

1. Questions are written down before the data is looked at.
2. No weekly automatic slicing. Nothing here runs on a schedule.
3. Findings below the preregistered n are not shown, not even greyed out.
4. Nothing from this screen is ever surfaced during a trading session.

Rule 2 is the important one. The weekly report deliberately contains no
correlation analysis: it reports performance, process, mistakes, missed setups
and discretion, all of which are descriptive. Inferential work happens when a
preregistered question is unlocked, and not otherwise.

## 5. The research questions this design supports

Enabled by the schema, to be asked later, each with an honest n requirement:

| Question | Grain | Rough n needed |
|---|---|---|
| Personal state vs execution quality | session | ~120 sessions |
| Personal state vs rule violations | session | ~120 sessions |
| Personal state vs strategy-relative R | trade | ~250 trades |
| Sleep / focus / stress vs mistake count | session | ~60 sessions |
| Manual intervention vs mechanical outcome | opportunity | ~100 opportunities |
| Strategy vs market regime | trade, stratified | ~200 trades per regime |
| Take rate vs time of day | opportunity | ~150 opportunities |

`r_session_features`, `r_manual_vs_mechanical` and `r_state_vs_violations`
provide analysis-ready grains for these. All three are outside the daily
allow-list.

**Historical records never change to accommodate an analysis.** Every one of
these questions is answerable from stored columns plus derived metrics. If a new
question needs a feature that was never captured, it is captured going forward
and the analysis waits — it is not back-filled from memory or inferred.

## 6. The discretion question specifically

`USER_VALUE_ADDED = Σ actual R − Σ mechanical R over all qualified opportunities`
is the journal's flagship metric and also its most abusable. Conditions on
reporting it:

- Report it with n, always. The prototype's weekly card shows the period's
  qualified-setup count beside it.
- Never report it for a single session as though it meant something. The session
  view shows it because it is the day's arithmetic, labelled "your difference",
  not a verdict.
- State the assumptions: mechanical fills may not have been achievable; some
  discretionary skips were correct for reasons outside the rules; and slippage is
  modelled, not observed, on the mechanical side.
- Split it by status. Value destroyed by `MISSED` (nobody watching) is an
  organisational problem; value destroyed by `SKIPPED_DISCRETIONARY` (looked and
  declined) is a judgement problem. They have different fixes and should never be
  reported as one number.

## 7. What Research Mode may eventually show

Once n supports it: effect sizes with confidence intervals, holdout validation by
month, experiment cohorts, and correlation matrices over behavioural features.
All of it stays inside the locked room, none of it appears on Today, Session
Review, Weekly or Strategy pages, and the Strategy page states explicitly that
blinded variables are not sliceable from there.
