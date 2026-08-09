# 06 — UX and design system

The working version of everything below is in `prototype/`. Open
`prototype/index.html` or `dist/trading-journal-prototype.html`. This document
records the decisions; the prototype is the specification.

## 1. Sitemap

```
Today                    #/today          landing; the day at three points in time
History (calendar)       #/calendar       month grid → any session
  └ Session Review       #/session/:date  the core review experience
      └ Trade Detail     #/trade/:uid     one trade, chart-led
Weekly Review            #/weekly         one week, performance + process + focus
Monthly Review           #/monthly        trends across weeks
Strategies               #/strategies     per strategy version scorecard
Capture                  #/capture        mobile check-in / check-out flows
────────────────────────────────────────  restricted
Research Mode            #/research       locked; blinded features, preregistration
```

Seven everyday destinations and one restricted one. Research Mode sits under its
own "Restricted" heading in the navigation, visually separated, labelled
"blinded" — present so it is not forgotten, distinct so it is not wandered into.

Mobile collapses the rail to a five-item bottom bar: Today, History, Weekly,
Setups, Capture. Monthly and Strategies detail are reachable from within those;
Research Mode is not on the phone at all, by design — it is not something to open
one-handed at a bus stop.

## 2. Desktop screens

Layout constant: rail 236px, content column max 1120px, two-column grid at
roughly 1.62 : 1 (the wide column carries the narrative, the narrow one carries
state and provenance).

### A. Today

```
┌──────┬──────────────────────────────────────────────────────────────┐
│ rail │ FRIDAY 7 AUGUST 2026 · SESSION 2026-08-07:MAIN:ACCT1         │
│      │ Today                       [Before open│Mid-session│After]  │
│      │ Session closed. Check-out submitted at 16:05.                │
│      ├──────────┬──────────┬───────────┬──────────────────────────  │
│      │ NET P&L  │ R        │ RISK USED │ QUALIFIED SETUPS           │
│      │ +$81.05  │ +0.84R   │ 1.0R      │ 3                          │
│      ├──────────┴──────────┴───────────┴──────────┬───────────────  │
│      │ TRADES              full session review →  │ HOW IT SCORED   │
│      │ ┌──────────┬──────────────────────────┐    │  ◔ 74/100       │
│      │ │ minichart│ 10AM MODEL long   +1.84R │    │  execution 4/5  │
│      │ │          │ entry/exit/MFE/held/mech │    │  adherence 4/5  │
│      │ └──────────┴──────────────────────────┘    ├───────────────  │
│      │ ┌──────────┬──────────────────────────┐    │ DAILY SUMMARY   │
│      │ │ minichart│ DORB short        −1.00R │    │ generated prose │
│      │ └──────────┴──────────────────────────┘    │ + provenance    │
│      ├────────────────────────────────────────┐   ├───────────────  │
│      │ QUALIFIED SETUPS   3 qualified·1 missed│   │ MORNING CHECK-IN│
│      │ 10:02 10AM long      taken     +1.84R  │   │ energy/focus/…  │
│      │ 13:18 DORB long      missed    +1.35R  │   │ well-traded def │
│      ├────────────────────────────────────────┤   └───────────────  │
│      │ REFLECTION          well traded: mixed │                     │
└──────┴────────────────────────────────────────┴─────────────────────┘
```

The segmented control at top right is a genuine product feature, not a demo
device: it shows the same day **before open**, **mid-session** and **after
close**. Before the open, Today is the plan and the check-in. Mid-session it is
risk-against-plan and what has happened so far. After the close it is the review.
One screen, three states, no separate "pre-market" page.

### B. Session Review

Summary stats (two banded rows: P&L / R / process / adherence, then qualified /
mechanical / your difference) → the generated daily summary at full width →
then two columns:

- **wide:** Timeline, Trades, Qualified setups
- **narrow:** How I traded (dial + before/after ratings), Reflection, Voice note,
  Market context

The **timeline** is the spine of the screen: a hairline rule with monospaced times
down the left, one dot per event, colour-coded by kind (trade / missed / flag /
neutral) and expandable where there is detail behind it.

```
09:12 ○ Pre-session check-in        energy 3/5 · focus 4/5 · stress 2/5
09:30 ○ Regular session open        market
10:02 ○ 10AM Model qualified        long, MES
10:04 ● Entry — 10AM MODEL long ×3  5712.25 · stop 5707.50
10:41 ● Exit — +1.84R gain          target · held 37m
11:06 ● Entry — DORB short ×2       5716.75 · stop 5721.25
11:24 ◆ Exit — −1.00R loss          stop · held 18m
13:18 ◆ DORB long — missed          mechanical +1.35R
16:05 ○ Post-session check-out      well traded: mixed
```

