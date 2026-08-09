"""Execution source adapters.

The journal's schema must not learn the shape of any one vendor. Everything
that ingests executions produces the same thing — a list of normalised
`ExecutionEvent` dicts — and `trades.record_event` handles identity and
idempotency for all of them.

    ExecutionSourceAdapter
        ├── ManualAdapter        implemented — a human keying a trade
        ├── CsvAdapter           implemented — any export, via a mapping profile
        ├── TradeSeaAdapter      blocked — needs a real export sample
        └── TradeSyncerAdapter   blocked — needs a real export sample

The two blocked adapters are declared, not faked. Each states exactly what it
needs, and `parse` raises rather than returning invented events, because an
invented fill is indistinguishable from a real one once it is in the database.

Investigation notes, with evidence, are in
docs/AUTOMATION-REALITY.md — read that before assuming any of these can be
switched on.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol
from zoneinfo import ZoneInfo

from . import trades
from .contracts import NotImplementedContract

# Adapter readiness. Mirrors journal.contracts vocabulary.
IMPLEMENTED = "IMPLEMENTED"
BLOCKED_ON_SAMPLE = "BLOCKED_ON_SAMPLE"
BLOCKED_ON_ACCESS = "BLOCKED_ON_ACCESS"


class ExecutionSourceAdapter(Protocol):
    """Turns one vendor's evidence into normalised execution events.

    An adapter never writes. It parses, and the caller decides what to store —
    which keeps parsing testable against a file and keeps the write path single.
    """

    name: str
    status: str

    def parse(self, path: Path) -> List[dict]: ...


class AdapterError(RuntimeError):
    pass


def _event(*, account_ref: str, symbol: str, event_type: str, occurred_at: str,
           quantity=None, price=None, side=None, stop_price=None, target_price=None,
           order_external_id=None, fill_external_id=None, source: str) -> dict:
    return {
        "account_ref": account_ref, "symbol": symbol, "event_type": event_type,
        "occurred_at": occurred_at, "quantity": quantity, "price": price, "side": side,
        "stop_price": stop_price, "target_price": target_price,
        "order_external_id": order_external_id, "fill_external_id": fill_external_id,
        "source": source,
    }


# --------------------------------------------------------------------- manual
class ManualAdapter:
    """A human keying what they did. Always available, always the fallback."""

    name = "manual"
    status = IMPLEMENTED
    needs = "nothing — this is the path that always works"
    evidence = ("Implemented and tested. Whatever else is blocked, a trade can "
                "always be keyed by hand and is recorded as such.")

    def parse(self, payload: dict) -> List[dict]:
        events = []
        for raw in payload.get("events", []):
            events.append(_event(
                account_ref=payload.get("account_ref") or raw.get("account_ref"),
                symbol=payload["symbol"],
                event_type=raw["event_type"],
                occurred_at=raw["occurred_at"],
                quantity=raw.get("quantity"),
                price=raw.get("price"),
                side=raw.get("side"),
                stop_price=raw.get("stop_price"),
                target_price=raw.get("target_price"),
                source="manual"))
        return events


# --------------------------------------------------------------------- csv
class CsvAdapter:
    """Any tabular export, described by a stored mapping profile.

    This is the workhorse. A new venue is a new profile row, not new code, and
    the profile is not marked verified until it has parsed a real file.
    """

    name = "csv"
    status = IMPLEMENTED

    # A row must carry at least these to be a usable execution event.
    REQUIRED = ("account", "symbol", "side", "quantity", "price", "occurred_at")

    def __init__(self, profile: dict):
        self.profile = profile
        missing = [f for f in self.REQUIRED if f not in profile["column_map"]]
        if missing:
            raise AdapterError(f"mapping profile is missing: {', '.join(missing)}")

    def _time(self, value: str) -> str:
        fmt = self.profile.get("datetime_format")
        tz = self.profile.get("source_tz", "America/New_York")
        dt = datetime.strptime(value.strip(), fmt) if fmt else \
            datetime.fromisoformat(value.strip().replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _side(self, value: str) -> str:
        mapping = self.profile.get("side_values") or {
            "BUY": ["Buy", "B", "BOT", "Long"], "SELL": ["Sell", "S", "SLD", "Short"]}
        v = (value or "").strip().lower()
        for side, accepted in mapping.items():
            if v in {a.lower() for a in accepted}:
                return side.upper()
        raise AdapterError(f"unrecognised side '{value}'")

    def parse(self, path: Path) -> List[dict]:
        cmap = self.profile["column_map"]
        symbol_map = self.profile.get("symbol_map") or {}
        events, problems = [], []

        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            for field, column in cmap.items():
                if column not in headers:
                    problems.append(f"column '{column}' (for {field}) is not in the file")
            if problems:
                raise AdapterError("; ".join(problems))

            for line, row in enumerate(reader, start=2):
                try:
                    symbol = row[cmap["symbol"]].strip()
                    events.append(_event(
                        account_ref=row[cmap["account"]].strip(),
                        symbol=symbol_map.get(symbol, symbol),
                        # Event type is derived from position context at import
                        # time, not guessed per row: a lone fill cannot know
                        # whether it opened, added to or closed a position.
                        event_type="FILL",
                        occurred_at=self._time(row[cmap["occurred_at"]]),
                        quantity=abs(float(row[cmap["quantity"]])),
                        price=float(row[cmap["price"]]),
                        side=self._side(row[cmap["side"]]),
                        order_external_id=(row.get(cmap["order_id"]).strip()
                                           if cmap.get("order_id") else None),
                        fill_external_id=(row.get(cmap["fill_id"]).strip()
                                          if cmap.get("fill_id") else None),
                        source="csv_import"))
                except (KeyError, ValueError, AdapterError) as exc:
                    problems.append(f"line {line}: {exc}")

        if problems:
            raise AdapterError("; ".join(problems[:5]))
        return events


# --------------------------------------------------------------------- blocked
class _BlockedAdapter:
    """Declared, interface-complete, and deliberately non-functional."""

    name = "blocked"
    status = BLOCKED_ON_SAMPLE
    needs = "a real export sample"
    evidence = ""

    def parse(self, path):
        raise NotImplementedContract(
            f"{self.name} adapter is {self.status}: needs {self.needs}. "
            "It will not guess a format — a wrong parse produces fills that look real.")


class TradeSeaAdapter(_BlockedAdapter):
    name = "tradesea"
    status = BLOCKED_ON_SAMPLE
    needs = (
        "ONE redacted export of lead-account execution history. Ideally it contains "
        "several futures trades including at least one scale-in and at least one "
        "partial scale-out, with timestamps, instrument, side, quantity, fill price, "
        "order/fill IDs if present, and fees if present. Redact account number, legal "
        "name, email, credentials and any private identifier — keep the headers and "
        "the row structure exactly as exported. A small file is enough; what is being "
        "mapped is the schema, not the performance."
    )
    evidence = (
        "TradeSea is a Rithmic-based web and mobile platform with its own analytics "
        "and journal component. No public third-party API is documented. The realistic "
        "route is an export from its journal, mapped through CsvAdapter; the alternative "
        "is Rithmic-level access, which needs a licence and prop-firm permission."
    )


class TradeSyncerAdapter(_BlockedAdapter):
    name = "tradesyncer"
    status = BLOCKED_ON_SAMPLE
    needs = (
        "an answer to ONE question, which no public documentation settles: in the "
        "TradeSyncer dashboard, does any export or download produce rows that carry "
        "BOTH a fill (timestamp, instrument, side, quantity, price) AND which "
        "follower account it happened on? If yes, one redacted sample maps it. If no, "
        "follower fills have to come from each prop dashboard or broker statement "
        "instead, and FOLLOWER_EXECUTION_SOURCE is INCOMPLETE until they do."
    )
    evidence = (
        "TradeSyncer is a cloud copier connecting brokers including Tradovate, Rithmic, "
        "NinjaTrader, TradingView and ProjectX, and ships a journal that accepts an "
        "uploaded trade history. Its documented inputs are leader connections; no public "
        "API for reading copy events outward is documented. What matters for this journal "
        "is whether follower fills can be exported per account — that is the open question."
    )


ADAPTERS = {
    "manual": ManualAdapter(),
    "tradesea": TradeSeaAdapter(),
    "tradesyncer": TradeSyncerAdapter(),
}


def status_report() -> List[dict]:
    report = [{"name": "csv", "status": IMPLEMENTED,
               "needs": "a mapping profile per venue",
               "evidence": "Implemented and tested against a synthetic export."}]
    for adapter in ADAPTERS.values():
        report.append({"name": adapter.name, "status": adapter.status,
                       "needs": getattr(adapter, "needs", ""),
                       "evidence": getattr(adapter, "evidence", "")})
    return report


# --------------------------------------------------------------------- ingest
def classify_fills(rows: Iterable[dict]) -> List[dict]:
    """Turn raw fills into OPEN / ADD / REDUCE / CLOSE.

    A fill on its own does not know what it did to the position; only the
    sequence does. This runs per account and instrument, which is why it lives
    here rather than in any one adapter.
    """
    ordered = sorted(rows, key=lambda r: (r["account_ref"], r["symbol"], r["occurred_at"]))
    position: Dict[tuple, float] = {}
    out = []

    for row in ordered:
        key = (row["account_ref"], row["symbol"])
        held = position.get(key, 0.0)
        signed = row["quantity"] * (1 if row["side"] == "BUY" else -1)
        after = held + signed

        if abs(held) < 1e-9:
            event_type = "OPEN"
        elif (held > 0) == (signed > 0):
            event_type = "ADD"
        elif abs(after) < 1e-9:
            event_type = "CLOSE"
        elif (held > 0) != (after > 0):
            # Flipped through flat in one fill: two decisions in one execution.
            # Recorded as a close, and the remainder opens a new position.
            event_type = "CLOSE"
        else:
            event_type = "REDUCE"

        position[key] = after
        out.append({**row, "event_type": event_type, "position_after": round(after, 6)})
    return out


def ingest(conn, events: List[dict], *, account_ids: Dict[str, int],
           instrument_ids: Dict[str, int], is_demo: bool = False) -> dict:
    """Write normalised events. Idempotent, because record_event is."""
    written, skipped, unknown = 0, 0, []
    for event in classify_fills([e for e in events if e.get("event_type") in (None, "FILL")]) \
            + [e for e in events if e.get("event_type") not in (None, "FILL")]:
        account_id = account_ids.get(event["account_ref"])
        instrument_id = instrument_ids.get(event["symbol"])
        if account_id is None or instrument_id is None:
            unknown.append(f"{event['account_ref']} / {event['symbol']}")
            continue
        result = trades.record_event(
            conn, account_id=account_id, instrument_id=instrument_id,
            event_type=event["event_type"], occurred_at=event["occurred_at"],
            quantity=event.get("quantity"), price=event.get("price"),
            side=event.get("side"), stop_price=event.get("stop_price"),
            target_price=event.get("target_price"),
            order_external_id=event.get("order_external_id"),
            fill_external_id=event.get("fill_external_id"),
            source=event.get("source", "csv_import"), is_demo=is_demo)
        if result is None:
            skipped += 1
        else:
            written += 1
    conn.commit()
    return {"events_written": written, "already_known": skipped,
            "unmapped": sorted(set(unknown))}
