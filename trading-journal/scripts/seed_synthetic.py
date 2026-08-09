#!/usr/bin/env python3
"""Synthetic fixtures.

Every scenario the director listed is here as a named, deliberately constructed
day rather than a random draw, so each one can be pointed at and checked:

    winner · loser · breakeven · partial fill · manual trade · bot trade ·
    missed setup · discretionary skip · bot failure · rule violation ·
    strategy-version transition · missing check-in · amended check-in ·
    voice note · negative session with a high process index ·
    positive session with a poor process index

The last two exist to prove the interface never equates P&L with process. If
the UI ever starts colouring a good process day by its loss, those two days
will show it immediately.

Everything is written through journal.repo — the same code path the interface
uses — so seeding also exercises the production write path.

Usage:  python3 scripts/seed_synthetic.py [--reset]
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import backup, config, db, metrics, repo  # noqa: E402

SEED = 20260807
ACCOUNT = "Futures Eval 50k"
SYMBOL = "MES"
rng = random.Random(SEED)

STRATEGIES = [
    ("10AM_MODEL", "10AM Model", "opening_range",
     "Post-open continuation model keyed to the 10:00 ET reference window."),
    ("DORB", "DORB", "opening_range",
     "Daily opening-range breakout with session-range risk normalisation."),
    ("DISCRETIONARY_X", "Discretionary X", "discretionary",
     "Unstructured discretionary trades, logged so discretion can be measured."),
]

MISTAKE_TAXONOMY = [
    ("OVERTRADE", "Overtraded", "discipline", "More trades than the session plan allowed."),
    ("REVENGE", "Revenge trade", "discipline", "Entry motivated by recovering a prior loss."),
    ("FOMO_ENTRY", "FOMO entry", "entry", "Entered after the setup because the move was running."),
    ("EARLY_ENTRY", "Early entry", "entry", "Entered before the trigger condition completed."),
    ("LATE_ENTRY", "Late entry", "entry", "Entered after the trigger, reducing available R."),
    ("PREMATURE_EXIT", "Premature exit", "exit", "Closed before target or invalidation."),
    ("STOP_MOVED", "Stop moved improperly", "risk", "Stop widened or moved against the plan."),
    ("OVERSIZED", "Oversized", "risk", "Position larger than the planned risk unit."),
    ("MISSED_SETUP", "Missed qualified setup", "process", "A qualified setup passed with no action."),
    ("RULE_OVERRIDE", "Rule override", "discipline", "Knowingly traded against a written rule."),
    ("UNPLANNED_TRADE", "Unplanned trade", "process", "Traded a strategy not in the session plan."),
    ("DISTRACTED", "Distracted", "process", "Attention away from the screen at a decision point."),
    ("TECHNICAL_ERROR", "Technical error", "system", "Platform, order-entry or connectivity error."),
    ("BOT_ROUTING_ERROR", "Bot routing error", "system", "Automation failed to route or filled incorrectly."),
    ("OTHER", "Other", "process", "Anything not covered; requires a note."),
]


def pre(**kw) -> dict:
    """A morning check-in with the seven daily fields filled in."""
    base = dict(sleep_hours=7.0, energy=4, focus=4, stress=2, desire_to_trade=3,
                bias="neutral", well_traded_definition="Only A-setups. Two trades maximum.",
                fill_seconds=44)
    base.update(kw)
    return base


def post(**kw) -> dict:
    base = dict(execution_quality=4, rule_adherence=4, patience=4, emotional_control=4,
                overtraded=0, revenge_trade=0, stop_moved=0, oversized=0,
                missed_qualified=0, manual_override=0, well_traded="yes", fill_seconds=95)
    base.update(kw)
    return base


def trade(strategy, side, entry_t, exit_t, entry_px, exit_px, stop, target, **kw) -> dict:
    base = dict(symbol=SYMBOL, strategy_id=strategy, side=side, quantity=2, planned_quantity=2,
                entry_at=entry_t, exit_at=exit_t, avg_entry_price=entry_px,
                avg_exit_price=exit_px, initial_stop=stop, initial_target=target,
                execution_mode="manual", commission=0.74, exchange_fees=0.30)
    base.update(kw)
    return base


# Trading days, most recent last. Each entry is (kind, builder).
def build_days():
    """Return {date: spec}. Dates are business days ending Friday 2026-08-07."""
    days = {}

    d = date(2026, 8, 7)          # ---- hero day: mixed session, one missed setup
    days[d] = dict(
        label="hero",
        pre=pre(sleep_hours=6.2, energy=3, focus=4, stress=2, desire_to_trade=3,
                well_traded_definition="Take only A-setups. Two trades maximum. "
                                       "No adds after 11:15.",
                note="Slept short but clear-headed.", fill_seconds=41),
        post=post(patience=3, missed_qualified=1, well_traded="mixed",
                  best_decision="Waited for the retest on the 10AM entry instead of "
                                "chasing the first push.",
                  biggest_mistake="Hesitated on the DORB trigger and paid two ticks for it.",
                  unusual_context="CPI at 08:30 left the first 20 minutes unusually wide.",
                  fill_seconds=94),
        trades=[
            trade("10AM_MODEL", "long", f"{d}T10:04", f"{d}T10:41", 5712.25, 5721.00,
                  5707.50, 5723.75, quantity=3, planned_quantity=3, execution_mode="semi_auto",
                  exit_reason="target", reported_mfe_price=5722.00, reported_mae_price=5710.75,
                  intended_entry_price=5712.00,
                  execution_note="Clean retest of the 10:00 reference. Held to target."),
            trade("DORB", "short", f"{d}T11:06", f"{d}T11:24", 5716.75, 5721.25,
                  5721.25, 5707.75, exit_reason="stop", tags=["LATE_ENTRY"],
                  reported_mfe_price=5714.00, reported_mae_price=5721.25,
                  intended_entry_price=5718.25,
                  execution_note="Trigger printed at 11:04, I entered at 11:06. "
                                 "Lost six ticks of stop distance."),
        ],
        opportunities=[
            dict(strategy_id="10AM_MODEL", symbol=SYMBOL, qualified_at=f"{d}T10:02",
                 direction="long", status="TAKEN", trade_index=0, detection_source="engine"),
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T11:04",
                 direction="short", status="TAKEN", trade_index=1, detection_source="engine"),
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T13:18", direction="long",
                 status="MISSED", detection_source="engine",
                 status_reason="Away from the desk; alert fired with no one watching."),
        ],
        voice=dict(seconds=48, at=f"{d}T16:08", transcript=(
            "Okay, end of day. The ten AM trade was the best execution I've had this week, I "
            "actually waited for the retest instead of grabbing the first push. The DORB one I "
            "was slow on. The one that annoys me is the one thirty trade, I wasn't at the desk. "
            "That's the second time this week.")),
        suggestions=[
            dict(trade_index=1, code="LATE_ENTRY", confidence=0.86, accept=True,
                 rationale="Entry 11:06:12 is 132s after the 11:04:00 signal; entry price "
                           "1.5 points adverse to signal price."),
            dict(trade_index=1, code="DISTRACTED", confidence=0.41, accept=False,
                 rationale="Voice note mentions being away from the desk in the same session."),
        ],
    )

    d = date(2026, 8, 6)          # ---- POSITIVE session, POOR process
    days[d] = dict(
        label="positive_pnl_poor_process",
        pre=pre(sleep_hours=5.1, energy=2, focus=2, stress=4, desire_to_trade=5,
                bias="bullish", well_traded_definition="Three trades maximum, no revenge.",
                money_pressure=1, impulsivity=4),
        post=post(execution_quality=2, rule_adherence=2, patience=1, emotional_control=2,
                  overtraded=1, revenge_trade=1, well_traded="no",
                  best_decision="Stopped at 12:00 instead of continuing.",
                  biggest_mistake="Traded five times on a plan that allowed three, and the "
                                  "fourth was pure revenge after the third stopped out."),
        trades=[
            trade("DORB", "long", f"{d}T09:41", f"{d}T09:58", 5688.00, 5695.50, 5684.00,
                  5700.00, exit_reason="manual", reported_mfe_price=5696.75,
                  reported_mae_price=5686.50, tags=["FOMO_ENTRY"]),
            trade("DORB", "long", f"{d}T10:22", f"{d}T10:31", 5697.25, 5693.25, 5693.25,
                  5707.00, exit_reason="stop", reported_mae_price=5693.25),
            trade("DISCRETIONARY_X", "long", f"{d}T10:44", f"{d}T11:19", 5694.50, 5706.25,
                  5689.50, 5709.00, exit_reason="target", quantity=4, planned_quantity=2,
                  reported_mfe_price=5707.50, reported_mae_price=5692.00,
                  tags=["OVERSIZED", "REVENGE"],
                  execution_note="Doubled size to get it back. It worked. That is the problem."),
            trade("DISCRETIONARY_X", "short", f"{d}T11:38", f"{d}T11:52", 5705.00, 5701.75,
                  5709.50, 5697.00, exit_reason="manual", reported_mfe_price=5700.50,
                  tags=["OVERTRADE"]),
            trade("DISCRETIONARY_X", "long", f"{d}T11:57", f"{d}T12:00", 5702.50, 5703.25,
                  5698.50, 5710.00, exit_reason="manual", tags=["OVERTRADE", "UNPLANNED_TRADE"]),
        ],
        opportunities=[
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T09:40", direction="long",
                 status="TAKEN", trade_index=0, detection_source="engine"),
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T10:21", direction="long",
                 status="TAKEN", trade_index=1, detection_source="engine"),
        ],
    )

    d = date(2026, 8, 5)          # ---- NEGATIVE session, HIGH process
    days[d] = dict(
        label="negative_pnl_high_process",
        pre=pre(sleep_hours=7.8, energy=4, focus=5, stress=1, desire_to_trade=2,
                well_traded_definition="Take the two setups if they come. Full stop distance. "
                                       "Nothing else."),
        post=post(execution_quality=5, rule_adherence=5, patience=5, emotional_control=5,
                  well_traded="yes",
                  best_decision="Took both stops exactly where they were placed and stopped "
                                "trading afterwards.",
                  biggest_mistake="Nothing. The setups failed; the execution did not."),
        trades=[
            trade("10AM_MODEL", "long", f"{d}T10:03", f"{d}T10:19", 5701.00, 5696.00, 5696.00,
                  5711.00, exit_reason="stop", reported_mfe_price=5703.50,
                  reported_mae_price=5696.00,
                  execution_note="Textbook entry, setup simply failed."),
            trade("DORB", "short", f"{d}T11:12", f"{d}T11:29", 5694.50, 5699.00, 5699.00,
                  5685.50, exit_reason="stop", reported_mfe_price=5692.25,
                  reported_mae_price=5699.00,
                  execution_note="Second stop of the day. Closed the platform after this."),
        ],
        opportunities=[
            dict(strategy_id="10AM_MODEL", symbol=SYMBOL, qualified_at=f"{d}T10:02",
                 direction="long", status="TAKEN", trade_index=0, detection_source="engine"),
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T11:10", direction="short",
                 status="TAKEN", trade_index=1, detection_source="engine"),
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T14:02", direction="long",
                 status="SKIPPED_BY_RULE", detection_source="engine",
                 status_reason="Two-stop rule: no further trades after two losses."),
        ],
    )

    d = date(2026, 8, 4)          # ---- breakeven + partial fill + bot trade
    days[d] = dict(
        label="breakeven_partial_bot",
        pre=pre(sleep_hours=7.2, energy=4, focus=4, stress=2, desire_to_trade=3),
        post=post(well_traded="yes",
                  best_decision="Scaled out half at the first target instead of holding "
                                "the whole position through the news.",
                  biggest_mistake="Nothing significant."),
        trades=[
            # breakeven: exits exactly at entry
            trade("DORB", "short", f"{d}T09:52", f"{d}T10:07", 5710.00, 5710.00, 5714.00,
                  5702.00, exit_reason="manual", reported_mfe_price=5706.50,
                  reported_mae_price=5712.00,
                  execution_note="Moved to breakeven and got taken out flat. Correct call."),
            # partial fill: two exit legs at different prices
            dict(symbol=SYMBOL, strategy_id="10AM_MODEL", side="long", quantity=4,
                 planned_quantity=4, entry_at=f"{d}T10:31", exit_at=f"{d}T11:14",
                 avg_entry_price=5706.00, avg_exit_price=5713.00, initial_stop=5701.50,
                 initial_target=5715.00, execution_mode="bot", exit_reason="target",
                 reported_mfe_price=5715.50, reported_mae_price=5704.25,
                 execution_note="Bot scaled out half at the first target, remainder at the second.",
                 fills=[
                     dict(leg="entry", price=5706.00, quantity=4, filled_at=f"{d}T10:31",
                          intended_price=5706.00, commission=1.48, exchange_fees=0.60),
                     dict(leg="partial", price=5711.00, quantity=2, filled_at=f"{d}T10:52",
                          commission=0.74, exchange_fees=0.30),
                     dict(leg="exit", price=5715.00, quantity=2, filled_at=f"{d}T11:14",
                          commission=0.74, exchange_fees=0.30),
                 ]),
        ],
        opportunities=[
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T09:50", direction="short",
                 status="TAKEN", trade_index=0, detection_source="engine"),
            dict(strategy_id="10AM_MODEL", symbol=SYMBOL, qualified_at=f"{d}T10:30",
                 direction="long", status="BOT_EXECUTED", trade_index=1,
                 detection_source="engine", decided_by="bot"),
        ],
    )

    d = date(2026, 8, 3)          # ---- bot failure + discretionary skip, no trades
    days[d] = dict(
        label="bot_failure_no_trade",
        pre=pre(sleep_hours=6.8, energy=3, focus=3, stress=3, desire_to_trade=2,
                bias="bearish"),
        post=post(execution_quality=3, patience=5, well_traded="yes",
                  best_decision="Did not hand-trade the setup the bot missed.",
                  biggest_mistake="Should have checked the bot was connected before the open.",
                  unusual_context="Automation was disconnected for the first hour."),
        trades=[],
        opportunities=[
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T09:47", direction="short",
                 status="BOT_FAILED", detection_source="engine", decided_by="bot",
                 status_reason="Automation was disconnected; the order never routed."),
            dict(strategy_id="10AM_MODEL", symbol=SYMBOL, qualified_at=f"{d}T10:05",
                 direction="long", status="SKIPPED_DISCRETIONARY", detection_source="engine",
                 status_reason="Range too compressed to pay for the stop."),
        ],
    )

    d = date(2026, 7, 31)         # ---- missing check-in: traded, never checked out
    days[d] = dict(
        label="missing_checkout",
        pre=pre(sleep_hours=6.0, energy=3, focus=3, stress=3, desire_to_trade=4),
        post=None,
        trades=[
            trade("DORB", "long", f"{d}T10:12", f"{d}T10:40", 5670.00, 5677.50, 5665.50,
                  5681.00, exit_reason="manual", reported_mfe_price=5679.00,
                  reported_mae_price=5668.75),
        ],
        opportunities=[
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T10:10", direction="long",
                 status="TAKEN", trade_index=0, detection_source="engine"),
        ],
    )

    d = date(2026, 7, 30)         # ---- amended check-in
    days[d] = dict(
        label="amended_checkin",
        pre=pre(sleep_hours=7.5, energy=4, focus=4, stress=2, desire_to_trade=3),
        pre_amendment=dict(data=dict(sleep_hours=5.5, energy=2, stress=4),
                           reason="Corrected in the evening: I had misread the sleep tracker "
                                  "and answered before coffee."),
        post=post(well_traded="mixed", patience=3),
        trades=[
            trade("10AM_MODEL", "short", f"{d}T10:06", f"{d}T10:52", 5662.25, 5654.00,
                  5666.75, 5653.00, exit_reason="target", reported_mfe_price=5653.50,
                  reported_mae_price=5664.00),
        ],
        opportunities=[
            dict(strategy_id="10AM_MODEL", symbol=SYMBOL, qualified_at=f"{d}T10:04",
                 direction="short", status="TAKEN", trade_index=0, detection_source="engine"),
        ],
    )

    d = date(2026, 7, 29)         # ---- rule violation + manual entry provenance
    days[d] = dict(
        label="rule_violation_manual",
        pre=pre(sleep_hours=6.4, energy=3, focus=3, stress=3, desire_to_trade=4),
        post=post(execution_quality=2, rule_adherence=2, patience=2, emotional_control=3,
                  stop_moved=1, manual_override=1, well_traded="no",
                  biggest_mistake="Widened the stop rather than accepting the loss."),
        trades=[
            trade("DORB", "long", f"{d}T09:55", f"{d}T10:48", 5651.00, 5644.00, 5646.50,
                  5661.00, exit_reason="stop", tags=["STOP_MOVED", "RULE_OVERRIDE"],
                  was_manual_override=True, reported_mae_price=5643.50,
                  reported_mfe_price=5653.00,
                  override_note="Moved the stop from 5646.50 to 5643.00 to avoid being "
                                "stopped. It went further.",
                  execution_note="Keyed by hand from the platform screen after the fact."),
        ],
        opportunities=[
            dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T09:53", direction="long",
                 status="TAKEN", trade_index=0, detection_source="human_logged"),
        ],
    )

    # ---- strategy-version transition: DORB 0.9 trades before 2026-06-15
    for i, day in enumerate((date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12))):
        px = 5600 + i * 6
        days[day] = dict(
            label=f"dorb_v09_{i}",
            pre=pre(sleep_hours=7.0 + i * 0.2, energy=4, focus=4, stress=2),
            post=post(well_traded="yes"),
            trades=[
                trade("DORB", "long" if i % 2 == 0 else "short",
                      f"{day}T10:0{i}", f"{day}T10:4{i}", px, px + (5 if i % 2 == 0 else -4),
                      px - 4.5, px + 9, exit_reason="target" if i % 2 == 0 else "manual",
                      reported_mfe_price=px + 6, reported_mae_price=px - 2),
            ],
            opportunities=[
                dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{day}T10:00",
                     direction="long" if i % 2 == 0 else "short", status="TAKEN",
                     trade_index=0, detection_source="engine"),
            ],
        )

    # ---- filler days, so the calendar and curves have texture
    filler_start = date(2026, 7, 1)
    d = filler_start
    while d <= date(2026, 7, 28):
        if d.weekday() < 5 and d not in days:
            n = rng.choice([0, 1, 1, 2])
            trades, opps = [], []
            base = 5600 + rng.uniform(-30, 60)
            for k in range(n):
                win = rng.random() < 0.42
                side = rng.choice(["long", "short"])
                sign = 1 if side == "long" else -1
                risk = rng.choice([4.0, 4.5, 5.0])
                entry = round(base * 4) / 4
                r = round(rng.uniform(0.9, 2.0), 2) if win else round(rng.uniform(-1.0, -0.6), 2)
                exit_px = round((entry + sign * risk * r) * 4) / 4
                hh, mm = 10 + k, rng.choice([5, 18, 34, 47])
                strategy = rng.choice(["10AM_MODEL", "DORB", "DISCRETIONARY_X"])
                trades.append(trade(
                    strategy, side, f"{d}T{hh:02d}:{mm:02d}", f"{d}T{hh + 1:02d}:{mm:02d}",
                    entry, exit_px, entry - sign * risk, entry + sign * risk * 2,
                    exit_reason="target" if r > 1 else "stop",
                    reported_mfe_price=entry + sign * risk * max(r, 0.3),
                    reported_mae_price=entry - sign * risk * rng.uniform(0.2, 0.7),
                    tags=(["LATE_ENTRY"] if (not win and rng.random() < 0.3) else [])))
                opps.append(dict(strategy_id=strategy, symbol=SYMBOL,
                                 qualified_at=f"{d}T{hh:02d}:{max(0, mm - 2):02d}",
                                 direction=side, status="TAKEN", trade_index=k,
                                 detection_source="engine"))
            if rng.random() < 0.3:
                opps.append(dict(strategy_id="DORB", symbol=SYMBOL, qualified_at=f"{d}T13:20",
                                 direction=rng.choice(["long", "short"]), status="MISSED",
                                 detection_source="engine",
                                 status_reason="Alert fired with nobody at the desk."))
            # Fill times and optional-section usage vary, so friction telemetry
            # has something to report before real sessions exist.
            days[d] = dict(label="filler", pre=pre(sleep_hours=round(rng.uniform(5.5, 8.2), 1),
                                                   energy=rng.randint(2, 5),
                                                   focus=rng.randint(2, 5),
                                                   stress=rng.randint(1, 4),
                                                   desire_to_trade=rng.randint(2, 5),
                                                   fill_seconds=rng.randint(31, 78),
                                                   optional_opened=1 if rng.random() < 0.25 else 0),
                           post=post(execution_quality=rng.randint(2, 5),
                                     rule_adherence=rng.randint(3, 5),
                                     patience=rng.randint(2, 5),
                                     emotional_control=rng.randint(3, 5),
                                     well_traded=rng.choice(["yes", "yes", "mixed"]),
                                     fill_seconds=rng.randint(62, 155),
                                     optional_opened=1 if rng.random() < 0.4 else 0),
                           trades=trades, opportunities=opps)
        d += timedelta(days=1)

    return dict(sorted(days.items()))


def seed(conn) -> dict:
    account_id = repo.ensure_account(conn, ACCOUNT, broker="demo-broker", mode="sim_eval")
    repo.ensure_instrument(conn, SYMBOL, name="Micro E-mini S&P 500", exchange="CME")

    for code, label, category, description in MISTAKE_TAXONOMY:
        conn.execute(
            "INSERT OR IGNORE INTO mistake_tag(code,label,category,description,sort_order)"
            " VALUES (?,?,?,?,?)",
            (code, label, category, description,
             [m[0] for m in MISTAKE_TAXONOMY].index(code)))

    for sid, name, family, description in STRATEGIES:
        repo.ensure_strategy(conn, sid, name, family, description)

    # DORB v0.9 first, then v1.0 — a real version transition, so trades before
    # 2026-06-15 stay attached to the rules that were actually in force.
    repo.publish_strategy_version(
        conn, "DORB", "0.9", {"entry": "opening range break", "stop": "range low"},
        automation_level="bot", qualification_status="retired", active_from="2026-04-01",
        reference_impl="engines.dorb.v0_9:evaluate")
    repo.publish_strategy_version(
        conn, "DORB", "1.0",
        {"entry": "opening range break with retest", "stop": "session range normalised"},
        automation_level="bot", qualification_status="forward_test", active_from="2026-06-15",
        reference_impl="engines.dorb.v1_0:evaluate")
    repo.publish_strategy_version(
        conn, "10AM_MODEL", "1.0", {"entry": "10:00 reference continuation"},
        automation_level="semi_auto", qualification_status="qualified", active_from="2026-05-01",
        reference_impl="engines.tenam.v1_0:evaluate")
    repo.publish_strategy_version(
        conn, "DISCRETIONARY_X", "1.0", {"entry": "unstructured"},
        automation_level="manual", qualification_status="research", active_from="2026-01-01")

    counts = {"sessions": 0, "trades": 0, "opportunities": 0, "voice": 0,
              "amendments": 0, "blinded_features": 0}
    blinded_batch = []

    for day, spec in build_days().items():
        session_id = repo.get_or_create_session(
            conn, day.isoformat(), account_id=account_id, session_kind="NY_AM",
            mode="sim_eval", planned_max_risk_r=2.0, planned_max_loss=500.0,
            daily_loss_limit=750.0, max_trades_planned=3)
        counts["sessions"] += 1

        if spec.get("pre"):
            repo.save_checkin_pre(conn, session_id, dict(
                spec["pre"], submitted_at=repo.local_to_utc(f"{day}T09:12", config.DEFAULT_TZ)))
        if spec.get("pre_amendment"):
            repo.save_checkin_pre(conn, session_id, spec["pre_amendment"]["data"],
                                  spec["pre_amendment"]["reason"])
            counts["amendments"] += 1

        trade_ids = []
        for payload in spec.get("trades", []):
            entry_source = "manual" if spec.get("label") == "rule_violation_manual" else "file_import"
            trade_ids.append(repo.record_trade(conn, session_id, payload,
                                               entry_source=entry_source))
            counts["trades"] += 1

        for opp in spec.get("opportunities", []):
            payload = dict(opp)
            index = payload.pop("trade_index", None)
            detection = payload.pop("detection_source", "human_logged")
            if index is not None:
                payload["trade_id"] = trade_ids[index]
            opp_id = repo.record_opportunity(conn, session_id, payload,
                                             detection_source=detection)
            if index is not None:
                conn.execute("UPDATE trade SET opportunity_id=? WHERE id=?",
                             (opp_id, trade_ids[index]))
            counts["opportunities"] += 1

        if spec.get("post"):
            repo.save_checkin_post(conn, session_id, dict(
                spec["post"], submitted_at=repo.local_to_utc(f"{day}T16:05", config.DEFAULT_TZ)))

        if spec.get("voice"):
            v = spec["voice"]
            repo.add_voice_note(
                conn, session_id=session_id,
                audio_path=f"media/{day}/checkout.m4a",
                audio_sha256=hashlib.sha256(f"audio{day}".encode()).hexdigest(),
                duration_seconds=v["seconds"],
                recorded_at=repo.local_to_utc(v["at"], config.DEFAULT_TZ),
                transcript=v["transcript"], transcript_engine="whisper-large-v3",
                transcript_version="3.0")
            counts["voice"] += 1

        for s in spec.get("suggestions", []):
            annotation_id = repo.suggest_tag(
                conn, target_type="trade", target_id=trade_ids[s["trade_index"]],
                code=s["code"], rationale=s["rationale"], confidence=s["confidence"],
                model="journal-tagger", model_version="0.2.0")
            if s["accept"]:
                repo.accept_suggestion(conn, annotation_id)
            else:
                repo.reject_suggestion(conn, annotation_id)

        # Blinded research features are stored from day one and read by nothing.
        # Collected here, written after the main transaction commits: the daily
        # connection is not permitted to touch the blinded layer at all, and two
        # writers cannot hold the database at once.
        uid = conn.execute("SELECT session_uid FROM session WHERE id=?",
                           (session_id,)).fetchone()["session_uid"]
        blinded_batch.append((uid, "personal_astro",
                              {f"pa_f{k:02d}": round(rng.uniform(0, 1), 4) for k in (1, 7, 17)}))
        blinded_batch.append((uid, "lunar", {"ln_f01": round(rng.uniform(0, 1), 4)}))

    conn.commit()

    blinded = db.connect(restricted=False)
    try:
        for uid, feature_set, features in blinded_batch:
            counts["blinded_features"] += repo.store_blinded_features(
                blinded, "session", uid, feature_set, features,
                engine="astro-engine-placeholder", engine_version="astro@0.0.0")
        blinded.commit()
    finally:
        blinded.close()

    return counts


def main(reset: bool = False) -> int:
    config.ensure_dirs()
    if reset and config.DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(config.DB_PATH) + suffix)
            if p.exists():
                p.unlink()

    setup = db.connect(restricted=False)
    try:
        db.migrate(setup)
        setup.commit()
    finally:
        setup.close()

    conn = db.connect()
    try:
        counts = seed(conn)
        derived = metrics.recompute_all(conn, note="seed")
        integrity = db.integrity_check(conn)
        exported = backup.export_all(conn)
    finally:
        conn.close()

    print(f"database   {config.DB_PATH}")
    for key, value in counts.items():
        print(f"{key:<11}{value}")
    print(f"derived    {derived['trade_metrics']} trade rows, "
          f"{derived['session_metrics']} session rows ({derived['calc_version']})")
    print(f"exports    {exported['directory']}")
    print(f"integrity  {'ok' if integrity['ok'] else integrity['problems']}")
    return 0 if integrity["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    raise SystemExit(main(**vars(parser.parse_args())))
