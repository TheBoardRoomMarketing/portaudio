"""Descriptive reporting.

Everything here counts, sums and medians. Nothing here tests, correlates,
slices for a best condition, or mines a parameter. That is not a stylistic
preference — a weekly report that quietly ran significance tests would produce
findings nobody preregistered, on a sample nobody checked, and those findings
would then shape trading.

The allowlist below is the enforcement. A section not named there cannot appear
in a generated report, and the sections actually included are stored with each
report so a later change to this list is visible in the record.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from typing import Dict, List, Optional

from . import conformance
from .db import utcnow

GENERATOR_VERSION = "weekly@1.0.0"

# The only sections a generated report may contain.
ALLOWED_SECTIONS = (
    "performance",          # R, P&L, counts — descriptive
    "process",              # both process views, descriptive
    "strategies",           # per-strategy counts and sums
    "mistakes",             # tag frequency
    "missed_setups",        # what qualified and went untaken
    "manual_overrides",     # count and where
    "capture_completeness", # how much of the record actually got written
)

# Never generated, at any sample size, without a preregistered question and a
# later director ruling. Named explicitly so the prohibition is greppable.
FORBIDDEN_CONTENT = (
    "astro", "correlation", "significance", "p_value", "regression",
    "best_conditions", "optimal", "predicts", "lunar", "transit",
)


def _median(values: List[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 1) if clean else None


def _sessions_in(conn, iso_week: str) -> List[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM v_session_daily WHERE iso_week=? ORDER BY session_date, session_kind",
        (iso_week,))]


def weekly(conn, iso_week: str) -> dict:
    """A descriptive week. Counts and sums only."""
    sessions = _sessions_in(conn, iso_week)
    if not sessions:
        return {"iso_week": iso_week, "sessions": 0, "sections": [], "note": "no sessions"}

    ids = [s["session_id"] for s in sessions]
    marks = ",".join("?" * len(ids))

    traded = [s for s in sessions if (s["trade_count"] or 0) > 0]
    total_r = sum(s["total_r"] or 0 for s in sessions)
    net = sum(s["net_pnl"] or 0 for s in sessions)
    trades = sum(s["trade_count"] or 0 for s in sessions)

    trade_rs = [r["r_multiple"] for r in conn.execute(
        f"SELECT tm.r_multiple FROM trade t JOIN trade_metrics tm ON tm.trade_id=t.id "
        f"WHERE t.session_id IN ({marks}) AND tm.r_multiple IS NOT NULL", ids)]
    wins = [r for r in trade_rs if r > 0]
    losses = [r for r in trade_rs if r < 0]

    self_scores = [s["self_reported_process_index"] for s in sessions
                   if s["self_reported_process_index"] is not None]
    mech_scores = [s["mechanical_conformance_index"] for s in sessions
                   if s["mechanical_conformance_index"] is not None]
    gaps = []
    for s in sessions:
        d = conformance.describe_disagreement(s["self_reported_process_index"],
                                              s["mechanical_conformance_index"])
        if d:
            gaps.append(d)

    by_strategy: Dict[str, dict] = {}
    for row in conn.execute(
            f"SELECT sv.strategy_id, sv.version, COUNT(*) n, SUM(tm.r_multiple) r "
            f"FROM trade t JOIN strategy_version sv ON sv.id=t.strategy_version_id "
            f"LEFT JOIN trade_metrics tm ON tm.trade_id=t.id "
            f"WHERE t.session_id IN ({marks}) GROUP BY sv.id", ids):
        key = f"{row['strategy_id']} v{row['version']}"
        by_strategy[key] = {"trades": row["n"], "total_r": round(row["r"] or 0, 2)}
    for row in conn.execute(
            f"SELECT sv.strategy_id, sv.version, COUNT(*) n, "
            f"SUM(CASE WHEN o.status='MISSED' THEN 1 ELSE 0 END) missed "
            f"FROM opportunity o JOIN strategy_version sv ON sv.id=o.strategy_version_id "
            f"WHERE o.session_id IN ({marks}) GROUP BY sv.id", ids):
        key = f"{row['strategy_id']} v{row['version']}"
        by_strategy.setdefault(key, {"trades": 0, "total_r": 0.0})
        by_strategy[key]["qualified"] = row["n"]
        by_strategy[key]["missed"] = row["missed"] or 0

    mistakes = Counter()
    for row in conn.execute(
            f"SELECT h.tag_code FROM human_tag h LEFT JOIN trade t ON t.id=h.trade_id "
            f"WHERE h.session_id IN ({marks}) OR t.session_id IN ({marks})", ids + ids):
        mistakes[row["tag_code"]] += 1

    missed = [dict(r) for r in conn.execute(
        f"SELECT s.session_date, sv.strategy_id, o.qualified_at, o.direction, o.status_reason "
        f"FROM opportunity o JOIN session s ON s.id=o.session_id "
        f"JOIN strategy_version sv ON sv.id=o.strategy_version_id "
        f"WHERE o.session_id IN ({marks}) AND o.status='MISSED' ORDER BY o.qualified_at", ids)]

    overrides = [dict(r) for r in conn.execute(
        f"SELECT s.session_date, t.trade_uid, t.override_note FROM trade t "
        f"JOIN session s ON s.id=t.session_id "
        f"WHERE t.session_id IN ({marks}) AND t.was_manual_override=1", ids)]

    capture = [dict(r) for r in conn.execute(
        f"SELECT * FROM v_capture_quality WHERE session_id IN ({marks})", ids)]

    payload = {
        "iso_week": iso_week,
        "generated_at": utcnow(),
        "generator_version": GENERATOR_VERSION,
        "sections": list(ALLOWED_SECTIONS),
        "sessions": len(sessions),
        "performance": {
            "total_r": round(total_r, 2),
            "net_pnl": round(net, 2),
            "trades": trades,
            "sessions_traded": len(traded),
            "sessions_without_a_trade": len(sessions) - len(traded),
            "winners": len(wins),
            "losers": len(losses),
            "expectancy_r": round(total_r / trades, 3) if trades else None,
            "gross_win_r": round(sum(wins), 2) if wins else 0.0,
            "gross_loss_r": round(sum(losses), 2) if losses else 0.0,
            "largest_win_r": round(max(wins), 2) if wins else None,
            "largest_loss_r": round(min(losses), 2) if losses else None,
        },
        "process": {
            "self_reported_median": _median(self_scores),
            "mechanical_median": _median(mech_scores),
            "sessions_with_both": len(gaps),
            "aligned": sum(1 for g in gaps if g["band"] == "aligned"),
            "some_difference": sum(1 for g in gaps if g["band"] == "some_difference"),
            "large_difference": sum(1 for g in gaps if g["band"] == "large_difference"),
            "note": "Both views are reported. They are not combined, and a difference "
                    "between them is not treated as an error in either.",
        },
        "strategies": by_strategy,
        "mistakes": dict(mistakes.most_common()),
        "missed_setups": {
            "count": len(missed),
            "detail": missed,
            "note": "Mechanical value of a missed setup is not shown: no reference "
                    "implementation exists, so there is nothing to compare against.",
        },
        "manual_overrides": {"count": len(overrides), "detail": overrides},
        "capture_completeness": {
            "sessions": len(capture),
            "morning_completed": sum(c["has_pre"] for c in capture),
            "evening_completed": sum(c["has_post"] for c in capture),
            "median_morning_seconds": _median([c["pre_fill_seconds"] for c in capture]),
            "median_evening_seconds": _median([c["post_fill_seconds"] for c in capture]),
            "voice_notes": sum(c["voice_notes"] for c in capture),
            "manual_trades": sum(c["manual_trades"] for c in capture),
        },
    }

    _assert_descriptive(payload)
    return payload


def _assert_descriptive(payload: dict) -> None:
    """Refuse to emit a report containing anything inferential or blinded.

    Cheap, and it means a future edit that adds a correlation section fails
    loudly at generation time instead of quietly shipping one.
    """
    blob = json.dumps(payload, default=str).lower()
    found = [word for word in FORBIDDEN_CONTENT if word in blob]
    if found:
        raise RuntimeError(
            f"report generation refused: contains non-descriptive content {found}. "
            "Weekly reports stay descriptive until a preregistered question exists.")
    extra = [s for s in payload.get("sections", []) if s not in ALLOWED_SECTIONS]
    if extra:
        raise RuntimeError(f"report contains sections outside the allowlist: {extra}")


def store_weekly(conn, iso_week: str) -> dict:
    payload = weekly(conn, iso_week)
    if not payload.get("sessions"):
        return payload
    row = conn.execute(
        "SELECT session_date FROM session WHERE iso_week=? ORDER BY session_date", (iso_week,)
    ).fetchall()
    conn.execute(
        "INSERT INTO report(kind,period_key,period_start,period_end,metrics,generated_at,"
        "calc_version,sections,generator_version) VALUES ('weekly',?,?,?,?,?,?,?,?)",
        (iso_week, row[0]["session_date"], row[-1]["session_date"],
         json.dumps(payload, default=str), utcnow(), conformance.INDEX_VERSION,
         json.dumps(list(ALLOWED_SECTIONS)), GENERATOR_VERSION),
    )
    conn.commit()
    return payload


# --------------------------------------------------------------------- friction
def friction(conn) -> dict:
    """How much the journal costs to use. Evidence before form redesign."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM v_capture_quality ORDER BY session_date")]
    if not rows:
        return {"sessions": 0, "note": "no sessions yet"}

    pre = [r["pre_fill_seconds"] for r in rows if r["pre_fill_seconds"] is not None]
    post = [r["post_fill_seconds"] for r in rows if r["post_fill_seconds"] is not None]
    with_pre = sum(r["has_pre"] for r in rows)
    with_post = sum(r["has_post"] for r in rows)

    return {
        "sessions": len(rows),
        "first_session": rows[0]["session_date"],
        "last_session": rows[-1]["session_date"],
        "completion": {
            "morning": round(with_pre / len(rows), 3),
            "evening": round(with_post / len(rows), 3),
            "both": round(sum(1 for r in rows if r["has_pre"] and r["has_post"]) / len(rows), 3),
        },
        "fill_seconds": {
            "morning_median": _median(pre),
            "morning_worst": max(pre) if pre else None,
            "morning_over_target": sum(1 for v in pre if v > 60),
            "evening_median": _median(post),
            "evening_worst": max(post) if post else None,
            "evening_over_target": sum(1 for v in post if v > 120),
            "targets": {"morning": 60, "evening": 120},
        },
        "optional_section_open_rate": {
            "morning": round(sum(1 for r in rows if r["pre_optional_opened"]) / len(rows), 3),
            "evening": round(sum(1 for r in rows if r["post_optional_opened"]) / len(rows), 3),
        },
        "voice_notes": {
            "total": sum(r["voice_notes"] for r in rows),
            "sessions_with_one": sum(1 for r in rows if r["voice_notes"]),
        },
        "manual_burden": {
            "manual_trades": sum(r["manual_trades"] for r in rows),
            "total_trades": sum(r["trades"] for r in rows),
            "human_logged_setups": sum(r["human_logged_setups"] for r in rows),
        },
        "taxonomy": {
            "other_tag_uses": sum(r["other_tag_uses"] for r in rows),
            "note": "Repeated use of OTHER is the signal that the taxonomy needs a new code.",
        },
    }


