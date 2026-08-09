"""The 20-day bias methodology observation period.

Three candidate methodologies run in dry-run over the first 20 complete real
trading days, and the choice between them is made from where they disagree.
Nothing here approves anything, and nothing here writes a bias outcome —
`daily_bias.outcome` stays NULL throughout.

Three properties are load-bearing, and each has a test:

  * **Isolation.** Verdicts land in `r_`-prefixed tables, which the daily
    connection's authorizer refuses outright. No screen, report or view can
    join to them by accident; reaching them needs an explicitly unrestricted
    connection, the same gate the blinded research layer sits behind.
  * **Identical input.** All three candidates judge the same recorded price
    envelope for a day. Candidates that saw different prices would compare
    nothing.
  * **No ranking.** The comparison reports agreement and disagreement. It does
    not score the candidates, does not name a favourite, and never looks at
    P&L — the question is which method operationalises the pre-session thesis,
    not which one makes the bias look good.

A day counts toward the 20 only when it has both a recorded bias and a recorded
session price. Days short of that are listed as incomplete rather than filled
in, because a comparison run on a partial sample would be settled by whichever
days happened to be complete.
"""

from __future__ import annotations

import json
from itertools import combinations
from typing import Dict, List, Optional

from . import bias_outcome
from .bias_outcome import SessionPrice
from .db import sha256_text, utcnow

TARGET_DAYS = 20
ENGINE_VERSION = "bias-candidates@1.0.0"


class NotIsolated(RuntimeError):
    """Raised when the observation layer is reached on a connection that the
    daily application also uses. The isolation is the safeguard; losing it
    quietly would be worse than failing loudly."""


def _require_unrestricted(conn) -> None:
    try:
        conn.execute("SELECT 1 FROM r_bias_candidate_run LIMIT 1")
    except Exception as exc:  # noqa: BLE001 — sqlite raises a plain error here
        raise NotIsolated(
            "the observation layer is not readable on this connection, which is "
            "correct for a daily connection. Open db.connect(restricted=False) "
            "deliberately."
        ) from exc


# --------------------------------------------------------------------- prices
def record_price(conn, trading_day_id: int, day_date: str, *, open: Optional[float],
                 high: Optional[float], low: Optional[float], close: Optional[float],
                 atr: Optional[float] = None, source: str) -> None:
    """Record the session price envelope a day's candidates will be judged on.

    `source` is required and is not decorative: a price typed from memory and a
    price taken from a chart export are different evidence, and which one it was
    has to survive to the comparison.
    """
    if not source:
        raise ValueError("a price needs a source — where it came from is part of it")
    _require_unrestricted(conn)
    conn.execute(
        "INSERT INTO r_session_price(trading_day_id,day_date,session_open,session_high,"
        "session_low,session_close,atr,source,recorded_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trading_day_id) DO UPDATE SET session_open=excluded.session_open,"
        "session_high=excluded.session_high,session_low=excluded.session_low,"
        "session_close=excluded.session_close,atr=excluded.atr,source=excluded.source,"
        "recorded_at=excluded.recorded_at",
        (trading_day_id, day_date, open, high, low, close, atr, source, utcnow()))
    conn.commit()


def _price_of(row) -> SessionPrice:
    return SessionPrice(open=row["session_open"], high=row["session_high"],
                        low=row["session_low"], close=row["session_close"],
                        atr=row["atr"])


def _price_hash(price: SessionPrice) -> str:
    return sha256_text(json.dumps(
        [price.open, price.high, price.low, price.close, price.atr]))[:16]


