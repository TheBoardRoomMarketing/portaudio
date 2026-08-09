"""Keeping invented data out of the real record.

Demo rows live in the same database as real ones, marked with `is_demo`. That is
a deliberate choice — a separate database would mean two schemas to migrate and
no way to prove, in one query, that nothing leaked. But it puts the burden here:
every read that feeds a count, a statistic or a review must exclude demo rows
unless demo mode was explicitly asked for.

The rule this module enforces:

    A production database contains no demo records unless someone asked for
    them by name, and the interface says so on screen when they are present.

`assert_clean` is the gate to run before real capture begins, and there is a
regression test for it. The failure it guards against is quiet and permanent:
synthetic sessions averaged into a real distribution cannot be separated out
afterwards by looking at the numbers.
"""

from __future__ import annotations

from typing import Dict, List

# Every table that carries an is_demo flag. Adding a table without adding it
# here is the mistake this list exists to make visible.
DEMO_MARKED_TABLES = (
    "trading_day", "daily_bias", "logical_trade", "execution_event",
    "account_execution", "account", "account_snapshot", "setup",
)


class DemoContaminationError(RuntimeError):
    """Raised when demo rows are present where only real data belongs."""


def counts(conn) -> Dict[str, int]:
    out = {}
    for table in DEMO_MARKED_TABLES:
        try:
            out[table] = conn.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE is_demo = 1").fetchone()["c"]
        except Exception:  # noqa: BLE001 — a table may predate the flag
            out[table] = 0
    return out


def present(conn) -> bool:
    return any(counts(conn).values())


def real_counts(conn) -> Dict[str, int]:
    out = {}
    for table in DEMO_MARKED_TABLES:
        try:
            out[table] = conn.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE is_demo = 0").fetchone()["c"]
        except Exception:  # noqa: BLE001
            out[table] = 0
    return out


def status(conn) -> dict:
    demo = counts(conn)
    real = real_counts(conn)
    total_demo = sum(demo.values())
    return {
        "demo_rows": total_demo,
        "demo_by_table": {k: v for k, v in demo.items() if v},
        "real_rows": sum(real.values()),
        "real_by_table": {k: v for k, v in real.items() if v},
        "clean_for_real_use": total_demo == 0,
        "mixed": total_demo > 0 and sum(real.values()) > 0,
    }


def assert_clean(conn) -> None:
    """Refuse to proceed if demo rows are present. Run before real capture."""
    state = status(conn)
    if not state["clean_for_real_use"]:
        raise DemoContaminationError(
            f"{state['demo_rows']} demo rows present in "
            f"{', '.join(state['demo_by_table'])}. Run `journal demo --purge` before "
            "recording real sessions, or accept that real and synthetic data are mixed."
        )


def purge(conn) -> dict:
    """Remove every demo row, leaving real data untouched.

    Ordered so children go before parents. Execution events are append-only by
    trigger, so the trigger is lifted for the length of this operation and
    restored immediately — a purge is the one legitimate deletion, and it only
    ever touches rows that were never real.
    """
    removed: Dict[str, int] = {}

    conn.execute("DROP TRIGGER IF EXISTS execution_event_no_delete")
    try:
        for table in ("account_execution", "execution_event", "daily_bias",
                      "logical_trade", "trading_day", "account_snapshot", "setup"):
            before = conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE is_demo=1"
                                  ).fetchone()["c"]
            if before:
                conn.execute(f"DELETE FROM {table} WHERE is_demo = 1")
                removed[table] = before

        # Accounts last: execution events reference them.
        orphaned = conn.execute(
            "SELECT COUNT(*) c FROM account WHERE is_demo=1 AND id NOT IN "
            "(SELECT DISTINCT account_id FROM execution_event)").fetchone()["c"]
        if orphaned:
            conn.execute(
                "DELETE FROM account WHERE is_demo=1 AND id NOT IN "
                "(SELECT DISTINCT account_id FROM execution_event)")
            removed["account"] = orphaned
    finally:
        conn.execute("""
            CREATE TRIGGER execution_event_no_delete BEFORE DELETE ON execution_event BEGIN
                SELECT RAISE(ABORT, 'execution events may not be deleted');
            END;""")
    conn.commit()
    return {"removed": removed, "now": status(conn)}


def scope(is_demo: bool = False) -> str:
    """SQL fragment for scoping a query to real or demo rows.

    Used so the intent is written at every call site rather than assumed.
    """
    return f"is_demo = {1 if is_demo else 0}"


def guard_report(conn) -> List[str]:
    """Problems worth surfacing before real use, in plain words."""
    state = status(conn)
    problems = []
    if state["mixed"]:
        problems.append(
            f"{state['demo_rows']} synthetic rows sit alongside {state['real_rows']} real "
            "rows. Any count that does not filter on is_demo will mix them.")
    elif state["demo_rows"]:
        problems.append(
            f"{state['demo_rows']} synthetic rows present. Fine for looking around; "
            "purge before recording a real session.")
    return problems
