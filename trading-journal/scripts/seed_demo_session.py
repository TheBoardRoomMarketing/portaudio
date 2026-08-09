#!/usr/bin/env python3
"""SYNTHETIC_DEMO_DATA — the day described in the brief, end to end.

Monday 10 August 2026. Bullish 4/5 morning read. One NQ long on the lead
account, scaled into twice and out of twice, copied across nine followers, with
one follower missing the second add. Reviewed that evening.

Every row this writes carries is_demo = 1. `journal demo --check` proves the
isolation with one query, and the interface refuses to mix demo and real.

    python3 scripts/seed_demo_session.py [--reset]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import adapters, bias, config, db, repo, trades  # noqa: E402
from journal.db import utcnow  # noqa: E402

DAY = "2026-08-10"
TZ = "America/New_York"
SYMBOL = "NQ"

LEAD = ("TradeSea Lead", 1.0)
FOLLOWERS = [
    ("BluSky 150k", 1.0), ("BluSky 100k", 1.0), ("Tradeify 150k", 1.0),
    ("Tradeify 100k", 1.0), ("Lucid 150k", 1.0), ("Lucid 100k", 1.0),
    ("FundedSeat 150k", 1.0), ("FundedSeat 100k", 1.0), ("BluSky Eval", 1.0),
]

# The lead account's decision, as it actually unfolded.
LEAD_EVENTS = [
    ("10:04", "BUY", 2, 23180.25, "OPEN",   23162.00),
    ("10:07", "BUY", 1, 23188.50, "ADD",    None),
    ("10:12", "BUY", 2, 23195.75, "ADD",    None),
    ("10:19", "SELL", 2, 23214.00, "REDUCE", None),
    ("10:25", "SELL", 1, 23221.50, "REDUCE", None),
    ("10:31", "SELL", 2, 23208.75, "CLOSE",  None),
]

# The follower that missed the 10:12 add. Everything else copied with a small
# lag and a little slippage, which is what a copier actually does.
MISSING_ADD_ACCOUNT = "Tradeify 100k"

SETUPS = [
    ("LIQUIDITY_SWEEP_RECLAIM", "Liquidity sweep and reclaim",
     "Sweep of a prior session low, reclaimed, then continuation."),
    ("VWAP_HOLD_CONTINUATION", "VWAP hold continuation",
     "Pullback holds VWAP in trend, continuation entry."),
    ("RANGE_FADE", "Range fade", "Fade an extreme inside an established range."),
    ("OPENING_DRIVE", "Opening drive", "Directional drive from the open with no retrace."),
]

CONTEXT_TAGS = [
    ("LIQUIDITY_MAP", "Liquidity Map", "tool", "Zack's TradingView Liquidity Map indicator."),
    ("OVERNIGHT_LOW", "Overnight low", "level", "Overnight session low as reference."),
    ("VWAP", "VWAP", "level", "Session VWAP."),
    ("HTF_UPTREND", "Higher-timeframe uptrend", "condition", "Aligned with the higher timeframe."),
    ("FIRST_HOUR", "First hour", "session_time", "Inside the first hour of the RTH session."),
]

PROCESS_TAGS = [
    ("FOLLOWED_PLAN", "Followed the plan", "GOOD"),
    ("WAITED_FOR_CONFIRMATION", "Waited for confirmation", "GOOD"),
    ("GOOD_SCALE", "Scaled well", "GOOD"),
    ("GOOD_RISK_CONTROL", "Good risk control", "GOOD"),
    ("GOOD_EXIT_DISCIPLINE", "Good exit discipline", "GOOD"),
    ("CHASED", "Chased", "MISTAKE"),
    ("ADDED_TO_BAD_TRADE", "Added to a losing trade", "MISTAKE"),
    ("MOVED_STOP_BADLY", "Moved the stop badly", "MISTAKE"),
    ("CUT_WINNER_EARLY", "Cut a winner early", "MISTAKE"),
    ("HELD_LOSER_TOO_LONG", "Held a loser too long", "MISTAKE"),
    ("IGNORED_BIAS_INVALIDATION", "Ignored bias invalidation", "MISTAKE"),
    ("IGNORED_LIQUIDITY_CONTEXT", "Ignored liquidity context", "MISTAKE"),
]


def local(hhmm: str) -> str:
    return repo.local_to_utc(f"{DAY}T{hhmm}", TZ)


def setup_reference(conn) -> dict:
    repo.ensure_instrument(conn, SYMBOL, name="E-mini Nasdaq-100", exchange="CME",
                           tick_size=0.25, tick_value=5.0, point_value=20.0)

    for sid, name, description in SETUPS:
        conn.execute(
            "INSERT OR IGNORE INTO setup(id,name,description,sort_order,created_at,is_demo)"
            " VALUES (?,?,?,?,?,1)",
            (sid, name, description, [s[0] for s in SETUPS].index(sid), utcnow()))
    for tid, name, kind, description in CONTEXT_TAGS:
        conn.execute(
            "INSERT OR IGNORE INTO context_tag(id,name,kind,description,sort_order)"
            " VALUES (?,?,?,?,?)",
            (tid, name, kind, description, [t[0] for t in CONTEXT_TAGS].index(tid)))
    for code, label, polarity in PROCESS_TAGS:
        conn.execute(
            "INSERT OR IGNORE INTO mistake_tag(code,label,category,polarity,sort_order)"
            " VALUES (?,?,?,?,?)",
            (code, label, "process", polarity, [p[0] for p in PROCESS_TAGS].index(code)))

    accounts = {}
    lead_id = repo.ensure_account(conn, LEAD[0], broker="TradeSea", mode="live")
    conn.execute(
        "UPDATE account SET role='LEAD', platform='TradeSea', prop_firm='BluSky', "
        "account_size=150000, status='ACTIVE', size_multiplier=?, is_demo=1, opened_at=? "
        "WHERE id=?", (LEAD[1], "2026-01-02", lead_id))
    accounts[LEAD[0]] = lead_id

    for label, multiplier in FOLLOWERS:
        aid = repo.ensure_account(conn, label, broker="TradeSyncer", mode="live")
        conn.execute(
            "UPDATE account SET role='FOLLOWER', platform='TradeSyncer', "
            "account_size=?, status='ACTIVE', size_multiplier=?, copy_source_account_id=?, "
            "is_demo=1, opened_at=? WHERE id=?",
            (150000 if "150k" in label else 100000, multiplier, lead_id, "2026-01-02", aid))
        accounts[label] = aid

    for label, aid in accounts.items():
        conn.execute(
            "INSERT INTO account_snapshot(account_id,effective_from,role,status,platform,"
            "account_size,size_multiplier,copy_source_account_id,reason,captured_at,is_demo)"
            " SELECT id, '2026-01-02', role, status, platform, account_size, size_multiplier,"
            " copy_source_account_id, 'demo baseline', ?, 1 FROM account WHERE id=?",
            (utcnow(), aid))

    conn.commit()
    return accounts


def build_events(accounts: dict) -> list:
    """Lead events, then the copier's version of them on each follower."""
    rand = hashlib.sha256(DAY.encode()).digest()
    events = []

    for hhmm, side, qty, price, kind, stop in LEAD_EVENTS:
        events.append({
            "account_ref": LEAD[0], "symbol": SYMBOL, "event_type": kind,
            "occurred_at": local(hhmm), "quantity": qty, "price": price, "side": side,
            "stop_price": stop, "target_price": None,
            "order_external_id": f"TS-{hhmm.replace(':', '')}",
            "fill_external_id": f"TSF-LEAD-{hhmm.replace(':', '')}",
            "source": "demo",
        })

    for index, (label, _multiplier) in enumerate(FOLLOWERS):
        for step, (hhmm, side, qty, price, kind, _stop) in enumerate(LEAD_EVENTS):
            if label == MISSING_ADD_ACCOUNT and hhmm == "10:12":
                continue    # the copier did not reproduce this add
            # A copier lags a second or two and fills a tick or so away.
            lag = 1 + (rand[(index + step) % len(rand)] % 3)
            slip = ((rand[(index * 3 + step) % len(rand)] % 5) - 1) * 0.25
            minute, second = hhmm.split(":")[0], hhmm.split(":")[1]
            stamp = repo.local_to_utc(f"{DAY}T{minute}:{second}:{lag:02d}", TZ)
            events.append({
                "account_ref": label, "symbol": SYMBOL, "event_type": kind,
                "occurred_at": stamp, "quantity": qty,
                "price": round(price + (slip if side == "BUY" else -slip), 2),
                "side": side, "stop_price": None, "target_price": None,
                "order_external_id": f"TSY-{index}-{hhmm.replace(':', '')}",
                "fill_external_id": f"TSYF-{index}-{hhmm.replace(':', '')}",
                "source": "demo",
            })
    return events