# --------------------------------------------------------------------- runs
def run_day(conn, trading_day_id: int) -> dict:
    """Dry-run all three candidates for one day and store the verdicts.

    Writes only to the observation layer. `daily_bias.outcome` is not touched
    here and is not touched anywhere until a methodology is approved.
    """
    _require_unrestricted(conn)

    bias_row = conn.execute(
        "SELECT * FROM daily_bias WHERE trading_day_id=? AND is_demo=0",
        (trading_day_id,)).fetchone()
    if not bias_row:
        return {"trading_day_id": trading_day_id, "ran": False,
                "reason": "no bias recorded for this day"}

    price_row = conn.execute(
        "SELECT * FROM r_session_price WHERE trading_day_id=?", (trading_day_id,)).fetchone()
    if not price_row:
        return {"trading_day_id": trading_day_id, "ran": False,
                "reason": "no session price recorded for this day"}

    price = _price_of(price_row)
    payload = {"direction": bias_row["direction"],
               "invalidation_level": bias_row["invalidation_level"]}
    digest = _price_hash(price)
    stored = {}

    for name, fn in bias_outcome.METHODOLOGIES.items():
        verdict = fn(payload, price)
        outcome = verdict.outcome if verdict else None
        reason = None if verdict else _why_silent(name, payload, price)
        conn.execute(
            "INSERT INTO r_bias_candidate_run(daily_bias_id,trading_day_id,day_date,"
            "methodology,outcome,unavailable_reason,inputs,detail,price_hash,"
            "engine_version,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(daily_bias_id,methodology,engine_version) DO UPDATE SET "
            "outcome=excluded.outcome,unavailable_reason=excluded.unavailable_reason,"
            "inputs=excluded.inputs,detail=excluded.detail,price_hash=excluded.price_hash,"
            "computed_at=excluded.computed_at",
            (bias_row["id"], trading_day_id, price_row["day_date"], name, outcome, reason,
             json.dumps(verdict.inputs if verdict else {}),
             verdict.detail if verdict else "", digest, ENGINE_VERSION, utcnow()))
        stored[name] = outcome

    conn.commit()
    return {"trading_day_id": trading_day_id, "day_date": price_row["day_date"],
            "ran": True, "verdicts": stored}


def _why_silent(name: str, bias: dict, price: SessionPrice) -> str:
    """Why a candidate declined to judge. How often each stays silent is one of
    the things being compared, so the reason is recorded rather than inferred."""
    if not price.complete():
        return "incomplete session price"
    if bias.get("direction") not in ("BULLISH", "BEARISH"):
        return f"non-directional bias ({bias.get('direction')})"
    if name == "invalidation_respected" and bias.get("invalidation_level") is None:
        return "no numeric invalidation level was captured"
    return "candidate returned no verdict"


def run_all(conn) -> dict:
    """Run every day that has both a bias and a price. Idempotent."""
    _require_unrestricted(conn)
    rows = conn.execute(
        "SELECT d.id FROM trading_day d "
        "JOIN daily_bias b ON b.trading_day_id=d.id AND b.is_demo=0 "
        "JOIN r_session_price p ON p.trading_day_id=d.id "
        "WHERE d.is_demo=0 ORDER BY d.day_date").fetchall()
    results = [run_day(conn, r["id"]) for r in rows]
    return {"days_run": sum(1 for r in results if r["ran"]),
            "skipped": [r for r in results if not r["ran"]],
            "progress": progress(conn)}


def progress(conn) -> dict:
    _require_unrestricted(conn)
    row = conn.execute("SELECT * FROM r_bias_observation_progress").fetchone()
    complete = row["complete_days"]
    return {
        "complete_days": complete,
        "target_days": TARGET_DAYS,
        "remaining": max(0, TARGET_DAYS - complete),
        "ready_for_selection": complete >= TARGET_DAYS,
        "verdicts_stored": row["verdicts_stored"],
        "note": ("A day counts only with both a recorded bias and a recorded session "
                 "price. No methodology is approved and no bias outcome is written."),
    }


