"""File import — read only, always.

The journal never holds a credential that can place an order. A broker export
is a file; a file cannot route. This module reads files.

The generic path is a CSV mapping profile: a stored description of one broker's
export columns. A new broker means a new profile row, not new journal code, and
a profile is not marked `verified_against_sample` until it has actually parsed a
real export.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from . import repo
from .db import sha256_file, sha256_text, utcnow

# Fields a profile may map. Only the first five are required for a usable fill.
MAPPABLE = (
    "exec_id", "order_id", "symbol", "side", "quantity", "price", "filled_at",
    "leg", "commission", "exchange_fees", "intended_price", "account",
)
REQUIRED = ("exec_id", "symbol", "side", "quantity", "price", "filled_at")


class ImportError_(Exception):
    pass


class DuplicateImport(ImportError_):
    """The same file content has already been imported."""


def save_profile(conn, name: str, column_map: Dict[str, str], *,
                 datetime_format: Optional[str] = None,
                 source_tz: str = "America/New_York",
                 side_values: Optional[dict] = None,
                 symbol_map: Optional[dict] = None,
                 fee_columns: Optional[List[str]] = None,
                 notes: str = "") -> int:
    missing = [f for f in REQUIRED if f not in column_map]
    if missing:
        raise ImportError_(f"profile is missing required mappings: {', '.join(missing)}")

    existing = conn.execute("SELECT id FROM import_profile WHERE name=?", (name,)).fetchone()
    payload = (
        json.dumps(column_map), datetime_format, source_tz,
        json.dumps(side_values or {"buy": ["Buy", "B", "BOT"], "sell": ["Sell", "S", "SLD"]}),
        json.dumps(symbol_map or {}), json.dumps(fee_columns or []), notes,
    )
    if existing:
        conn.execute(
            "UPDATE import_profile SET column_map=?, datetime_format=?, source_tz=?,"
            " side_values=?, symbol_map=?, fee_columns=?, notes=? WHERE id=?",
            payload + (existing["id"],))
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO import_profile(name,kind,column_map,datetime_format,source_tz,side_values,"
        "symbol_map,fee_columns,notes,created_at) VALUES (?,'csv',?,?,?,?,?,?,?,?)",
        (name,) + payload + (utcnow(),))
    return cur.lastrowid


def get_profile(conn, name: str) -> dict:
    row = conn.execute("SELECT * FROM import_profile WHERE name=?", (name,)).fetchone()
    if not row:
        raise ImportError_(f"no import profile named '{name}'")
    p = dict(row)
    for key in ("column_map", "side_values", "symbol_map", "fee_columns"):
        p[key] = json.loads(p[key]) if p[key] else ({} if key != "fee_columns" else [])
    return p


def _coerce_time(value: str, fmt: Optional[str], tz: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ImportError_("empty timestamp")
    dt = datetime.strptime(value, fmt) if fmt else datetime.fromisoformat(value.replace("Z", ""))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_side(value: str, side_values: dict) -> str:
    v = (value or "").strip().lower()
    for side, accepted in side_values.items():
        if v in {a.lower() for a in accepted}:
            return side
    raise ImportError_(f"unrecognised side value '{value}'")


def parse_file(conn, path: Path, profile_name: str) -> dict:
    """Parse without writing. Always run before importing an unfamiliar export."""
    profile = get_profile(conn, profile_name)
    cmap, path = profile["column_map"], Path(path)
    rows, problems = [], []

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        for field, column in cmap.items():
            if column not in headers:
                problems.append(f"column '{column}' (mapped to {field}) is not in the file")
        if problems:
            return {"ok": False, "problems": problems, "headers": headers, "rows": []}

        for n, raw in enumerate(reader, start=2):
            try:
                symbol = raw[cmap["symbol"]].strip()
                rows.append({
                    "exec_id": raw[cmap["exec_id"]].strip(),
                    "order_id": raw.get(cmap.get("order_id", ""), "").strip() or None,
                    "symbol": profile["symbol_map"].get(symbol, symbol),
                    "side": _coerce_side(raw[cmap["side"]], profile["side_values"]),
                    "quantity": abs(float(raw[cmap["quantity"]])),
                    "price": float(raw[cmap["price"]]),
                    "filled_at": _coerce_time(raw[cmap["filled_at"]],
                                              profile["datetime_format"], profile["source_tz"]),
                    "leg": (raw.get(cmap.get("leg", ""), "") or "").strip() or None,
                    "commission": float(raw.get(cmap.get("commission", ""), 0) or 0),
                    "exchange_fees": float(raw.get(cmap.get("exchange_fees", ""), 0) or 0),
                    "intended_price": (float(raw[cmap["intended_price"]])
                                       if cmap.get("intended_price")
                                       and raw.get(cmap["intended_price"]) else None),
                    "_line": n,
                })
            except (KeyError, ValueError, ImportError_) as exc:
                problems.append(f"line {n}: {exc}")

    return {"ok": not problems, "problems": problems, "rows": rows,
            "row_count": len(rows), "profile": profile_name,
            "content_sha256": sha256_file(path)}


def import_file(conn, path: Path, profile_name: str, *, account_label: str,
                session_kind: str = "NY_AM", strict: bool = True) -> dict:
    """Import a broker export into RAW, then group fills into trades.

    Re-importing identical content is refused by the RAW layer's uniqueness
    constraint rather than silently producing duplicate trades.
    """
    path = Path(path)
    parsed = parse_file(conn, path, profile_name)
    if strict and not parsed["ok"]:
        raise ImportError_("; ".join(parsed["problems"][:5]))
    if not parsed["rows"]:
        raise ImportError_("no usable rows")

    digest = parsed["content_sha256"]
    already = conn.execute(
        "SELECT id, imported_at FROM raw_import_batch WHERE source=? AND content_sha256=?",
        ("file_import", digest)).fetchone()
    if already:
        raise DuplicateImport(
            f"this file was already imported as batch {already['id']} "
            f"on {already['imported_at']}")

    cur = conn.execute(
        "INSERT INTO raw_import_batch(source,source_kind,source_uri,content_sha256,imported_at,"
        "row_count,importer,status,notes) VALUES ('file_import','broker',?,?,?,?,?,'ok',?)",
        (str(path), digest, utcnow(), len(parsed["rows"]),
         f"csv_profile:{profile_name}@1.0.0", f"profile {profile_name}"))
    batch_id = cur.lastrowid

    account_id = repo.ensure_account(conn, account_label)
    tz = conn.execute("SELECT tz FROM account WHERE id=?", (account_id,)).fetchone()["tz"]

    # Group fills into round-turn trades per symbol, closing a trade when the
    # running position returns to flat. Anything left open stays open.
    by_symbol: Dict[str, List[dict]] = {}
    for row in sorted(parsed["rows"], key=lambda r: r["filled_at"]):
        by_symbol.setdefault(row["symbol"], []).append(row)

    created, skipped = [], []
    for symbol, fills in by_symbol.items():
        position = 0.0
        current: List[dict] = []
        for fill in fills:
            signed = fill["quantity"] * (1 if fill["side"] == "buy" else -1)
            opening = position == 0
            position += signed
            fill = {**fill, "leg": fill["leg"] or ("entry" if opening else
                                                   ("exit" if abs(position) < 1e-9 else "partial"))}
            current.append(fill)
            if abs(position) < 1e-9 and current:
                try:
                    created.append(_write_trade(conn, current, account_id, tz, batch_id,
                                                session_kind, symbol))
                except sqlite3.IntegrityError as exc:
                    skipped.append(f"{symbol} {current[0]['exec_id']}: {exc}")
                current = []
        if current:
            skipped.append(f"{symbol}: {len(current)} fills left an open position, not imported")

    conn.commit()
    return {"batch_id": batch_id, "trades_created": len(created), "trade_ids": created,
            "rows": len(parsed["rows"]), "skipped": skipped,
            "warnings": [] if parsed["ok"] else parsed["problems"]}


def _write_trade(conn, fills: List[dict], account_id: int, tz: str, batch_id: int,
                 session_kind: str, symbol: str) -> int:
    first = fills[0]
    local_open = repo.utc_to_local(first["filled_at"], tz)
    session_id = repo.get_or_create_session(
        conn, local_open.strftime("%Y-%m-%d"), account_id=account_id,
        session_kind=session_kind, tz=tz)

    side = "long" if first["side"] == "buy" else "short"
    entry_qty = sum(f["quantity"] for f in fills if f["leg"] in ("entry", "scale_in"))
    avg_entry = (sum(f["price"] * f["quantity"] for f in fills if f["leg"] in ("entry", "scale_in"))
                 / entry_qty) if entry_qty else first["price"]
    exits = [f for f in fills if f["leg"] not in ("entry", "scale_in")]
    exit_qty = sum(f["quantity"] for f in exits)
    avg_exit = (sum(f["price"] * f["quantity"] for f in exits) / exit_qty) if exit_qty else None

    payload = {
        "symbol": symbol,
        "side": side,
        "quantity": entry_qty or first["quantity"],
        "planned_quantity": entry_qty or first["quantity"],
        "entry_at": repo.utc_to_local(first["filled_at"], tz).strftime("%Y-%m-%dT%H:%M:%S"),
        "exit_at": (repo.utc_to_local(exits[-1]["filled_at"], tz).strftime("%Y-%m-%dT%H:%M:%S")
                    if exits else None),
        "avg_entry_price": round(avg_entry, 4),
        "avg_exit_price": round(avg_exit, 4) if avg_exit is not None else None,
        "execution_mode": "manual",
        "exit_reason": "manual" if exits else None,
        # Imported fills keep their broker identifiers, so a re-import collides
        # on exec_id rather than creating a second copy of the same trade.
        "fills": [{"leg": f["leg"], "price": f["price"], "quantity": f["quantity"],
                   "filled_at": f["filled_at"], "exec_id": f["exec_id"],
                   "order_id": f["order_id"], "intended_price": f["intended_price"],
                   "commission": f["commission"], "exchange_fees": f["exchange_fees"]}
                  for f in fills],
    }
    return repo.record_trade(conn, session_id, payload,
                             entry_source="file_import", batch_id=batch_id)


# A starting profile shaped like a typical futures execution export. It is
# deliberately NOT marked verified: no real sample has been seen.
EXAMPLE_PROFILE = {
    "name": "generic_futures_csv",
    "column_map": {
        "exec_id": "Execution ID",
        "order_id": "Order ID",
        "symbol": "Symbol",
        "side": "Side",
        "quantity": "Quantity",
        "price": "Price",
        "filled_at": "Fill Time",
        "commission": "Commission",
        "exchange_fees": "Fees",
    },
    "datetime_format": "%Y-%m-%d %H:%M:%S",
    "notes": "Starting point only. Remap against a real export before use; "
             "no broker-specific assumption here has been checked against a sample.",
}