DAY_TARGETS = {"bias_seconds": 60, "review_seconds_per_trade": 90, "close_seconds": 120}


def day_friction(conn) -> dict:
    """Capture cost on the model actually in use: days, biases and reviews.

    The session-model report above still runs, because the older sessions are
    real evidence. This one measures the workflow Zack is on now.

    Every rate here is over days that exist. A day with no bias counts against
    morning capture — that is the number worth seeing, and quietly excluding it
    would turn a missed morning into a clean record.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM v_day_capture_quality ORDER BY day_date")]
    if not rows:
        return {"days": 0, "note": "no real trading days yet — this fills in with use"}

    def rate(predicate) -> float:
        return round(sum(1 for r in rows if predicate(r)) / len(rows), 3)

    bias_times = [r["bias_seconds"] for r in rows if r["bias_seconds"] is not None]
    close_times = [r["close_seconds"] for r in rows if r["close_seconds"] is not None]
    trades = sum(r["trades"] for r in rows)
    reviewed = sum(r["trades_reviewed"] for r in rows)
    review_seconds = [r["review_seconds_total"] for r in rows
                      if r["review_seconds_total"] is not None]
    manual = sum(r["manual_events"] for r in rows)
    imported = sum(r["imported_events"] for r in rows)

    return {
        "days": len(rows),
        "first_day": rows[0]["day_date"],
        "last_day": rows[-1]["day_date"],
        "completion": {
            "morning_bias": rate(lambda r: r["has_bias"]),
            "evening_reflection": rate(lambda r: r["has_reflection"]),
            "both": rate(lambda r: r["has_bias"] and r["has_reflection"]),
            "trades_reviewed": round(reviewed / trades, 3) if trades else None,
        },
        "seconds": {
            "bias_median": _median(bias_times),
            "bias_worst": max(bias_times) if bias_times else None,
            "bias_over_target": sum(1 for v in bias_times if v > DAY_TARGETS["bias_seconds"]),
            "close_median": _median(close_times),
            "review_median_per_day": _median(review_seconds),
            "targets": DAY_TARGETS,
            "unmeasured_days": sum(1 for r in rows if r["bias_seconds"] is None),
        },
        "backlog": {
            "unreviewed_trades": trades - reviewed,
            "needing_grouping_review": sum(r["trades_needing_grouping"] for r in rows),
            "note": "A growing backlog is the earliest sign the evening review is too "
                    "long, well before anyone reports it as annoying.",
        },
        "manual_burden": {
            "manual_events": manual,
            "imported_events": imported,
            "manual_share": round(manual / (manual + imported), 3)
                            if (manual + imported) else None,
            "note": "Every manual event is a fill keyed by hand. This is the number an "
                    "export sample from TradeSea or TradeSyncer would move.",
        },
        "bias_capture_depth": {
            "with_invalidation_text": rate(lambda r: r["has_invalidation_text"]),
            "with_invalidation_level": rate(lambda r: r["has_invalidation_level"]),
            "amended_days": sum(1 for r in rows if r["bias_amendments"]),
            "note": "A numeric invalidation level is what one candidate outcome "
                    "methodology needs. No methodology is approved and no outcome is "
                    "assigned; this only measures whether the option stays open.",
        },
        "attachments": {
            "voice_notes": sum(r["voice_notes"] for r in rows),
            "media_assets": sum(r["media_assets"] for r in rows),
        },
    }


def real_use_review(conn) -> dict:
    """The first real-use checkpoint: 20 sessions or four weeks, whichever first.

    Purely descriptive. No relationship between any of these numbers and P&L is
    computed here, by design.
    """
    rows = [dict(r) for r in conn.execute("SELECT * FROM v_capture_quality ORDER BY session_date")]
    sessions = len(rows)
    weeks = len({r["session_date"][:4] + r["session_date"][5:7] + str(int(r["session_date"][8:10]) // 7)
                 for r in rows}) if rows else 0

    self_scores = [r["self_reported_process_index"] for r in rows
                   if r["self_reported_process_index"] is not None]
    mech_scores = [r["mechanical_conformance_index"] for r in rows
                   if r["mechanical_conformance_index"] is not None]

    bands = Counter()
    for r in rows:
        d = conformance.describe_disagreement(r["self_reported_process_index"],
                                              r["mechanical_conformance_index"])
        if d:
            bands[d["band"]] += 1

    def distribution(values):
        if not values:
            return None
        return {
            "n": len(values),
            "min": round(min(values), 1),
            "median": _median(values),
            "max": round(max(values), 1),
            "under_50": sum(1 for v in values if v < 50),
            "50_to_80": sum(1 for v in values if 50 <= v < 80),
            "80_plus": sum(1 for v in values if v >= 80),
        }

    missing = {
        "sessions_without_morning": sum(1 for r in rows if not r["has_pre"]),
        "sessions_without_evening": sum(1 for r in rows if not r["has_post"]),
        "sessions_without_conformance": sum(
            1 for r in rows if r["mechanical_conformance_index"] is None),
    }

    return {
        "checkpoint_reached": sessions >= 20 or weeks >= 4,
        "sessions": sessions,
        "friction": friction(conn),
        "self_reported_distribution": distribution(self_scores),
        "mechanical_distribution": distribution(mech_scores),
        "disagreement": dict(bands),
        "missing_data": missing,
        "note": "Descriptive only. No relationship between these measures and P&L has "
                "been computed, and none should be until a question is preregistered.",
    }