# --------------------------------------------------------------------- compare
def comparison(conn) -> dict:
    """The report the director selects from. Descriptive; nothing is ranked.

    Deliberately absent: any score, any ordering, any mention of P&L. The output
    is what the candidates said and where they differed, so the selection is
    made on the evidence rather than on this module's opinion.
    """
    _require_unrestricted(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM r_bias_candidate_run WHERE engine_version=? ORDER BY day_date",
        (ENGINE_VERSION,))]
    state = progress(conn)

    by_day: Dict[str, Dict[str, dict]] = {}
    for row in rows:
        by_day.setdefault(row["day_date"], {})[row["methodology"]] = row

    names = sorted(bias_outcome.METHODOLOGIES)
    counts = {n: {} for n in names}
    silence = {n: 0 for n in names}
    for row in rows:
        if row["outcome"]:
            counts[row["methodology"]][row["outcome"]] = \
                counts[row["methodology"]].get(row["outcome"], 0) + 1
        else:
            silence[row["methodology"]] += 1

    # Pairwise, over days where BOTH members of the pair spoke. A pair where one
    # stayed silent is not a disagreement — it is a different fact, counted
    # separately so silence is never read as dissent.
    pairwise = {}
    for a, b in combinations(names, 2):
        agree = disagree = one_silent = 0
        for day, verdicts in by_day.items():
            va = verdicts.get(a, {}).get("outcome")
            vb = verdicts.get(b, {}).get("outcome")
            if va and vb:
                agree, disagree = (agree + 1, disagree) if va == vb else (agree, disagree + 1)
            elif va or vb:
                one_silent += 1
        total = agree + disagree
        pairwise[f"{a} vs {b}"] = {
            "days_both_spoke": total,
            "agree": agree,
            "disagree": disagree,
            "agreement_rate": round(agree / total, 3) if total else None,
            "days_only_one_spoke": one_silent,
        }

    unanimous = 0
    split_days = []
    for day, verdicts in by_day.items():
        spoken = [v["outcome"] for v in verdicts.values() if v["outcome"]]
        if len(spoken) >= 2 and len(set(spoken)) == 1:
            unanimous += 1
        elif len(set(spoken)) > 1:
            split_days.append({
                "day_date": day,
                "verdicts": {m: v["outcome"] for m, v in verdicts.items()},
                "why_they_differ": {m: v["detail"] for m, v in verdicts.items() if v["detail"]},
            })

    return {
        "engine_version": ENGINE_VERSION,
        "progress": state,
        "status": ("READY_FOR_DIRECTOR_SELECTION" if state["ready_for_selection"]
                   else "OBSERVING"),
        "per_methodology": {
            n: {"verdicts": counts[n], "silent_days": silence[n],
                "status": bias_outcome.STATUS[n]} for n in names},
        "pairwise": pairwise,
        "unanimous_days": unanimous,
        "disagreement_days": sorted(split_days, key=lambda d: d["day_date"]),
        "ranking": "NONE — the director selects; this report does not recommend",
        "selection_question": (
            "Which method best operationalises Zack's pre-session directional thesis?"),
        "not_used": ["profit and loss", "process scores", "conformance",
                     "anything shown during a trading day"],
        "confirmations": {"BIAS_OUTCOMES_ASSIGNED": False,
                          "ACTIVE_METHODOLOGY": bias_outcome.ACTIVE_METHODOLOGY},
    }


def leak_check(conn) -> List[str]:
    """Prove no candidate label reached anything operational.

    Two things are checked, both of which would be silent failures: a written
    bias outcome, and an approved methodology. Run before any return packet.
    """
    problems = []
    written = conn.execute(
        "SELECT COUNT(*) c FROM daily_bias WHERE outcome IS NOT NULL").fetchone()["c"]
    if written:
        problems.append(
            f"{written} daily_bias rows carry an outcome. No methodology is approved, "
            "so no outcome should exist.")
    if bias_outcome.ACTIVE_METHODOLOGY is not None:
        problems.append(
            f"ACTIVE_METHODOLOGY is {bias_outcome.ACTIVE_METHODOLOGY!r}; expected None.")
    return problems
