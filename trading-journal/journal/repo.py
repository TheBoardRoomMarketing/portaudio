"""Read and write operations.

Every write in this module preserves provenance. Nothing keyed by hand is ever
recorded in a way that makes it look imported, no check-in is overwritten
without leaving the previous value behind, and no model output becomes a label
without an explicit human confirmation.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from . import config
from .db import sha256_text, utcnow

# --------------------------------------------------------------------- time
# Stored timestamps are always UTC with a Z suffix. Local wall clock is a
# presentation concern resolved through the session's tz, never assumed.


def local_to_utc(local: str, tz: str) -> str:
    """'2026-08-07T10:04' (wall clock in tz) -> '2026-08-07T14:04:00Z'."""
    naive = datetime.fromisoformat(local.replace("Z", "").strip())
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=ZoneInfo(tz))
    return naive.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_to_local(stamp: Optional[str], tz: str) -> Optional[datetime]:
    if not stamp:
        return None
    dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(tz))


def iso_week_of(date_str: str) -> str:
    y, w, _ = datetime.strptime(date_str, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def _rows(conn, sql, *args) -> List[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _one(conn, sql, *args) -> Optional[dict]:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------- registry
def ensure_account(conn, label: str, broker: str = "unspecified",
                   mode: str = "paper", tz: str = config.DEFAULT_TZ) -> int:
    row = conn.execute("SELECT id FROM account WHERE label=?", (label,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO account(label,broker,mode,currency,tz,active) VALUES (?,?,?,'USD',?,1)",
        (label, broker, mode, tz),
    )
    return cur.lastrowid


def ensure_instrument(conn, symbol: str, *, name: str = "", asset_class: str = "future",
                      exchange: str = "", tick_size: float = 0.25,
                      tick_value: float = 1.25, point_value: float = 5.0) -> int:
    row = conn.execute("SELECT id FROM instrument WHERE symbol=?", (symbol,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO instrument(symbol,name,asset_class,exchange,tick_size,tick_value,"
        "point_value,currency) VALUES (?,?,?,?,?,?,?,'USD')",
        (symbol, name or symbol, asset_class, exchange, tick_size, tick_value, point_value),
    )
    return cur.lastrowid


def ensure_strategy(conn, strategy_id: str, name: str, family: str = "",
                    description: str = "") -> str:
    row = conn.execute("SELECT id FROM strategy WHERE id=?", (strategy_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO strategy(id,name,family,description,created_at) VALUES (?,?,?,?,?)",
            (strategy_id, name, family, description, utcnow()),
        )
    return strategy_id


def publish_strategy_version(conn, strategy_id: str, version: str, rules: dict, *,
                             automation_level: str = "manual",
                             qualification_status: str = "research",
                             reference_impl: Optional[str] = None,
                             active_from: Optional[str] = None) -> int:
    """Publish an immutable strategy version.

    Editing rules is not possible by design — a change is a new version, and
    trades taken under the old one keep pointing at it forever.
    """
    existing = conn.execute(
        "SELECT id FROM strategy_version WHERE strategy_id=? AND version=?",
        (strategy_id, version),
    ).fetchone()
    if existing:
        return existing["id"]

    canonical = json.dumps(rules, sort_keys=True, separators=(",", ":"))
    active_from = active_from or utcnow()[:10]

    # Close the previous open version at the moment this one takes effect.
    conn.execute(
        "UPDATE strategy_version SET active_to=? "
        "WHERE strategy_id=? AND active_to IS NULL",
        (active_from, strategy_id),
    )
    cur = conn.execute(
        "INSERT INTO strategy_version(strategy_id,version,rule_hash,rules,automation_level,"
        "reference_impl,qualification_status,active_from,active_to,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,NULL,?)",
        (strategy_id, version, sha256_text(canonical), canonical, automation_level,
         reference_impl, qualification_status, active_from, utcnow()),
    )
    return cur.lastrowid


def strategy_version_id(conn, strategy_id: str, version: Optional[str] = None,
                        on_date: Optional[str] = None) -> Optional[int]:
    """Resolve the version in force. Explicit version wins; otherwise the one
    active on the given date, so a backfilled trade lands on the right rules."""
    if version:
        row = conn.execute(
            "SELECT id FROM strategy_version WHERE strategy_id=? AND version=?",
            (strategy_id, version),
        ).fetchone()
        return row["id"] if row else None
    on_date = on_date or utcnow()[:10]
    row = conn.execute(
        "SELECT id FROM strategy_version WHERE strategy_id=? AND active_from<=? "
        "AND (active_to IS NULL OR active_to>=?) ORDER BY active_from DESC LIMIT 1",
        (strategy_id, on_date, on_date),
    ).fetchone()
    return row["id"] if row else None


# --------------------------------------------------------------------- sessions
def get_or_create_session(conn, session_date: str, *, account_id: int,
                          session_kind: str = config.DEFAULT_SESSION_KIND,
                          tz: str = config.DEFAULT_TZ, mode: str = "paper",
                          planned_max_risk_r: Optional[float] = None,
                          planned_max_loss: Optional[float] = None,
                          daily_loss_limit: Optional[float] = None,
                          max_trades_planned: Optional[int] = None,
                          window_start: Optional[str] = None,
                          window_end: Optional[str] = None) -> int:
    row = conn.execute(
        "SELECT id FROM session WHERE session_date=? AND session_kind=? AND account_id=?",
        (session_date, session_kind, account_id),
    ).fetchone()
    if row:
        return row["id"]

    label = conn.execute("SELECT label FROM account WHERE id=?", (account_id,)).fetchone()["label"]
    uid = f"{session_date}:{session_kind}:{label.replace(' ', '_')}"
    default_start, default_end = config.SESSION_WINDOWS.get(session_kind, (None, None))
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO session(session_uid,session_date,session_kind,tz,account_id,mode,status,"
        "planned_max_risk_r,planned_max_loss,daily_loss_limit,max_trades_planned,"
        "planned_window_start,planned_window_end,iso_week,iso_month,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,'planned',?,?,?,?,?,?,?,?,?,?)",
        (uid, session_date, session_kind, tz, account_id, mode, planned_max_risk_r,
         planned_max_loss, daily_loss_limit, max_trades_planned,
         window_start or default_start, window_end or default_end,
         iso_week_of(session_date), session_date[:7], now, now),
    )
    return cur.lastrowid


def set_session_status(conn, session_id: int, status: str) -> None:
    conn.execute("UPDATE session SET status=?, updated_at=? WHERE id=?",
                 (status, utcnow(), session_id))


CHECKIN_PRE_FIELDS = (
    "sleep_hours", "energy", "focus", "stress", "desire_to_trade",
    "sleep_quality", "irritability", "impulsivity", "confidence", "money_pressure",
    "physical_state", "caffeine_mg", "life_stress_note", "bias", "planned_note",
    "well_traded_definition", "note", "fill_seconds", "optional_opened",
)

CHECKIN_POST_FIELDS = (
    "execution_quality", "rule_adherence", "patience", "emotional_control",
    "overtraded", "revenge_trade", "stop_moved", "oversized", "missed_qualified",
    "manual_override", "best_decision", "biggest_mistake", "unusual_context",
    "well_traded", "note", "fill_seconds", "optional_opened",
)


def _save_checkin(conn, table: str, which: str, fields: tuple,
                  session_id: int, data: Dict[str, Any],
                  amend_reason: Optional[str] = None) -> str:
    """Insert a check-in, or amend it field by field if one already exists.

    Returns 'created' or 'amended'. An amendment writes the previous value to
    checkin_amendment before the new one lands, so the original answer is never
    lost — which matters because a check-in edited after a bad session is a
    different kind of evidence from one written before the open.
    """
    clean = {k: data.get(k) for k in fields if k in data}
    existing = conn.execute(f"SELECT * FROM {table} WHERE session_id=?", (session_id,)).fetchone()
    # submitted_at is normally now. It may be supplied when backfilling a record
    # whose real submission time is known — never to make a late entry look timely.
    submitted_at = data.get("submitted_at") or utcnow()

    if existing is None:
        cols = ["session_id", "submitted_at"] + list(clean)
        vals = [session_id, submitted_at] + [clean[k] for k in clean]
        conn.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
        return "created"

    now = utcnow()
    changed = []
    for key, new in clean.items():
        old = existing[key]
        if old == new or (old is None and new is None):
            continue
        conn.execute(
            "INSERT INTO checkin_amendment(session_id,which,field,old_value,new_value,"
            "reason,amended_at) VALUES (?,?,?,?,?,?,?)",
            (session_id, which, key,
             None if old is None else str(old),
             None if new is None else str(new),
             amend_reason or "edited after submission", now),
        )
        changed.append(key)

    if changed:
        sets = ", ".join(f"{k}=?" for k in changed)
        conn.execute(f"UPDATE {table} SET {sets} WHERE session_id=?",
                     [clean[k] for k in changed] + [session_id])
    return "amended"


def save_checkin_pre(conn, session_id: int, data: dict, reason: Optional[str] = None) -> str:
    outcome = _save_checkin(conn, "checkin_pre", "pre", CHECKIN_PRE_FIELDS,
                            session_id, data, reason)
    if outcome == "created":
        conn.execute(
            "UPDATE session SET status='checked_in', started_at=COALESCE(started_at,?), "
            "updated_at=? WHERE id=? AND status='planned'",
            (utcnow(), utcnow(), session_id),
        )
    if data.get("planned_max_risk_r") is not None:
        conn.execute("UPDATE session SET planned_max_risk_r=?, updated_at=? WHERE id=?",
                     (data["planned_max_risk_r"], utcnow(), session_id))
    return outcome


def save_checkin_post(conn, session_id: int, data: dict, reason: Optional[str] = None) -> str:
    outcome = _save_checkin(conn, "checkin_post", "post", CHECKIN_POST_FIELDS,
                            session_id, data, reason)
    if outcome == "created":
        traded = conn.execute("SELECT COUNT(*) c FROM trade WHERE session_id=?",
                              (session_id,)).fetchone()["c"]
        conn.execute(
            "UPDATE session SET status=?, ended_at=COALESCE(ended_at,?), updated_at=? WHERE id=?",
            ("closed" if traded else "no_trade", utcnow(), utcnow(), session_id),
        )
    return outcome


def amendments(conn, session_id: int) -> List[dict]:
    return _rows(conn, "SELECT * FROM checkin_amendment WHERE session_id=? "
                       "ORDER BY amended_at, id", session_id)


# --------------------------------------------------------------------- capture
def _manual_batch(conn, label: str, payload: dict) -> int:
    """A manual entry still goes through the RAW layer.

    Hand-keyed data is a source like any other, and recording it as a batch
    means coverage questions ('how much of this month was imported?') have an
    honest answer.
    """
    digest = sha256_text(json.dumps(payload, sort_keys=True, default=str) + uuid.uuid4().hex)
    cur = conn.execute(
        "INSERT INTO raw_import_batch(source,source_kind,source_uri,content_sha256,"
        "imported_at,row_count,importer,status,notes) VALUES (?,'manual',?,?,?,?,?,'ok',?)",
        ("manual_entry", f"ui://{label}", digest, utcnow(), 1, "journal-ui@1.0.0",
         "keyed by hand in the journal interface"),
    )
    return cur.lastrowid


REQUIRED_TRADE_FIELDS = ("symbol", "side", "quantity", "entry_at", "avg_entry_price")
REQUIRED_OPPORTUNITY_FIELDS = ("strategy_id", "symbol", "qualified_at", "status")


def _require(data: dict, fields: tuple, what: str) -> None:
    missing = [f for f in fields if data.get(f) in (None, "")]
    if missing:
        raise ValueError(f"{what} needs {', '.join(missing)}")


def record_trade(conn, session_id: int, data: dict, *, entry_source: str = "manual",
                 batch_id: Optional[int] = None) -> int:
    """Record a trade and its fills.

    Required: instrument, side, quantity, entry_at, avg_entry_price (local wall
    clock times, resolved through the session timezone).
    """
    _require(data, REQUIRED_TRADE_FIELDS, "a trade")
    session = _one(conn, "SELECT * FROM session WHERE id=?", session_id)
    if not session:
        raise ValueError(f"no session {session_id}")
    tz = session["tz"]

    instrument_id = ensure_instrument(conn, data["symbol"])
    sv_id = None
    if data.get("strategy_id"):
        sv_id = strategy_version_id(conn, data["strategy_id"], data.get("strategy_version"),
                                    session["session_date"])

    entry_at = local_to_utc(data["entry_at"], tz)
    exit_at = local_to_utc(data["exit_at"], tz) if data.get("exit_at") else None

    if batch_id is None:
        batch_id = _manual_batch(conn, "trade", data)

    uid = data.get("trade_uid") or sha256_text(
        f"{session_id}|{data['symbol']}|{entry_at}|{data['avg_entry_price']}|{uuid.uuid4().hex}"
    )[:16]

    now = utcnow()
    cur = conn.execute(
        "INSERT INTO trade(trade_uid,session_id,account_id,mode,instrument_id,contract_id,"
        "strategy_version_id,execution_mode,opportunity_id,side,quantity,entry_at,exit_at,"
        "avg_entry_price,avg_exit_price,initial_stop,initial_target,planned_risk_r,"
        "planned_quantity,exit_reason,was_manual_override,override_note,execution_note,"
        "created_at,updated_at,entry_source,import_batch_id,reported_mfe_price,reported_mae_price)"
        " VALUES (?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, session_id, session["account_id"], session["mode"], instrument_id, sv_id,
         data.get("execution_mode", "manual"), data.get("opportunity_id"),
         data["side"], data["quantity"], entry_at, exit_at,
         data["avg_entry_price"], data.get("avg_exit_price"), data.get("initial_stop"),
         data.get("initial_target"), data.get("planned_risk_r", 1.0),
         data.get("planned_quantity", data["quantity"]), data.get("exit_reason"),
         1 if data.get("was_manual_override") else 0, data.get("override_note"),
         data.get("execution_note"), now, now, entry_source, batch_id,
         data.get("reported_mfe_price"), data.get("reported_mae_price")),
    )
    trade_id = cur.lastrowid

    fills = data.get("fills")
    if not fills:
        # Synthesise the two obvious legs from the summary the human gave us,
        # flagged as derived-from-manual-entry rather than reported by a broker.
        fills = [{"leg": "entry", "price": data["avg_entry_price"], "quantity": data["quantity"],
                  "filled_at": data["entry_at"], "intended_price": data.get("intended_entry_price")}]
        if exit_at:
            fills.append({"leg": "exit", "price": data["avg_exit_price"],
                          "quantity": data["quantity"], "filled_at": data["exit_at"]})

    total_commission = data.get("commission", 0.0) or 0.0
    total_fees = data.get("exchange_fees", 0.0) or 0.0
    per_fill_commission = total_commission / len(fills) if fills else 0.0
    per_fill_fees = total_fees / len(fills) if fills else 0.0

    for i, fill in enumerate(fills):
        filled_at = fill["filled_at"]
        filled_at = filled_at if filled_at.endswith("Z") else local_to_utc(filled_at, tz)
        exec_id = fill.get("exec_id") or f"manual:{uid}:{i}"
        raw = conn.execute(
            "INSERT INTO raw_record(batch_id,source,record_type,external_id,occurred_at,"
            "received_at,payload,payload_sha256) VALUES (?,?,'fill',?,?,?,?,?)",
            (batch_id, "manual_entry" if entry_source == "manual" else entry_source,
             exec_id, filled_at, utcnow(),
             json.dumps({**fill, "trade_uid": uid, "entry_source": entry_source}, default=str),
             sha256_text(f"{exec_id}|{fill['price']}|{fill['quantity']}")),
        )
        buy = (data["side"] == "long") == (fill["leg"] in ("entry", "scale_in"))
        conn.execute(
            "INSERT INTO trade_fill(trade_id,raw_record_id,order_id,exec_id,leg,side,quantity,"
            "price,filled_at,intended_price,commission,exchange_fees)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, raw.lastrowid, fill.get("order_id"), exec_id, fill["leg"],
             "buy" if buy else "sell", fill["quantity"], fill["price"], filled_at,
             fill.get("intended_price"),
             fill.get("commission", per_fill_commission),
             fill.get("exchange_fees", per_fill_fees)),
        )

    for code in data.get("tags", []) or []:
        confirm_tag(conn, code, trade_id=trade_id)

    if data.get("opportunity_id"):
        conn.execute("UPDATE opportunity SET trade_id=?, status=? WHERE id=?",
                     (trade_id, "BOT_EXECUTED" if data.get("execution_mode") == "bot" else "TAKEN",
                      data["opportunity_id"]))
    return trade_id


def record_opportunity(conn, session_id: int, data: dict, *,
                       detection_source: str = "human_logged") -> int:
    _require(data, REQUIRED_OPPORTUNITY_FIELDS, "a qualified setup")
    session = _one(conn, "SELECT * FROM session WHERE id=?", session_id)
    tz = session["tz"]
    sv_id = strategy_version_id(conn, data["strategy_id"], data.get("strategy_version"),
                                session["session_date"])
    if sv_id is None:
        raise ValueError(f"no active version for strategy {data['strategy_id']}")

    cur = conn.execute(
        "INSERT INTO opportunity(session_id,strategy_version_id,instrument_id,signal_id,"
        "qualified_at,direction,status,status_reason,decided_by,trade_id,detection_source,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, sv_id, ensure_instrument(conn, data["symbol"]), data.get("signal_id"),
         local_to_utc(data["qualified_at"], tz), data.get("direction"), data["status"],
         data.get("status_reason"), data.get("decided_by", "human"), data.get("trade_id"),
         detection_source, utcnow()),
    )
    opp_id = cur.lastrowid
    if data["status"] == "MISSED":
        confirm_tag(conn, "MISSED_SETUP", session_id=session_id,
                    note=data.get("status_reason"))
    elif data["status"] == "BOT_FAILED":
        confirm_tag(conn, "BOT_ROUTING_ERROR", session_id=session_id,
                    note=data.get("status_reason"))
    return opp_id


def confirm_tag(conn, code: str, *, trade_id: Optional[int] = None,
                session_id: Optional[int] = None, note: Optional[str] = None,
                from_annotation_id: Optional[int] = None) -> int:
    """Write a canonical, human-confirmed label.

    This is the only path by which anything becomes a label counted in a
    statistic. Model suggestions reach it via accept_suggestion, never directly.
    """
    cur = conn.execute(
        "INSERT INTO human_tag(session_id,trade_id,tag_code,note,confirmed_at,"
        "from_annotation_id) VALUES (?,?,?,?,?,?)",
        (session_id, trade_id, code, note, utcnow(), from_annotation_id),
    )
    return cur.lastrowid


def suggest_tag(conn, *, target_type: str, target_id: int, code: str, rationale: str,
                confidence: float, model: str, model_version: str) -> int:
    cur = conn.execute(
        "INSERT INTO ai_annotation(target_type,target_id,annotation_type,tag_code,content,"
        "evidence,confidence,model,model_version,prompt_hash,created_at,status)"
        " VALUES (?,?,'tag',?,?,?,?,?,?,?,?,'suggested')",
        (target_type, target_id, code, rationale, None, confidence, model, model_version,
         sha256_text(f"{model}|{model_version}|{code}"), utcnow()),
    )
    return cur.lastrowid


def accept_suggestion(conn, annotation_id: int) -> int:
    a = _one(conn, "SELECT * FROM ai_annotation WHERE id=?", annotation_id)
    if not a:
        raise ValueError(f"no annotation {annotation_id}")
    if a["annotation_type"] != "tag":
        raise ValueError("only tag suggestions can be promoted to labels")
    conn.execute("UPDATE ai_annotation SET status='accepted', reviewed_at=? WHERE id=?",
                 (utcnow(), annotation_id))
    return confirm_tag(
        conn, a["tag_code"],
        trade_id=a["target_id"] if a["target_type"] == "trade" else None,
        session_id=a["target_id"] if a["target_type"] == "session" else None,
        note="accepted from model suggestion", from_annotation_id=annotation_id,
    )


def reject_suggestion(conn, annotation_id: int) -> None:
    conn.execute("UPDATE ai_annotation SET status='rejected', reviewed_at=? WHERE id=?",
                 (utcnow(), annotation_id))


def add_voice_note(conn, *, session_id: Optional[int] = None, trade_id: Optional[int] = None,
                   logical_trade_id: Optional[int] = None,
                   trading_day_id: Optional[int] = None,
                   audio_path: str, audio_sha256: str, duration_seconds: Optional[float] = None,
                   recorded_at: Optional[str] = None, transcript: Optional[str] = None,
                   transcript_engine: Optional[str] = None,
                   transcript_version: Optional[str] = None) -> int:
    """Store the recording first; transcription is a separate, later step.

    Audio lands with its hash before any transcriber runs, so a failed or
    absent transcription still leaves the human record intact.
    """
    cur = conn.execute(
        "INSERT INTO voice_note(session_id,trade_id,logical_trade_id,trading_day_id,"
        "recorded_at,audio_path,audio_sha256,duration_seconds,transcript,transcript_engine,"
        "transcript_version,transcript_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, trade_id, logical_trade_id, trading_day_id,
         recorded_at or utcnow(), audio_path, audio_sha256,
         duration_seconds, transcript, transcript_engine, transcript_version,
         utcnow() if transcript else None),
    )
    return cur.lastrowid


def attach_transcript(conn, voice_note_id: int, transcript: str, engine: str,
                      version: str) -> None:
    """Write a transcript exactly once. The trigger enforces the 'once'."""
    conn.execute(
        "UPDATE voice_note SET transcript=?, transcript_engine=?, transcript_version=?, "
        "transcript_at=? WHERE id=?",
        (transcript, engine, version, utcnow(), voice_note_id),
    )


# --------------------------------------------------------------------- blinded
def store_blinded_features(conn, scope_type: str, scope_id: str, feature_set: str,
                           features: Dict[str, Any], engine: str, engine_version: str) -> int:
    """Store research features without ever labelling them.

    Requires an unrestricted connection. Storing is not looking: these rows are
    written from the moment the journal is in use, and remain unreadable from
    every daily view until Research Mode is built in a later phase.
    """
    n = 0
    for key, value in features.items():
        num = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        text = None if num is not None else (None if value is None else str(value))
        conn.execute(
            "INSERT OR REPLACE INTO blinded_feature(scope_type,scope_id,feature_set,feature_key,"
            "value_num,value_text,engine,engine_version,computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (scope_type, scope_id, feature_set, key, num, text, engine, engine_version, utcnow()),
        )
        n += 1
    return n


# --------------------------------------------------------------------- payload
def journal_payload(conn) -> dict:
    """The single read the interface performs.

    Shaped for the UI rather than for the schema: one round trip, no client-side
    joins. Touches no research object, so it is safe on a restricted connection.
    """
    sessions = []
    for s in _rows(conn, "SELECT * FROM session ORDER BY session_date, session_kind"):
        sid, uid = s["id"], s["session_uid"]
        trades = _rows(
            conn,
            "SELECT t.id, t.trade_uid, i.symbol, sv.strategy_id, sv.version AS strategy_version,"
            " t.execution_mode, t.entry_source, t.side, t.quantity, t.planned_quantity,"
            " t.entry_at, t.exit_at, t.avg_entry_price, t.avg_exit_price, t.initial_stop,"
            " t.initial_target, t.exit_reason, t.execution_note, t.was_manual_override,"
            " tm.net_pnl, tm.gross_pnl, tm.fees, tm.r_multiple, tm.mfe_r, tm.mae_r,"
            " tm.holding_seconds, tm.seconds_to_mfe, tm.seconds_to_mae,"
            " tm.entry_slippage_ticks, tm.size_deviation_pct, tm.r_captured_pct,"
            " tm.session_cum_pnl_after, o.status AS opportunity_status,"
            " mr.r_multiple AS mechanical_r"
            " FROM trade t JOIN instrument i ON i.id=t.instrument_id"
            " LEFT JOIN strategy_version sv ON sv.id=t.strategy_version_id"
            " LEFT JOIN trade_metrics tm ON tm.trade_id=t.id"
            " LEFT JOIN opportunity o ON o.id=t.opportunity_id"
            " LEFT JOIN mechanical_reference mr ON mr.opportunity_id=o.id"
            " WHERE t.session_id=? ORDER BY t.entry_at", sid)
        for t in trades:
            t["tags"] = [r["tag_code"] for r in
                         conn.execute("SELECT tag_code FROM human_tag WHERE trade_id=?", (t["id"],))]
            t["fills"] = _rows(conn, "SELECT leg,side,quantity,price,filled_at,intended_price,"
                                     "commission,exchange_fees,exec_id FROM trade_fill "
                                     "WHERE trade_id=? ORDER BY filled_at", t["id"])
            t["media"] = _rows(conn, "SELECT phase,timeframe,path,captured_at,capture_status "
                                     "FROM media_asset WHERE trade_id=? ORDER BY captured_at",
                               t["id"])
            t["ai_suggestions"] = _rows(
                conn, "SELECT id,tag_code,content,confidence,status,model,model_version,created_at"
                      " FROM ai_annotation WHERE target_type='trade' AND target_id=?", t["id"])

        sessions.append({
            "session_uid": uid,
            "session_id": sid,
            "date": s["session_date"],
            "session_kind": s["session_kind"],
            "status": s["status"],
            "mode": s["mode"],
            "tz": s["tz"],
            "iso_week": s["iso_week"],
            "iso_month": s["iso_month"],
            "planned_max_risk_r": s["planned_max_risk_r"],
            "daily_loss_limit": s["daily_loss_limit"],
            "metrics": _one(conn, "SELECT * FROM session_metrics WHERE session_id=?", sid),
            "pre": _one(conn, "SELECT * FROM checkin_pre WHERE session_id=?", sid),
            "post": _one(conn, "SELECT * FROM checkin_post WHERE session_id=?", sid),
            "amendments": amendments(conn, sid),
            "trades": trades,
            "opportunities": _rows(
                conn,
                "SELECT o.id, sv.strategy_id, sv.version AS strategy_version, o.qualified_at,"
                " o.direction, o.status, o.status_reason, o.decided_by, o.trade_id,"
                " o.detection_source, mr.r_multiple AS mechanical_r"
                " FROM opportunity o JOIN strategy_version sv ON sv.id=o.strategy_version_id"
                " LEFT JOIN mechanical_reference mr ON mr.opportunity_id=o.id"
                " WHERE o.session_id=? ORDER BY o.qualified_at", sid),
            "events": _rows(conn, "SELECT occurred_at,source,level,message FROM system_event"
                                  " WHERE session_id=? ORDER BY occurred_at", sid),
            "voice": _rows(conn, "SELECT recorded_at,duration_seconds,transcript,"
                                 "transcript_engine,transcript_version FROM voice_note"
                                 " WHERE session_id=?", sid),
            "narrative": _rows(conn, "SELECT text,model,model_version,created_at FROM ai_narrative"
                                     " WHERE scope='session' AND scope_key=?", uid),
            "market": _rows(conn, "SELECT * FROM market_context_session WHERE session_id=?", sid),
        })

    return {
        "meta": {
            "generated_at": utcnow(),
            "calc_version": config.CALC_VERSION,
            "today": max((s["date"] for s in sessions), default=utcnow()[:10]),
            "schema": _one(conn, "SELECT MAX(version) AS v FROM schema_migration")["v"],
            "source": "live",
        },
        "sessions": sessions,
        "strategies": _rows(
            conn, "SELECT sv.id, sv.strategy_id, st.name, sv.version, sv.automation_level,"
                  " sv.qualification_status, sv.active_from, sv.active_to, sv.rule_hash,"
                  " sv.reference_impl FROM strategy_version sv JOIN strategy st"
                  " ON st.id=sv.strategy_id ORDER BY sv.strategy_id, sv.version DESC"),
        "strategy_scorecard": _rows(conn, "SELECT * FROM v_strategy_scorecard"),
        "mistake_taxonomy": _rows(conn, "SELECT code,label,category,description FROM mistake_tag"
                                        " WHERE active=1 ORDER BY sort_order"),
        "mistake_frequency": _rows(conn, "SELECT * FROM v_mistake_frequency"
                                         " ORDER BY occurrences DESC"),
        "accounts": _rows(conn, "SELECT id,label,broker,mode,tz FROM account WHERE active=1"),
        "instruments": _rows(conn, "SELECT id,symbol,tick_size,point_value FROM instrument"),
        "coverage": coverage_report(conn),
    }


def day_journal_payload(conn) -> dict:
    """Everything the interface reads, in one round trip.

    Built around the real object model: a trading day holds a morning read and
    logical trades; a logical trade holds execution events across every account
    that participated. Account executions are never presented as trades.
    """
    from . import trades as trades_mod

    days = []
    for d in _rows(conn, "SELECT * FROM trading_day ORDER BY day_date DESC LIMIT 90"):
        day_id = d["id"]
        bias_row = _one(conn, "SELECT * FROM daily_bias WHERE trading_day_id=?", day_id)
        if bias_row:
            bias_row["sources"] = json.loads(bias_row["sources"] or "[]")
            bias_row["amendments"] = _rows(
                conn, "SELECT field,old_value,new_value,reason,amended_at FROM bias_amendment "
                      "WHERE daily_bias_id=? ORDER BY amended_at", bias_row["id"])

        trade_rows = _rows(conn, "SELECT * FROM v_trade_inbox WHERE day_date=? "
                                 "ORDER BY opened_at", d["day_date"])
        for t in trade_rows:
            tid = t["logical_trade_id"]
            t["timeline"] = trades_mod.position_timeline(conn, tid)
            t["accounts"] = _rows(
                conn, "SELECT ae.*, a.label AS account_label FROM account_execution ae "
                      "JOIN account a ON a.id=ae.account_id WHERE ae.logical_trade_id=? "
                      "ORDER BY CASE WHEN ae.role_at_time=\'LEAD\' THEN 0 ELSE 1 END, a.label",
                tid)
            for acc in t["accounts"]:
                acc["discrepancies"] = json.loads(acc["discrepancies"] or "[]")
            t["events"] = _rows(
                conn, "SELECT e.*, a.label AS account_label FROM execution_event e "
                      "JOIN account a ON a.id=e.account_id WHERE e.logical_trade_id=? "
                      "ORDER BY e.occurred_at, e.id", tid)
            t["voice"] = _rows(conn, "SELECT recorded_at,duration_seconds,transcript "
                                     "FROM voice_note WHERE logical_trade_id=?", tid)
            t["media"] = _rows(conn, "SELECT phase,timeframe,path,captured_at "
                                     "FROM media_asset WHERE logical_trade_id=?", tid)
            t["context_tags"] = [r["context_tag_id"] for r in conn.execute(
                "SELECT context_tag_id FROM trade_context_tag WHERE logical_trade_id=?", (tid,))]
            t["process_tags"] = _rows(
                conn, "SELECT m.code,m.label,m.polarity FROM trade_process_tag tp "
                      "JOIN mistake_tag m ON m.code=tp.tag_code WHERE tp.logical_trade_id=?", tid)
            annotation = _one(conn, "SELECT * FROM trade_annotation WHERE logical_trade_id=?", tid)
            if annotation:
                t.update({k: v for k, v in annotation.items() if k != "logical_trade_id"})

        summary = _one(conn, "SELECT * FROM v_day_summary WHERE trading_day_id=?", day_id) or {}
        days.append({
            "trading_day_id": day_id,
            "day_date": d["day_date"],
            "tz": d["tz"],
            "status": d["status"],
            "iso_week": d["iso_week"],
            "went_well": d["went_well"],
            "went_poorly": d["went_poorly"],
            "lesson": d["lesson"],
            "is_demo": d["is_demo"],
            "bias": bias_row,
            "trades": trade_rows,
            "lead_pnl": summary.get("lead_pnl"),
            "total_pnl": summary.get("total_pnl"),
            "normalized_pnl": summary.get("normalized_pnl"),
            "accounts": conn.execute(
                "SELECT COUNT(*) c FROM account WHERE status=\'ACTIVE\' AND is_demo=?",
                (d["is_demo"],)).fetchone()["c"],
        })

    demo_rows = sum(r["demo_rows"] for r in conn.execute("SELECT * FROM v_demo_isolation"))
    return {
        "meta": {
            "generated_at": utcnow(),
            "today": days[0]["day_date"] if days else utcnow()[:10],
            "calc_version": config.CALC_VERSION,
            "demo_rows": demo_rows,
            "source": "live",
        },
        "days": days,
    }


def taxonomy_payload(conn) -> dict:
    from . import adapters
    return {
        "setups": _rows(conn, "SELECT id,name,description FROM setup WHERE active=1 "
                              "ORDER BY sort_order"),
        "context_tags": _rows(conn, "SELECT id,name,kind FROM context_tag WHERE active=1 "
                                    "ORDER BY sort_order"),
        "process_tags": _rows(conn, "SELECT code,label,polarity,category FROM mistake_tag "
                                    "WHERE active=1 ORDER BY polarity DESC, sort_order"),
        "accounts": _rows(conn, "SELECT id,label,role,platform,broker,prop_firm,account_size,"
                                "size_multiplier,status FROM account ORDER BY "
                                "CASE role WHEN \'LEAD\' THEN 0 WHEN \'FOLLOWER\' THEN 1 "
                                "ELSE 2 END, label"),
        "instruments": _rows(conn, "SELECT id,symbol,tick_size,point_value FROM instrument"),
        "adapters": adapters.status_report(),
        "bias_directions": ["BULLISH", "BEARISH", "NEUTRAL", "UNSURE"],
        "research_mode_enabled": False,
    }


def coverage_report(conn) -> dict:
    """How much of the record is hand-keyed.

    Displayed in the interface so manual data can never quietly read as
    complete automatic coverage.
    """
    by_source = {r["entry_source"]: r["n"] for r in conn.execute(
        "SELECT entry_source, COUNT(*) n FROM trade GROUP BY entry_source")}
    by_detection = {r["detection_source"]: r["n"] for r in conn.execute(
        "SELECT detection_source, COUNT(*) n FROM opportunity GROUP BY detection_source")}
    total_trades = sum(by_source.values())
    total_opps = sum(by_detection.values())
    return {
        "trades_by_entry_source": by_source,
        "opportunities_by_detection_source": by_detection,
        "manual_trade_share": round(by_source.get("manual", 0) / total_trades, 3) if total_trades else None,
        "engine_detected_opportunity_share":
            round(by_detection.get("engine", 0) / total_opps, 3) if total_opps else None,
        "note": "Opportunity coverage is only complete for engine-detected setups. "
                "Human-logged opportunities record what was noticed, not what occurred.",
    }
