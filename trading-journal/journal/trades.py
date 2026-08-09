"""Execution events into logical trades.

This is the hardest correctness problem in the journal, so it is explicit
rather than clever. Machines see a stream of fills across ten accounts. What
matters is the decision behind them, and reconstructing that is a judgement:
every logical trade records the rule that produced it and how confident that
rule was, and anything genuinely ambiguous is flagged rather than guessed.

Three rules govern everything here.

  1. **Scaling stays inside one trade.** Adds and reductions do not start new
     trades; returning to flat does.
  2. **A copier is not a research sample.** Ten accounts executing one decision
     are one observation. Statistics count logical trades; `normalized_pnl`
     exists so account count never inflates an edge.
  3. **Nothing is manufactured.** No stop means no R, not an invented one.
     A follower that did not fill is a missing fill, not a zero.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import config
from .db import sha256_text, utcnow

CALC_VERSION = "logical@1.0.0"

# Two fills on the same account, same instrument, same direction, separated by
# more than this with a flat position between them are different decisions.
# Below it, a re-entry is more likely a continuation of the same idea and gets
# flagged for review rather than silently split or silently merged.
REENTRY_AMBIGUITY_SECONDS = 120

# How far outside the lead's own window a follower fill may fall and still be
# recognised as a copy of it. A copier lags; it does not lead.
COPY_LEAD_MARGIN_SECONDS = 30
COPY_TAIL_MARGIN_SECONDS = 120

OPENING = ("OPEN", "ADD")
CLOSING = ("REDUCE", "CLOSE")
POSITION_EVENTS = OPENING + CLOSING
NON_POSITION = ("STOP_CHANGE", "TARGET_CHANGE", "CANCEL", "REJECT")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def fingerprint(account_id: int, instrument_id: int, occurred_at: str,
                event_type: str, quantity, price, fill_external_id=None) -> str:
    """Stable identity for an execution event.

    The venue's own fill id is used when there is one, because it is the only
    truly stable identifier. Otherwise a deterministic fingerprint of the
    event's content — which makes re-importing the same export idempotent, and
    makes a genuinely different fill (different time, size or price) distinct.
    """
    if fill_external_id:
        return f"fill:{fill_external_id}"
    return "fp:" + sha256_text(
        f"{account_id}|{instrument_id}|{occurred_at}|{event_type}|{quantity}|{price}")[:32]


# --------------------------------------------------------------------- ingestion
def record_event(conn, *, account_id: int, instrument_id: int, event_type: str,
                 occurred_at: str, quantity=None, price=None, side=None,
                 stop_price=None, target_price=None, order_external_id=None,
                 fill_external_id=None, source: str = "manual",
                 raw_record_id=None, is_demo: bool = False) -> Optional[int]:
    """Append one execution event. Returns None if it is already known.

    Idempotency lives here rather than in each adapter, so every import path
    gets it for free.
    """
    fp = fingerprint(account_id, instrument_id, occurred_at, event_type,
                     quantity, price, fill_external_id)
    existing = conn.execute(
        "SELECT id FROM execution_event WHERE fingerprint=?", (fp,)).fetchone()
    if existing:
        return None

    snapshot = conn.execute(
        "SELECT id FROM account_snapshot WHERE account_id=? AND effective_from<=? "
        "AND (effective_to IS NULL OR effective_to>=?) ORDER BY effective_from DESC LIMIT 1",
        (account_id, occurred_at, occurred_at)).fetchone()

    cur = conn.execute(
        "INSERT INTO execution_event(account_id,account_snapshot_id,instrument_id,event_type,"
        "occurred_at,side,quantity,price,stop_price,target_price,order_external_id,"
        "fill_external_id,source,raw_record_id,fingerprint,imported_at,is_demo)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, snapshot["id"] if snapshot else None, instrument_id, event_type,
         occurred_at, side, quantity, price, stop_price, target_price, order_external_id,
         fill_external_id, source, raw_record_id, fp, utcnow(), 1 if is_demo else 0))
    return cur.lastrowid


# --------------------------------------------------------------------- grouping
def _lead_account_id(conn, is_demo: bool) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM account WHERE role='LEAD' AND is_demo=? ORDER BY id LIMIT 1",
        (1 if is_demo else 0,)).fetchone()
    return row["id"] if row else None


def group_events(conn, trading_day_id: int) -> dict:
    """Assign every ungrouped event on a day to a logical trade.

    Grouping is driven by the LEAD account, because the lead is where the
    decision was made. Follower events attach to whichever lead trade was open
    on the same instrument and direction when they occurred — a follower is a
    copy of a decision, never a decision of its own.
    """
    day = conn.execute("SELECT * FROM trading_day WHERE id=?", (trading_day_id,)).fetchone()
    if not day:
        raise ValueError(f"no trading day {trading_day_id}")
    is_demo = bool(day["is_demo"])

    events = conn.execute(
        "SELECT e.* FROM execution_event e "
        "WHERE e.logical_trade_id IS NULL AND e.is_demo=? "
        "  AND date(e.occurred_at) BETWEEN date(?, '-1 day') AND date(?, '+1 day') "
        "ORDER BY e.occurred_at, e.id",
        (1 if is_demo else 0, day["day_date"], day["day_date"])).fetchall()
    if not events:
        return {"trading_day_id": trading_day_id, "logical_trades": 0, "events_grouped": 0}

    lead_id = _lead_account_id(conn, is_demo)
    lead_events = [e for e in events if e["account_id"] == lead_id]
    follower_events = [e for e in events if e["account_id"] != lead_id]

    # Without a designated lead there is nothing to copy from, so every account
    # is treated as its own decision stream.
    if lead_id is None:
        lead_events, follower_events = events, []

    created: List[int] = []
    grouped = 0
    open_trades: List[dict] = []   # {id, instrument_id, direction, opened_at, closed_at}

    position = 0.0
    current: Optional[dict] = None
    last_flat_at: Optional[str] = None

    for event in lead_events:
        if event["event_type"] in NON_POSITION:
            # Stop and target changes belong to whatever position is open.
            if current:
                conn.execute("UPDATE execution_event SET logical_trade_id=? WHERE id=?",
                             (current["id"], event["id"]))
                grouped += 1
            continue

        signed = (event["quantity"] or 0) * (1 if event["event_type"] in OPENING else -1)

        if current is None:
            direction = "LONG" if event["side"] == "BUY" else "SHORT"
            confidence, note = "high", None
            if last_flat_at:
                gap = (_parse(event["occurred_at"]) - _parse(last_flat_at)).total_seconds()
                if gap < REENTRY_AMBIGUITY_SECONDS:
                    # Flat and straight back in. This may be one idea being
                    # re-established or two separate decisions; the evidence
                    # does not say, so it is flagged rather than assumed.
                    confidence = "low"
                    note = (f"re-entered {int(gap)}s after going flat — may be a "
                            "continuation of the previous trade rather than a new one")
            current = _create_logical_trade(
                conn, trading_day_id, event, direction, lead_id, confidence, note, is_demo)
            created.append(current["id"])
            position = 0.0

        conn.execute("UPDATE execution_event SET logical_trade_id=? WHERE id=?",
                     (current["id"], event["id"]))
        grouped += 1
        position += signed
        conn.execute("UPDATE execution_event SET position_after=? WHERE id=?",
                     (round(position, 6), event["id"]))

        if abs(position) < 1e-9:
            conn.execute(
                "UPDATE logical_trade SET closed_at=?, status=CASE WHEN status="
                "'NEEDS_GROUPING_REVIEW' THEN status ELSE 'CLOSED' END, updated_at=? WHERE id=?",
                (event["occurred_at"], utcnow(), current["id"]))
            current["closed_at"] = event["occurred_at"]
            open_trades.append(current)
            last_flat_at = event["occurred_at"]
            current = None

    if current:
        open_trades.append(current)

    # Follower fills must be able to attach to lead trades that already exist,
    # not only to ones created in this pass. The real sequence is exactly that:
    # the lead export arrives and is grouped, and the follower exports arrive
    # afterwards — sometimes days later, from a different source. Considering
    # only this run's trades left every late follower fill orphaned, and copy
    # quality then reported nothing at all while looking perfectly healthy.
    known = {t["id"] for t in open_trades}
    for row in conn.execute(
            "SELECT id, instrument_id, direction, opened_at, closed_at FROM logical_trade "
            "WHERE trading_day_id=? AND is_demo=? ORDER BY opened_at",
            (trading_day_id, 1 if is_demo else 0)):
        if row["id"] not in known:
            open_trades.append(dict(row))

    attached, touched = _attach_followers(conn, follower_events, open_trades)
    grouped += attached

    # Rebuild trades created here AND any that gained follower events, since a
    # newly attached follower changes that trade's copy quality and rollups.
    for trade_id in dict.fromkeys(created + touched):
        rebuild_trade(conn, trade_id)

    _refresh_day_status(conn, trading_day_id)
    conn.commit()
    return {"trading_day_id": trading_day_id, "logical_trades": len(created),
            "events_grouped": grouped,
            "needs_review": sum(1 for t in created if conn.execute(
                "SELECT status FROM logical_trade WHERE id=?", (t,)
            ).fetchone()["status"] == "NEEDS_GROUPING_REVIEW")}


def _create_logical_trade(conn, trading_day_id, event, direction, lead_id,
                          confidence, note, is_demo) -> dict:
    uid = sha256_text(f"{trading_day_id}|{event['instrument_id']}|{event['occurred_at']}"
                      f"|{direction}")[:16]
    now = utcnow()
    status = "NEEDS_GROUPING_REVIEW" if confidence == "low" else "OPEN"
    cur = conn.execute(
        "INSERT INTO logical_trade(logical_trade_uid,trading_day_id,instrument_id,direction,"
        "lead_account_id,opened_at,status,grouping_rule,grouping_confidence,grouping_note,"
        "review_state,created_at,updated_at,is_demo)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'NEEDS_REVIEW',?,?,?)",
        (uid, trading_day_id, event["instrument_id"], direction, lead_id,
         event["occurred_at"], status,
         "lead_account_flat_to_flat", confidence, note, now, now, 1 if is_demo else 0))
    return {"id": cur.lastrowid, "instrument_id": event["instrument_id"],
            "direction": direction, "opened_at": event["occurred_at"], "closed_at": None}


def _attach_followers(conn, follower_events, open_trades):
    """Attach follower fills to the lead trade they were copying.

    A follower event belongs to the lead trade on the same instrument and
    direction whose window contains it. Anything that matches nothing is left
    unattached and surfaces as an orphan rather than inventing a trade.

    Returns (count attached, ids of trades that gained events) so the caller can
    rebuild exactly the trades whose copy quality changed.
    """
    attached = 0
    touched: List[int] = []
    for event in follower_events:
        match = None
        for trade in open_trades:
            if trade["instrument_id"] != event["instrument_id"]:
                continue
            expected_side = "BUY" if trade["direction"] == "LONG" else "SELL"
            opening = event["event_type"] in OPENING
            if event["side"] and ((event["side"] == expected_side) != opening):
                continue
            start = _parse(trade["opened_at"])
            end = _parse(trade["closed_at"]) if trade["closed_at"] else None
            when = _parse(event["occurred_at"])
            # A copier lags the lead by milliseconds to seconds, and a follower
            # exit can land after the lead is already flat. Allow a small margin
            # either side of the lead's own window, and no more.
            if (when - start).total_seconds() < -COPY_LEAD_MARGIN_SECONDS:
                continue
            if end and (when - end).total_seconds() > COPY_TAIL_MARGIN_SECONDS:
                continue
            match = trade
            break
        if match:
            conn.execute("UPDATE execution_event SET logical_trade_id=? WHERE id=?",
                         (match["id"], event["id"]))
            attached += 1
            if match["id"] not in touched:
                touched.append(match["id"])
    return attached, touched


def orphan_events(conn, is_demo: bool = False) -> List[dict]:
    """Events no logical trade claimed. Visible, never silently dropped."""
    return [dict(r) for r in conn.execute(
        "SELECT e.*, a.label AS account_label FROM execution_event e "
        "JOIN account a ON a.id = e.account_id "
        "WHERE e.logical_trade_id IS NULL AND e.is_demo=? ORDER BY e.occurred_at",
        (1 if is_demo else 0,))]


# --------------------------------------------------------------------- rebuild
def _position_series(events) -> List[Tuple[str, float, float, str]]:
    """(timestamp, signed_delta, running_position, event_type) for one account."""
    series, position = [], 0.0
    for e in events:
        if e["event_type"] in NON_POSITION:
            continue
        signed = (e["quantity"] or 0) * (1 if e["event_type"] in OPENING else -1)
        position += signed
        series.append((e["occurred_at"], signed, round(position, 6), e["event_type"]))
    return series


def _account_rollup(conn, trade, account_id, events, point_value) -> dict:
    opens = [e for e in events if e["event_type"] in OPENING and e["quantity"]]
    closes = [e for e in events if e["event_type"] in CLOSING and e["quantity"]]

    open_qty = sum(e["quantity"] for e in opens)
    close_qty = sum(e["quantity"] for e in closes)
    avg_entry = (sum(e["price"] * e["quantity"] for e in opens) / open_qty) if open_qty else None
    avg_exit = (sum(e["price"] * e["quantity"] for e in closes) / close_qty) if close_qty else None

    direction = 1 if trade["direction"] == "LONG" else -1
    realized = None
    if avg_entry is not None and close_qty:
        realized = round(
            sum((e["price"] - avg_entry) * direction * e["quantity"] for e in closes)
            * point_value, 2)

    series = _position_series(events)
    max_position = max((abs(p) for _, _, p, _ in series), default=0.0)

    return {
        "first_event_at": events[0]["occurred_at"] if events else None,
        "last_event_at": events[-1]["occurred_at"] if events else None,
        "event_count": len(events),
        "adds": sum(1 for e in events if e["event_type"] == "ADD"),
        "reductions": sum(1 for e in events if e["event_type"] == "REDUCE"),
        "max_position": max_position,
        "contracts_traded": open_qty + close_qty,
        "avg_entry_price": round(avg_entry, 4) if avg_entry is not None else None,
        "avg_exit_price": round(avg_exit, 4) if avg_exit is not None else None,
        "realized_pnl": realized,
        "opens": opens,
        "closes": closes,
    }


def _discrepancies(lead: dict, follower: dict, expected_multiplier: float) -> List[dict]:
    """What the copier failed to reproduce.

    Reported as findings, never repaired. A follower that missed an add did
    miss it, and the record should say so.
    """
    found = []
    lead_events = len(lead["opens"]) + len(lead["closes"])
    follower_events = len(follower["opens"]) + len(follower["closes"])

    if follower_events < lead_events:
        found.append({
            "kind": "missed_events",
            "detail": f"{lead_events - follower_events} of {lead_events} lead executions "
                      "have no matching fill on this account",
        })
    if len(follower["opens"]) < len(lead["opens"]):
        found.append({"kind": "missed_add",
                      "detail": f"lead opened/added {len(lead['opens'])} times, "
                                f"this account {len(follower['opens'])}"})
    if len(follower["closes"]) < len(lead["closes"]):
        found.append({"kind": "missed_reduction",
                      "detail": f"lead reduced/closed {len(lead['closes'])} times, "
                                f"this account {len(follower['closes'])}"})

    if lead["max_position"] and follower["max_position"]:
        expected = lead["max_position"] * expected_multiplier
        if expected and abs(follower["max_position"] - expected) / expected > 0.01:
            found.append({
                "kind": "size_mismatch",
                "detail": f"peak position {follower['max_position']:g} against "
                          f"{expected:g} expected at {expected_multiplier:g}x",
            })
    return found


def rebuild_trade(conn, logical_trade_id: int) -> dict:
    """Recompute everything derived for one logical trade."""
    trade = conn.execute("SELECT * FROM logical_trade WHERE id=?",
                         (logical_trade_id,)).fetchone()
    if not trade:
        raise ValueError(f"no logical trade {logical_trade_id}")

    instrument = conn.execute("SELECT * FROM instrument WHERE id=?",
                              (trade["instrument_id"],)).fetchone()
    point_value = instrument["point_value"]

    events = conn.execute(
        "SELECT * FROM execution_event WHERE logical_trade_id=? ORDER BY occurred_at, id",
        (logical_trade_id,)).fetchall()

    by_account: Dict[int, list] = {}
    for e in events:
        by_account.setdefault(e["account_id"], []).append(e)

    lead_id = trade["lead_account_id"]
    if lead_id not in by_account and by_account:
        lead_id = sorted(by_account)[0]

    rollups = {aid: _account_rollup(conn, trade, aid, evts, point_value)
               for aid, evts in by_account.items()}
    lead = rollups.get(lead_id)

    conn.execute("DELETE FROM account_execution WHERE logical_trade_id=?", (logical_trade_id,))
    for account_id, rollup in rollups.items():
        account = conn.execute("SELECT * FROM account WHERE id=?", (account_id,)).fetchone()
        is_lead = account_id == lead_id
        discrepancies, slippage, ratio, missed = [], None, None, 0

        if not is_lead and lead:
            discrepancies = _discrepancies(lead, rollup, account["size_multiplier"])
            missed = sum(1 for d in discrepancies if d["kind"] == "missed_events")
            if lead["avg_entry_price"] and rollup["avg_entry_price"]:
                direction = 1 if trade["direction"] == "LONG" else -1
                # Positive means the follower got a worse price than the lead.
                slippage = round(
                    (rollup["avg_entry_price"] - lead["avg_entry_price"]) * direction, 4)
            if lead["max_position"] and account["size_multiplier"]:
                expected = lead["max_position"] * account["size_multiplier"]
                ratio = round(rollup["max_position"] / expected, 4) if expected else None

        conn.execute(
            "INSERT INTO account_execution(logical_trade_id,account_id,role_at_time,"
            "first_event_at,last_event_at,event_count,adds,reductions,max_position,"
            "contracts_traded,avg_entry_price,avg_exit_price,realized_pnl,fees,"
            "entry_slippage_points,quantity_ratio,missed_events,discrepancies,calc_version,"
            "computed_at,is_demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (logical_trade_id, account_id, "LEAD" if is_lead else account["role"],
             rollup["first_event_at"], rollup["last_event_at"], rollup["event_count"],
             rollup["adds"], rollup["reductions"], rollup["max_position"],
             rollup["contracts_traded"], rollup["avg_entry_price"], rollup["avg_exit_price"],
             rollup["realized_pnl"], None, slippage, ratio, missed,
             json.dumps(discrepancies), CALC_VERSION, utcnow(), trade["is_demo"]))

    _rebuild_metrics(conn, trade, lead, rollups, events, point_value)
    conn.commit()
    return {"logical_trade_id": logical_trade_id, "accounts": len(rollups),
            "events": len(events)}


def _rebuild_metrics(conn, trade, lead, rollups, events, point_value) -> None:
    position_events = [e for e in events
                       if e["account_id"] == trade["lead_account_id"]
                       and e["event_type"] in POSITION_EVENTS]
    if not position_events and events:
        position_events = [e for e in events if e["event_type"] in POSITION_EVENTS]

    series = _position_series(position_events)
    max_position = max((abs(p) for _, _, p, _ in series), default=0.0)
    seconds_to_max = None
    if series and max_position:
        opened = _parse(series[0][0])
        for ts, _, pos, _ in series:
            if abs(abs(pos) - max_position) < 1e-9:
                seconds_to_max = int((_parse(ts) - opened).total_seconds())
                break

    duration = None
    if trade["closed_at"]:
        duration = int((_parse(trade["closed_at"]) - _parse(trade["opened_at"])).total_seconds())

    # R only when a real initial stop was recorded before or at the open.
    initial_stop = None
    stops = [e for e in events if e["event_type"] == "STOP_CHANGE" and e["stop_price"]]
    first_open = next((e for e in position_events if e["event_type"] == "OPEN"), None)
    if first_open and first_open["stop_price"]:
        initial_stop = first_open["stop_price"]
    elif stops:
        earliest = min(stops, key=lambda e: e["occurred_at"])
        if first_open and earliest["occurred_at"] <= first_open["occurred_at"]:
            initial_stop = earliest["stop_price"]

    initial_entry = first_open["price"] if first_open else None
    risk_points = (abs(initial_entry - initial_stop)
                   if initial_stop and initial_entry else None)

    lead_pnl = lead["realized_pnl"] if lead else None
    total_pnl = sum(r["realized_pnl"] or 0 for r in rollups.values()) if rollups else None
    r_multiple, r_status = None, "UNKNOWN"
    if risk_points and lead and lead["avg_entry_price"] and lead["avg_exit_price"]:
        direction = 1 if trade["direction"] == "LONG" else -1
        r_multiple = round(
            (lead["avg_exit_price"] - lead["avg_entry_price"]) * direction / risk_points, 3)
        r_status = "COMPUTED"

    expected_accounts = conn.execute(
        "SELECT COUNT(*) c FROM account WHERE status='ACTIVE' AND is_demo=? "
        "AND role IN ('LEAD','FOLLOWER')", (trade["is_demo"],)).fetchone()["c"]

    conn.execute("DELETE FROM logical_trade_metrics WHERE logical_trade_id=?", (trade["id"],))
    conn.execute(
        "INSERT INTO logical_trade_metrics(logical_trade_id,initial_entry_price,"
        "avg_entry_price,avg_exit_price,max_position,contracts_traded,adds,reductions,"
        "duration_seconds,seconds_to_max_size,initial_stop,initial_risk_points,r_multiple,"
        "r_status,lead_pnl,total_pnl,normalized_pnl,fees,accounts_participating,"
        "accounts_expected,copy_complete,calc_version,computed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade["id"], initial_entry,
         lead["avg_entry_price"] if lead else None,
         lead["avg_exit_price"] if lead else None,
         max_position,
         lead["contracts_traded"] if lead else None,
         lead["adds"] if lead else 0,
         lead["reductions"] if lead else 0,
         duration, seconds_to_max, initial_stop, risk_points, r_multiple, r_status,
         lead_pnl, round(total_pnl, 2) if total_pnl is not None else None,
         # Normalised P&L is the lead's result. Ten copies of one decision are
         # one decision, and this is the number behavioural statistics use.
         lead_pnl, None,
         len(rollups), expected_accounts,
         1 if expected_accounts and len(rollups) >= expected_accounts else 0,
         CALC_VERSION, utcnow()))


def rebuild_day(conn, trading_day_id: int) -> dict:
    trades = conn.execute("SELECT id FROM logical_trade WHERE trading_day_id=?",
                          (trading_day_id,)).fetchall()
    for row in trades:
        rebuild_trade(conn, row["id"])
    _refresh_day_status(conn, trading_day_id)
    conn.commit()
    return {"trading_day_id": trading_day_id, "trades": len(trades)}


def _refresh_day_status(conn, trading_day_id: int) -> None:
    counts = conn.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN review_state='NEEDS_REVIEW' THEN 1 ELSE 0 END) "
        "pending FROM logical_trade WHERE trading_day_id=?", (trading_day_id,)).fetchone()
    has_bias = conn.execute("SELECT COUNT(*) c FROM daily_bias WHERE trading_day_id=?",
                            (trading_day_id,)).fetchone()["c"]
    if not counts["total"]:
        status = "NO_TRADE" if has_bias else "BIAS_MISSING"
    elif counts["pending"]:
        status = "REVIEW_PENDING"
    else:
        status = "DAY_COMPLETE"
    conn.execute("UPDATE trading_day SET status=?, updated_at=? WHERE id=?",
                 (status, utcnow(), trading_day_id))


# --------------------------------------------------------------------- review
def position_timeline(conn, logical_trade_id: int) -> List[dict]:
    """The lead account's position over time, for the lifecycle visual."""
    trade = conn.execute("SELECT * FROM logical_trade WHERE id=?",
                         (logical_trade_id,)).fetchone()
    events = conn.execute(
        "SELECT * FROM execution_event WHERE logical_trade_id=? AND account_id=? "
        "ORDER BY occurred_at, id", (logical_trade_id, trade["lead_account_id"])).fetchall()
    if not events:
        events = conn.execute(
            "SELECT * FROM execution_event WHERE logical_trade_id=? ORDER BY occurred_at, id",
            (logical_trade_id,)).fetchall()

    out, position = [], 0.0
    for e in events:
        if e["event_type"] in POSITION_EVENTS:
            position += (e["quantity"] or 0) * (1 if e["event_type"] in OPENING else -1)
        out.append({
            "at": e["occurred_at"],
            "event_type": e["event_type"],
            "quantity": e["quantity"],
            "price": e["price"],
            "stop_price": e["stop_price"],
            "target_price": e["target_price"],
            "position_after": round(position, 6) if e["event_type"] in POSITION_EVENTS else None,
        })
    return out


