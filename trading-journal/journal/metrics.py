"""The derived layer.

Every value here is recomputed from RAW and stamped with a calc_version. The
table can be dropped and rebuilt at any time; nothing in it is authored.

Two definitions are load-bearing and easy to get wrong, so they are stated once
here and tested directly:

    entry_slippage_ticks   negative is ALWAYS adverse, for longs and shorts
                           alike: (intended - filled) * direction / tick_size

    r_captured_pct         NULL on losers. A -1R trade that saw +0.6R in its
                           favour has no meaningful "share of MFE captured";
                           computing one yields -164%, which reads as a metric
                           and is noise.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from . import config
from .db import sha256_text, utcnow

# The self-reported index weights. Four of five inputs are self-reported, which
# is why the column is named self_reported_process_index and not process_score.
# No P&L term appears here and none may be added: a day can be well traded and
# lose money.
PROCESS_WEIGHTS = {
    "rule_adherence": 0.35,
    "execution_quality": 0.20,
    "patience": 0.15,
    "emotional_control": 0.10,
    "plan_conformance": 0.20,
}
FLAGS_FOR_ZERO_CONFORMANCE = 3


def _parse(ts: Optional[str]) -> Optional[datetime]:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") if ts else None


def self_reported_process_index(post: sqlite3.Row, mistake_count: int) -> Optional[float]:
    if not post:
        return None
    needed = ("rule_adherence", "execution_quality", "patience", "emotional_control")
    if any(post[k] is None for k in needed):
        return None
    conformance = 1 - min(1.0, mistake_count / FLAGS_FOR_ZERO_CONFORMANCE)
    score = sum(PROCESS_WEIGHTS[k] * post[k] / 5 for k in needed)
    score += PROCESS_WEIGHTS["plan_conformance"] * conformance
    return round(100 * score, 1)


def _trade_pnl(conn, trade: sqlite3.Row, point_value: float) -> dict:
    """Realised P&L from fills, so partial exits are handled correctly."""
    fills = conn.execute(
        "SELECT leg, quantity, price, filled_at, intended_price, commission, exchange_fees "
        "FROM trade_fill WHERE trade_id=? ORDER BY filled_at, id", (trade["id"],)
    ).fetchall()
    if not fills:
        return {}

    direction = 1 if trade["side"] == "long" else -1
    entry_legs = [f for f in fills if f["leg"] in ("entry", "scale_in")]
    exit_legs = [f for f in fills if f["leg"] in ("exit", "partial", "stop", "target")]

    entry_qty = sum(f["quantity"] for f in entry_legs) or trade["quantity"]
    avg_entry = (sum(f["price"] * f["quantity"] for f in entry_legs) / entry_qty
                 if entry_legs else trade["avg_entry_price"])

    closed_qty = sum(f["quantity"] for f in exit_legs)
    gross = sum((f["price"] - avg_entry) * direction * f["quantity"] * point_value
                for f in exit_legs)
    avg_exit = (sum(f["price"] * f["quantity"] for f in exit_legs) / closed_qty
                if closed_qty else None)

    fees = round(sum((f["commission"] or 0) + (f["exchange_fees"] or 0) for f in fills), 4)

    slippage = None
    if entry_legs and entry_legs[0]["intended_price"] is not None:
        tick = conn.execute("SELECT tick_size FROM instrument WHERE id=?",
                            (trade["instrument_id"],)).fetchone()["tick_size"]
        slippage = round(
            (entry_legs[0]["intended_price"] - entry_legs[0]["price"]) * direction / tick, 2)

    return {
        "avg_entry": avg_entry,
        "avg_exit": avg_exit,
        "entry_qty": entry_qty,
        "closed_qty": closed_qty,
        "fully_closed": closed_qty >= entry_qty - 1e-9,
        "gross": round(gross, 2),
        "fees": fees,
        "net": round(gross - fees, 2),
        "slippage": slippage,
        "first_fill_at": entry_legs[0]["filled_at"] if entry_legs else None,
        "last_fill_at": exit_legs[-1]["filled_at"] if exit_legs else None,
    }


def recompute_trade_metrics(conn, run_id: int) -> int:
    conn.execute("DELETE FROM trade_metrics")
    trades = conn.execute("SELECT * FROM trade ORDER BY session_id, entry_at, id").fetchall()

    cumulative: dict = {}
    index: dict = {}
    written = 0

    for t in trades:
        instrument = conn.execute("SELECT point_value FROM instrument WHERE id=?",
                                  (t["instrument_id"],)).fetchone()
        p = _trade_pnl(conn, t, instrument["point_value"])
        if not p:
            continue

        direction = 1 if t["side"] == "long" else -1
        risk_per_unit = abs(p["avg_entry"] - t["initial_stop"]) if t["initial_stop"] else None

        r_multiple = None
        if risk_per_unit and p["avg_exit"] is not None:
            r_multiple = round((p["avg_exit"] - p["avg_entry"]) * direction / risk_per_unit, 3)

        # MFE/MAE have no automatic source until a market-data enricher exists.
        # When the human reported the excursion prices, the R values are derived
        # from them; otherwise they stay NULL rather than being guessed.
        mfe_r = mae_r = None
        if risk_per_unit and t["reported_mfe_price"] is not None:
            mfe_r = round((t["reported_mfe_price"] - p["avg_entry"]) * direction / risk_per_unit, 2)
        if risk_per_unit and t["reported_mae_price"] is not None:
            mae_r = round((t["reported_mae_price"] - p["avg_entry"]) * direction / risk_per_unit, 2)

        entry_dt, exit_dt = _parse(t["entry_at"]), _parse(t["exit_at"])
        holding = int((exit_dt - entry_dt).total_seconds()) if entry_dt and exit_dt else None

        size_dev = None
        if t["planned_quantity"]:
            size_dev = round((p["entry_qty"] - t["planned_quantity"]) / t["planned_quantity"] * 100, 1)

        captured = None
        if r_multiple is not None and r_multiple > 0 and mfe_r and mfe_r > 0:
            captured = round(r_multiple / mfe_r * 100, 1)

        index[t["session_id"]] = index.get(t["session_id"], 0) + 1
        cumulative[t["session_id"]] = round(cumulative.get(t["session_id"], 0.0) + p["net"], 2)

        conn.execute(
            "INSERT INTO trade_metrics(trade_id,gross_pnl,fees,net_pnl,risk_per_unit,r_multiple,"
            "mfe_price,mae_price,mfe_r,mae_r,seconds_to_mfe,seconds_to_mae,holding_seconds,"
            "entry_slippage_ticks,exit_slippage_ticks,size_deviation_pct,r_captured_pct,"
            "session_cum_pnl_after,session_trade_index,calc_version,inputs_hash,derived_run_id,"
            "computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["id"], p["gross"], p["fees"], p["net"], risk_per_unit, r_multiple,
             t["reported_mfe_price"], t["reported_mae_price"], mfe_r, mae_r, None, None,
             holding, p["slippage"], None, size_dev, captured,
             cumulative[t["session_id"]], index[t["session_id"]], config.CALC_VERSION,
             sha256_text(f"{t['trade_uid']}|{p['avg_entry']}|{p['avg_exit']}|{p['fees']}"),
             run_id, utcnow()),
        )
        written += 1
    return written


def recompute_session_metrics(conn, run_id: int) -> int:
    conn.execute("DELETE FROM session_metrics")
    written = 0

    for s in conn.execute("SELECT id FROM session").fetchall():
        sid = s["id"]
        agg = conn.execute(
            "SELECT COUNT(*) n,"
            " SUM(CASE WHEN tm.r_multiple>0 THEN 1 ELSE 0 END) wins,"
            " SUM(CASE WHEN tm.r_multiple<0 THEN 1 ELSE 0 END) losses,"
            " SUM(CASE WHEN tm.r_multiple=0 THEN 1 ELSE 0 END) scratches,"
            " SUM(tm.gross_pnl) gross, SUM(tm.fees) fees, SUM(tm.net_pnl) net,"
            " SUM(tm.r_multiple) total_r"
            " FROM trade t JOIN trade_metrics tm ON tm.trade_id=t.id WHERE t.session_id=?",
            (sid,)).fetchone()

        opps = conn.execute(
            "SELECT COUNT(*) n,"
            " SUM(CASE WHEN status IN ('TAKEN','BOT_EXECUTED') THEN 1 ELSE 0 END) taken,"
            " SUM(CASE WHEN status='MISSED' THEN 1 ELSE 0 END) missed"
            " FROM opportunity WHERE session_id=?", (sid,)).fetchone()

        # USER_VALUE_ADDED is only defined when a mechanical reference exists for
        # the session's opportunities. Until a reference implementation is built,
        # there are no such rows, and the honest value is NULL — not total_r minus
        # zero, which would read as "discretion cost you 2R" when nothing was
        # compared against anything.
        reference = conn.execute(
            "SELECT COUNT(*) n, SUM(mr.r_multiple) v FROM mechanical_reference mr"
            " JOIN opportunity o ON o.id=mr.opportunity_id WHERE o.session_id=?",
            (sid,)).fetchone()
        mechanical = reference["v"] if reference["n"] else None

        mistakes = conn.execute(
            "SELECT COUNT(*) n FROM human_tag ht LEFT JOIN trade t ON t.id=ht.trade_id"
            " WHERE ht.session_id=? OR t.session_id=?", (sid, sid)).fetchone()["n"]

        post = conn.execute("SELECT * FROM checkin_post WHERE session_id=?", (sid,)).fetchone()

        r_sequence = [r["r_multiple"] or 0 for r in conn.execute(
            "SELECT tm.r_multiple FROM trade t JOIN trade_metrics tm ON tm.trade_id=t.id"
            " WHERE t.session_id=? ORDER BY t.entry_at", (sid,))]
        run = peak = drawdown = 0.0
        for r in r_sequence:
            run += r
            peak = max(peak, run)
            drawdown = min(drawdown, run - peak)

        n = agg["n"] or 0
        total_r = agg["total_r"]
        user_value_added = (round((total_r or 0.0) - mechanical, 2)
                            if mechanical is not None else None)

        conn.execute(
            "INSERT INTO session_metrics(session_id,trade_count,win_count,loss_count,"
            "scratch_count,gross_pnl,fees,net_pnl,total_r,max_drawdown_r,expectancy_r,"
            "opportunities_total,opportunities_taken,opportunities_missed,mechanical_r,"
            "user_value_added_r,self_reported_process_index,mechanical_conformance_index,"
            "rule_adherence,mistake_count,calc_version,derived_run_id,computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, n, agg["wins"] or 0, agg["losses"] or 0, agg["scratches"] or 0,
             round(agg["gross"] or 0, 2), round(agg["fees"] or 0, 2), round(agg["net"] or 0, 2),
             round(total_r, 2) if total_r is not None else 0.0,
             round(drawdown, 2), round(total_r / n, 3) if n and total_r is not None else None,
             opps["n"] or 0, opps["taken"] or 0, opps["missed"] or 0,
             round(mechanical, 2) if mechanical is not None else None,
             user_value_added,
             self_reported_process_index(post, mistakes),
             # MECHANICAL_CONFORMANCE_INDEX is authorised but not computed in this
             # phase: one of its inputs (entry deviation against rule-defined
             # intended entry) requires the 10AM reference implementation, which
             # is BLOCKED_ON_STRATEGY_SPEC. A partial index would read as the
             # real one, so the column stays NULL. See docs/11-phase-2-notes.md.
             None,
             post["rule_adherence"] if post else None,
             mistakes, config.CALC_VERSION, run_id, utcnow()),
        )
        written += 1
    return written


def recompute_all(conn, note: str = "") -> dict:
    """Rebuild the whole derived layer. Safe to run at any time."""
    started = utcnow()
    cur = conn.execute(
        "INSERT INTO derived_run(calc_version,code_sha,scope,started_at,status)"
        " VALUES (?,?,?,?,'ok')",
        (config.CALC_VERSION, sha256_text(config.CALC_VERSION + note),
         "trade_metrics+session_metrics", started),
    )
    run_id = cur.lastrowid
    trades = recompute_trade_metrics(conn, run_id)
    sessions = recompute_session_metrics(conn, run_id)
    conn.execute("UPDATE derived_run SET finished_at=?, rows_written=? WHERE id=?",
                 (utcnow(), trades + sessions, run_id))
    conn.commit()
    return {"run_id": run_id, "calc_version": config.CALC_VERSION,
            "trade_metrics": trades, "session_metrics": sessions}


# Which MECHANICAL_CONFORMANCE_INDEX inputs the journal can already count, and
# which are waiting on something. Surfaced by `journal status` so the gap is
# visible rather than remembered.
CONFORMANCE_INPUT_READINESS = {
    "qualified_setups_missed": "available",
    "rule_violations": "available",
    "size_deviation": "available",
    "unauthorized_overrides": "available",
    "trading_outside_planned_window": "available",
    "stop_rule_violations": "available",
    "entry_deviation_vs_rule_defined_entry": "blocked: needs 10AM reference implementation",
}
