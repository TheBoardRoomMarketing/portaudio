# 03 — Capture design

Capture is the only part of this system that costs the trader time every day. If
it is slow it will be skipped, and a journal with gaps cannot answer behavioural
questions. Both forms are therefore designed against a time budget first and a
completeness wish-list second.

Both are implemented and interactive in the prototype at `#/capture`.

## 1. Pre-session check-in — target under 60 seconds

**Asked, in this order, on one screen:**

| Field | Control | Why it survives the cut |
|---|---|---|
| Sleep hours | slider, 3–10, 0.1 | Most-cited candidate driver; cheap to answer. |
| Energy | slider 1–5 | Broad state proxy. |
| Focus | slider 1–5 | Best single predictor of execution errors, hypothesised. |
| Stress | slider 1–5 | Pairs with focus; separates "tired" from "wound up". |
| Desire to trade | slider 1–5 | Catches the "I want to be in something" state that precedes forcing. |
| Bias | 3-way segmented | One tap. Lets later analysis test whether stated bias predicts direction of error. |
| A well-traded day today means… | one line of text, prefilled with yesterday's | **The anchor.** Everything in the evening is scored against it. |

That is five sliders, one segmented control and one sentence. Measured fill time
in the prototype's synthetic record: median 41 seconds.

**Collapsed behind "More (optional)":** sleep quality, irritability,
impulsivity, confidence, pressure to make money (yes/no), physical state,
caffeine, unusual life stress, planned max risk, free note.

**Deliberately not asked every day:** the full 20-field battery from the brief.
Collecting all of it daily buys marginal statistical power and reliably destroys
compliance. Two mitigations preserve most of the value:

- **Weekly deep check-in.** On Monday the optional block is expanded by default.
  Roughly 50 richer observations a year, at a cost of about 90 seconds a week.
- **Missing is recorded as missing.** An unanswered optional field is NULL, never
  imputed and never back-filled from memory. NULL is a finding in itself.

The one field that is genuinely required is the last: without a definition of a
well-traded day, the evening question has nothing to compare against.

**Prefill rules.** Sliders open at yesterday's values (a state that changed is
one drag; a state that did not is zero interaction). The well-traded sentence
prefills with yesterday's text, editable. Nothing else prefills — prefilled
ratings that get accepted unthinkingly are worse than missing ones.

## 2. Post-session check-out — target under 2 minutes

**Card 1 — four ratings** (sliders 1–5): execution quality, rule adherence,
patience, emotional control.

**Card 2 — "Anything happen?"** — six toggles in a two-column grid, each writing
directly into the mistake taxonomy: overtraded, revenge trade, stop moved,
oversized, missed a setup, overrode a rule. Untapped means no. Tapping one turns
it clay and adds a `human_tag`.

**Card 3 — the anchor question**, in full: *"Would this still count as a
well-traded day if P&L were hidden?"* → **yes / mixed / no**. Shown directly
beneath the morning's own definition, so the comparison is made rather than
recalled.

**Card 4 — voice note** (optional, hold to record) or two text fields: best
decision, biggest mistake.

**Deferred to the weekly review, not asked daily:** unusual context, detailed
per-trade notes, anything requiring recall of a specific fill. Per-trade notes
are captured on the Trade Detail screen when the trader chooses to open it, not
demanded at 16:05.

The evening form is where fatigue and self-justification are highest. Ratings
first, taxonomy second, narrative last, so that if the trader bails after 40
seconds the structured data is already saved.

## 3. Mistake taxonomy

Fifteen codes, five categories, stable. Stability matters more than
completeness: a taxonomy that grows every month cannot be counted over time.

| Code | Label | Category |
|---|---|---|
| `OVERTRADE` | Overtraded | discipline |
| `REVENGE` | Revenge trade | discipline |
| `RULE_OVERRIDE` | Rule override | discipline |
| `FOMO_ENTRY` | FOMO entry | entry |
| `EARLY_ENTRY` | Early entry | entry |
| `LATE_ENTRY` | Late entry | entry |
| `PREMATURE_EXIT` | Premature exit | exit |
| `STOP_MOVED` | Stop moved improperly | risk |
| `OVERSIZED` | Oversized | risk |
| `MISSED_SETUP` | Missed qualified setup | process |
| `UNPLANNED_TRADE` | Unplanned trade | process |
| `DISTRACTED` | Distracted | process |
| `OTHER` | Other (note required) | process |
| `TECHNICAL_ERROR` | Technical error | system |
| `BOT_ROUTING_ERROR` | Bot routing error | system |

Design notes:

- **Categories, not severity.** Severity is contextual and would need
  re-judging; category is stable and supports "am I making entry mistakes or
  risk mistakes?".
- **`OTHER` requires a note.** Notes accumulating under `OTHER` are the signal
  that a new code is needed. New codes are added at a version boundary, never
  retroactively re-coded across history.
- **System errors are separated from human errors** so platform failures do not
  quietly depress the process score.
- **`MISSED_SETUP` is derivable** from `opportunity.status = 'MISSED'` and is
  also offered as a checkbox. The two are reconciled at check-out: if the
  opportunity table says a setup was missed and the trader did not tick the box,
  the box is pre-ticked with the setup named.

**Governance.** The taxonomy is a versioned table. Adding a code is allowed;
renaming or removing one is not. If a code is retired it gets `active = 0` and
stays queryable in history.

## 4. Voice notes

The flow, end to end:

1. **Hold to record** on the check-out card, or from any Trade Detail screen.
2. **Audio is written first**, to `media/<date>/`, with its sha256, before
   anything else runs. If transcription fails the recording still exists.
3. **Transcription** runs locally (`whisper-large-v3` or equivalent) and writes
   `transcript`, `transcript_engine`, `transcript_version`, `transcript_at`.
4. **Both become immutable.** A trigger blocks UPDATE of `transcript`,
   `audio_path` and `audio_sha256` once a transcript exists.
5. **Model output lands elsewhere.** Summary and suggested tags go to
   `ai_annotation` with `target_type = 'voice_note'`, `status = 'suggested'`.
6. **The trader confirms or rejects** suggested tags. Only confirmations write
   `human_tag`.

The interface shows the transcript in full, as a quotation, with a provenance
line reading *"Your words, transcribed verbatim — engine whisper-large-v3,
edited never."* Any model summary appears in a separate card with its own
provenance. The trader must always be able to tell which sentences are theirs.

Transcription errors are corrected by amendment, in the same pattern as
check-ins: the original transcript stays, the correction is recorded alongside.

## 5. Where each capture surface lives

| Moment | Surface | Time budget |
|---|---|---|
| 09:10 pre-session | phone notification → check-in form | < 60s |
| intraday, optional | quick note or voice memo against the open trade | < 20s |
| 16:05 post-session | phone notification → check-out form | < 2 min |
| evening / weekend | laptop: session review, per-trade notes, tag confirmation | unbudgeted |
| Friday 16:30 | weekly review, generated then annotated | ~10 min |

The daily budget is roughly three minutes. Everything richer is opt-in and
happens on a bigger screen when the trader has chosen to sit down with it.