def submit_review(conn, logical_trade_id: int, payload: dict) -> dict:
    """Store the human context a machine cannot know."""
    now = utcnow()
    existing = conn.execute("SELECT logical_trade_id FROM trade_annotation "
                            "WHERE logical_trade_id=?", (logical_trade_id,)).fetchone()
    fields = ("setup_id", "why", "planning_mode", "conviction", "execution_grade",
              "process_grade", "setup_quality", "note")
    values = {k: payload.get(k) for k in fields if k in payload}

    if existing:
        if values:
            sets = ", ".join(f"{k}=?" for k in values)
            conn.execute(f"UPDATE trade_annotation SET {sets}, reviewed_at=?, updated_at=? "
                         "WHERE logical_trade_id=?",
                         list(values.values()) + [now, now, logical_trade_id])
    else:
        cols = ["logical_trade_id", "reviewed_at", "review_seconds", "created_at", "updated_at"]
        vals = [logical_trade_id, now, payload.get("review_seconds"), now, now]
        cols += list(values)
        vals += list(values.values())
        conn.execute(f"INSERT INTO trade_annotation({','.join(cols)}) "
                     f"VALUES ({','.join('?' * len(cols))})", vals)

    for tag in payload.get("process_tags", []) or []:
        conn.execute("INSERT OR IGNORE INTO trade_process_tag(logical_trade_id,tag_code,"
                     "confirmed_at) VALUES (?,?,?)", (logical_trade_id, tag, now))
    for tag in payload.get("context_tags", []) or []:
        conn.execute("INSERT OR IGNORE INTO trade_context_tag(logical_trade_id,context_tag_id)"
                     " VALUES (?,?)", (logical_trade_id, tag))

    conn.execute("UPDATE logical_trade SET review_state='REVIEWED', updated_at=? WHERE id=?",
                 (now, logical_trade_id))
    day = conn.execute("SELECT trading_day_id FROM logical_trade WHERE id=?",
                       (logical_trade_id,)).fetchone()["trading_day_id"]
    _refresh_day_status(conn, day)
    conn.commit()
    return {"logical_trade_id": logical_trade_id, "review_state": "REVIEWED"}


