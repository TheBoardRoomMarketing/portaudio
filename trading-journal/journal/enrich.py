"""Blinded research enrichment — storage only.

This is the path by which research features begin accumulating from the first
real session. It writes and never returns a value to any caller in the daily
application, because nothing in the daily application is permitted to read one.

The engine itself is not implemented here and will not be. It lives in its own
project, and is loaded by dotted path at run time:

    journal enrich-blinded --engine mypackage.astro:Engine --from 2026-08-01

An engine must satisfy `journal.contracts.BlindedEnricher`:

    feature_set     str   which family the values belong to
    engine_version  str   bumped whenever the calculation changes
    compute(session_uid, date) -> dict[str, float]

Two rules the loader enforces rather than trusts:

  * feature keys must be opaque. A key that reads as an interpretation
    ('money_house_favourable', 'lunar_good') is refused, because the whole
    point of the blind is that a leaked value means nothing without the
    separately-held dictionary.
  * every stored row carries session, engine, engine hash, source hypothesis
    and computation time, so values computed under different revisions are
    never silently pooled.
"""

from __future__ import annotations

import importlib
import inspect
import re
from typing import Dict, List, Optional

from .db import sha256_text, utcnow

# An opaque key is a short prefix and a number: pa_f01, ma_f17, ln_f03.
OPAQUE_KEY = re.compile(r"^[a-z]{2,4}_[a-z]?\d{1,3}$")

# Words that must never appear in a feature key. The list is not a security
# boundary — it is a tripwire against the natural drift toward readable names.
INTERPRETIVE_WORDS = (
    "good", "bad", "favour", "favor", "bull", "bear", "lucky", "strong", "weak",
    "money", "moon", "lunar", "transit", "fortune", "pof", "bradley", "aspect",
)


class EnrichmentError(RuntimeError):
    pass


def load_engine(spec: str):
    """Load `package.module:Attribute` and check it satisfies the protocol."""
    if ":" not in spec:
        raise EnrichmentError("engine must be given as 'package.module:Attribute'")
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise EnrichmentError(f"could not import {module_name}: {exc}") from exc
    if not hasattr(module, attr):
        raise EnrichmentError(f"{module_name} has no attribute {attr}")

    engine = getattr(module, attr)
    if inspect.isclass(engine):
        engine = engine()

    for required in ("feature_set", "engine_version", "compute"):
        if not hasattr(engine, required):
            raise EnrichmentError(f"engine is missing '{required}'")
    if not callable(engine.compute):
        raise EnrichmentError("engine.compute must be callable")
    return engine


def _engine_hash(engine) -> str:
    """Identify the engine's code, not just its declared version.

    A version string that someone forgot to bump is the classic way two
    different calculations end up pooled as one variable.
    """
    try:
        source = inspect.getsource(type(engine))
    except (OSError, TypeError):
        source = f"{type(engine).__module__}.{type(engine).__name__}"
    return sha256_text(f"{engine.engine_version}|{source}")


def validate_keys(features: Dict[str, object]) -> None:
    for key in features:
        lowered = key.lower()
        if not OPAQUE_KEY.match(lowered):
            raise EnrichmentError(
                f"feature key '{key}' is not opaque. Keys must look like 'pa_f01'; "
                "human-readable names belong in blinded_feature_dict, which the daily "
                "application cannot read.")
        for word in INTERPRETIVE_WORDS:
            if word in lowered:
                raise EnrichmentError(
                    f"feature key '{key}' contains the interpretive word '{word}'")


def enrich(conn, engine, *, since: Optional[str] = None, until: Optional[str] = None,
           source_hypothesis: str = "unspecified", overwrite: bool = False) -> dict:
    """Compute and store blinded features for sessions in a date range.

    Requires an unrestricted connection: the daily one is refused access to the
    blinded layer entirely, which is what makes this a separate operation.
    """
    clauses, params = [], []
    if since:
        clauses.append("session_date >= ?")
        params.append(since)
    if until:
        clauses.append("session_date <= ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sessions = conn.execute(
        f"SELECT id, session_uid, session_date FROM session{where} ORDER BY session_date",
        params).fetchall()

    engine_hash = _engine_hash(engine)
    written = 0
    skipped = 0
    failures: List[str] = []

    for session in sessions:
        if not overwrite:
            existing = conn.execute(
                "SELECT COUNT(*) c FROM blinded_feature WHERE session_id=? AND feature_set=? "
                "AND engine_version=?",
                (session["id"], engine.feature_set, engine.engine_version)).fetchone()["c"]
            if existing:
                skipped += 1
                continue
        try:
            features = engine.compute(session["session_uid"], session["session_date"])
        except Exception as exc:  # noqa: BLE001 — one bad session must not stop the run
            failures.append(f"{session['session_date']}: {exc}")
            continue
        if not features:
            continue
        validate_keys(features)

        computed_at = utcnow()
        for key, value in features.items():
            numeric = (value if isinstance(value, (int, float))
                       and not isinstance(value, bool) else None)
            text = None if numeric is not None else (None if value is None else str(value))
            conn.execute(
                "INSERT OR REPLACE INTO blinded_feature(scope_type,scope_id,session_id,"
                "feature_set,feature_key,value_num,value_text,engine,engine_version,"
                "engine_hash,source_hypothesis,computed_at)"
                " VALUES ('session',?,?,?,?,?,?,?,?,?,?,?)",
                (session["session_uid"], session["id"], engine.feature_set, key,
                 numeric, text, type(engine).__name__, engine.engine_version,
                 engine_hash, source_hypothesis, computed_at))
            written += 1

    conn.commit()
    return {
        "feature_set": engine.feature_set,
        "engine": type(engine).__name__,
        "engine_version": engine.engine_version,
        "engine_hash": engine_hash[:16],
        "source_hypothesis": source_hypothesis,
        "sessions_considered": len(sessions),
        "rows_written": written,
        "sessions_skipped_already_enriched": skipped,
        "failures": failures,
        "note": "Values are stored and unreadable from every daily view. "
                "No analysis has been performed.",
    }


def coverage(conn) -> List[dict]:
    """Which sessions have which feature sets. Counts only — never values."""
    return [dict(r) for r in conn.execute(
        "SELECT feature_set, engine_version, COUNT(DISTINCT session_id) sessions, "
        "COUNT(*) rows_stored, MIN(computed_at) first_computed, MAX(computed_at) last_computed "
        "FROM blinded_feature GROUP BY feature_set, engine_version")]
