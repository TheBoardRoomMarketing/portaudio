"""External source mapping, provenance and sync state.

Deliberately source-neutral. The TradeSyncer investigation prompted this, but
nothing here knows what TradeSyncer is — whichever route eventually wins (an
API, an export endpoint, a scheduled download, a hand-uploaded CSV), the same
three problems have to be solved, and solving them once means the route can
change without the journal changing.

    RAW SOURCE  →  NORMALIZATION  →  CANONICAL EXECUTION EVENT
                   (this module)      (trades.record_event)

The boundary matters more than it looks. If a vendor's field names reach the
schema, the schema belongs to that vendor, and swapping sources later becomes a
migration instead of a new adapter.

**No network client lives here.** No credential is stored, read or accepted by
any function in this module, and `source_sync_state` has no column that could
hold one. Connecting to anything is a separate decision that has not been made.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from .db import sha256_text, utcnow

# Bumped when normalisation changes shape. Stored on every row it produces, so a
# later parser change is traceable rather than retroactive.
ADAPTER_VERSION = "sources@1.0.0"

# Anything that looks like a secret must never reach the database or a log. This
# is belt-and-braces: no code path is supposed to pass one, and this is what
# catches the path nobody thought of.
# Compared after lowercasing and turning '-' into '_', so every entry here is
# written in that normalised form — "set_cookie", never "Set-Cookie".
SECRET_KEYS = ("password", "token", "access_token", "refresh_token", "cookie",
               "authorization", "auth", "api_key", "apikey", "secret", "session",
               "bearer", "credential", "credentials", "jwt", "set_cookie",
               "session_id", "sessionid", "x_api_key", "private_key")


class MappingError(RuntimeError):
    pass


class SecretLeak(RuntimeError):
    """Raised when a payload carrying something secret-shaped is about to be
    stored. Failing loudly beats writing a token into a journal that gets
    backed up, synced and kept for years."""


def assert_no_secrets(payload: dict, where: str = "payload") -> None:
    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).strip().lower().replace("-", "_") in SECRET_KEYS:
                    raise SecretLeak(
                        f"{where} contains a secret-shaped field at "
                        f"{'.'.join(path + [str(key)])}. Authentication material is "
                        "never stored or logged by this project.")
                walk(value, path + [str(key)])
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, path + [str(i)])

    walk(payload, [])


def payload_fingerprint(payload: dict) -> str:
    """A stable hash of the raw record, for provenance.

    Hashed rather than stored: the raw payload may carry account numbers and
    other identifiers that have no business being duplicated across a database,
    a backup and an export. The hash proves which bytes produced a row without
    keeping them.
    """
    assert_no_secrets(payload, "raw payload")
    return sha256_text(json.dumps(payload, sort_keys=True, default=str))


# --------------------------------------------------------------- account map
def map_account(conn, *, source: str, source_account_id: str, account_id: int,
                source_label: str = "", source_role: str = "UNKNOWN",
                active_from: Optional[str] = None) -> int:
    """Bind a source's account identifier to a journal account.

    `source_role` is what the source claims. It is recorded as evidence and does
    not overwrite `account.role` — when a copier's idea of who leads disagrees
    with the journal's, that disagreement is worth seeing rather than resolving
    silently in favour of whoever wrote last.
    """
    if source_role not in ("LEAD", "FOLLOWER", "UNKNOWN"):
        raise MappingError(f"unknown source role '{source_role}'")
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO source_account_map(source,source_account_id,source_label,account_id,"
        "source_role,active_from,created_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(source,source_account_id,active_from) DO UPDATE SET "
        "source_label=excluded.source_label,account_id=excluded.account_id,"
        "source_role=excluded.source_role",
        (source, str(source_account_id), source_label, account_id, source_role,
         active_from or now[:10], now))
    conn.commit()
    return cur.lastrowid


def resolve_account(conn, source: str, source_account_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT account_id FROM source_account_map WHERE source=? AND source_account_id=? "
        "AND active_to IS NULL ORDER BY active_from DESC LIMIT 1",
        (source, str(source_account_id))).fetchone()
    return row["account_id"] if row else None


def retire_mapping(conn, source: str, source_account_id: str) -> None:
    """Close a mapping without deleting it. An account Zack no longer trades
    still has to be interpretable in the days when he did."""
    conn.execute(
        "UPDATE source_account_map SET active_to=? WHERE source=? AND source_account_id=? "
        "AND active_to IS NULL", (utcnow()[:10], source, str(source_account_id)))
    conn.commit()


def unmapped(conn, source: str, seen_ids: List[str]) -> List[str]:
    """Source account IDs with no mapping. Reported, never guessed at.

    Guessing — by matching labels, say — is how a follower's fills end up
    attributed to the wrong prop account, which then silently corrupts copy
    quality for every trade on that day.
    """
    return sorted({str(i) for i in seen_ids if resolve_account(conn, source, i) is None})


def mapping_report(conn) -> List[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT m.source, m.source_account_id, m.source_label, m.source_role, "
        "a.label AS account_label, a.role AS journal_role, m.active_from, m.active_to "
        "FROM source_account_map m JOIN account a ON a.id=m.account_id "
        "ORDER BY m.source, m.source_account_id")]


def role_disagreements(conn) -> List[dict]:
    """Where the source's idea of lead/follower differs from the journal's."""
    return [dict(r) for r in conn.execute(
        "SELECT m.source, m.source_account_id, a.label AS account_label, "
        "m.source_role, a.role AS journal_role FROM source_account_map m "
        "JOIN account a ON a.id=m.account_id "
        "WHERE m.active_to IS NULL AND m.source_role != 'UNKNOWN' "
        "AND m.source_role != COALESCE(a.role,'UNKNOWN')")]


# --------------------------------------------------------------- sync state
def record_sync(conn, *, source: str, source_account_id: Optional[str] = None,
                status: str, last_seen_at: Optional[str] = None,
                cursor: Optional[str] = None, last_record_id: Optional[str] = None,
                records: int = 0, error: str = "") -> None:
    """Record the outcome of a sync attempt.

    The watermark only advances on success. A partial or failed run leaves it
    where it was, so the next attempt re-reads the overlap — which is safe
    precisely because import is idempotent, and is far safer than advancing past
    records that may never have arrived.
    """
    if status not in ("OK", "PARTIAL", "FAILED"):
        raise ValueError(f"unknown sync status '{status}'")
    now = utcnow()
    existing = conn.execute(
        "SELECT * FROM source_sync_state WHERE source=? AND source_account_id IS ?",
        (source, source_account_id)).fetchone()

    failures = 0 if status == "OK" else ((existing["consecutive_failures"] if existing else 0) + 1)
    keep_watermark = status != "OK"

    if existing:
        conn.execute(
            "UPDATE source_sync_state SET last_attempt_at=?, last_status=?, last_error=?, "
            "consecutive_failures=?, records_last_run=?, updated_at=?, "
            "last_success_at=CASE WHEN ?='OK' THEN ? ELSE last_success_at END, "
            "last_seen_at=CASE WHEN ? THEN last_seen_at ELSE COALESCE(?, last_seen_at) END, "
            "last_cursor=CASE WHEN ? THEN last_cursor ELSE COALESCE(?, last_cursor) END, "
            "last_record_id=CASE WHEN ? THEN last_record_id ELSE COALESCE(?, last_record_id) END "
            "WHERE id=?",
            (now, status, error[:500], failures, records, now, status, now,
             keep_watermark, last_seen_at, keep_watermark, cursor,
             keep_watermark, last_record_id, existing["id"]))
    else:
        conn.execute(
            "INSERT INTO source_sync_state(source,source_account_id,last_seen_at,last_cursor,"
            "last_record_id,last_attempt_at,last_success_at,last_status,last_error,"
            "consecutive_failures,records_last_run,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (source, source_account_id,
             last_seen_at if status == "OK" else None,
             cursor if status == "OK" else None,
             last_record_id if status == "OK" else None,
             now, now if status == "OK" else None, status, error[:500], failures,
             records, now))
    conn.commit()


def watermark(conn, source: str, source_account_id: Optional[str] = None) -> dict:
    """Where to resume from. Empty means read from the beginning of the window."""
    row = conn.execute(
        "SELECT last_seen_at, last_cursor, last_record_id FROM source_sync_state "
        "WHERE source=? AND source_account_id IS ?",
        (source, source_account_id)).fetchone()
    if not row:
        return {"last_seen_at": None, "last_cursor": None, "last_record_id": None}
    return dict(row)


def sync_health(conn) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM v_sync_health")]
    failing = [r for r in rows if r["health"] in ("failing", "last run failed")]
    return {
        "sources": rows,
        "failing": failing,
        "ok": not failing,
        # The point of surfacing this: a day with no trades and a day whose
        # trades never arrived look identical unless something says otherwise.
        "note": ("A failed sync is shown, never assumed away. Ingestion never blocks "
                 "trading — the copier and the execution path do not depend on this "
                 "journal being reachable."),
    }


# --------------------------------------------------------------- normalisation
REQUIRED = ("source_account_id", "symbol", "side", "quantity", "price", "occurred_at")


def normalize(records: List[dict], *, source: str, field_map: Dict[str, str],
              side_values: Optional[Dict[str, List[str]]] = None) -> dict:
    """Turn a source's records into canonical execution events.

    Pure: takes records, returns records, touches no database and opens no
    socket. That is what makes it testable against a redacted fixture the day
    one arrives, without any of the surrounding machinery existing yet.

    Event type is NOT set here. A single fill does not know whether it opened,
    added to, reduced or closed a position — only the sequence knows, and
    `adapters.classify_fills` derives it per account from position context.
    """
    side_values = side_values or {"BUY": ["Buy", "B", "BOT", "Long", "buy"],
                                  "SELL": ["Sell", "S", "SLD", "Short", "sell"]}
    accepted = {s.upper(): {v.lower() for v in vs} for s, vs in side_values.items()}

    events, problems, seen_accounts = [], [], set()

    for index, record in enumerate(records):
        assert_no_secrets(record, f"record {index + 1}")
        try:
            row = {canonical: record.get(external)
                   for canonical, external in field_map.items()}
            missing = [f for f in REQUIRED if row.get(f) in (None, "")]
            if missing:
                problems.append(f"record {index + 1}: missing {', '.join(missing)}")
                continue

            side = str(row["side"]).strip().lower()
            resolved = next((s for s, vs in accepted.items() if side in vs), None)
            if resolved is None:
                problems.append(f"record {index + 1}: unrecognised side {row['side']!r}")
                continue

            seen_accounts.add(str(row["source_account_id"]))
            events.append({
                "source": source,
                "source_account_id": str(row["source_account_id"]),
                "symbol": str(row["symbol"]).strip(),
                "side": resolved,
                "quantity": abs(float(row["quantity"])),
                "price": float(row["price"]),
                "occurred_at": str(row["occurred_at"]),
                "order_external_id": row.get("order_id") or None,
                "fill_external_id": row.get("fill_id") or None,
                "fees": (float(row["fees"]) if row.get("fees") not in (None, "") else None),
                # Provenance travels with the record from the first moment.
                "source_record_id": (str(row["record_id"])
                                     if row.get("record_id") not in (None, "") else None),
                "source_payload_sha256": payload_fingerprint(record),
                "adapter_version": ADAPTER_VERSION,
            })
        except (TypeError, ValueError) as exc:
            problems.append(f"record {index + 1}: {exc}")

    return {
        "events": events,
        "problems": problems,
        "accounts_seen": sorted(seen_accounts),
        "adapter_version": ADAPTER_VERSION,
        # A partial parse is reported, never quietly returned as success: half a
        # day's fills reconstructs a position path that never existed.
        "complete": not problems,
    }
