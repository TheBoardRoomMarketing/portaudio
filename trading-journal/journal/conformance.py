"""MECHANICAL_CONFORMANCE_INDEX_V1.

The second process view. Where SELF_REPORTED_PROCESS_INDEX asks the trader how
they think they did, this counts what the record shows — and only from fields
that can be observed without anyone's opinion.

The governing rule, and the reason this file is careful rather than short:

    A component that cannot be measured is NEVER scored as perfect.

Scoring an absent component as 1.0 would silently reward missing data — a
session with no qualified setups logged would look more disciplined than one
where a setup was logged and missed. Instead, unavailable components are
excluded from the normalisation and named in `components_missing`, and the
stored score records which components were actually in it. A conformance index
of 78 built from six components and one built from three are different numbers,
and the record says which is which.

Entry deviation against a rule-defined intended entry stays permanently
NOT_AVAILABLE in v1: it needs a strategy reference implementation, which is
BLOCKED_ON_STRATEGY_SPEC. It is declared missing on every session rather than
quietly dropped from the definition.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

INDEX_VERSION = "mci@1.0.0"

# Weights are relative. The score normalises over whichever components were
# available, so these need not sum to 1 — but they do, which makes a
# fully-available session's arithmetic easy to check by hand.
WEIGHTS = {
    "qualified_setups_taken": 0.25,
    "rule_violations": 0.25,
    "size_discipline": 0.15,
    "override_discipline": 0.10,
    "stop_discipline": 0.10,
    "session_window": 0.15,
    # Declared but never available in v1. Listed here so the definition of the
    # index is complete in one place and the gap is explicit.
    "entry_deviation": 0.00,
}

NOT_AVAILABLE = "not_available"
NOT_APPLICABLE = "not_applicable"

# Tags that count as a rule violation by the human. System failures are excluded
# deliberately: a platform error is not an act of indiscipline, and letting it
# depress a discipline score would teach the trader to distrust the metric.
VIOLATION_TAGS = (
    "OVERTRADE", "REVENGE", "FOMO_ENTRY", "EARLY_ENTRY", "LATE_ENTRY",
    "PREMATURE_EXIT", "OVERSIZED", "RULE_OVERRIDE", "UNPLANNED_TRADE", "DISTRACTED",
)
SYSTEM_TAGS = ("TECHNICAL_ERROR", "BOT_ROUTING_ERROR")

# Above this many violations in one session the component scores zero. Three is
# the same threshold the self-reported index uses for plan conformance, so the
# two views disagree because of what they measure, not because of their scales.
VIOLATIONS_FOR_ZERO = 3
STOPS_MOVED_FOR_ZERO = 2
SIZE_DEVIATION_FOR_ZERO = 100.0   # percent over plan


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _within_window(entry_minutes: int, start: str, end: str) -> bool:
    """True if a local wall-clock minute falls inside the planned window.

    Windows may wrap midnight (an overnight session), so the comparison is not
    simply start <= t <= end.
    """
    first, last = _minutes(start), _minutes(end)
    if first <= last:
        return first <= entry_minutes <= last
    return entry_minutes >= first or entry_minutes <= last


def _component_qualified_setups(conn, session_id: int) -> Optional[float]:
    row = conn.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN status='MISSED' THEN 1 ELSE 0 END) missed "
        "FROM opportunity WHERE session_id=?", (session_id,)).fetchone()
    if not row["total"]:
        # No qualified setup was recorded. That is not perfect compliance, it is
        # an absence of evidence, so the component does not apply.
        return None
    return _clamp01(1 - (row["missed"] or 0) / row["total"])


def _component_rule_violations(conn, session_id: int) -> Optional[float]:
    # A session where nothing happened has not demonstrated discipline; it has
    # demonstrated nothing. Without a trade or a qualified setup there is no
    # behaviour to score, so the component does not apply.
    activity = conn.execute(
        "SELECT (SELECT COUNT(*) FROM trade WHERE session_id=?) "
        "     + (SELECT COUNT(*) FROM opportunity WHERE session_id=?) AS n",
        (session_id, session_id)).fetchone()["n"]
    if not activity:
        return None

    placeholders = ",".join("?" * len(VIOLATION_TAGS))
    count = conn.execute(
        f"SELECT COUNT(*) c FROM human_tag h LEFT JOIN trade t ON t.id = h.trade_id "
        f"WHERE (h.session_id = ? OR t.session_id = ?) AND h.tag_code IN ({placeholders})",
        (session_id, session_id, *VIOLATION_TAGS)).fetchone()["c"]
    return _clamp01(1 - count / VIOLATIONS_FOR_ZERO)


def _component_size_discipline(conn, session_id: int) -> Optional[float]:
    rows = conn.execute(
        "SELECT tm.size_deviation_pct d FROM trade t "
        "JOIN trade_metrics tm ON tm.trade_id = t.id "
        "WHERE t.session_id = ? AND tm.size_deviation_pct IS NOT NULL", (session_id,)
    ).fetchall()
    if not rows:
        return None
    # Only oversizing counts against conformance. Trading smaller than planned is
    # a different behaviour and not a risk-limit breach.
    excess = [max(0.0, r["d"]) for r in rows]
    mean_excess = sum(excess) / len(excess)
    return _clamp01(1 - mean_excess / SIZE_DEVIATION_FOR_ZERO)


def _component_override_discipline(conn, session_id: int) -> Optional[float]:
    row = conn.execute(
        "SELECT COUNT(*) total, SUM(was_manual_override) overrides "
        "FROM trade WHERE session_id=?", (session_id,)).fetchone()
    if not row["total"]:
        return None
    return _clamp01(1 - (row["overrides"] or 0) / row["total"])


def _component_stop_discipline(conn, session_id: int) -> Optional[float]:
    count = conn.execute(
        "SELECT COUNT(*) c FROM human_tag h LEFT JOIN trade t ON t.id = h.trade_id "
        "WHERE (h.session_id = ? OR t.session_id = ?) AND h.tag_code = 'STOP_MOVED'",
        (session_id, session_id)).fetchone()["c"]
    trades = conn.execute("SELECT COUNT(*) c FROM trade WHERE session_id=?",
                          (session_id,)).fetchone()["c"]
    if not trades:
        return None
    return _clamp01(1 - count / STOPS_MOVED_FOR_ZERO)


def _component_session_window(conn, session_id: int) -> Optional[float]:
    session = conn.execute(
        "SELECT tz, planned_window_start, planned_window_end FROM session WHERE id=?",
        (session_id,)).fetchone()
    if not session or not session["planned_window_start"] or not session["planned_window_end"]:
        return None
    trades = conn.execute("SELECT entry_at FROM trade WHERE session_id=?",
                          (session_id,)).fetchall()
    if not trades:
        return None

    from .repo import utc_to_local  # local import keeps the module dependency-light

    inside = 0
    for t in trades:
        local = utc_to_local(t["entry_at"], session["tz"])
        minute = local.hour * 60 + local.minute
        if _within_window(minute, session["planned_window_start"], session["planned_window_end"]):
            inside += 1
    return _clamp01(inside / len(trades))


COMPONENTS = {
    "qualified_setups_taken": _component_qualified_setups,
    "rule_violations": _component_rule_violations,
    "size_discipline": _component_size_discipline,
    "override_discipline": _component_override_discipline,
    "stop_discipline": _component_stop_discipline,
    "session_window": _component_session_window,
}


def compute(conn, session_id: int) -> dict:
    """Return the conformance index for one session, with its composition.

    Returns a dict with score (0-100 or None), version, available, missing and
    per-component detail. The score is None when nothing could be measured —
    an empty session yields no opinion rather than a perfect one.
    """
    detail: Dict[str, object] = {}
    available: List[str] = []
    missing: List[str] = []

    for name, fn in COMPONENTS.items():
        value = fn(conn, session_id)
        if value is None:
            missing.append(name)
            detail[name] = {"status": NOT_APPLICABLE, "weight": WEIGHTS[name]}
        else:
            available.append(name)
            detail[name] = {"status": "measured", "value": round(value, 4),
                            "weight": WEIGHTS[name]}

    # Permanently unavailable in v1, and declared rather than dropped.
    missing.append("entry_deviation")
    detail["entry_deviation"] = {
        "status": NOT_AVAILABLE,
        "weight": WEIGHTS["entry_deviation"],
        "blocked_by": "10AM_REFERENCE_IMPL is BLOCKED_ON_STRATEGY_SPEC",
    }

    weight_total = sum(WEIGHTS[name] for name in available)
    if not available or weight_total <= 0:
        score = None
    else:
        weighted = sum(WEIGHTS[name] * detail[name]["value"] for name in available)
        # Normalised over available weight only. A missing component neither
        # helps nor hurts; it narrows what the number is about.
        score = round(100 * weighted / weight_total, 1)

    return {
        "score": score,
        "version": INDEX_VERSION,
        "available": available,
        "missing": missing,
        "detail": detail,
        "coverage": round(weight_total / sum(w for k, w in WEIGHTS.items()
                                             if k != "entry_deviation"), 3),
    }


def store(conn, session_id: int, result: dict) -> None:
    conn.execute(
        "UPDATE session_metrics SET mechanical_conformance_index=?, "
        "conformance_index_version=?, conformance_components_available=?, "
        "conformance_components_missing=?, conformance_detail=? WHERE session_id=?",
        (result["score"], result["version"], json.dumps(result["available"]),
         json.dumps(result["missing"]), json.dumps(result["detail"]), session_id),
    )


def describe_disagreement(self_reported: Optional[float],
                          mechanical: Optional[float]) -> Optional[dict]:
    """Characterise the gap between the two process views.

    Deliberately neutral. A gap is information about the relationship between
    self-perception and record, not a verdict on either. The interface says the
    two differed; it never says the trader was wrong.
    """
    if self_reported is None or mechanical is None:
        return None
    gap = round(self_reported - mechanical, 1)
    magnitude = abs(gap)
    if magnitude < 10:
        band = "aligned"
    elif magnitude < 25:
        band = "some_difference"
    else:
        band = "large_difference"
    return {
        "gap": gap,
        "band": band,
        "direction": "self_higher" if gap > 0 else ("record_higher" if gap < 0 else "equal"),
    }
