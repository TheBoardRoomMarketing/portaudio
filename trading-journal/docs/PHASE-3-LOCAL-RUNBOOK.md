# Phase 3 local runbook

Everything in this file must be run on Zack's Mac. A cloud session cannot reach
`~/AI-Projects/`, so these steps are the handover point between the build and
real use.

Run them in order. Each one prints what it did; stop at the first surprise.

---

## 1. Get the code onto the machine

```sh
mkdir -p ~/AI-Projects/projects && cd ~/AI-Projects/projects
git clone --depth 1 --branch claude/trading-journal-v1-spec-yw7w8r \
  https://github.com/TheBoardRoomMarketing/portaudio.git _tj-tmp
```

## 2. Inspect the destination before touching it

```sh
cd _tj-tmp/trading-journal
scripts/install_local.sh --dry-run
```

Read the output. It prints the source, the destination, and the Python it found.
It will refuse to continue if the destination holds anything that is not this
project, and it will name what it found.

**If it stops, stop.** Do not pass a different flag to make it proceed — pick a
different destination or move the other project first.

## 3. Install

```sh
scripts/install_local.sh
```

This copies the project to `~/AI-Projects/projects/trading-journal`, runs
`git init` and a first commit so it is standalone, and runs `journal init`.

Then remove the clone you no longer need:

```sh
cd ~/AI-Projects/projects && rm -rf _tj-tmp
```

## 4. Verify before trusting it

```sh
cd ~/AI-Projects/projects/trading-journal
python3 -m unittest tests.test_journal tests.test_phase3   # expect 95 passing
python3 bin/journal verify                                  # expect ok: true
python3 bin/journal status                                  # counts, coverage, contracts
```

If the tests do not pass on your machine, stop and report — they pass in CI and
a local failure means something environmental worth understanding before real
data goes in.

## 5. Look at it with synthetic data first

```sh
python3 bin/journal seed          # ~6 weeks of invented sessions
python3 bin/journal serve --open
```

Click through Today, a Session Review, Capture. Look at **5 August** (a losing
session with a perfect self-reported index) and **6 August** (a winning session
with a poor one) — those two exist to show the journal never treats P&L as
process.

## 6. Clear the synthetic data before real capture

**This is the step it would be easy to skip and expensive to skip.** Synthetic
sessions must not be mixed into the real record.

```sh
python3 bin/journal backup --label pre-real-use    # keep the demo, just in case
rm -f data/journal.db data/journal.db-wal data/journal.db-shm
python3 bin/journal init --account "<your account label>" --mode sim_eval
python3 bin/journal verify
```

`journal status` should now show every count at zero.

## 7. Turn on the prompts

```sh
ops/install_schedule.sh
```

Installs launchd agents: 09:10 check-in, 16:05 check-out, 18:00 verified backup,
Friday 16:30 weekly review. Times follow the machine's clock — if it is not set
to America/New_York, edit the `Hour` values in
`~/Library/LaunchAgents/com.tradingjournal.*.plist`.

macOS will ask permission the first time a notification is posted. Allow it.

Check it took:

```sh
launchctl list | grep tradingjournal
python3 bin/journal job pre_session_checkin      # fire one by hand
```

The journal must be running for the notification's link to open a form, so leave
`journal serve` running during the trading day, or start it in the morning.

## 8. Start capturing

Morning, before the open: five sliders, a bias, one sentence. Evening, after the
close: four ratings, the mistake checkboxes, the verdict. Trades and setups keyed
by hand from the Capture screen until an importer exists.

Nothing else is required. Do not wait for broker import, market data, or the
reference implementation — none of them change what the check-ins are worth.

## 9. After a couple of weeks

```sh
python3 bin/journal friction     # is the morning form actually under a minute?
python3 bin/journal weekly       # descriptive weekly report
python3 bin/journal review       # the real-use checkpoint
```

`journal review` says whether the checkpoint has been reached (20 sessions or
four weeks). When it has, that output is the basis of the real-use review the
director asked for.

---

## When you have a broker export

Redact it first: remove account number, name, email, and any key. Keep headers,
timestamps, instrument, side, quantity, price, fees and order IDs. A handful of
trades including one partial fill is enough.

```sh
python3 bin/journal profiles --add-example
python3 bin/journal import ~/Downloads/export.csv \
  --profile generic_futures_csv --account "<label>" --dry-run
```

The dry run parses and reports without writing. The starting profile is a guess
and will almost certainly need remapping — that is expected, and the point of
the dry run. Once the parse looks right, drop `--dry-run`.

Then check the import against the original export by eye: fills, commissions,
timestamps, quantity, side, and that a re-run of the same file is refused.

## When the astro engine is available

```sh
python3 bin/journal enrich-blinded \
  --engine yourpackage.astro:Engine \
  --hypothesis personal-state-v1 --since 2026-09-01
```

The engine must expose `feature_set`, `engine_version` and
`compute(session_uid, date) -> {opaque_key: value}`. Feature keys must look like
`pa_f01`; readable names are refused at the door, because the blind only works
if a leaked value is uninterpretable.

Storage only. Nothing reads these values back, and Research Mode stays off.

---

## Things that should never happen

- The journal asking for a credential that can place an order.
- A research feature appearing anywhere in the daily interface.
- `journal.db` inside iCloud, Dropbox or Drive. Sync `data/backups/` instead.
- Synthetic sessions in the real database.