### C. Trade Detail

A chart-led page. Full-width chart with entry, stop, target, the holding band,
and MFE/MAE annotated on the side away from the price path. Then a four-stat row
(R, net P&L, MFE/MAE, held), then Execution, "Against the mechanical reference",
Labels (confirmed vs model-suggested, accepted and rejected both shown), and in
the narrow column market context at entry, session state, and a collapsed
**Research details** disclosure.

### D. History (calendar)

Month grid, one cell per day. Each cell carries the day number and weekday, the
R value **or the words "No trade"**, a line of text ("2 trades · process 74"),
and word-labelled markers ("1 flag", "1 missed", "good process"). Colour is
never the only carrier — every marker is spelled out, and the legend says so.

### E / F. Weekly and Monthly

Weekly: performance stats → cumulative R curve → R by session → strategy table →
manual-vs-mechanical → missed opportunities, with process, mistakes, sessions
worth revisiting and next-week focus in the narrow column. Monthly is the same
shape at lower resolution, with by-week trend bars for R, process and rule flags.

Both end with generated prose carrying its provenance line. Neither has more than
two charts above the fold.

### G. Strategies

One card per strategy *version*, showing the version, rule hash, active dates,
automation level and qualification status, then trades / total R / profit factor
/ max drawdown, opportunity handling (qualified, take rate, miss rate, skipped by
rule, skipped by choice), you-vs-the-rules, and the most common flags. Market
conditions sit behind a disclosure. A standing note states that blinded variables
are not sliceable here.

### H. Research Mode

Locked by default with a plain-language explanation and a single button. Once
opened: sample-size warning band, feature-set table with opaque keys and redacted
labels, preregistered questions, available analyses each marked runnable or
under-powered, the unblinding log, and the rules of the room.

## 3. Mobile

Breakpoint at 900px. The rail becomes a sticky top bar plus a fixed bottom tab
bar; every two-column grid becomes one column; trade cards stack chart-above-
detail; calendar cells shrink and drop their marker text, keeping day, R and the
word.

The two capture forms are the mobile design that matters, and both are
interactive in the prototype at `#/capture`:

```
┌─ 9:10 ──────── Pre-session ─┐   ┌─ 16:05 ────── Post-session ─┐
│ ┌─────────────────────────┐ │   │ ┌─────────────────────────┐ │
│ │ TRADING JOURNAL         │ │   │ │ TRADING JOURNAL         │ │
│ │ Twenty minutes to the   │ │   │ │ Session closed. 2 trades│ │
│ │ open. Thirty seconds —  │ │   │ │ 3 qualified setups.     │ │
│ └─────────────────────────┘ │   │ └─────────────────────────┘ │
│ Sleep            6.2 h      │   │ Execution quality      4/5  │
│ ▬▬▬▬▬▬▬●────────            │   │ ▬▬▬▬▬▬▬▬▬●───               │
│ Energy            3/5       │   │ Rule adherence         4/5  │
│ Focus             4/5       │   │ Patience               3/5  │
│ Stress            2/5       │   │ Emotional control      4/5  │
│ Desire to trade   3/5       │   │ ANYTHING HAPPEN?            │
│ BIAS                        │   │ ☐ Overtraded  ☐ Revenge     │
│ [Bullish][Neutral][Bearish] │   │ ☐ Stop moved  ☐ Oversized   │
│ A WELL-TRADED DAY MEANS     │   │ ☑ Missed setup ☐ Overrode   │
│ [Only A-setups. Two trades] │   │ WELL-TRADED WITH P&L HIDDEN?│
│ + MORE (OPTIONAL)           │   │ [Yes][Mixed][No]            │
│ ┌─────────────────────────┐ │   │ ● Hold to record      0:48  │
│ │ Save and start the day  │ │   │ ┌─────────────────────────┐ │
│ └─────────────────────────┘ │   │ │    Finish session       │ │
│ Median completion: 41s      │   │ └─────────────────────────┘ │
└─────────────────────────────┘   └─────────────────────────────┘
```

## 4. Design system

**Direction — "the session log".** A logbook rather than a terminal. Time is the
organising spine; numbers are instruments; prose is set in a serif because a
journal is something you read, not something you monitor.

**The one rule that shapes everything: P&L is never green or red.** Positive R
takes the page's single accent, negative takes a muted clay, and both always
carry an explicit sign and an outcome word, so colour is redundant rather than
load-bearing. The only warm alert colour in the system is reserved for
*process* — rule flags, missed setups, sample-size warnings. The eye is pulled
toward behaviour rather than outcome, which is the product thesis rendered in
CSS.

