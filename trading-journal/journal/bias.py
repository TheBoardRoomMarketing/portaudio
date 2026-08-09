"""The morning read.

A bias recorded before the open and editable after the close is worth nothing
as evidence — hindsight is not a weak force. So the thesis is written once,
hashed, and frozen by trigger. Corrections are amendments with a timestamp and
a reason, and the original stays readable next to them.

Bias outcome is deliberately not assigned by anything here. §XI requires an
objective market-based methodology approved before activation, and an LLM
deciding whether a morning thesis "came true" is exactly the kind of plausible
judgement that would contaminate every downstream analysis.
"""

from __future__ import annotations

import json
from typing import List, Optional

from .db import sha256_text, utcnow

DIRECTIONS = ("BULLISH", "BEARISH", "NEUTRAL", "UNSURE")
SOURCES = ("LIQUIDITY_MAP", "PRICE_ACTION", "HIGHER_TIMEFRAME", "NEWS_MACRO",
           "DISCRETION_GUT", "MULTIPLE", "OTHER")

# Set only by a methodology that does not exist yet. Listed so the vocabulary is
# fixed in advance rather than invented at the moment of first use.
OUTCOMES = ("CORRECT", "PARTIALLY_CORRECT", "INCORRECT", "INDETERMINATE")


def _hash(direction, strength, thesis, invalidation, sources) -> str:
    return sha256_text(json.dumps(
        {"direction": direction, "strength": strength, "thesis": thesis,
         "invalidation": invalidation, "sources": sorted(sources or [])},
        sort_keys=True))


def record(conn, trading_day_id: int, *, direction: str, strength: Optional[int] = None,
           thesis: Optional[str] = None, invalidation: Optional[str] = None,
           invalidation_level: Optional[float] = None,
           sources: Optional[List[str]] = None, capture_seconds: Optional[int] = None,
           recorded_at: Optional[str] = None, is_demo: bool = False) -> int:
    """Write the morning read. Once per day; after that it can only be amended.

    `invalidation_level` is the prose invalidation written as a number, when
    there is one. It is optional and stays optional: it exists so a bias can
    eventually be judged against its author's own stated terms, and no
    methodology is approved to do that yet.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if strength is not None and not 1 <= strength <= 5:
        raise ValueError("strength must be 1-5")
    for source in sources or []:
        if source not in SOURCES:
            raise ValueError(f"unknown bias source '{source}'")
    if invalidation_level is not None:
        invalidation_level = float(invalidation_level)

    existing = conn.execute("SELECT id FROM daily_bias WHERE trading_day_id=?",
                            (trading_day_id,)).fetchone()
    if existing:
        raise ValueError("a bias already exists for this day; amend it instead")

    payload = json.dumps(sources or [])
    cur = conn.execute(
        "INSERT INTO daily_bias(trading_day_id,recorded_at,direction,strength,thesis,"
        "invalidation,invalidation_level,sources,capture_seconds,original_hash,is_demo)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (trading_day_id, recorded_at or utcnow(), direction, strength, thesis,
         invalidation, invalidation_level, payload, capture_seconds,
         _hash(direction, strength, thesis, invalidation, sources), 1 if is_demo else 0))
    conn.commit()
    return cur.lastrowid


AMENDABLE = ("direction", "strength", "thesis", "invalidation", "sources")


def amend(conn, trading_day_id: int, changes: dict, reason: str) -> dict:
    """Correct a recorded bias without destroying what was originally written.

    The trigger blocks a direct UPDATE, so this is the only route — which is the
    point. Every amendment carries a reason, and the original values remain in
    `bias_amendment` forever.
    """
    if not reason:
        raise ValueError("an amendment needs a reason")
    row = conn.execute("SELECT * FROM daily_bias WHERE trading_day_id=?",
                       (trading_day_id,)).fetchone()
    if not row:
        raise ValueError("no bias recorded for this day")

    now = utcnow()
    applied = []
    for field, new in changes.items():
        if field not in AMENDABLE:
            raise ValueError(f"'{field}' is not an amendable bias field")
        old = row[field]
        new_stored = json.dumps(new) if field == "sources" else new
        if str(old) == str(new_stored):
            continue
        conn.execute(
            "INSERT INTO bias_amendment(daily_bias_id,field,old_value,new_value,reason,"
            "amended_at) VALUES (?,?,?,?,?,?)",
            (row["id"], field, None if old is None else str(old),
             None if new_stored is None else str(new_stored), reason, now))
        applied.append(field)

    if applied:
        # The trigger guards these columns, so it is lifted only for the length
        # of this statement — after the amendment record already exists.
        conn.execute("DROP TRIGGER daily_bias_thesis_immutable")
        try:
            sets = ", ".join(f"{f}=?" for f in applied)
            values = [json.dumps(changes[f]) if f == "sources" else changes[f] for f in applied]
            conn.execute(f"UPDATE daily_bias SET {sets} WHERE id=?", values + [row["id"]])
        finally:
            conn.execute("""
                CREATE TRIGGER daily_bias_thesis_immutable
                BEFORE UPDATE OF direction, strength, thesis, invalidation, sources,
                                 recorded_at, original_hash
                ON daily_bias BEGIN
                    SELECT RAISE(ABORT,
                      'the morning bias is immutable: record an amendment instead of editing it');
                END;""")
    conn.commit()
    return {"amended": applied, "reason": reason, "at": now}


def get(conn, trading_day_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM daily_bias WHERE trading_day_id=?",
                       (trading_day_id,)).fetchone()
    if not row:
        return None
    bias = dict(row)
    bias["sources"] = json.loads(bias["sources"] or "[]")
    bias["amendments"] = [dict(r) for r in conn.execute(
        "SELECT field, old_value, new_value, reason, amended_at FROM bias_amendment "
        "WHERE daily_bias_id=? ORDER BY amended_at, id", (row["id"],))]
    # If this is false the row was changed by something other than amend().
    bias["matches_original"] = (
        bias["original_hash"] == _hash(bias["direction"], bias["strength"], bias["thesis"],
                                       bias["invalidation"], bias["sources"])
    ) or bool(bias["amendments"])
    return bias


def set_outcome(conn, trading_day_id: int, outcome: str, method: str) -> None:
    """Record a bias outcome under a named, approved methodology.

    `method` is required and must not be a model's opinion. Nothing in this
    codebase calls this function; it exists so that when a methodology is
    approved there is one auditable place to write the result.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    if not method or method.lower() in ("llm", "ai", "model", "gpt", "claude"):
        raise ValueError(
            "bias outcome requires a named objective methodology; a model's judgement "
            "is not one. See docs/BIAS-OUTCOME-METHODOLOGY.md")
    conn.execute(
        "UPDATE daily_bias SET outcome=?, outcome_method=?, outcome_at=? WHERE trading_day_id=?",
        (outcome, method, utcnow(), trading_day_id))
    conn.commit()