def main(reset: bool = False) -> int:
    config.ensure_dirs()
    setup_conn = db.connect(restricted=False)
    try:
        db.migrate(setup_conn)
        setup_conn.commit()
    finally:
        setup_conn.close()

    conn = db.connect()
    try:
        if reset:
            for table in ("trade_process_tag", "trade_context_tag", "trade_annotation",
                          "logical_trade_metrics", "account_execution"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM logical_trade WHERE is_demo=1")
            conn.execute("DELETE FROM daily_bias WHERE is_demo=1")
            conn.execute("DELETE FROM trading_day WHERE is_demo=1")
            conn.commit()

        accounts = setup_reference(conn)

        day_row = conn.execute("SELECT id FROM trading_day WHERE day_date=? AND is_demo=1",
                               (DAY,)).fetchone()
        if day_row:
            day_id = day_row["id"]
        else:
            now = utcnow()
            day_id = conn.execute(
                "INSERT INTO trading_day(day_date,tz,status,iso_week,iso_month,created_at,"
                "updated_at,is_demo) VALUES (?,?,'PLANNED',?,?,?,?,1)",
                (DAY, TZ, repo.iso_week_of(DAY), DAY[:7], now, now)).lastrowid
            conn.commit()

        if not bias.get(conn, day_id):
            bias.record(
                conn, day_id, direction="BULLISH", strength=4,
                thesis="Expecting the overnight low sweep to hold and buyers to reclaim "
                       "liquidity above. Map shows resting liquidity overhead at 23220.",
                invalidation="Acceptance back below the overnight low.",
                sources=["LIQUIDITY_MAP", "HIGHER_TIMEFRAME"],
                capture_seconds=38, recorded_at=local("08:41"), is_demo=True)

        instrument_ids = {SYMBOL: conn.execute(
            "SELECT id FROM instrument WHERE symbol=?", (SYMBOL,)).fetchone()["id"]}
        result = adapters.ingest(conn, build_events(accounts), account_ids=accounts,
                                 instrument_ids=instrument_ids, is_demo=True)

        grouping = trades.group_events(conn, day_id)

        trade_row = conn.execute(
            "SELECT id FROM logical_trade WHERE trading_day_id=? ORDER BY opened_at LIMIT 1",
            (day_id,)).fetchone()
        trade_id = trade_row["id"] if trade_row else None

        if trade_id:
            repo.add_voice_note(
                conn, logical_trade_id=trade_id, trading_day_id=day_id,
                audio_path=f"media/demo/{DAY}-nq-open.m4a",
                audio_sha256=hashlib.sha256(b"demo-audio").hexdigest(),
                duration_seconds=14, recorded_at=local("10:06"),
                transcript="Long NQ. Liquidity sweep under the morning low, reclaimed the "
                           "map level, adding on the hold above VWAP. I think sellers are "
                           "trapped here.",
                transcript_engine="demo", transcript_version="0")
            conn.execute(
                "INSERT INTO media_asset(kind,logical_trade_id,trading_day_id,phase,timeframe,"
                "captured_at,path,sha256,width,height,capture_status)"
                " VALUES ('screenshot',?,?,'entry','5m',?,?,?,1170,2532,'ok')",
                (trade_id, day_id, local("10:05"),
                 f"media/demo/{DAY}-nq-entry-5m.png",
                 hashlib.sha256(b"demo-shot").hexdigest()))

            trades.submit_review(conn, trade_id, {
                "setup_id": "LIQUIDITY_SWEEP_RECLAIM",
                "why": "Swept the overnight low, reclaimed the map level and held above "
                       "VWAP on the retest. Added twice into strength.",
                "planning_mode": "PLANNED",
                "conviction": 4,
                "execution_grade": 4,
                "process_grade": 5,
                "setup_quality": 4,
                "note": "Last third came off late — price was already rolling over when I "
                        "closed. Scaling was right, the final exit was slow.",
                "process_tags": ["FOLLOWED_PLAN", "GOOD_SCALE", "WAITED_FOR_CONFIRMATION"],
                "context_tags": ["LIQUIDITY_MAP", "OVERNIGHT_LOW", "VWAP", "FIRST_HOUR"],
                "review_seconds": 96,
            })

        trades.rebuild_day(conn, day_id)

        metrics = conn.execute(
            "SELECT * FROM logical_trade_metrics WHERE logical_trade_id=?",
            (trade_id,)).fetchone() if trade_id else None
        discrepancies = conn.execute(
            "SELECT a.label, ae.discrepancies FROM account_execution ae "
            "JOIN account a ON a.id=ae.account_id "
            "WHERE ae.logical_trade_id=? AND ae.discrepancies NOT IN ('[]','')",
            (trade_id,)).fetchall() if trade_id else []

        print(f"day          {DAY} (demo)")
        print(f"accounts     {len(accounts)} (1 lead + {len(FOLLOWERS)} followers)")
        print(f"events       {result['events_written']} written, "
              f"{result['already_known']} already known")
        print(f"logical      {grouping['logical_trades']} trade(s), "
              f"{grouping['events_grouped']} events grouped")
        if metrics:
            print(f"position     max {metrics['max_position']:g}, {metrics['adds']} adds, "
                  f"{metrics['reductions']} reductions")
            print(f"lead P&L     {metrics['lead_pnl']}")
            print(f"total P&L    {metrics['total_pnl']} across "
                  f"{metrics['accounts_participating']} accounts")
            print(f"normalized   {metrics['normalized_pnl']} (what statistics count)")
            print(f"R            {metrics['r_status']}"
                  + (f" {metrics['r_multiple']}" if metrics["r_multiple"] else ""))
        for row in discrepancies:
            for d in json.loads(row["discrepancies"]):
                print(f"discrepancy  {row['label']}: {d['detail']}")

        isolation = conn.execute("SELECT * FROM v_demo_isolation").fetchall()
        print("demo rows    " + ", ".join(f"{r['table_name']}={r['demo_rows']}"
                                          for r in isolation))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true")
    raise SystemExit(main(**vars(parser.parse_args())))
