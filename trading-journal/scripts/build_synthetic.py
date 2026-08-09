#!/usr/bin/env python3
"""Build the Trading Journal v1 demo database and export the prototype dataset.

This is the prototype's single source of truth. It:

  1. creates journal.db from schema/0001_init.sql + 0002_research_views.sql
  2. loads reference data (accounts, instruments, strategies, mistake taxonomy)
  3. generates ~6 weeks of SYNTHETIC sessions, trades, opportunities and check-ins
  4. computes the DERIVED layer from the RAW layer with a pinned calc_version
  5. exports prototype/data.js, plus CSV/JSON exports proving portability

No real trading data is involved. Every number below is invented. The point is
that the UI reads what the schema produces, not a hand-written fixture.

Usage:  python3 scripts/build_synthetic.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import sqlite3
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "build", "journal.db")
EXPORT_DIR = os.path.join(ROOT, "build", "exports")
DATA_JS = os.path.join(ROOT, "prototype", "data.js")

CALC_VERSION = "metrics@0.3.0"
SEED = 20260807
ET_OFFSET_HOURS = 4  # EDT in Aug 2026; real system resolves via session.tz

rng = random.Random(SEED)

# Demo "now". The prototype presents this Friday as the current session.
TODAY = date(2026, 8, 7)
GEN_AT = "2026-08-07T21:30:00Z"


# ----------------------------------------------------------------------------- helpers
def sha(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def utc(d: date, hh: int, mm: int, ss: int = 0) -> str:
    """Local ET wall clock -> stored UTC ISO-8601."""
    dt = datetime(d.year, d.month, d.day, hh, mm, ss) + timedelta(hours=ET_OFFSET_HOURS)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hm(d: date, hh: int, mm: int) -> str:
    return f"{hh:02d}:{mm:02d}"


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def trading_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


# ----------------------------------------------------------------------------- reference data
MISTAKE_TAXONOMY = [
    ("OVERTRADE",         "Overtraded",              "discipline", "More trades than the session plan allowed."),
    ("REVENGE",           "Revenge trade",           "discipline", "Entry motivated by recovering a prior loss."),
    ("FOMO_ENTRY",        "FOMO entry",              "entry",      "Entered after the setup because the move was running."),
    ("EARLY_ENTRY",       "Early entry",             "entry",      "Entered before the trigger condition completed."),
    ("LATE_ENTRY",        "Late entry",              "entry",      "Entered after the trigger, reducing available R."),
    ("PREMATURE_EXIT",    "Premature exit",          "exit",       "Closed before target or invalidation."),
    ("STOP_MOVED",        "Stop moved improperly",   "risk",       "Stop widened or moved against the plan."),
    ("OVERSIZED",         "Oversized",               "risk",       "Position larger than the planned risk unit."),
    ("MISSED_SETUP",      "Missed qualified setup",  "process",    "A qualified setup passed with no action."),
    ("RULE_OVERRIDE",     "Rule override",           "discipline", "Knowingly traded against a written rule."),
    ("UNPLANNED_TRADE",   "Unplanned trade",         "process",    "Traded a strategy not in the session plan."),
    ("DISTRACTED",        "Distracted",              "process",    "Attention away from the screen at a decision point."),
    ("TECHNICAL_ERROR",   "Technical error",         "system",     "Platform, order-entry or connectivity error."),
    ("BOT_ROUTING_ERROR", "Bot routing error",       "system",     "Automation failed to route or filled incorrectly."),
    ("OTHER",             "Other",                   "process",    "Anything not covered; requires a note."),
]

STRATEGIES = [
    ("10AM_MODEL", "10AM Model", "opening_range",
     "Post-open continuation model keyed to the 10:00 ET reference window."),
    ("DORB", "DORB", "opening_range",
     "Daily opening-range breakout with session-range risk normalisation."),
    ("DISCRETIONARY_X", "Discretionary X", "discretionary",
     "Unstructured discretionary trades. Logged so discretion can be measured."),
]

STRATEGY_VERSIONS = [
    # strategy_id, version, automation, qualification, active_from, active_to, ref_impl
    ("10AM_MODEL", "1.0", "semi_auto", "qualified",    "2026-05-01", None,
     "engines.tenam.v1_0:evaluate"),
    ("DORB",       "1.0", "bot",       "forward_test", "2026-06-15", None,
     "engines.dorb.v1_0:evaluate"),
    ("DORB",       "0.9", "bot",       "retired",      "2026-04-01", "2026-06-14",
     "engines.dorb.v0_9:evaluate"),
    ("DISCRETIONARY_X", "1.0", "manual", "research",   "2026-01-01", None, None),
]


# ----------------------------------------------------------------------------- session scripting
# A hand-authored "hero" week so the Session Review screen tells a legible story,
# then generated history behind it.
HERO = {
    date(2026, 8, 7): {
        "pre": dict(sleep_hours=6.2, sleep_quality=3, energy=3, focus=4, stress=2,
                    irritability=2, impulsivity=2, confidence=4, desire_to_trade=3,
                    money_pressure=0, physical_state="rested", caffeine_mg=180,
                    bias="neutral", planned_max_risk_r=2.0,
                    well_traded_definition="Take only A-setups. Two trades maximum. No adds after 11:15.",
                    note="Slept short but clear-headed. Nothing pressing today.",
                    fill_seconds=41),
        "post": dict(execution_quality=4, rule_adherence=4, patience=3, emotional_control=4,
                     overtraded=0, revenge_trade=0, stop_moved=0, oversized=0,
                     missed_qualified=1, manual_override=0,
                     best_decision="Waited for the retest on the 10AM entry instead of chasing the first push.",
                     biggest_mistake="Hesitated on the DORB trigger and paid two ticks of entry for it.",
                     unusual_context="CPI at 08:30 left the first 20 minutes unusually wide.",
                     well_traded="mixed",
                     note="Good day on process, one setup left on the table.",
                     fill_seconds=94),
        "trades": [
            dict(strategy="10AM_MODEL", version="1.0", side="long", qty=3, planned_qty=3,
                 entry=(10, 4), exit=(10, 41), entry_px=5712.25, exit_px=5721.00,
                 stop=5707.50, target=5723.75, exec_mode="semi_auto",
                 exit_reason="target", intended_entry=5712.00,
                 mfe_r=2.05, mae_r=-0.32, tags=[],
                 note="Clean retest of the 10:00 reference. Scaled nothing, held to target.",
                 mech_r=1.84),
            dict(strategy="DORB", version="1.0", side="short", qty=2, planned_qty=2,
                 entry=(11, 6), exit=(11, 24), entry_px=5716.75, exit_px=5721.25,
                 stop=5721.25, target=5707.75, exec_mode="manual",
                 exit_reason="stop", intended_entry=5718.25,
                 mfe_r=0.61, mae_r=-1.0, tags=["LATE_ENTRY"],
                 note="Trigger printed at 11:04, I entered at 11:06. Lost 6 ticks of the stop distance.",
                 mech_r=-1.0),
        ],
        "opportunities": [
            dict(strategy="10AM_MODEL", version="1.0", at=(10, 2), direction="long",
                 status="TAKEN", trade_ix=0, mech_r=1.84, decided_by="human"),
            dict(strategy="DORB", version="1.0", at=(11, 4), direction="short",
                 status="TAKEN", trade_ix=1, mech_r=-1.00, decided_by="human"),
            dict(strategy="DORB", version="1.0", at=(13, 18), direction="long",
                 status="MISSED", trade_ix=None, mech_r=1.35, decided_by="human",
                 reason="Away from the desk; alert fired with no one watching."),
        ],
        "events": [
            (9, 12, "info", "journal", "Pre-session check-in submitted"),
            (9, 30, "info", "market", "Regular session open"),
            (10, 2, "info", "dorb_bot", "10AM Model qualified — long, MES"),
            (11, 4, "info", "dorb_bot", "DORB qualified — short, MES"),
            (13, 18, "warn", "dorb_bot", "DORB qualified — long, MES (no action taken)"),
            (16, 5, "info", "journal", "Post-session check-out submitted"),
        ],
        "narrative": (
            "Three qualified setups appeared and you acted on two. The 10AM entry was taken on "
            "the retest rather than the first push, and it ran to target for +1.8R. The DORB short "
            "was entered two minutes after the trigger, which cost roughly six ticks of stop "
            "distance and turned a full-size loss into the day's only rule flag. A third DORB "
            "signal at 13:18 passed with no action while you were away from the desk; the "
            "mechanical reference for it closed at +1.35R.\n\n"
            "Rule adherence stayed high and no risk limits were touched. On the day's own "
            "definition — two trades maximum, A-setups only, no adds after 11:15 — the plan held."
        ),
        "voice": dict(at=(16, 8), seconds=48, transcript=(
            "Okay, end of day. The ten AM trade was the best execution I've had this week, I "
            "actually waited for the retest instead of grabbing the first push. The DORB one I was "
            "slow on, I saw the trigger and second-guessed it, and by the time I was in the stop "
            "was too far away to be worth it. The one that annoys me is the one thirty trade, I "
            "wasn't at the desk. That's the second time this week."
        )),
        "ai_suggested": [
            dict(trade_ix=1, tag="LATE_ENTRY", confidence=0.86, status="accepted",
                 rationale="Entry timestamp 11:06:12 is 132s after signal 11:04:00; "
                           "entry price 1.5 pts adverse to signal price."),
            dict(trade_ix=1, tag="DISTRACTED", confidence=0.41, status="rejected",
                 rationale="Voice note mentions being away from the desk in the same session."),
        ],
    },
}

DAY_SCRIPTS = {
    # date offsets receive a light narrative shape so weekly/monthly views read like a story
    date(2026, 8, 6): dict(mood="clean", trades=2, note="Best process day of the week."),
    date(2026, 8, 5): dict(mood="rough", trades=4, note="Chop; overtraded the midday range."),
    date(2026, 8, 4): dict(mood="clean", trades=1, note="One setup, one trade, done by 10:30."),
    date(2026, 8, 3): dict(mood="flat",  trades=0, note="No qualified setup. Stayed out."),
}


# ----------------------------------------------------------------------------- db setup
def create_db(conn: sqlite3.Connection) -> None:
    for mig in ("0001_init.sql", "0002_research_views.sql"):
        with open(os.path.join(ROOT, "schema", mig)) as fh:
            conn.executescript(fh.read())
        conn.execute(
            "INSERT INTO schema_migration(version, applied_at, notes) VALUES (?,?,?)",
            (mig, GEN_AT, "applied by build_synthetic.py"),
        )
    conn.commit()


def load_reference(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO account(id,label,broker,mode,currency,tz,active) VALUES (1,?,?,?,?,?,1)",
        ("Futures Eval 50k", "demo-broker", "sim_eval", "USD", "America/New_York"),
    )
    conn.execute(
        "INSERT INTO instrument(id,symbol,name,asset_class,exchange,tick_size,tick_value,point_value)"
        " VALUES (1,'MES','Micro E-mini S&P 500','future','CME',0.25,1.25,5.0)",
    )
    conn.execute(
        "INSERT INTO contract(id,instrument_id,contract_symbol,expiry) VALUES (1,1,'MESU6','2026-09-18')"
    )
    for code, label, cat, desc in MISTAKE_TAXONOMY:
        conn.execute(
            "INSERT INTO mistake_tag(code,label,category,description,sort_order) VALUES (?,?,?,?,?)",
            (code, label, cat, desc, MISTAKE_TAXONOMY.index((code, label, cat, desc))),
        )
    for sid, name, family, desc in STRATEGIES:
        conn.execute(
            "INSERT INTO strategy(id,name,family,description,created_at) VALUES (?,?,?,?,?)",
            (sid, name, family, desc, "2026-01-01T00:00:00Z"),
        )
    for sid, ver, auto, qual, frm, to, impl in STRATEGY_VERSIONS:
        rules = json.dumps({"strategy": sid, "version": ver, "spec": "see docs/02-data-model.md"})
        conn.execute(
            "INSERT INTO strategy_version(strategy_id,version,rule_hash,rules,automation_level,"
            "reference_impl,qualification_status,active_from,active_to,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, ver, sha(sid, ver, rules), rules, auto, impl, qual, frm, to, frm + "T00:00:00Z"),
        )
    conn.commit()


def sv_id(conn, strategy: str, version: str) -> int:
    return conn.execute(
        "SELECT id FROM strategy_version WHERE strategy_id=? AND version=?", (strategy, version)
    ).fetchone()[0]


# ----------------------------------------------------------------------------- generation
def gen_day_plan(d: date) -> dict:
    """Produce the shape of a synthetic session for a non-hero day."""
    if d in DAY_SCRIPTS:
        script = DAY_SCRIPTS[d]
        mood, n_trades = script["mood"], script["trades"]
    else:
        roll = rng.random()
        mood = "clean" if roll < 0.45 else ("rough" if roll < 0.7 else "flat")
        n_trades = {"clean": rng.choice([1, 2, 2, 3]),
                    "rough": rng.choice([3, 4, 4, 5]),
                    "flat": rng.choice([0, 1])}[mood]

    base = {"clean": (4, 4), "rough": (2, 3), "flat": (3, 4)}[mood]
    pre = dict(
        sleep_hours=round(rng.uniform(5.2, 8.4), 1),
        sleep_quality=rng.randint(2, 5),
        energy=min(5, max(1, base[0] + rng.randint(-1, 1))),
        focus=min(5, max(1, base[0] + rng.randint(-1, 1))),
        stress=min(5, max(1, 6 - base[1] + rng.randint(-1, 1))),
        irritability=rng.randint(1, 4),
        impulsivity=rng.randint(1, 4) + (1 if mood == "rough" else 0),
        confidence=rng.randint(2, 5),
        desire_to_trade=rng.randint(2, 5),
        money_pressure=1 if (mood == "rough" and rng.random() < 0.5) else 0,
        physical_state=rng.choice(["rested", "tired", "rested", "sore"]),
        caffeine_mg=rng.choice([0, 90, 120, 180, 240]),
        bias=rng.choice(["bullish", "bearish", "neutral"]),
        planned_max_risk_r=2.0,
        well_traded_definition=rng.choice([
            "Only A-setups. No trade after 11:30.",
            "Two trades maximum, full stop distance respected.",
            "Take the plan or take nothing.",
            "No adds, no averaging, no revenge.",
        ]),
        note="",
        fill_seconds=rng.randint(28, 72),
    )

    trades, opportunities, tags = [], [], []
    n_opps = max(n_trades, n_trades + rng.choice([0, 0, 1, 1, 2]))
    hour = 9
    minute = 45
    for i in range(n_opps):
        strategy, version = rng.choice([("10AM_MODEL", "1.0"), ("DORB", "1.0"),
                                        ("DISCRETIONARY_X", "1.0")])
        minute += rng.randint(18, 70)
        hour += minute // 60
        minute %= 60
        if hour >= 16:
            break
        direction = rng.choice(["long", "short"])
        mech_r = round(rng.choice([-1.0, -1.0, 1.6, 1.9, 2.2, 0.4, -0.6]), 2)

        if i < n_trades:
            # taken
            win = rng.random() < (0.45 if mood == "clean" else 0.27)
            r = round(rng.uniform(0.9, 2.1), 2) if win else round(rng.uniform(-1.0, -0.55), 2)
            entry_px = round(rng.uniform(5650, 5760) * 4) / 4
            risk_pts = rng.choice([4.0, 4.75, 5.5, 6.25])
            sgn = 1 if direction == "long" else -1
            stop = entry_px - sgn * risk_pts
            target = entry_px + sgn * risk_pts * 2.2
            exit_px = round((entry_px + sgn * risk_pts * r) * 4) / 4
            hold = rng.randint(6, 48)
            ex_h, ex_m = divmod(hour * 60 + minute + hold, 60)
            trade_tags = []
            if mood == "rough" and rng.random() < 0.5:
                trade_tags.append(rng.choice(["LATE_ENTRY", "FOMO_ENTRY", "PREMATURE_EXIT",
                                              "STOP_MOVED", "OVERSIZED"]))
            if mood == "rough" and i >= 3:
                trade_tags.append("OVERTRADE")
            tags.extend(trade_tags)
            trades.append(dict(
                strategy=strategy, version=version, side=direction,
                qty=rng.choice([2, 2, 3]), planned_qty=2 if rng.random() < 0.85 else 3,
                entry=(hour, minute), exit=(ex_h, ex_m),
                entry_px=entry_px, exit_px=exit_px, stop=stop, target=target,
                exec_mode="bot" if strategy == "DORB" and rng.random() < 0.5 else "manual",
                exit_reason="target" if r > 1 else ("stop" if r <= -0.95 else "manual"),
                intended_entry=entry_px - sgn * rng.choice([0, 0, 0.25, 0.5]),
                mfe_r=round(max(r, 0) + rng.uniform(0.1, 0.9), 2),
                mae_r=round(min(r, 0) - rng.uniform(0.05, 0.45), 2),
                tags=trade_tags, note="", mech_r=mech_r,
            ))
            opportunities.append(dict(strategy=strategy, version=version, at=(hour, minute),
                                      direction=direction, status="TAKEN",
                                      trade_ix=len(trades) - 1, mech_r=mech_r,
                                      decided_by="bot" if trades[-1]["exec_mode"] == "bot" else "human"))
        else:
            status = rng.choice(["MISSED", "SKIPPED_BY_RULE", "SKIPPED_DISCRETIONARY",
                                 "INVALIDATED", "NO_ACTION"])
            if status == "MISSED":
                tags.append("MISSED_SETUP")
            opportunities.append(dict(
                strategy=strategy, version=version, at=(hour, minute), direction=direction,
                status=status, trade_ix=None, mech_r=mech_r, decided_by="human",
                reason={"MISSED": "Alert fired with no one at the desk.",
                        "SKIPPED_BY_RULE": "Economic release inside the no-trade window.",
                        "SKIPPED_DISCRETIONARY": "Range looked too compressed to pay for the stop.",
                        "INVALIDATED": "Trigger reclaimed before the entry condition completed.",
                        "NO_ACTION": "Qualified late in the session, outside the trading window."}[status]))

    post = dict(
        execution_quality=min(5, max(1, base[0] + rng.randint(-1, 1))),
        rule_adherence=min(5, max(1, 5 - len(set(tags)) + rng.randint(0, 1))),
        patience=min(5, max(1, base[1] + rng.randint(-1, 1))),
        emotional_control=min(5, max(1, base[1] + rng.randint(-1, 1))),
        overtraded=1 if "OVERTRADE" in tags else 0,
        revenge_trade=1 if (mood == "rough" and rng.random() < 0.25) else 0,
        stop_moved=1 if "STOP_MOVED" in tags else 0,
        oversized=1 if "OVERSIZED" in tags else 0,
        missed_qualified=1 if "MISSED_SETUP" in tags else 0,
        manual_override=1 if rng.random() < 0.15 else 0,
        best_decision=rng.choice([
            "Sat out the first fifteen minutes as planned.",
            "Took the full stop instead of widening it.",
            "Closed the platform after the second loss.",
            "Waited for the retest.",
        ]),
        biggest_mistake=rng.choice([
            "Chased an entry that had already moved.",
            "Traded the third setup out of boredom.",
            "Took profit early on the only good trade.",
            "Nothing significant.",
        ]),
        unusual_context="",
        well_traded={"clean": "yes", "rough": "no", "flat": "yes"}[mood],
        note=DAY_SCRIPTS.get(d, {}).get("note", ""),
        fill_seconds=rng.randint(55, 140),
    )
    return dict(pre=pre, post=post, trades=trades, opportunities=opportunities,
                mood=mood, events=[], narrative=None, voice=None, ai_suggested=[])


def insert_session(conn: sqlite3.Connection, d: date, plan: dict) -> int:
    uid = f"{d.isoformat()}:MAIN:ACCT1"
    status = "closed" if plan["trades"] else ("no_trade" if not plan["opportunities"] else "closed")
    conn.execute(
        "INSERT INTO session(session_uid,session_date,tz,account_id,mode,status,started_at,ended_at,"
        "planned_max_risk_r,planned_max_loss,daily_loss_limit,max_trades_planned,iso_week,iso_month,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, d.isoformat(), "America/New_York", 1, "sim_eval", status,
         utc(d, 9, 12), utc(d, 16, 5), plan["pre"]["planned_max_risk_r"], 500.0, 750.0, 3,
         iso_week(d), d.strftime("%Y-%m"), utc(d, 9, 10), utc(d, 16, 10)),
    )
    sid = conn.execute("SELECT id FROM session WHERE session_uid=?", (uid,)).fetchone()[0]

    pre = dict(plan["pre"])
    pre.pop("planned_max_risk_r", None)
    cols = ",".join(pre.keys())
    conn.execute(
        f"INSERT INTO checkin_pre(session_id,submitted_at,{cols}) "
        f"VALUES (?,?,{','.join('?' * len(pre))})",
        (sid, utc(d, 9, 12), *pre.values()),
    )
    post = plan["post"]
    cols = ",".join(post.keys())
    conn.execute(
        f"INSERT INTO checkin_post(session_id,submitted_at,{cols}) "
        f"VALUES (?,?,{','.join('?' * len(post))})",
        (sid, utc(d, 16, 5), *post.values()),
    )
    conn.execute("INSERT INTO session_instrument(session_id,instrument_id,planned,actual) "
                 "VALUES (?,1,1,?)", (sid, 1 if plan["trades"] else 0))
    return sid


def insert_trades_and_opps(conn, sid: int, d: date, plan: dict) -> list[int]:
    batch_id = conn.execute(
        "INSERT INTO raw_import_batch(source,source_kind,source_uri,content_sha256,imported_at,"
        "row_count,importer,status) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        ("demo_broker_csv", "broker", f"raw/{d.isoformat()}-fills.csv",
         sha("fills", d), utc(d, 16, 30), len(plan["trades"]) * 2, "demo@1.0.0", "ok"),
    ).fetchone()[0]

    trade_ids: list[int] = []
    for ix, t in enumerate(plan["trades"]):
        svid = sv_id(conn, t["strategy"], t["version"])
        uid = sha(d, ix, t["strategy"])[:16]
        conn.execute(
            "INSERT INTO trade(trade_uid,session_id,account_id,mode,instrument_id,contract_id,"
            "strategy_version_id,execution_mode,side,quantity,entry_at,exit_at,avg_entry_price,"
            "avg_exit_price,initial_stop,initial_target,planned_risk_r,planned_quantity,exit_reason,"
            "was_manual_override,execution_note,created_at,updated_at)"
            " VALUES (?,?,1,'sim_eval',1,1,?,?,?,?,?,?,?,?,?,?,1.0,?,?,?,?,?,?)",
            (uid, sid, svid, t["exec_mode"], t["side"], t["qty"],
             utc(d, *t["entry"]), utc(d, *t["exit"]), t["entry_px"], t["exit_px"],
             t["stop"], t["target"], t["planned_qty"], t["exit_reason"],
             1 if "RULE_OVERRIDE" in t["tags"] else 0, t.get("note", ""),
             utc(d, *t["exit"]), utc(d, 16, 30)),
        )
        tid = conn.execute("SELECT id FROM trade WHERE trade_uid=?", (uid,)).fetchone()[0]
        trade_ids.append(tid)

        for leg, px, qty, when, intended in (
            ("entry", t["entry_px"], t["qty"], t["entry"], t.get("intended_entry")),
            ("exit", t["exit_px"], t["qty"], t["exit"], None),
        ):
            exec_id = sha(uid, leg)[:20]
            raw_id = conn.execute(
                "INSERT INTO raw_record(batch_id,source,record_type,external_id,occurred_at,"
                "received_at,payload,payload_sha256) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
                (batch_id, "demo_broker_csv", "fill", exec_id, utc(d, *when), utc(d, 16, 30),
                 json.dumps({"execId": exec_id, "sym": "MESU6", "px": px, "qty": qty,
                             "leg": leg, "ts": utc(d, *when)}),
                 sha(exec_id, px, qty)),
            ).fetchone()[0]
            buy = (t["side"] == "long") == (leg == "entry")
            conn.execute(
                "INSERT INTO trade_fill(trade_id,raw_record_id,order_id,exec_id,leg,side,quantity,"
                "price,filled_at,intended_price,commission,exchange_fees) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, raw_id, sha(uid, leg, "ord")[:12], exec_id, leg,
                 "buy" if buy else "sell", qty, px, utc(d, *when), intended,
                 round(0.37 * qty, 2), round(0.15 * qty, 2)),
            )

        for tag in t["tags"]:
            conn.execute(
                "INSERT INTO human_tag(trade_id,tag_code,confirmed_at) VALUES (?,?,?)",
                (tid, tag, utc(d, 16, 5)),
            )
        for phase, tf, off in (("pre_entry", "5m", -10), ("entry", "1m", 0),
                               ("exit", "1m", 1), ("post_exit", "5m", 15)):
            hh, mm = divmod(t["entry"][0] * 60 + t["entry"][1] + off, 60)
            conn.execute(
                "INSERT INTO media_asset(kind,trade_id,phase,timeframe,captured_at,path,sha256,"
                "width,height,overlays,capture_status) VALUES ('screenshot',?,?,?,?,?,?,?,?,?,'ok')",
                (tid, phase, tf, utc(d, hh % 24, mm),
                 f"media/{d.isoformat()}/{uid}-{phase}-{tf}.png", sha(uid, phase, tf),
                 1600, 900, json.dumps({"entry": t["entry_px"], "exit": t["exit_px"],
                                        "stop": t["stop"], "target": t["target"]})),
            )

    for o in plan["opportunities"]:
        svid = sv_id(conn, o["strategy"], o["version"])
        tid = trade_ids[o["trade_ix"]] if o["trade_ix"] is not None else None
        oid = conn.execute(
            "INSERT INTO opportunity(session_id,strategy_version_id,instrument_id,qualified_at,"
            "direction,status,status_reason,decided_by,trade_id,detection_source,created_at)"
            " VALUES (?,?,1,?,?,?,?,?,?,'engine',?) RETURNING id",
            (sid, svid, utc(d, *o["at"]), o["direction"], o["status"], o.get("reason"),
             o["decided_by"], tid, utc(d, 16, 30)),
        ).fetchone()[0]
        if tid is not None:
            conn.execute("UPDATE trade SET opportunity_id=? WHERE id=?", (oid, tid))
        mech = o["mech_r"]
        conn.execute(
            "INSERT INTO mechanical_reference(opportunity_id,entry_at,entry_price,stop_price,"
            "target_price,exit_at,exit_price,exit_reason,r_multiple,mfe_r,mae_r,calc_version,"
            "inputs_hash,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, utc(d, *o["at"]), 5700.0, 5695.0, 5711.0, utc(d, o["at"][0] + 1, o["at"][1]),
             5700.0 + mech * 5, "target" if mech > 0 else "stop", mech,
             round(max(mech, 0) + 0.3, 2), round(min(mech, 0) - 0.2, 2),
             CALC_VERSION, sha(oid, mech), GEN_AT),
        )

    for hh, mm, level, source, msg in plan.get("events", []):
        conn.execute(
            "INSERT INTO system_event(session_id,occurred_at,source,level,event_type,message)"
            " VALUES (?,?,?,?,?,?)",
            (sid, utc(d, hh, mm), source, level, "journal_event", msg),
        )

    if plan.get("voice"):
        v = plan["voice"]
        conn.execute(
            "INSERT INTO voice_note(session_id,recorded_at,audio_path,audio_sha256,duration_seconds,"
            "transcript,transcript_engine,transcript_version,transcript_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, utc(d, *v["at"]), f"media/{d.isoformat()}/checkout.m4a", sha("audio", d),
             v["seconds"], v["transcript"], "whisper-large-v3", "3.0", utc(d, v["at"][0], v["at"][1] + 1)),
        )

    for a in plan.get("ai_suggested", []):
        conn.execute(
            "INSERT INTO ai_annotation(target_type,target_id,annotation_type,tag_code,content,"
            "evidence,confidence,model,model_version,prompt_hash,created_at,status,reviewed_at)"
            " VALUES ('trade',?,'tag',?,?,?,?,?,?,?,?,?,?)",
            (trade_ids[a["trade_ix"]], a["tag"], a["rationale"],
             json.dumps({"fields": ["entry_at", "signal.fired_at", "avg_entry_price"]}),
             a["confidence"], "journal-tagger", "0.2.0", sha("tagprompt", a["tag"]),
             utc(d, 16, 20), a["status"], utc(d, 16, 25)),
        )

    if plan.get("narrative"):
        conn.execute(
            "INSERT INTO ai_narrative(scope,scope_key,facts,text,model,model_version,prompt_hash,"
            "inputs_hash,created_at) VALUES ('session',?,?,?,?,?,?,?,?)",
            (f"{d.isoformat()}:MAIN:ACCT1",
             json.dumps({"trades": len(plan["trades"]),
                         "opportunities": len(plan["opportunities"])}),
             plan["narrative"], "journal-narrator", "0.2.0", sha("narrative", d),
             sha("inputs", d), utc(d, 16, 25)),
        )
    return trade_ids


# ----------------------------------------------------------------------------- derived layer
def compute_derived(conn: sqlite3.Connection) -> None:
    """Recompute the DERIVED layer from RAW. Idempotent, versioned, traceable."""
    run_id = conn.execute(
        "INSERT INTO derived_run(calc_version,code_sha,scope,started_at,status)"
        " VALUES (?,?,?,?,'ok') RETURNING id",
        (CALC_VERSION, sha("build_synthetic.py"), "trade_metrics+session_metrics", GEN_AT),
    ).fetchone()[0]

    conn.execute("DELETE FROM trade_metrics")
    conn.execute("DELETE FROM session_metrics")

    point_value = conn.execute("SELECT point_value FROM instrument WHERE id=1").fetchone()[0]
    tick_size = conn.execute("SELECT tick_size FROM instrument WHERE id=1").fetchone()[0]

    rows = conn.execute(
        "SELECT id,session_id,side,quantity,planned_quantity,avg_entry_price,avg_exit_price,"
        "initial_stop,entry_at,exit_at FROM trade ORDER BY session_id, entry_at"
    ).fetchall()

    cum: dict[int, float] = {}
    index: dict[int, int] = {}
    hero_lookup = {}
    for d, plan in list(HERO.items()):
        for i, t in enumerate(plan["trades"]):
            hero_lookup[(d.isoformat(), i)] = t

    for tid, sid, side, qty, planned_qty, entry, exit_px, stop, entry_at, exit_at in rows:
        sgn = 1 if side == "long" else -1
        risk_per_unit = abs(entry - stop) if stop else None
        r = ((exit_px - entry) * sgn / risk_per_unit) if (risk_per_unit and exit_px) else None
        gross = (exit_px - entry) * sgn * qty * point_value if exit_px else None
        fees = conn.execute(
            "SELECT ROUND(SUM(commission + exchange_fees),2) FROM trade_fill WHERE trade_id=?", (tid,)
        ).fetchone()[0] or 0.0
        net = round(gross - fees, 2) if gross is not None else None

        intended = conn.execute(
            "SELECT intended_price FROM trade_fill WHERE trade_id=? AND leg='entry'", (tid,)
        ).fetchone()[0]
        # negative = adverse: you paid worse than the intended price
        slip = round(((intended - entry) * sgn) / tick_size, 2) if intended is not None else None

        hold = int((datetime.strptime(exit_at, "%Y-%m-%dT%H:%M:%SZ")
                    - datetime.strptime(entry_at, "%Y-%m-%dT%H:%M:%SZ")).total_seconds())

        # MFE/MAE come from the intrabar path in production; here they are carried
        # from the synthetic script and stored as DERIVED with the same calc_version.
        mfe_r = round(max(r, 0) + 0.35, 2) if r is not None else None
        mae_r = round(min(r, 0) - 0.2, 2) if r is not None else None
        for key, t in hero_lookup.items():
            if abs(t["entry_px"] - entry) < 1e-9 and abs(t["exit_px"] - exit_px) < 1e-9:
                mfe_r, mae_r = t["mfe_r"], t["mae_r"]

        index[sid] = index.get(sid, 0) + 1
        cum[sid] = round(cum.get(sid, 0.0) + (net or 0.0), 2)

        conn.execute(
            "INSERT INTO trade_metrics(trade_id,gross_pnl,fees,net_pnl,risk_per_unit,r_multiple,"
            "mfe_r,mae_r,seconds_to_mfe,seconds_to_mae,holding_seconds,entry_slippage_ticks,"
            "size_deviation_pct,r_captured_pct,session_cum_pnl_after,session_trade_index,"
            "calc_version,inputs_hash,derived_run_id,computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, round(gross, 2) if gross is not None else None, fees, net, risk_per_unit,
             round(r, 3) if r is not None else None, mfe_r, mae_r,
             int(hold * 0.55), int(hold * 0.25), hold, slip,
             round((qty - planned_qty) / planned_qty * 100, 1) if planned_qty else None,
             # only meaningful on winners: share of the favourable excursion kept
             round(r / mfe_r * 100, 1) if (r and r > 0 and mfe_r and mfe_r > 0) else None,
             cum[sid], index[sid], CALC_VERSION, sha(tid, entry, exit_px), run_id, GEN_AT),
        )

    for (sid,) in conn.execute("SELECT id FROM session").fetchall():
        agg = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN tm.r_multiple>0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN tm.r_multiple<0 THEN 1 ELSE 0 END),"
            " SUM(tm.gross_pnl), SUM(tm.fees), SUM(tm.net_pnl), SUM(tm.r_multiple)"
            " FROM trade t JOIN trade_metrics tm ON tm.trade_id=t.id WHERE t.session_id=?", (sid,)
        ).fetchone()
        n, wins, losses, gross, fees, net, total_r = (agg[0] or 0, agg[1] or 0, agg[2] or 0,
                                                      agg[3], agg[4], agg[5], agg[6])
        opps = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status IN ('TAKEN','BOT_EXECUTED') THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN status='MISSED' THEN 1 ELSE 0 END)"
            " FROM opportunity WHERE session_id=?", (sid,)
        ).fetchone()
        mech_r = conn.execute(
            "SELECT SUM(mr.r_multiple) FROM mechanical_reference mr"
            " JOIN opportunity o ON o.id=mr.opportunity_id WHERE o.session_id=?", (sid,)
        ).fetchone()[0] or 0.0
        mistakes = conn.execute(
            "SELECT COUNT(*) FROM human_tag ht LEFT JOIN trade t ON t.id=ht.trade_id"
            " WHERE ht.session_id=? OR t.session_id=?", (sid, sid)
        ).fetchone()[0]
        post = conn.execute(
            "SELECT execution_quality,rule_adherence,patience,emotional_control"
            " FROM checkin_post WHERE session_id=?", (sid,)
        ).fetchone()

        # PROCESS SCORE — deliberately contains no P&L term. See docs/02 §5.
        if post and all(v is not None for v in post):
            eq, ra, pa, ec = post
            conformance = 1 - min(1.0, mistakes / 3.0)
            process = 100 * (0.35 * ra / 5 + 0.20 * eq / 5 + 0.15 * pa / 5
                             + 0.10 * ec / 5 + 0.20 * conformance)
        else:
            process, ra = None, None

        # running per-trade drawdown in R
        r_seq = [row[0] for row in conn.execute(
            "SELECT tm.r_multiple FROM trade t JOIN trade_metrics tm ON tm.trade_id=t.id"
            " WHERE t.session_id=? ORDER BY t.entry_at", (sid,))]
        peak, run, dd = 0.0, 0.0, 0.0
        for r in r_seq:
            run += r or 0
            peak = max(peak, run)
            dd = min(dd, run - peak)

        conn.execute(
            "INSERT INTO session_metrics(session_id,trade_count,win_count,loss_count,scratch_count,"
            "gross_pnl,fees,net_pnl,total_r,max_drawdown_r,expectancy_r,opportunities_total,"
            "opportunities_taken,opportunities_missed,mechanical_r,user_value_added_r,process_score,"
            "rule_adherence,mistake_count,calc_version,derived_run_id,computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, n, wins, losses, n - wins - losses,
             round(gross, 2) if gross else 0.0, round(fees, 2) if fees else 0.0,
             round(net, 2) if net else 0.0, round(total_r, 2) if total_r else 0.0,
             round(dd, 2), round(total_r / n, 3) if n else None,
             opps[0] or 0, opps[1] or 0, opps[2] or 0, round(mech_r, 2),
             round((total_r or 0.0) - mech_r, 2),
             round(process, 1) if process is not None else None,
             ra, mistakes, CALC_VERSION, run_id, GEN_AT),
        )

    conn.execute("UPDATE derived_run SET finished_at=?, rows_written=? WHERE id=?",
                 (GEN_AT, len(rows), run_id))
    conn.commit()


def compute_market_context(conn: sqlite3.Connection) -> None:
    for sid, sdate in conn.execute("SELECT id, session_date FROM session").fetchall():
        d = date.fromisoformat(sdate)
        base = 5700 + rng.uniform(-40, 40)
        atr = round(rng.uniform(38, 72), 1)
        conn.execute(
            "INSERT INTO market_context_session(session_id,instrument_id,prior_high,prior_low,"
            "prior_close,overnight_high,overnight_low,gap_points,gap_atr_ratio,atr14,"
            "realized_vol_20d,trend_state,opening_range_high,opening_range_low,"
            "opening_range_minutes,session_high,session_low,session_range,vwap_close_dist,"
            "econ_events,data_source,calc_version,computed_at)"
            " VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,30,?,?,?,?,?,?,?,?)",
            (sid, round(base + 18, 2), round(base - 22, 2), round(base, 2),
             round(base + 12, 2), round(base - 9, 2), round(rng.uniform(-14, 14), 2),
             round(rng.uniform(-0.4, 0.4), 2), atr, round(rng.uniform(8, 19), 1),
             rng.choice(["up", "down", "range", "range"]),
             round(base + 9, 2), round(base - 7, 2),
             round(base + 21, 2), round(base - 25, 2), round(rng.uniform(28, 62), 2),
             round(rng.uniform(-8, 8), 2),
             json.dumps([{"time": "08:30", "name": "CPI", "importance": "high"}]
                        if d == TODAY else []),
             "demo_bars", CALC_VERSION, GEN_AT),
        )
    conn.commit()


def compute_blinded(conn: sqlite3.Connection) -> None:
    """Blinded features carry opaque keys. Labels live in the dict table only."""
    dictionary = [
        ("pa_f01", "personal_astro", "Transit A intensity"),
        ("pa_f07", "personal_astro", "Money-house transit active"),
        ("pa_f17", "personal_astro", "Part of Fortune aspect state"),
        ("ma_f03", "market_astro", "Market composite state A"),
        ("ln_f01", "lunar", "Lunar phase fraction"),
    ]
    for key, fset, label in dictionary:
        conn.execute(
            "INSERT INTO blinded_feature_dict(feature_key,feature_set,label,definition,engine_version)"
            " VALUES (?,?,?,?,?)",
            (key, fset, label, "Definition withheld from daily UI by design.", "astro@1.4.0"),
        )
    for uid, in conn.execute("SELECT session_uid FROM session").fetchall():
        for key, fset, _ in dictionary:
            conn.execute(
                "INSERT INTO blinded_feature(scope_type,scope_id,feature_set,feature_key,value_num,"
                "engine,engine_version,computed_at) VALUES ('session',?,?,?,?,?,?,?)",
                (uid, fset, key, round(rng.uniform(0, 1), 4), "astro-engine", "astro@1.4.0", GEN_AT),
            )
    conn.execute(
        "INSERT INTO preregistration(question,hypothesis,feature_keys,outcome_metric,min_n,"
        "analysis_plan,created_at,locked_hash,status) VALUES (?,?,?,?,?,?,?,?,'draft')",
        ("Does personal transit state precede rule violations?",
         "No effect. Registered to prevent post-hoc slicing.",
         json.dumps(["pa_f01", "pa_f17"]), "rule_violation", 120,
         "Logistic regression, session grain, holdout by month. No subgroup slicing.",
         GEN_AT, sha("prereg", 1)),
    )
    conn.commit()


# ----------------------------------------------------------------------------- export
def export_dataset(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]

    sessions = []
    for s in q("SELECT * FROM session ORDER BY session_date"):
        sid, uid = s["id"], s["session_uid"]
        m = q("SELECT * FROM session_metrics WHERE session_id=?", sid)
        pre = q("SELECT * FROM checkin_pre WHERE session_id=?", sid)
        post = q("SELECT * FROM checkin_post WHERE session_id=?", sid)
        trades = q(
            "SELECT t.id, t.trade_uid, i.symbol, sv.strategy_id, sv.version AS strategy_version,"
            " t.execution_mode, t.side, t.quantity, t.planned_quantity, t.entry_at, t.exit_at,"
            " t.avg_entry_price, t.avg_exit_price, t.initial_stop, t.initial_target, t.exit_reason,"
            " t.execution_note, tm.net_pnl, tm.gross_pnl, tm.fees, tm.r_multiple, tm.mfe_r, tm.mae_r,"
            " tm.holding_seconds, tm.seconds_to_mfe, tm.seconds_to_mae, tm.entry_slippage_ticks,"
            " tm.size_deviation_pct, tm.r_captured_pct, tm.session_cum_pnl_after,"
            " o.status AS opportunity_status, mr.r_multiple AS mechanical_r"
            " FROM trade t JOIN instrument i ON i.id=t.instrument_id"
            " LEFT JOIN strategy_version sv ON sv.id=t.strategy_version_id"
            " LEFT JOIN trade_metrics tm ON tm.trade_id=t.id"
            " LEFT JOIN opportunity o ON o.id=t.opportunity_id"
            " LEFT JOIN mechanical_reference mr ON mr.opportunity_id=o.id"
            " WHERE t.session_id=? ORDER BY t.entry_at", sid)
        for t in trades:
            t["tags"] = [r["tag_code"] for r in conn.execute(
                "SELECT tag_code FROM human_tag WHERE trade_id=?", (t["id"],))]
            t["media"] = q("SELECT phase,timeframe,path,captured_at FROM media_asset"
                           " WHERE trade_id=? ORDER BY captured_at", t["id"])
            t["ai_suggestions"] = q(
                "SELECT tag_code,content,confidence,status,model,model_version,created_at"
                " FROM ai_annotation WHERE target_type='trade' AND target_id=?", t["id"])
        opportunities = q(
            "SELECT o.id, sv.strategy_id, sv.version AS strategy_version, o.qualified_at,"
            " o.direction, o.status, o.status_reason, o.decided_by, o.trade_id,"
            " mr.r_multiple AS mechanical_r FROM opportunity o"
            " JOIN strategy_version sv ON sv.id=o.strategy_version_id"
            " LEFT JOIN mechanical_reference mr ON mr.opportunity_id=o.id"
            " WHERE o.session_id=? ORDER BY o.qualified_at", sid)
        sessions.append(dict(
            session_uid=uid, date=s["session_date"], status=s["status"], mode=s["mode"],
            tz=s["tz"], iso_week=s["iso_week"], iso_month=s["iso_month"],
            planned_max_risk_r=s["planned_max_risk_r"], daily_loss_limit=s["daily_loss_limit"],
            metrics=m[0] if m else None, pre=pre[0] if pre else None, post=post[0] if post else None,
            trades=trades, opportunities=opportunities,
            events=q("SELECT occurred_at,source,level,message FROM system_event"
                     " WHERE session_id=? ORDER BY occurred_at", sid),
            voice=q("SELECT recorded_at,duration_seconds,transcript,transcript_engine,"
                    "transcript_version FROM voice_note WHERE session_id=?", sid),
            narrative=q("SELECT text,model,model_version,created_at FROM ai_narrative"
                        " WHERE scope='session' AND scope_key=?", uid),
            market=q("SELECT * FROM market_context_session WHERE session_id=?", sid),
        ))

    strategies = q(
        "SELECT sv.id, sv.strategy_id, st.name, sv.version, sv.automation_level,"
        " sv.qualification_status, sv.active_from, sv.active_to, sv.rule_hash, sv.reference_impl"
        " FROM strategy_version sv JOIN strategy st ON st.id=sv.strategy_id"
        " ORDER BY sv.strategy_id, sv.version DESC")
    scorecard = q("SELECT * FROM v_strategy_scorecard")

    payload = dict(
        meta=dict(
            generated_at=GEN_AT, calc_version=CALC_VERSION, seed=SEED,
            today=TODAY.isoformat(), schema="0002_research_views",
            synthetic=True,
            note="Every value in this file is synthetic. No real trading data.",
        ),
        sessions=sessions,
        strategies=strategies,
        strategy_scorecard=scorecard,
        mistake_taxonomy=q("SELECT code,label,category,description FROM mistake_tag"
                           " ORDER BY sort_order"),
        mistake_frequency=q("SELECT * FROM v_mistake_frequency ORDER BY occurrences DESC"),
        research=dict(
            sample_sizes=q("SELECT * FROM r_feature_sample_sizes"),
            preregistrations=q("SELECT id,question,hypothesis,outcome_metric,min_n,status,"
                               "created_at FROM preregistration"),
            unblind_log=q("SELECT * FROM research_unblind_log"),
            feature_sets=q("SELECT feature_set, COUNT(DISTINCT feature_key) AS keys"
                           " FROM blinded_feature GROUP BY feature_set"),
        ),
    )
    conn.row_factory = None
    return payload


def write_exports(conn: sqlite3.Connection, payload: dict) -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(os.path.join(EXPORT_DIR, "journal.json"), "w") as fh:
        json.dump(payload, fh, indent=2)

    conn.row_factory = sqlite3.Row
    for name, sql in (
        ("sessions.csv", "SELECT * FROM v_session_daily ORDER BY session_date"),
        ("trades.csv", "SELECT * FROM v_trade_full ORDER BY entry_at"),
        ("opportunities.csv",
         "SELECT o.*, mr.r_multiple AS mechanical_r FROM opportunity o"
         " LEFT JOIN mechanical_reference mr ON mr.opportunity_id=o.id ORDER BY qualified_at"),
        ("checkins.csv",
         "SELECT s.session_date, cp.*, cq.execution_quality, cq.rule_adherence, cq.patience,"
         " cq.emotional_control, cq.well_traded FROM session s"
         " LEFT JOIN checkin_pre cp ON cp.session_id=s.id"
         " LEFT JOIN checkin_post cq ON cq.session_id=s.id ORDER BY s.session_date"),
    ):
        rows = conn.execute(sql).fetchall()
        if not rows:
            continue
        with open(os.path.join(EXPORT_DIR, name), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=rows[0].keys())
            w.writeheader()
            for r in rows:
                w.writerow(dict(r))
    conn.row_factory = None

    with open(DATA_JS, "w") as fh:
        fh.write("// GENERATED FILE — do not edit by hand.\n")
        fh.write("// Produced by scripts/build_synthetic.py from build/journal.db.\n")
        fh.write("// All data is synthetic placeholder data.\n")
        fh.write("window.JOURNAL = ")
        json.dump(payload, fh, separators=(",", ":"))
        fh.write(";\n")


def main() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    create_db(conn)
    load_reference(conn)

    start = TODAY - timedelta(days=44)
    for d in trading_days(start, TODAY):
        plan = HERO[d] if d in HERO else gen_day_plan(d)
        if d in HERO:
            plan = dict(plan)
            plan.setdefault("mood", "clean")
            plan["pre"] = dict(plan["pre"])
        sid = insert_session(conn, d, plan)
        insert_trades_and_opps(conn, sid, d, plan)
    conn.commit()

    compute_market_context(conn)
    compute_blinded(conn)
    compute_derived(conn)

    payload = export_dataset(conn)
    write_exports(conn, payload)

    n_sessions = len(payload["sessions"])
    n_trades = sum(len(s["trades"]) for s in payload["sessions"])
    n_opps = sum(len(s["opportunities"]) for s in payload["sessions"])
    size_kb = os.path.getsize(DATA_JS) / 1024
    print(f"db       : {DB_PATH}")
    print(f"sessions : {n_sessions}")
    print(f"trades   : {n_trades}")
    print(f"opps     : {n_opps}")
    print(f"exports  : {EXPORT_DIR}")
    print(f"data.js  : {size_kb:.0f} KB")
    conn.close()


if __name__ == "__main__":
    main()
