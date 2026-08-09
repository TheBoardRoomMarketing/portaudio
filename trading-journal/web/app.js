/* =============================================================================
   Trading Journal — interface

   Six screens: Today, Inbox, History, Insights, Accounts, Settings, plus the
   two capture moments (morning bias, trade review).

   Built for a phone held one-handed while the market moves, and for a calm
   half-hour that evening. Nothing here asks for typing during a session.
   ============================================================================= */
(function () {
  "use strict";

  var D = null;                 // journal payload
  var META = null;              // taxonomies, accounts, adapters
  var LIVE = false;
  var ET = "America/New_York";
  var MINUS = "−";

  /* ---------------------------------------------------------------- format */
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"];
  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  /* Stored timestamps are UTC. Display is the trading day's own wall clock. */
  function localTime(iso, tz) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleTimeString("en-GB", {
        hour: "2-digit", minute: "2-digit", timeZone: tz || ET, hour12: false });
    } catch (e) { return iso.slice(11, 16); }
  }
  function longDate(ymd) {
    var p = ymd.split("-");
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    return DAYS[d.getUTCDay()] + " " + (+p[2]) + " " + MONTHS[+p[1] - 1] + " " + p[0];
  }
  function shortDate(ymd) {
    var p = ymd.split("-");
    return (+p[2]) + " " + MONTHS[+p[1] - 1].slice(0, 3);
  }

  function money(v, opts) {
    opts = opts || {};
    if (v == null) return "—";
    var n = Math.abs(v).toFixed(opts.cents === false ? 0 : 2)
      .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (v > 0.005 ? "+" : v < -0.005 ? MINUS : "") + "$" + n;
  }
  function sign(v) { return v == null ? "flat" : v > 0.005 ? "pos" : v < -0.005 ? "neg" : "flat"; }
  function num(v, dp) { return v == null ? "—" : Number(v).toFixed(dp == null ? 2 : dp); }
  function dur(seconds) {
    if (seconds == null) return "—";
    var m = Math.round(seconds / 60);
    return m >= 60 ? Math.floor(m / 60) + "h " + (m % 60) + "m" : m + "m";
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : (many || one + "s")); }

  /* ---------------------------------------------------------------- state */
  var DAYS_BY_DATE = {}, TRADES_BY_ID = {}, TODAY = null;

  function buildIndexes() {
    DAYS_BY_DATE = {}; TRADES_BY_ID = {};
    (D.days || []).forEach(function (d) {
      DAYS_BY_DATE[d.day_date] = d;
      (d.trades || []).forEach(function (t) { t._day = d; TRADES_BY_ID[t.logical_trade_id] = t; });
    });
    TODAY = D.meta.today;
  }

  function inboxTrades() {
    var out = [];
    (D.days || []).forEach(function (d) {
      (d.trades || []).forEach(function (t) {
        if (t.review_state === "NEEDS_REVIEW" || t.status === "NEEDS_GROUPING_REVIEW") out.push(t);
      });
    });
    return out.sort(function (a, b) { return a.opened_at < b.opened_at ? 1 : -1; });
  }

  /* ---------------------------------------------------------------- pieces */
  function stat(k, v, sub, klass) {
    return '<div class="stat"><span class="k">' + esc(k) + '</span>' +
      '<span class="v ' + (klass || "") + '">' + v + '</span>' +
      (sub ? '<span class="s">' + sub + "</span>" : "") + "</div>";
  }

  function biasBadge(bias, opts) {
    opts = opts || {};
    if (!bias) {
      return '<div class="bias"><span class="chip chip-amber"><span class="dot"></span>' +
        'no read yet</span><span class="thesis">The morning read has not been recorded. ' +
        '<a href="#/capture/bias">Add it</a>.</span></div>';
    }
    var pips = "";
    for (var i = 1; i <= 5; i++) {
      pips += '<i class="' + (i <= (bias.strength || 0) ? "on" : "") + '"></i>';
    }
    var tone = { BULLISH: "chip-pos", BEARISH: "chip-neg",
                 NEUTRAL: "chip-ghost", UNSURE: "chip-amber" }[bias.direction] || "chip-ghost";
    return '<div class="bias' + (opts.live ? " on" : "") + '">' +
      '<div><span class="chip ' + tone + '">' + esc(bias.direction.toLowerCase()) + "</span>" +
      '<div class="strength" style="margin-top:8px" role="img" aria-label="conviction ' +
      (bias.strength || 0) + ' of 5">' + pips + "</div></div>" +
      '<div style="min-width:0"><p class="thesis">' + esc(bias.thesis || "No thesis written.") +
      "</p>" +
      (bias.invalidation ? '<p class="small muted" style="margin-top:6px">Invalid if: ' +
        esc(bias.invalidation) + "</p>" : "") +
      (bias.amendments && bias.amendments.length
        ? '<p class="small muted" style="margin-top:6px">' +
          plural(bias.amendments.length, "amendment") + " — original preserved</p>" : "") +
      "</div></div>";
  }

  /* The position lifecycle: a stepped area showing how size actually evolved.
     Reading this should be faster than reading a fill table, which is the
     entire point of drawing it. */
  function lifecycle(events, opts) {
    opts = opts || {};
    var steps = (events || []).filter(function (e) { return e.position_after != null; });
    if (steps.length < 2) return "";

    var W = 640, H = opts.h || 96, P = 10, B = opts.labels ? 18 : 6;
    var peak = Math.max.apply(null, steps.map(function (s) { return Math.abs(s.position_after); }));
    if (!peak) return "";
    var t0 = Date.parse(steps[0].at), t1 = Date.parse(steps[steps.length - 1].at);
    var span = Math.max(1, t1 - t0);
    var x = function (at) { return P + ((Date.parse(at) - t0) / span) * (W - P * 2); };
    var y = function (p) { return H - B - (Math.abs(p) / peak) * (H - P - B); };

    var d = "M" + x(steps[0].at).toFixed(1) + " " + (H - B);
    var prev = 0;
    steps.forEach(function (s) {
      d += " L" + x(s.at).toFixed(1) + " " + y(prev).toFixed(1) +
           " L" + x(s.at).toFixed(1) + " " + y(s.position_after).toFixed(1);
      prev = s.position_after;
    });
    d += " L" + x(steps[steps.length - 1].at).toFixed(1) + " " + (H - B) + " Z";

    var line = "";
    prev = 0;
    steps.forEach(function (s, i) {
      line += (i ? " L" : "M") + x(s.at).toFixed(1) + " " + y(prev).toFixed(1) +
              " L" + x(s.at).toFixed(1) + " " + y(s.position_after).toFixed(1);
      prev = s.position_after;
    });

    var marks = steps.map(function (s) {
      return '<circle class="lc-mark ' + s.event_type.toLowerCase() + '" cx="' +
        x(s.at).toFixed(1) + '" cy="' + y(s.position_after).toFixed(1) + '" r="3.4"/>';
    }).join("");

    var labels = "";
    if (opts.labels) {
      labels = '<text class="lc-label" x="' + P + '" y="' + (H - 4) + '">' +
        localTime(steps[0].at, opts.tz) + "</text>" +
        '<text class="lc-label" x="' + (W - P) + '" y="' + (H - 4) + '" text-anchor="end">' +
        localTime(steps[steps.length - 1].at, opts.tz) + "</text>" +
        '<text class="lc-label" x="' + P + '" y="' + (P + 2) + '">peak ' + peak + "</text>";
    }

    return '<figure class="lifecycle"><svg viewBox="0 0 ' + W + " " + H +
      '" preserveAspectRatio="none" role="img" aria-label="Position size over time, peak ' +
      peak + ' contracts, ' + steps.length + ' executions">' +
      '<path class="lc-step" d="' + d + '"/><path class="lc-line" d="' + line + '"/>' +
      marks + labels + "</svg></figure>";
  }

  function tradeCard(t) {
    var needs = t.review_state === "NEEDS_REVIEW";
    var pnl = t.lead_pnl;
    return '<a class="tcard' + (needs ? " needs" : "") + '" href="#/trade/' +
      t.logical_trade_id + '">' +
      '<div class="tcard-head">' +
        '<span class="tcard-sym">' + esc(t.symbol) + "</span>" +
        '<span class="tcard-dir ' + (t.direction === "LONG" ? "pos" : "neg") + '">' +
          esc(t.direction) + "</span>" +
        '<span class="tcard-time">' + localTime(t.opened_at, t._day.tz) +
          (t.closed_at ? " → " + localTime(t.closed_at, t._day.tz) : " · open") + "</span>" +
        '<span class="tcard-pnl ' + sign(pnl) + '">' + money(pnl, { cents: false }) + "</span>" +
      "</div>" +
      lifecycle(t.timeline, { h: 74, tz: t._day.tz }) +
      '<div class="tcard-meta">' +
        "<span>peak <b>" + num(t.max_position, 0) + "</b></span>" +
        "<span><b>" + (t.adds || 0) + "</b> adds</span>" +
        "<span><b>" + (t.reductions || 0) + "</b> reductions</span>" +
        "<span>" + dur(t.duration_seconds) + "</span>" +
        "<span><b>" + (t.accounts_participating || 0) + "</b> accounts</span>" +
      "</div>" +
      '<div class="row" style="margin-top:11px">' +
        (t.setup_id
          ? '<span class="chip chip-violet">' + esc(setupName(t.setup_id)) + "</span>"
          : '<span class="chip chip-amber"><span class="dot"></span>setup missing</span>') +
        (t.accounts_with_discrepancies
          ? '<span class="chip chip-amber">' +
            plural(t.accounts_with_discrepancies, "account") + " off-copy</span>" : "") +
        (t.status === "NEEDS_GROUPING_REVIEW"
          ? '<span class="chip chip-amber">grouping unsure</span>' : "") +
        (t.r_status === "COMPUTED"
          ? '<span class="chip chip-ghost">' + num(t.r_multiple, 2) + "R</span>"
          : '<span class="chip chip-ghost">R unknown</span>') +
        (needs ? '<span class="spacer"></span><span class="chip chip-cyan">review</span>' : "") +
      "</div></a>";
  }

  function setupName(id) {
    var found = ((META && META.setups) || []).filter(function (s) { return s.id === id; })[0];
    return found ? found.name : id;
  }

  /* ---------------------------------------------------------------- TODAY */
  function viewToday() {
    var day = DAYS_BY_DATE[TODAY] || (D.days || [])[0];
    if (!day) {
      return head("Today", "No trading day recorded yet.") +
        '<div class="card"><p class="muted">Record a morning read to start the day.</p>' +
        '<a class="btn btn-primary" style="margin-top:14px" href="#/capture/bias">' +
        "Add today's read</a></div>";
    }
    var pending = (day.trades || []).filter(function (t) {
      return t.review_state === "NEEDS_REVIEW"; }).length;
    var reviewed = (day.trades || []).length - pending;

    return '<div class="page-head"><div class="grow">' +
      '<span class="eyebrow">' + esc(longDate(day.day_date)) + "</span>" +
      "<h1>Today</h1>" +
      '<p class="page-sub">' +
        (day.trades.length
          ? plural(day.trades.length, "trade") + " · " + reviewed + " reviewed" +
            (pending ? " · " + pending + " waiting" : "")
          : "No trades yet.") +
      "</p></div>" +
      (pending ? '<a class="btn btn-primary" href="#/inbox">Review ' + pending + "</a>" : "") +
      "</div>" +

      '<div class="stack">' +
      '<section><span class="eyebrow" style="margin-bottom:9px">Morning read</span>' +
      biasBadge(day.bias, { live: true }) + "</section>" +

      '<div class="stats stats-4">' +
        stat("Lead P&L", money(day.lead_pnl, { cents: false }), "the account you trade",
             sign(day.lead_pnl)) +
        stat("All accounts", money(day.total_pnl, { cents: false }),
             (day.accounts || 0) + " accounts", "sm " + sign(day.total_pnl)) +
        stat("Trades", String((day.trades || []).length),
             pending ? pending + " need review" : "all reviewed", "sm") +
        stat("Status", esc((day.status || "").replace(/_/g, " ").toLowerCase()),
             day.day_date === TODAY ? "today" : "", "sm") +
      "</div>" +

      (day.trades.length
        ? '<section><span class="eyebrow" style="margin-bottom:10px">Trades</span>' +
          '<div class="stack">' + day.trades.map(tradeCard).join("") + "</div></section>"
        : '<div class="card"><p class="muted">Nothing traded yet today. A day with no ' +
          "trades is still a day worth recording.</p></div>") +

      (day.went_well || day.went_poorly || day.lesson
        ? '<section class="card"><div class="card-head">' +
          '<span class="eyebrow">How the day went</span></div>' +
          (day.went_well ? '<p style="margin-bottom:8px"><span class="eyebrow">Well</span>' +
            esc(day.went_well) + "</p>" : "") +
          (day.went_poorly ? '<p style="margin-bottom:8px"><span class="eyebrow">Poorly</span>' +
            esc(day.went_poorly) + "</p>" : "") +
          (day.lesson ? '<p><span class="eyebrow">Lesson</span>' + esc(day.lesson) + "</p>" : "") +
          "</section>"
        : "") +
      "</div>";
  }

  function head(title, sub, extra) {
    return '<div class="page-head"><div class="grow">' +
      (extra ? '<span class="eyebrow">' + extra + "</span>" : "") +
      "<h1>" + esc(title) + "</h1>" +
      (sub ? '<p class="page-sub">' + sub + "</p>" : "") + "</div></div>";
  }

  /* ---------------------------------------------------------------- INBOX */
  function viewInbox() {
    var pending = inboxTrades();
    if (!pending.length) {
      return head("Inbox", "Nothing waiting.") +
        '<div class="card" style="text-align:center;padding:40px 22px">' +
        '<p style="font-size:17px;margin-bottom:6px">Inbox clear</p>' +
        '<p class="muted small">Every trade has its context. Come back after the next session.'
        + "</p></div>";
    }
    return head("Inbox", plural(pending.length, "trade") + " waiting for context.") +
      '<div class="stack">' + pending.map(function (t) {
        return '<div>' +
          '<div class="eyebrow" style="margin-bottom:7px">' + esc(shortDate(t._day.day_date)) +
          "</div>" + tradeCard(t) + "</div>";
      }).join("") + "</div>";
  }

  /* ---------------------------------------------------------------- TRADE */
  function viewTrade(id) {
    var t = TRADES_BY_ID[id];
    if (!t) return '<div class="card">Trade not found.</div>';
    var day = t._day;
    var accounts = t.accounts || [];
    var lead = accounts.filter(function (a) { return a.role_at_time === "LEAD"; })[0];
    var off = accounts.filter(function (a) { return (a.discrepancies || []).length; });

    return '<div class="page-head"><div class="grow">' +
      '<a class="eyebrow" href="#/today" style="margin-bottom:8px;display:inline-block">' +
      "← " + esc(longDate(day.day_date)) + "</a>" +
      "<h1>" + esc(t.symbol) + " " + esc(t.direction.toLowerCase()) + "</h1>" +
      '<p class="page-sub">' + localTime(t.opened_at, day.tz) + " → " +
      localTime(t.closed_at, day.tz) + " · " + dur(t.duration_seconds) + " · " +
      plural(t.accounts_participating || 0, "account") + "</p></div>" +
      '<div class="row"><span class="num ' + sign(t.lead_pnl) +
      '" style="font-size:26px">' + money(t.lead_pnl) + "</span>" +
      '<span class="muted small">lead</span></div></div>' +

      '<div class="stack">' +

      '<section class="card"><div class="card-head">' +
      '<span class="eyebrow">Position lifecycle</span>' +
      '<span class="muted small">peak ' + num(t.max_position, 0) + " · " +
      (t.adds || 0) + " adds · " + (t.reductions || 0) + " reductions</span></div>" +
      lifecycle(t.timeline, { h: 150, labels: true, tz: day.tz }) +
      '<div class="ladder" style="margin-top:14px">' +
      (t.timeline || []).map(function (e) {
        return '<div class="lrow" data-k="' + esc(e.event_type) + '">' +
          '<span class="t">' + localTime(e.at, day.tz) + "</span>" +
          '<span class="pip"></span>' +
          '<span class="kind">' + esc(e.event_type) + "</span>" +
          '<span class="qty">' + (e.quantity != null
            ? (["OPEN", "ADD"].indexOf(e.event_type) >= 0 ? "+" : MINUS) + num(e.quantity, 0)
            : "") + (e.price != null ? '<span class="muted"> @ ' + num(e.price, 2) +
              "</span>" : "") + "</span>" +
          '<span class="pos">' + (e.position_after != null
            ? num(e.position_after, 0) + " held" : "") + "</span></div>";
      }).join("") + "</div>" +
      '<details class="disc"><summary>Raw executions, all accounts</summary>' +
      rawExecutionTable(t) + "</details></section>" +

      '<div class="stats stats-4">' +
        stat("Lead P&L", money(t.lead_pnl, { cents: false }), "one decision", sign(t.lead_pnl)) +
        stat("All accounts", money(t.total_pnl, { cents: false }),
             plural(t.accounts_participating || 0, "account"), "sm " + sign(t.total_pnl)) +
        stat("R", t.r_status === "COMPUTED" ? num(t.r_multiple, 2) + "R" : "—",
             t.r_status === "COMPUTED" ? "from the initial stop" : "no initial stop recorded",
             "sm") +
        stat("Avg entry", num(t.avg_entry_price, 2),
             t.avg_exit_price ? "exit " + num(t.avg_exit_price, 2) : "", "sm") +
      "</div>" +

      '<div class="grid g-main">' +
      '<div class="stack">' + reviewPanel(t) + "</div>" +
      '<div class="stack">' + copyPanel(t, lead, off) + notesPanel(t) + "</div>" +
      "</div></div>";
  }

  function rawExecutionTable(t) {
    var rows = t.events || [];
    if (!rows.length) return '<p class="muted small">No raw events loaded.</p>';
    return '<div class="tw"><table><thead><tr><th>Time</th><th>Account</th><th>Event</th>' +
      '<th class="n">Qty</th><th class="n">Price</th><th>Source</th><th>Fill ID</th>' +
      "</tr></thead><tbody>" + rows.map(function (e) {
        return "<tr><td>" + localTime(e.occurred_at, t._day.tz) + "</td><td>" +
          esc(e.account_label) + "</td><td>" + esc(e.event_type) + '</td><td class="n">' +
          num(e.quantity, 0) + '</td><td class="n">' + num(e.price, 2) + "</td><td>" +
          esc(e.source) + '</td><td class="num small">' + esc(e.fill_external_id || "—") +
          "</td></tr>";
      }).join("") + "</tbody></table></div>" +
      '<p class="small muted" style="margin-top:9px">Execution events are append-only. ' +
      "A correction is a re-import, never an edit.</p>";
  }

  function reviewPanel(t) {
    if (t.review_state === "REVIEWED") {
      return '<section class="card"><div class="card-head">' +
        '<span class="eyebrow">Your review</span>' +
        '<button class="chip chip-ghost" data-reopen="' + t.logical_trade_id +
        '">edit</button></div>' +
        '<div class="row" style="margin-bottom:12px">' +
        (t.setup_id ? '<span class="chip chip-violet">' + esc(setupName(t.setup_id)) +
          "</span>" : "") +
        (t.planning_mode ? '<span class="chip chip-ghost">' +
          esc(t.planning_mode.toLowerCase()) + "</span>" : "") +
        (t.context_tags || []).map(function (c) {
          return '<span class="chip chip-ghost">' + esc(c) + "</span>"; }).join("") +
        "</div>" +
        (t.why ? "<p>" + esc(t.why) + "</p>" : "") +
        (t.note ? '<p class="small muted" style="margin-top:10px">' + esc(t.note) + "</p>" : "") +
        '<div class="grid g-3" style="margin-top:16px;gap:10px">' +
        gradeBlock("Execution", t.execution_grade) +
        gradeBlock("Process", t.process_grade) +
        gradeBlock("Setup quality", t.setup_quality) +
        "</div>" +
        ((t.process_tags || []).length
          ? '<div class="row" style="margin-top:14px">' + t.process_tags.map(function (p) {
              return '<span class="chip ' + (p.polarity === "GOOD" ? "chip-pos" : "chip-neg") +
                '">' + esc(p.label) + "</span>"; }).join("") + "</div>"
          : "") +
        '<p class="small muted" style="margin-top:14px">Outcome, execution, process and setup ' +
        "quality are recorded separately on purpose. A profitable trade can be badly executed." +
        "</p></section>";
    }
    return reviewForm(t);
  }

  function gradeBlock(label, value) {
    var pips = "";
    for (var i = 1; i <= 5; i++) {
      pips += '<i style="display:block;width:100%;height:4px;border-radius:2px;background:' +
        (i <= (value || 0) ? "var(--cyan)" : "var(--surface-3)") + '"></i>';
    }
    return '<div><span class="eyebrow" style="margin-bottom:6px">' + esc(label) + "</span>" +
      '<div style="display:flex;gap:3px;align-items:center">' + pips + "</div>" +
      '<span class="num small muted" style="margin-top:5px;display:block">' +
      (value ? value + "/5" : "not rated") + "</span></div>";
  }

  /* The one-to-two-minute review. Everything is a tap; the only typing is one
     optional sentence. */
  function reviewForm(t) {
    var setups = (META && META.setups) || [];
    var contexts = (META && META.context_tags) || [];
    var good = ((META && META.process_tags) || []).filter(function (p) {
      return p.polarity === "GOOD"; });
    var bad = ((META && META.process_tags) || []).filter(function (p) {
      return p.polarity === "MISTAKE"; });

    return '<section class="card" id="review-form" data-trade="' + t.logical_trade_id + '">' +
      '<div class="card-head"><span class="eyebrow">What was this?</span>' +
      '<span class="muted small">about a minute</span></div>' +

      '<div class="field" style="margin-bottom:16px">' +
      '<span class="eyebrow">Setup</span><div class="choices" data-single="setup_id">' +
      setups.map(function (s) {
        return '<button type="button" class="choice violet" data-value="' + esc(s.id) +
          '" aria-pressed="false">' + esc(s.name) + "</button>"; }).join("") +
      "</div></div>" +

      '<div class="field" style="margin-bottom:16px">' +
      '<span class="eyebrow">How did it come about</span>' +
      '<div class="choices" data-single="planning_mode">' +
      [["PLANNED", "Planned"], ["REACTIVE", "Reactive"], ["IMPULSIVE", "Impulsive"]]
        .map(function (o) {
          return '<button type="button" class="choice" data-value="' + o[0] +
            '" aria-pressed="false">' + o[1] + "</button>"; }).join("") +
      "</div></div>" +

      '<div class="field" style="margin-bottom:16px">' +
      '<span class="eyebrow">Context</span><div class="choices" data-multi="context_tags">' +
      contexts.map(function (c) {
        return '<button type="button" class="choice" data-value="' + esc(c.id) +
          '" aria-pressed="false">' + esc(c.name) + "</button>"; }).join("") +
      "</div></div>" +

      '<details class="disc" open><summary>Grades and process</summary>' +
      '<div class="grid g-3" style="gap:12px;margin-bottom:14px">' +
      ["execution_grade", "process_grade", "setup_quality"].map(function (f, i) {
        return '<div class="field"><span class="eyebrow">' +
          ["Execution", "Process", "Setup quality"][i] + "</span>" +
          '<div class="choices" data-single="' + f + '" style="gap:5px">' +
          [1, 2, 3, 4, 5].map(function (n) {
            return '<button type="button" class="choice" data-value="' + n +
              '" style="min-width:0;padding:12px 0" aria-pressed="false">' + n + "</button>";
          }).join("") + "</div></div>";
      }).join("") + "</div>" +
      '<div class="field" style="margin-bottom:12px"><span class="eyebrow">Went well</span>' +
      '<div class="choices" data-multi="process_tags">' + good.map(function (p) {
        return '<button type="button" class="choice" data-value="' + esc(p.code) +
          '" aria-pressed="false">' + esc(p.label) + "</button>"; }).join("") + "</div></div>" +
      '<div class="field"><span class="eyebrow">Went wrong</span>' +
      '<div class="choices" data-multi="process_tags">' + bad.map(function (p) {
        return '<button type="button" class="choice warn-sel" data-value="' + esc(p.code) +
          '" aria-pressed="false">' + esc(p.label) + "</button>"; }).join("") + "</div></div>" +
      "</details>" +

      '<div class="field" style="margin:16px 0">' +
      '<span class="eyebrow">Why did you take it? (optional)</span>' +
      '<textarea rows="2" data-field="why" placeholder="Swept the overnight low and ' +
      'reclaimed the map level…"></textarea></div>' +

      '<button class="btn btn-primary btn-wide" data-submit="review">Done</button>' +
      "</section>";
  }

  function copyPanel(t, lead, off) {
    var accounts = t.accounts || [];
    return '<section class="card"><div class="card-head">' +
      '<span class="eyebrow">Accounts</span>' +
      '<span class="muted small">' + plural(accounts.length, "account") + "</span></div>" +
      (off.length
        ? '<div class="warn" style="margin-bottom:14px"><span class="g">off copy</span>' +
          "<p>" + off.map(function (a) {
            return "<b>" + esc(a.account_label) + "</b> — " +
              a.discrepancies.map(function (d) { return esc(d.detail); }).join("; ");
          }).join("<br>") + "</p></div>"
        : '<p class="small muted" style="margin-bottom:12px">Every account reproduced the ' +
          "lead.</p>") +
      accounts.map(function (a) {
        return '<div class="acct-row"><div style="min-width:0">' +
          '<div class="nm">' + esc(a.account_label) +
          (a.role_at_time === "LEAD" ? ' <span class="chip chip-cyan">lead</span>' : "") +
          "</div>" +
          '<div class="sub">' + a.event_count + " fills · peak " + num(a.max_position, 0) +
          (a.entry_slippage_points != null
            ? " · " + (a.entry_slippage_points > 0 ? "+" : "") +
              num(a.entry_slippage_points, 2) + " vs lead" : "") + "</div></div>" +
          '<div class="val ' + sign(a.realized_pnl) + '">' +
          money(a.realized_pnl, { cents: false }) + "</div>" +
          "<div>" + ((a.discrepancies || []).length
            ? '<span class="chip chip-amber">off</span>'
            : '<span class="chip chip-ghost">ok</span>') + "</div></div>";
      }).join("") +
      '<p class="small muted" style="margin-top:12px">Statistics count this as ' +
      "<b>one</b> trade. Ten accounts copying one decision are one observation, not ten." +
      "</p></section>";
  }

  function notesPanel(t) {
    var voice = t.voice || [];
    var media = t.media || [];
    return '<section class="card"><div class="card-head">' +
      '<span class="eyebrow">Captured in the moment</span></div>' +
      (voice.length
        ? voice.map(function (v) {
            return '<div class="card-2" style="padding:13px 15px;border-radius:var(--r-sm);' +
              'margin-bottom:10px"><div class="row" style="margin-bottom:7px">' +
              '<span class="chip chip-cyan"><span class="dot"></span>voice</span>' +
              '<span class="muted small num">' + Math.round(v.duration_seconds || 0) +
              "s · " + localTime(v.recorded_at, t._day.tz) + "</span></div>" +
              "<p>“" + esc(v.transcript || "not transcribed") + "”</p>" +
              '<p class="small muted" style="margin-top:8px">Your words, kept verbatim.</p>' +
              "</div>";
          }).join("")
        : '<p class="small muted">No voice note.</p>') +
      (media.length
        ? '<div class="row" style="margin-top:10px">' + media.map(function (m) {
            return '<span class="chip chip-ghost">' + esc(m.phase || "chart") + " · " +
              esc(m.timeframe || "") + "</span>"; }).join("") + "</div>"
        : '<p class="small muted" style="margin-top:10px">No chart attached. ' +
          "<a href=\"#/trade/" + t.logical_trade_id + "\">Add one</a> whenever suits — " +
          "it is never needed during the session.</p>") +
      "</section>";
  }

  /* ---------------------------------------------------------------- CAPTURE */
  var CAPTURE = { busy: false };

  function viewCaptureBias() {
    var day = DAYS_BY_DATE[TODAY];
    if (day && day.bias) {
      return head("Morning read", "Already recorded for " + esc(longDate(day.day_date)) + ".") +
        biasBadge(day.bias) +
        '<div class="warn" style="margin-top:16px"><span class="g">immutable</span>' +
        "<p>The morning read is written once and frozen. It can be amended with a reason, " +
        "and the original stays in the record — a thesis that can be quietly rewritten " +
        "after the close is worth nothing as evidence.</p></div>";
    }
    return head("Morning read", "Thirty seconds. What do you expect today?") +
      '<section class="card" id="bias-form">' +
      '<div class="field" style="margin-bottom:18px"><span class="eyebrow">Direction</span>' +
      '<div class="choices" data-single="direction">' +
      [["BULLISH", "Bullish"], ["BEARISH", "Bearish"], ["NEUTRAL", "Neutral"],
       ["UNSURE", "Unsure"]].map(function (o) {
        return '<button type="button" class="choice" data-value="' + o[0] +
          '" aria-pressed="false">' + o[1] + "</button>"; }).join("") + "</div></div>" +

      '<div class="field" style="margin-bottom:18px"><span class="eyebrow">Conviction</span>' +
      '<div class="choices" data-single="strength" style="gap:6px">' +
      [1, 2, 3, 4, 5].map(function (n) {
        return '<button type="button" class="choice" data-value="' + n +
          '" aria-pressed="' + (n === 3) + '">' + n + "</button>"; }).join("") + "</div></div>" +

      '<div class="field" style="margin-bottom:18px"><span class="eyebrow">Thesis</span>' +
      '<textarea rows="3" data-field="thesis" placeholder="Expecting the overnight low sweep ' +
      'to hold and buyers to reclaim liquidity above…"></textarea></div>' +

      '<div class="field" style="margin-bottom:18px">' +
      '<span class="eyebrow">Invalidated if (optional)</span>' +
      '<textarea rows="2" data-field="invalidation" ' +
      'placeholder="Acceptance back below the overnight low."></textarea></div>' +

      '<div class="field" style="margin-bottom:20px"><span class="eyebrow">Where it came from' +
      "</span>" +
      '<div class="choices" data-multi="sources">' +
      [["LIQUIDITY_MAP", "Liquidity Map"], ["PRICE_ACTION", "Price action"],
       ["HIGHER_TIMEFRAME", "Higher timeframe"], ["NEWS_MACRO", "News / macro"],
       ["DISCRETION_GUT", "Gut"], ["OTHER", "Other"]].map(function (o) {
        return '<button type="button" class="choice" data-value="' + o[0] +
          '" aria-pressed="false">' + o[1] + "</button>"; }).join("") + "</div></div>" +

      '<button class="btn btn-primary btn-wide" data-submit="bias">Save the read</button>' +
      '<p class="small muted" style="margin-top:12px;text-align:center">Written once, ' +
      "then frozen.</p></section>";
  }

  /* ---------------------------------------------------------------- HISTORY */
  var calMonth = null;

  function viewHistory() {
    var months = [];
    (D.days || []).forEach(function (d) {
      var m = d.day_date.slice(0, 7);
      if (months.indexOf(m) < 0) months.push(m);
    });
    months.sort().reverse();
    if (!calMonth || months.indexOf(calMonth) < 0) calMonth = months[0];
    if (!calMonth) return head("History", "No days recorded yet.");

    var p = calMonth.split("-"), yr = +p[0], mo = +p[1];
    var first = new Date(Date.UTC(yr, mo - 1, 1)).getUTCDay();
    var count = new Date(Date.UTC(yr, mo, 0)).getUTCDate();

    var cells = "";
    for (var i = 0; i < first; i++) cells += '<div class="cal-cell empty"></div>';
    for (var d = 1; d <= count; d++) {
      var key = yr + "-" + pad(mo) + "-" + pad(d);
      var day = DAYS_BY_DATE[key];
      if (!day) {
        cells += '<div class="cal-cell empty"><span class="d">' + d + "</span></div>";
        continue;
      }
      cells += '<a class="cal-cell linked" href="#/day/' + key + '">' +
        '<span class="d">' + d + "</span>" +
        '<span class="v ' + sign(day.lead_pnl) + '">' +
        (day.trades.length ? money(day.lead_pnl, { cents: false }) : "—") + "</span>" +
        '<span class="t">' + (day.trades.length
          ? plural(day.trades.length, "trade")
          : (day.bias ? "no trade" : "")) + "</span></a>";
    }

    return head(MONTHS[mo - 1] + " " + yr, "", "History") +
      '<div class="row" style="margin-bottom:16px">' + months.map(function (m) {
        var q = m.split("-");
        return '<button class="chip ' + (m === calMonth ? "chip-cyan" : "chip-ghost") +
          '" data-cal="' + m + '">' + MONTHS[+q[1] - 1].slice(0, 3) + " " + q[0] + "</button>";
      }).join("") + "</div>" +
      '<section class="card"><div class="cal" style="margin-bottom:7px">' +
      ["S", "M", "T", "W", "T", "F", "S"].map(function (x) {
        return '<div class="cal-dow">' + x + "</div>"; }).join("") + "</div>" +
      '<div class="cal">' + cells + "</div></section>";
  }

  function viewDay(date) {
    var day = DAYS_BY_DATE[date];
    if (!day) return '<div class="card">No record for that day.</div>';
    return head(longDate(day.day_date), plural(day.trades.length, "trade"), "Day") +
      '<div class="stack">' + biasBadge(day.bias) +
      '<div class="stats stats-3">' +
        stat("Lead P&L", money(day.lead_pnl, { cents: false }), "", sign(day.lead_pnl)) +
        stat("All accounts", money(day.total_pnl, { cents: false }), "", "sm " +
             sign(day.total_pnl)) +
        stat("Trades", String(day.trades.length), "", "sm") +
      "</div>" +
      day.trades.map(tradeCard).join("") + "</div>";
  }

  /* ---------------------------------------------------------------- ACCOUNTS */
  function viewAccounts() {
    var accounts = (META && META.accounts) || [];
    var lead = accounts.filter(function (a) { return a.role === "LEAD"; });
    var followers = accounts.filter(function (a) { return a.role === "FOLLOWER"; });
    return head("Accounts", plural(accounts.length, "account") + " · " +
      lead.length + " lead, " + followers.length + " following", "Registry") +
      '<div class="stack">' +
      ["LEAD", "FOLLOWER", "STANDALONE"].map(function (role) {
        var group = accounts.filter(function (a) { return a.role === role; });
        if (!group.length) return "";
        return '<section class="card"><div class="card-head"><span class="eyebrow">' +
          role.toLowerCase() + "</span></div>" + group.map(function (a) {
            return '<div class="acct-row"><div><div class="nm">' + esc(a.label) + "</div>" +
              '<div class="sub">' + esc(a.platform || a.broker || "") +
              (a.prop_firm ? " · " + esc(a.prop_firm) : "") +
              (a.account_size ? " · $" + (a.account_size / 1000) + "k" : "") + "</div></div>" +
              '<div class="val">' + (a.size_multiplier != null
                ? a.size_multiplier + "×" : "") + "</div>" +
              '<div><span class="chip ' + (a.status === "ACTIVE" ? "chip-pos" : "chip-ghost") +
              '">' + esc((a.status || "").toLowerCase()) + "</span></div></div>";
          }).join("") + "</section>";
      }).join("") +
      '<div class="warn"><span class="g">by design</span><p>Historical trades keep the ' +
      "account configuration that existed when they happened. Passing, failing, resetting or " +
      "replacing an account never rewrites what an old trade was.</p></div></div>";
  }

  /* ---------------------------------------------------------------- INSIGHTS */
  function viewInsights() {
    var trades = [];
    (D.days || []).forEach(function (d) { trades = trades.concat(d.trades || []); });
    var reviewed = trades.filter(function (t) { return t.review_state === "REVIEWED"; });
    var MIN = 20;

    return head("Insights", "What the record can and cannot yet say.", "Analytics") +
      '<div class="stack">' +
      '<div class="warn"><span class="g">sample</span><p><b>' +
      plural(trades.length, "logical trade") + " recorded, " + reviewed.length +
      " reviewed.</b> Nothing here is sliced by setup, time of day or context until there " +
      "are at least " + MIN + " reviewed trades in a group — a pattern found in six trades " +
      "is a story, not an edge.</p></div>" +

      '<div class="stats stats-4">' +
        stat("Logical trades", String(trades.length), "the unit of analysis", "sm") +
        stat("Account executions",
             String(trades.reduce(function (s, t) {
               return s + (t.accounts_participating || 0); }, 0)),
             "never counted as trades", "sm") +
        stat("Reviewed", String(reviewed.length),
             trades.length ? Math.round(reviewed.length / trades.length * 100) + "% complete"
                           : "", "sm") +
        stat("With R", String(trades.filter(function (t) {
               return t.r_status === "COMPUTED"; }).length),
             "the rest had no initial stop", "sm") +
      "</div>" +

      '<section class="card"><div class="card-head">' +
      '<span class="eyebrow">Waiting on evidence</span></div>' +
      '<div class="stack-sm">' +
      [["Performance by setup", "needs " + MIN + " reviewed trades per setup"],
       ["Scale-in effectiveness", "needs trades with and without adds"],
       ["Bias accuracy", "needs an approved outcome methodology, not a model's opinion"],
       ["Liquidity Map context", "needs tagged trades across several sessions"],
       ["Copy quality over time", "available now, but one day is not a trend"],
       ["Planned vs impulsive", "needs enough of both to compare"]].map(function (r) {
        return '<div class="acct-row"><div class="nm">' + r[0] + "</div>" +
          '<div class="sub" style="text-align:right">' + r[1] + "</div><div></div></div>";
      }).join("") + "</div>" +
      '<p class="small muted" style="margin-top:14px">Every one of these is designed for and ' +
      "stored. None will be shown until the sample supports it.</p></section></div>";
  }

  /* ---------------------------------------------------------------- SETTINGS */
  function viewSettings() {
    var adapters = (META && META.adapters) || [];
    return head("Settings", "Taxonomies and data sources.", "Configuration") +
      '<div class="stack">' +
      '<section class="card"><div class="card-head"><span class="eyebrow">Where trades come ' +
      "from</span></div><div class=\"stack-sm\">" +
      adapters.map(function (a) {
        var ok = a.status === "IMPLEMENTED";
        return '<div><div class="acct-row"><div><div class="nm">' + esc(a.name) + "</div>" +
          '<div class="sub">' + esc(a.needs || "") + "</div></div><div></div>" +
          '<div><span class="chip ' + (ok ? "chip-pos" : "chip-amber") + '">' +
          esc(a.status.toLowerCase().replace(/_/g, " ")) + "</span></div></div>" +
          (a.evidence ? '<p class="small muted" style="padding:2px 0 8px">' +
            esc(a.evidence) + "</p>" : "") + "</div>";
      }).join("") + "</div></section>" +

      '<section class="card"><div class="card-head"><span class="eyebrow">Setups</span>' +
      '<span class="muted small">' + ((META && META.setups) || []).length + "</span></div>" +
      '<div class="row">' + ((META && META.setups) || []).map(function (s) {
        return '<span class="chip chip-violet">' + esc(s.name) + "</span>"; }).join("") +
      "</div></section>" +

      '<section class="card"><div class="card-head"><span class="eyebrow">Context tags</span>' +
      "</div><div class=\"row\">" + ((META && META.context_tags) || []).map(function (c) {
        return '<span class="chip chip-ghost">' + esc(c.name) + "</span>"; }).join("") +
      "</div></section>" +

      '<section class="card"><div class="card-head"><span class="eyebrow">Process tags</span>' +
      "</div><div class=\"row\">" + ((META && META.process_tags) || []).map(function (p) {
        return '<span class="chip ' + (p.polarity === "GOOD" ? "chip-pos" : "chip-neg") + '">' +
          esc(p.label) + "</span>"; }).join("") + "</div></section></div>";
  }

  /* ---------------------------------------------------------------- forms */
  function collect(scope) {
    var out = {};
    [].forEach.call(scope.querySelectorAll("[data-single]"), function (group) {
      var on = group.querySelector('[aria-pressed="true"]');
      if (on) out[group.getAttribute("data-single")] = on.getAttribute("data-value");
    });
    [].forEach.call(scope.querySelectorAll("[data-multi]"), function (group) {
      var key = group.getAttribute("data-multi");
      var values = [].slice.call(group.querySelectorAll('[aria-pressed="true"]'))
        .map(function (b) { return b.getAttribute("data-value"); });
      if (values.length) out[key] = (out[key] || []).concat(values);
    });
    [].forEach.call(scope.querySelectorAll("[data-field]"), function (el) {
      if (el.value) out[el.getAttribute("data-field")] = el.value;
    });
    return out;
  }

  function api(path, body) {
    if (!LIVE) {
      return Promise.reject(new Error(
        "This is the offline demo. Run bin/journal serve to record anything."));
    }
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || (r.status + " " + r.statusText));
        return j;
      });
    });
  }

  function toast(message, kind) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 3200);
  }

  function submitBias(button) {
    var form = document.getElementById("bias-form");
    var body = collect(form);
    if (!body.direction) { toast("Pick a direction first", "err"); return; }
    body.date = TODAY;
    if (body.strength) body.strength = Number(body.strength);
    body.capture_seconds = Math.round((Date.now() - MOUNTED) / 1000);
    run(button, "/api/bias", body, "Morning read saved.");
  }

  function submitReview(button) {
    var form = document.getElementById("review-form");
    var body = collect(form);
    ["conviction", "execution_grade", "process_grade", "setup_quality"].forEach(function (k) {
      if (body[k]) body[k] = Number(body[k]);
    });
    body.logical_trade_id = Number(form.getAttribute("data-trade"));
    body.review_seconds = Math.round((Date.now() - MOUNTED) / 1000);
    run(button, "/api/review", body, "Reviewed.");
  }

  function run(button, path, body, okMessage) {
    if (CAPTURE.busy) return;
    CAPTURE.busy = true;
    var original = button.textContent;
    button.textContent = "Saving…";
    api(path, body)
      .then(function () { toast(okMessage); return refresh(); })
      .then(function () { location.hash = "#/today"; render(); })
      .catch(function (err) { toast(err.message, "err"); })
      .then(function () { CAPTURE.busy = false; button.textContent = original; });
  }

  /* ---------------------------------------------------------------- router */
  var NAV = [
    ["#/today", "Today", "◈"],
    ["#/inbox", "Inbox", "⧉"],
    ["#/history", "History", "▦"],
    ["#/insights", "Insights", "◇"],
    ["#/accounts", "Accounts", "⊞"],
    ["#/settings", "Settings", "⚙"]
  ];
  var MOUNTED = Date.now();

  function render() {
    var hash = location.hash || "#/today";
    var parts = hash.split("/");
    var html;

    if (parts[1] === "trade") html = viewTrade(Number(parts[2]));
    else if (parts[1] === "day") html = viewDay(parts[2]);
    else if (parts[1] === "inbox") html = viewInbox();
    else if (parts[1] === "history") html = viewHistory();
    else if (parts[1] === "insights") html = viewInsights();
    else if (parts[1] === "accounts") html = viewAccounts();
    else if (parts[1] === "settings") html = viewSettings();
    else if (parts[1] === "capture") html = viewCaptureBias();
    else html = viewToday();

    document.getElementById("view").innerHTML = '<div class="view">' + html + "</div>";
    window.scrollTo(0, 0);
    MOUNTED = Date.now();

    var current = "#/" + (parts[1] || "today");
    var alias = { "#/trade": "#/today", "#/day": "#/history", "#/capture": "#/today" };
    [].forEach.call(document.querySelectorAll("[data-nav]"), function (a) {
      var target = a.getAttribute("href");
      if (target === current || alias[current] === target) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    paintBadges();
  }

  function paintBadges() {
    var pending = inboxTrades().length;
    [].forEach.call(document.querySelectorAll('[data-nav][href="#/inbox"]'), function (a) {
      var badge = a.querySelector(".count, .badge");
      if (badge) badge.remove();
      if (!pending) return;
      var el = document.createElement("span");
      el.className = a.closest(".tabbar") ? "badge" : "count";
      el.textContent = pending;
      a.appendChild(el);
    });
  }

  /* ---------------------------------------------------------------- events */
  document.addEventListener("click", function (e) {
    var target = e.target.closest && e.target.closest(
      "[data-cal],[data-submit],[data-reopen],[data-theme-btn],.choice");
    if (!target) return;

    if (target.hasAttribute("data-cal")) { calMonth = target.getAttribute("data-cal"); render(); return; }
    if (target.hasAttribute("data-theme-btn")) { toggleTheme(); return; }
    if (target.hasAttribute("data-reopen")) {
      var t = TRADES_BY_ID[Number(target.getAttribute("data-reopen"))];
      if (t) { t.review_state = "NEEDS_REVIEW"; render(); }
      return;
    }
    if (target.hasAttribute("data-submit")) {
      if (target.getAttribute("data-submit") === "bias") submitBias(target);
      else submitReview(target);
      return;
    }
    if (target.classList.contains("choice")) {
      var group = target.parentElement;
      var on = target.getAttribute("aria-pressed") === "true";
      if (group.hasAttribute("data-single")) {
        [].forEach.call(group.children, function (b) { b.setAttribute("aria-pressed", "false"); });
        target.setAttribute("aria-pressed", String(!on));
      } else {
        target.setAttribute("aria-pressed", String(!on));
      }
    }
  });

  function toggleTheme() {
    var root = document.documentElement;
    var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    var label = document.querySelector("[data-theme-btn] .theme-state");
    if (label) label.textContent = next === "light" ? "Light" : "Dark";
  }

  /* ---------------------------------------------------------------- chrome */
  function chrome() {
    document.getElementById("rail").innerHTML =
      '<div class="wordmark"><span class="mark"></span>' +
      "<div><b>Journal</b><br><span>futures</span></div></div>" +
      '<nav class="nav">' + NAV.map(function (r) {
        return '<a data-nav href="' + r[0] + '"><span class="g" aria-hidden="true">' + r[2] +
          "</span>" + r[1] + "</a>";
      }).join("") + "</nav>" +
      '<div class="rail-foot">' +
      '<a class="btn btn-primary" href="#/capture/bias" style="min-height:42px;font-size:14px">' +
      "Morning read</a>" +
      '<button class="pill-btn" data-theme-btn><span>Appearance</span>' +
      '<span class="spacer"></span><span class="theme-state num">Dark</span></button>' +
      '<p class="foot-note" id="source-note"></p></div>';

    document.getElementById("topbar").innerHTML =
      '<span class="mark"></span><b>Journal</b><span class="st" id="source-note-m"></span>';

    document.getElementById("tabbar").innerHTML = NAV.slice(0, 5).map(function (r) {
      return '<a data-nav href="' + r[0] + '"><span class="g" aria-hidden="true">' + r[2] +
        "</span>" + r[1] + "</a>";
    }).join("");
  }

  function sourceNote() {
    var demo = D && D.meta && D.meta.demo_rows;
    var text = LIVE
      ? (demo ? "Live database · showing SYNTHETIC_DEMO_DATA" : "Live database")
      : "Offline demo · synthetic data";
    var a = document.getElementById("source-note");
    var b = document.getElementById("source-note-m");
    if (a) a.textContent = text + ". No broker connection, no order capability.";
    if (b) b.textContent = demo ? "DEMO" : (LIVE ? "LIVE" : "OFFLINE");
  }

  function fetchJSON(path) {
    return fetch(path, { headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) throw new Error(path + " " + r.status);
      return r.json();
    });
  }

  function refresh() {
    return fetchJSON("/api/day-journal")
      .then(function (payload) {
        D = payload; LIVE = true;
        return fetchJSON("/api/meta").catch(function () { return null; });
      })
      .catch(function () { D = window.JOURNAL; LIVE = false; return window.JOURNAL_META || null; })
      .then(function (meta) {
        if (meta) META = meta;
        buildIndexes();
        sourceNote();
      });
  }

  function boot() {
    chrome();
    document.getElementById("view").innerHTML =
      '<div class="view"><p class="muted">Loading…</p></div>';
    refresh().then(function () {
      window.addEventListener("hashchange", render);
      render();
    }).catch(function (err) {
      document.getElementById("view").innerHTML =
        '<div class="view"><div class="warn"><span class="g">error</span><p>' +
        esc(err.message) + "</p></div></div>";
    });
  }

  boot();
})();
