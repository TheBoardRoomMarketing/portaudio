# Automation reality: TradeSea, TradeSyncer, TradingView

What can actually be captured automatically, with the evidence for each claim
and what remains unknown. Written after a public-source investigation from a
sandbox with no access to Zack's accounts — which is itself the main limit on
what could be established.

**Nothing here was verified by logging in.** No credentials were handled, no
authenticated service was scraped, and no undocumented API was invented. Where
the answer needs a logged-in look, that is stated rather than guessed.

---

## Summary

| Source | Classification | What it gives us |
|---|---|---|
| **TradeSea** | **EXPORT-BASED (probable)** · needs a sample | Lead-account executions |
| **TradeSyncer** | **EXPORT-BASED (unverified)** · needs a sample | Follower fills per account |
| **TradingView** | **AUTOMATIC NOW, context only** | Alerts and chart context — never executions |
| Rithmic (underneath TradeSea) | **REQUIRES AUTHORIZATION** | Full execution history |
| Prop firm dashboards | **UNKNOWN** | Per-account statements |

---

## TradeSea

**What it is.** A Rithmic-based futures platform for prop traders, web and
mobile, combining TradingView-powered charts with a fast DOM and low-latency
execution. It ships its own analytics and journal component alongside the
trading workspace, and is used by BluSky, FundedSeat, Lucid and Tradeify among
others.

**Evidence.** Vendor and review material describes the platform as an
integrated execution-plus-journal environment built on Rithmic data. No public
third-party API documentation was found — no developer portal, no published
REST or webhook surface for reading one's own execution history out.

**Classification: EXPORT-BASED (probable), blocked on a sample.**

The realistic route is an export from its journal component, mapped through the
CSV adapter. That adapter is built and tested; what is missing is one file to
map against. Until then the journal treats TradeSea trades as manual entry,
which works and is recorded honestly as `entry_source = 'manual'`.

**What Zack can settle in two minutes:** open the journal/analytics section and
look for an export or download control. If one exists, its format decides the
mapping profile. If none exists, manual capture remains the path and that is a
finding worth writing down.

**The alternative, and why it is not the default.** TradeSea sits on Rithmic,
and Rithmic does expose an execution API. Using it would need a licence, a
credential, and the prop firms' permission — and a Rithmic credential is not
obviously scoped to read-only. Under the standing rule that the journal never
holds a credential capable of routing an order, this route is not taken without
explicit authorization and evidence of scoping.

## TradeSyncer

**What it is.** A cloud trade copier for futures, connecting brokers including
Tradovate, Rithmic, NinjaTrader, TradingView and ProjectX, copying from one
leader account to many followers with sub-100ms latency. It also ships a
trading journal that accepts an uploaded trade history.

**Evidence.** Its documented integration surface is *inbound*: a leader can be
driven by TradingView webhook alerts or a NinjaTrader connection. The journal
component is described as accepting an uploaded history — which implies export
exists somewhere, but not necessarily from TradeSyncer itself. No public API for
reading copy events outward was found.

**Classification: EXPORT-BASED (unverified), blocked on a sample.**

The question that matters for this journal is narrower than "does it have an
API": **can follower fills be exported per account?** If yes, follower
executions can be matched to lead executions and copy quality becomes
measurable automatically. If no, follower fills must come from each broker or
prop dashboard separately, which is a materially larger integration.

**What Zack can settle:** look for a trade-history or report export in the
TradeSyncer dashboard, and whether its rows identify the follower account.

**Note on what is already possible without it.** The demo session proves the
journal detects a missed follower add from fills alone. The detection does not
need TradeSyncer's cooperation — it needs the fills, from wherever they come.

## TradingView

**What it is.** Zack's analysis environment, including the Liquidity Map
indicator. Not used for execution.

**Evidence.** TradingView's alert webhooks are documented and real: an alert can
POST JSON to an endpoint. This is the mechanism TradeSyncer itself uses to drive
a leader account.

**Classification: AUTOMATIC NOW — for context only.**

Two caveats keep this from being simple:

1. **An alert is not a trade.** The journal will never infer an execution from
   an alert. A signal that fired and a position that existed are different
   facts, and conflating them would corrupt every statistic built on top.
2. **A webhook needs a reachable endpoint.** The journal is a local application
   bound to loopback by design. Receiving TradingView webhooks would need a
   tunnel or a small relay, which is a real architectural decision rather than
   a configuration detail — and it is not needed for capture correctness.

Screenshots are the higher-value TradingView integration and need no API at all:
share a chart image, attach it to the trade later. That path is already built.

## Rithmic

**Classification: REQUIRES AUTHORIZATION.**

The layer underneath TradeSea, and the only route that would give complete
execution history automatically. It needs a licence agreement, credentials, and
almost certainly prop-firm permission. A Rithmic credential is not evidently
scoped to read-only, which under the project's standing safety rule means it is
not used without explicit authorization and evidence that order capability is
absent.

## Prop firm dashboards

**Classification: UNKNOWN.**

BluSky, Tradeify, Lucid and FundedSeat each have their own portal, and each may
offer a statement or trade export per account. This could be the most reliable
route to *follower* fills, since each account's own firm is the authority on
what filled there. Unknown until someone with accounts looks.

---

## What this means for the build

The adapter boundary exists precisely because none of this is settled:

```
ExecutionSourceAdapter
    ManualAdapter        IMPLEMENTED  — always works, always the fallback
    CsvAdapter           IMPLEMENTED  — any export, one mapping profile per venue
    TradeSeaAdapter      BLOCKED_ON_SAMPLE
    TradeSyncerAdapter   BLOCKED_ON_SAMPLE
```

Both blocked adapters raise rather than returning invented events. A wrong parse
produces fills that look exactly like real ones once they are in the database,
and there is no way to tell them apart afterwards.

**The single most useful thing Zack can provide** is one redacted export from
whichever of the two has one — a handful of trades including a partial fill,
with headers, timestamps, instrument, side, quantity, price and order/fill IDs
intact, and account number, name, email and keys removed. One file converts the
largest remaining source of manual work into a mapping profile.