def regroup(conn, logical_trade_id: int, action: str, note: str = "") -> dict:
    """Correct a grouping mistake.

    'split_at' and 'merge_into' would be the full vocabulary; the two supported
    here are the ones that resolve the flag honestly without inventing history.
    """
    if action == "confirm":
        conn.execute("UPDATE logical_trade SET status=CASE WHEN closed_at IS NULL THEN 'OPEN' "
                     "ELSE 'CLOSED' END, grouping_confidence='high', grouping_note=?, "
                     "updated_at=? WHERE id=?", (note or "confirmed by hand", utcnow(),
                                                 logical_trade_id))
    elif action == "flag":
        conn.execute("UPDATE logical_trade SET status='NEEDS_GROUPING_REVIEW', "
                     "grouping_confidence='low', grouping_note=?, updated_at=? WHERE id=?",
                     (note, utcnow(), logical_trade_id))
    else:
        raise ValueError(f"unknown grouping action '{action}'")
    conn.commit()
    return {"logical_trade_id": logical_trade_id, "action": action}


# --------------------------------------------------------------- manual capture
# The first real week runs before any execution import exists, so a scaled trade
# has to be enterable by hand. §17: manual input operates at the LOGICAL TRADE
# level, not ten account copies.
#
# What this deliberately does NOT do is fan the lead's legs out across the
# follower accounts by multiplier. It would be one line of code and it would be
# fabrication: a follower fill that was never observed is indistinguishable from
# one that was, once it is in the database, and the entire copy-quality feature
# exists to detect exactly the case where a follower did something different.
# Followers stay absent until real evidence arrives, and absence is recorded as
# absence.
LEG_ACTIONS = ("OPEN", "ADD", "REDUCE", "CLOSE")


