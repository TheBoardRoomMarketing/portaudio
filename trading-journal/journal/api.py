"""The local application server.

Binds to loopback by default. That bind is the security boundary — there is no
account system because there is no remote surface to authenticate. Two habits
keep a local server from becoming a hole a browser can reach through:

  * POSTs must be application/json, which blocks the cross-site form post;
  * a request carrying a foreign Origin is refused, which blocks the
    DNS-rebinding trick where a hostile page resolves to 127.0.0.1.

A wider bind — for capture from the phone — is possible, and switches on token
authentication automatically. There is no flag combination that produces an
unauthenticated service beyond loopback; journal/access.py enforces that, and
refuses a publicly routable address outright.

There are no endpoints here that place, cancel or route an order, and no
credential capable of doing so exists anywhere in this project.
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (access, backup, bias as bias_mod, config, contracts, db, demo as demo_mod,
               importers, metrics, repo, trades as trades_mod)

_LOCK = threading.Lock()


class JournalHandler(BaseHTTPRequestHandler):
    server_version = "TradingJournal/1.0"
    protocol_version = "HTTP/1.1"
    _set_cookie = False

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urlparse(origin).hostname
        # The bound address is added to the allowed set, because on a phone the
        # page's own origin IS the Tailscale or LAN address.
        return host in ("127.0.0.1", "localhost", "::1", self.server.bind_host)

    # ------------------------------------------------------------- token
    def _supplied_token(self):
        """Token from, in order: header, query string, cookie.

        The query string is what makes a phone bookmark work — iOS cannot set a
        header by typing a URL. The first such request is answered with a
        cookie so the token stops travelling in URLs (and therefore stops
        appearing in history and referrers) for everything after it.
        """
        header = self.headers.get("X-Journal-Token")
        if header:
            return header.strip(), "header"
        query = parse_qs(urlparse(self.path).query).get("t")
        if query:
            return query[0], "query"
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == access.COOKIE_NAME:
                return value, "cookie"
        return None, None

    def _authorized(self) -> bool:
        """True if this request may proceed. Loopback needs no token."""
        if not self.server.require_token:
            return True
        supplied, where = self._supplied_token()
        if not access.token_matches(supplied, self.server.token):
            return False
        self._set_cookie = (where == "query")
        return True

    def _refuse_unauthorized(self):
        # No hint about what a valid token looks like, and the rejected value is
        # never echoed or logged — a log full of near-miss tokens is its own leak.
        self._json({"error": "a valid access token is required"}, 401)

    def _send(self, status: int, body: bytes, content_type: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self._set_cookie:
            self._set_cookie = False
            self.send_header(
                "Set-Cookie",
                f"{access.COOKIE_NAME}={self.server.token}; Path=/; HttpOnly; "
                "SameSite=Strict; Max-Age=2592000")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200):
        self._send(status, json.dumps(payload, default=str).encode(), "application/json")

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    # ------------------------------------------------------------- routing
    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._refuse_unauthorized()
        try:
            if path.startswith("/api/"):
                return self._get_api(path)
            return self._static(path)
        except Exception as exc:  # noqa: BLE001 - surface, never swallow
            traceback.print_exc()
            self._error(500, str(exc))

    do_HEAD = do_GET

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._refuse_unauthorized()
        if not self._origin_ok():
            return self._error(403, "cross-origin request refused")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._error(415, "Content-Type must be application/json")
        try:
            payload = self._body()
        except ValueError as exc:
            return self._error(400, str(exc))

        try:
            with _LOCK:
                conn = self.server.connect()
                try:
                    result = self._post_api(path, payload, conn)
                    conn.commit()
                finally:
                    conn.close()
            self._json(result)
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except contracts.NotImplementedContract as exc:
            self._error(501, str(exc))
        except importers.DuplicateImport as exc:
            self._error(409, str(exc))
        except sqlite3.IntegrityError as exc:
            self._error(409, f"database rejected the write: {exc}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._error(500, str(exc))

    # ------------------------------------------------------------- static
    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (config.WEB_DIR / rel).resolve()
        if not str(target).startswith(str(config.WEB_DIR.resolve())) or not target.is_file():
            return self._error(404, "not found")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
                "application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)

    # ------------------------------------------------------------- GET api
    def _get_api(self, path: str):
        conn = self.server.connect()
        try:
            if path == "/api/journal":
                return self._json(repo.journal_payload(conn))
            if path == "/api/day-journal":
                query = parse_qs(urlparse(self.path).query)
                mode = (query.get("mode") or [None])[0]
                demo = True if mode == "demo" else (False if mode == "real" else None)
                return self._json(repo.day_journal_payload(conn, demo=demo))
            if path == "/api/meta":
                return self._json(self._meta(conn))
            if path == "/api/status":
                return self._json(status_report(conn))
            if path == "/api/health":
                return self._json({"ok": True})
            return self._error(404, f"no route {path}")
        finally:
            conn.close()

    def _meta(self, conn) -> dict:
        # Legacy first, then the real-workflow taxonomy on top: the newer
        # account rows carry role and copy relationship, which the old query
        # does not, and the interface needs those.
        payload = dict(self._legacy_meta(conn))
        payload.update(repo.taxonomy_payload(conn))
        return payload

    def _legacy_meta(self, conn) -> dict:
        return {
            "schema": db.schema_state(conn),
            "calc_version": config.CALC_VERSION,
            "accounts": [dict(r) for r in conn.execute(
                "SELECT id,label,broker,mode,tz FROM account WHERE active=1")],
            "instruments": [dict(r) for r in conn.execute(
                "SELECT id,symbol,tick_size,point_value FROM instrument")],
            "strategies": [dict(r) for r in conn.execute(
                "SELECT sv.id, sv.strategy_id, st.name, sv.version, sv.automation_level,"
                " sv.qualification_status, sv.active_to FROM strategy_version sv"
                " JOIN strategy st ON st.id=sv.strategy_id"
                " WHERE sv.active_to IS NULL ORDER BY sv.strategy_id")],
            "mistake_taxonomy": [dict(r) for r in conn.execute(
                "SELECT code,label,category,description FROM mistake_tag WHERE active=1"
                " ORDER BY sort_order")],
            "session_kinds": ["NY_AM", "NY_PM", "OVERNIGHT", "OTHER"],
            "opportunity_statuses": ["TAKEN", "MISSED", "SKIPPED_BY_RULE",
                                     "SKIPPED_DISCRETIONARY", "BOT_EXECUTED", "BOT_FAILED",
                                     "INVALIDATED", "NO_ACTION"],
            "integrations": contracts.status_report(),
            "conformance_inputs": metrics.CONFORMANCE_INPUT_READINESS,
            "coverage": repo.coverage_report(conn),
            "research_mode_enabled": False,
        }

    # ------------------------------------------------------------- POST api
    def _post_api(self, path: str, payload: dict, conn) -> dict:
        def session_id() -> int:
            if payload.get("session_id"):
                return int(payload["session_id"])
            account = payload.get("account_label") or _default_account(conn)
            return repo.get_or_create_session(
                conn, payload["date"], account_id=repo.ensure_account(conn, account),
                session_kind=payload.get("session_kind", config.DEFAULT_SESSION_KIND),
                tz=payload.get("tz", config.DEFAULT_TZ),
                mode=payload.get("mode", "paper"))

        if path == "/api/session":
            sid = session_id()
            return {"session_id": sid,
                    "session": dict(conn.execute(
                        "SELECT * FROM session WHERE id=?", (sid,)).fetchone())}

        if path == "/api/checkin/pre":
            sid = session_id()
            outcome = repo.save_checkin_pre(conn, sid, payload, payload.get("reason"))
            return {"session_id": sid, "outcome": outcome}

        if path == "/api/checkin/post":
            sid = session_id()
            outcome = repo.save_checkin_post(conn, sid, payload, payload.get("reason"))
            metrics.recompute_all(conn, note="checkin_post")
            return {"session_id": sid, "outcome": outcome}

        if path == "/api/trade":
            sid = session_id()
            trade_id = repo.record_trade(conn, sid, payload,
                                         entry_source=payload.get("entry_source", "manual"))
            metrics.recompute_all(conn, note="trade")
            return {"session_id": sid, "trade_id": trade_id, "entry_source":
                    payload.get("entry_source", "manual")}

        if path == "/api/opportunity":
            sid = session_id()
            opp_id = repo.record_opportunity(
                conn, sid, payload,
                detection_source=payload.get("detection_source", "human_logged"))
            metrics.recompute_all(conn, note="opportunity")
            return {"session_id": sid, "opportunity_id": opp_id}

        if path == "/api/voice":
            sid = session_id() if (payload.get("date") or payload.get("session_id")) else None
            note_id = repo.add_voice_note(
                conn, session_id=sid, trade_id=payload.get("trade_id"),
                audio_path=payload["audio_path"], audio_sha256=payload["audio_sha256"],
                duration_seconds=payload.get("duration_seconds"),
                transcript=payload.get("transcript"),
                transcript_engine=payload.get("transcript_engine"),
                transcript_version=payload.get("transcript_version"))
            return {"voice_note_id": note_id}

        if path == "/api/tag":
            return {"tag_id": repo.confirm_tag(
                conn, payload["code"], trade_id=payload.get("trade_id"),
                session_id=payload.get("session_id"), note=payload.get("note"))}

        if path == "/api/suggestion/accept":
            return {"tag_id": repo.accept_suggestion(conn, int(payload["annotation_id"]))}

        if path == "/api/suggestion/reject":
            repo.reject_suggestion(conn, int(payload["annotation_id"]))
            return {"ok": True}

        if path == "/api/bias":
            day_id = _trading_day_id(conn, payload.get("date"))
            bias_id = bias_mod.record(
                conn, day_id, direction=payload["direction"],
                strength=payload.get("strength"), thesis=payload.get("thesis"),
                invalidation=payload.get("invalidation"),
                invalidation_level=payload.get("invalidation_level"),
                sources=payload.get("sources"),
                capture_seconds=payload.get("capture_seconds"))
            return {"trading_day_id": day_id, "daily_bias_id": bias_id}

        if path == "/api/bias/amend":
            day_id = _trading_day_id(conn, payload.get("date"))
            return bias_mod.amend(conn, day_id, payload["changes"], payload["reason"])

        if path == "/api/log-trade":
            # Manual capture at the logical-trade level: one decision's whole
            # lifecycle in one submission, lead account only.
            return trades_mod.log_manual_trade(
                conn, day_date=payload.get("date"), symbol=payload["symbol"],
                legs=payload["legs"], account_label=payload.get("account_label"),
                stop_price=payload.get("stop_price"),
                target_price=payload.get("target_price"),
                capture_seconds=payload.get("capture_seconds"))

        if path == "/api/review":
            return trades_mod.submit_review(conn, int(payload["logical_trade_id"]), payload)

        if path == "/api/regroup":
            return trades_mod.regroup(conn, int(payload["logical_trade_id"]),
                                      payload["action"], payload.get("note", ""))

        if path == "/api/group":
            day_id = _trading_day_id(conn, payload.get("date"))
            return trades_mod.group_events(conn, day_id)

        if path == "/api/demo/purge":
            return demo_mod.purge(conn)

        if path == "/api/recompute":
            return metrics.recompute_all(conn, note="manual")

        if path == "/api/export":
            return backup.export_all(conn)

        if path == "/api/backup":
            archive = backup.create(conn, label=payload.get("label", ""))
            return {"archive": str(archive), "verified": backup.verify(archive)["ok"]}

        raise KeyError(f"no route {path}")


def _trading_day_id(conn, date: str) -> int:
    """Find or create the trading day. A day exists as soon as anything is said
    about it, including a day on which nothing was traded."""
    if not date:
        raise ValueError("a date is required")
    row = conn.execute("SELECT id FROM trading_day WHERE day_date=? AND is_demo=0",
                       (date,)).fetchone()
    if row:
        return row["id"]
    now = db.utcnow()
    return conn.execute(
        "INSERT INTO trading_day(day_date,tz,status,iso_week,iso_month,created_at,updated_at,"
        "is_demo) VALUES (?,?,'BIAS_MISSING',?,?,?,?,0)",
        (date, config.DEFAULT_TZ, repo.iso_week_of(date), date[:7], now, now)).lastrowid


def _default_account(conn) -> str:
    row = conn.execute("SELECT label FROM account WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    return row["label"] if row else "Default Account"


def status_report(conn) -> dict:
    integrity = db.integrity_check(conn)
    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("session", "trade", "opportunity", "checkin_pre", "checkin_post",
                        "human_tag", "voice_note", "raw_record")}

    # How many research rows exist is an operational fact; what they contain is
    # not. The count comes from a separate connection because the daily one is
    # not permitted to read that table at all — which is the point.
    blinded = db.connect(restricted=False)
    try:
        counts["blinded_feature"] = blinded.execute(
            "SELECT COUNT(*) c FROM blinded_feature").fetchone()["c"]
    finally:
        blinded.close()

    return {
        "integrity": integrity,
        "counts": counts,
        "coverage": repo.coverage_report(conn),
        "integrations": contracts.status_report(),
        "conformance_inputs": metrics.CONFORMANCE_INPUT_READINESS,
        "data_home": str(config.DATA_HOME),
        "confirmations": {
            "LIVE_TRADING_ACTIONS": "NONE",
            "BROKER_ORDER_CAPABILITY_ADDED": False,
            "PERSONAL_ASTRO_ANALYSIS_EXECUTED": False,
            "RESEARCH_MODE_ENABLED": False,
            "REAL_PERFORMANCE_CORRELATION_ANALYSIS": False,
        },
    }


class JournalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, db_path=None, verbose=False,
                 token=None, require_token=False):
        super().__init__(addr, handler)
        self.db_path = db_path or config.DB_PATH
        self.verbose = verbose
        self.bind_host = addr[0]
        self.token = token
        self.require_token = require_token

    def connect(self):
        return db.connect(self.db_path)


def serve(host: str = config.SERVER_HOST, port: int = config.SERVER_PORT,
          db_path=None, verbose: bool = False, token=None) -> JournalServer:
    """Start the server. A non-loopback bind is token-authenticated, always.

    The decision lives in journal/access.py so there is exactly one place where
    "may this be reachable, and what protects it" is answered.
    """
    plan = access.resolve_bind(host, token=token)
    server = JournalServer((plan["host"], port), JournalHandler, db_path=db_path,
                           verbose=verbose, token=plan["token"],
                           require_token=plan["require_token"])
    server.plan = plan
    return server