### Colour

| Token | Light | Dark | Role |
|---|---|---|---|
| `--paper` | `#F2F5F6` | `#0F1417` | page ground |
| `--card` | `#FCFDFD` | `#161D21` | card surface |
| `--ink` / `--ink-2` / `--ink-3` | `#16232B` / `#4B5C66` / `#7C8D96` | `#E8EDEF` / `#A5B3BA` / `#74858D` | text hierarchy |
| `--rule` / `--rule-2` | `#D9E1E4` / `#C6D2D6` | `#263137` / `#33424A` | hairlines |
| `--accent` | `#00719A` | `#22A2C0` | navigation, chart marks, positive R |
| `--clay` | `#BC5A2E` | `#D4783F` | negative R, rule flags |
| `--amber` | `#8A6A1E` | `#C09A4E` | needs attention: missed setups, low n |

Neutrals are biased cool toward the accent rather than left as pure grey. The
accent/clay pair was validated with a colour-vision-deficiency checker in both
modes: lightness band, chroma floor, adjacent-pair CVD separation (ΔE ≥ 8 under
protan/deutan/tritan), normal-vision separation and contrast against the surface
all pass. Amber is a **status** colour, never a chart series colour — warm hues
do not separate from clay under deuteranopia, so they are never used as adjacent
series.

Three theme states are handled explicitly: bare `:root` defines the complete
light palette; `@media (prefers-color-scheme: dark)` scoped as
`:root:not([data-theme="light"])` redefines only the tokens; `:root[data-theme="dark"]`
redefines them again so an explicit toggle wins in either direction. No colour is
declared only inside a media or `[data-theme]` block.

### Type

- **Serif** (`ui-serif`, Iowan Old Style, Charter, Palatino, Georgia) — headings
  and all prose: narratives, reflections, notes, transcripts. The diary voice.
- **Sans** (`ui-sans-serif`, system UI stack) — interface: navigation, labels,
  buttons, table text.
- **Mono** (`ui-monospace`, SF Mono, JetBrains Mono, Menlo) — every figure, time,
  eyebrow label, tag and hash, with `font-variant-numeric: tabular-nums`.

The serif/sans split is content-driven: prose is set in the serif, the instrument
in the sans and mono. No webfont is loaded — a font CDN would be blocked in a
sandboxed host and would fall back silently, so the stacks are chosen to resolve
well on macOS, Windows and Linux.

### Components

Cards (1px hairline, 10px radius, generous padding); banded stat rows; the
process dial (a 64px arc, never a gauge with a red zone); pip ratings (five
squares, filled to the value, warm for inverted scales like stress); chips
(neutral, accent, flag, watch); the timeline spine; trade cards; horizontal bars
for counts; disclosures for progressive detail; and the provenance strip — a
dashed-top line under any generated or transcribed content naming its source,
model and version.

### Charts

Deliberately few and deliberately quiet. One cumulative-R line with a faint area
fill and an emphasised endpoint; one per-session bar strip diverging around a
neutral zero rule; horizontal bars for counts; sparkline-scale trade charts on
cards. Recessive grid lines, thin marks, no chart junk, no dual axes anywhere,
tabular figures throughout. Line and bar charts carry a crosshair and tooltip;
every tooltip repeats the value with its sign and an outcome word.

Maximum two charts above the fold on any screen. The brief's "no 15 charts on one
page" is enforced by layout, not by restraint.

### Motion and accessibility

A 280ms fade-and-rise on view change, wrapped in
`@media (prefers-reduced-motion: no-preference)`. Nothing else animates. Focus
states are visible everywhere; the timeline uses real `aria-expanded` buttons;
charts carry `role="img"` with a text label stating the value in words; markers
are always accompanied by text. Verified: no horizontal overflow at 390px on any
screen, no console errors on any route, light and dark both legible.

## 5. Writing style in the interface

Plain, specific, never coaching. "Missed" not "MISSED_SETUP" on screen. "What the
rules alone produced" not "mechanical baseline R". "Would this still count as a
well-traded day if P&L were hidden?" in full, every evening, unabbreviated.

Generated prose states facts and their consequences and stops. It does not
moralise, does not congratulate, does not rewrite what the trader wrote, and
always carries a provenance line so it can be told apart from the trader's own
words at a glance.

## 6. What the interface deliberately refuses

- No P&L on the home screen in a large green number.
- No streaks, badges, levels, or any other gamification.
- No dense admin table as a primary surface — the only tables are the weekly
  strategy breakdown and the Research Mode feature list.
- No blinded variable, anywhere outside Research Mode.
- No automatic statistical claim in a weekly report.
- No red/green outcome colouring, ever.