def log_manual_trade(conn, *, day_date: str, symbol: str, legs: List[dict],
                     account_label: Optional[str] = None, tz: str = None,
                     stop_price=None, target_price=None, capture_seconds=None,
                     is_demo: bool = False) -> dict:
    """Record one decision's lifecycle on the lead account, then group it.

    `legs` is the sequence as it happened: OPEN, any ADDs, any REDUCEs, CLOSE.
    Each leg is {action, time ('HH:MM' local or a full timestamp), quantity,
    price}. The initial stop belongs on the OPEN leg — that is what makes R
    computable, and R stays UNKNOWN without it rather than being reconstructed.
    """
    from . import config, repo

    tz = tz or config.DEFAULT_TZ
    if not legs:
        raise ValueError("a trade needs at least one leg")

    for i, leg in enumerate(legs):
        if leg.get("action") not in LEG_ACTIONS:
            raise ValueError(f"leg {i + 1}: action must be one of {LEG_ACTIONS}")
        if not leg.get("quantity") or float(leg["quantity"]) <= 0:
            raise ValueError(f"leg {i + 1}: a quantity is required")
        if leg.get("price") in (None, ""):
            raise ValueError(f"leg {i + 1}: a fill price is required")
    if legs[0]["action"] != "OPEN":
        raise ValueError("the first leg must be the OPEN — the sequence is the evidence")

    account_id = repo.ensure_account(conn, account_label) if account_label \
        else _lead_account_id(conn, is_demo)
    if account_id is None:
        raise ValueError(
            "no lead account is configured. Set one with `journal init --account` and "
            "mark its role LEAD, so a manual trade is attributed to the account that "
            "made the decision.")
    instrument_id = repo.ensure_instrument(conn, symbol)

    day = conn.execute("SELECT id FROM trading_day WHERE day_date=? AND is_demo=?",
                       (day_date, 1 if is_demo else 0)).fetchone()
    if day:
        day_id = day["id"]
    else:
        now = utcnow()
        day_id = conn.execute(
            "INSERT INTO trading_day(day_date,tz,status,iso_week,iso_month,created_at,"
            "updated_at,is_demo) VALUES (?,?,'BIAS_MISSING',?,?,?,?,?)",
            (day_date, tz, repo.iso_week_of(day_date), day_date[:7], now, now,
             1 if is_demo else 0)).lastrowid

    def when(value: str) -> str:
        text = str(value).strip()
        if len(text) <= 5:                       # "10:04"
            text = f"{day_date}T{text}:00"
        elif "T" not in text:                    # "10:04:32"
            text = f"{day_date}T{text}"
        return repo.local_to_utc(text.replace("Z", ""), tz)

    # Direction comes from the OPEN and the rest follow it, so a REDUCE is
    # always the opposite side without anyone having to say so on a phone.
    long_side = str(legs[0].get("side", "BUY")).upper() in ("BUY", "LONG")
    written, duplicates = 0, 0

    for index, leg in enumerate(legs):
        adding = leg["action"] in ("OPEN", "ADD")
        side = ("BUY" if long_side else "SELL") if adding else \
               ("SELL" if long_side else "BUY")
        event_id = record_event(
            conn, account_id=account_id, instrument_id=instrument_id,
            event_type=leg["action"], occurred_at=when(leg.get("time") or "09:30"),
            quantity=abs(float(leg["quantity"])), price=float(leg["price"]),
            side=side,
            stop_price=(leg.get("stop_price") or (stop_price if index == 0 else None)),
            target_price=(leg.get("target_price") or (target_price if index == 0 else None)),
            source="manual", is_demo=is_demo)
        if event_id is None:
            duplicates += 1
        else:
            written += 1

    conn.commit()
    grouped = group_events(conn, day_id)

    trade = conn.execute(
        "SELECT id, status, grouping_confidence FROM logical_trade "
        "WHERE trading_day_id=? ORDER BY id DESC LIMIT 1", (day_id,)).fetchone()

    if capture_seconds is not None and trade:
        conn.execute(
            "INSERT INTO trade_annotation(logical_trade_id,review_seconds,created_at,"
            "updated_at) VALUES (?,?,?,?) ON CONFLICT(logical_trade_id) DO UPDATE SET "
            "review_seconds=COALESCE(trade_annotation.review_seconds,0)+excluded.review_seconds,"
            "updated_at=excluded.updated_at",
            (trade["id"], int(capture_seconds), utcnow(), utcnow()))
        conn.commit()

    return {
        "trading_day_id": day_id,
        "logical_trade_id": trade["id"] if trade else None,
        "events_written": written,
        "already_known": duplicates,
        "grouping": grouped,
        "status": trade["status"] if trade else None,
        # Said plainly rather than left to be discovered: this is the lead only.
        "followers_captured": 0,
        "note": ("Lead account only. Follower fills are not inferred from the lead — "
                 "an unobserved copy is not evidence that the copy happened."),
    }
