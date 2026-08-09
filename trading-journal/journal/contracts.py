"""Integration contracts.

Five things will eventually feed the journal from outside. None of them exists
yet. Each gets an interface and an honest status here, so the journal can be
built against the interface and the gap stays visible instead of being
remembered — or worse, filled with a plausible fake.

The rule: no adapter may report READY unless it is actually implemented and
verified against a real sample. A placeholder that returns invented values is
worse than no adapter, because its output is indistinguishable from real data
once it lands in the database.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

READY = "READY"
BLOCKED_ON_STRATEGY_SPEC = "BLOCKED_ON_STRATEGY_SPEC"
BLOCKED_ON_SAMPLE = "BLOCKED_ON_SAMPLE"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class NotImplementedContract(RuntimeError):
    """Raised when something calls an adapter that does not exist yet."""


# --------------------------------------------------------------------- protocols
class ReferenceImplementation(Protocol):
    """Evaluates what a strategy's rules alone would have produced.

    This is what makes USER_VALUE_ADDED computable. It must be deterministic
    and reproducible from stored market data: given the same signal and the same
    strategy version, it returns the same result forever.
    """

    strategy_id: str
    version: str

    def evaluate(self, signal: dict, market: dict) -> dict:
        """Return entry/stop/target/exit/r_multiple for one qualified setup."""


class BrokerImporter(Protocol):
    """Read-only ingestion of executions. Never holds order authority."""

    name: str

    def parse(self, path: str) -> List[dict]: ...


class SignalImporter(Protocol):
    """Ingests strategy signals (alerts, engine logs) into `signal`."""

    name: str

    def parse(self, path: str) -> List[dict]: ...


class MarketContextEnricher(Protocol):
    """Computes objective session context: prior levels, ATR, ranges, VWAP."""

    def enrich(self, session_uid: str, symbol: str, date: str) -> dict: ...


class BlindedEnricher(Protocol):
    """Computes research features and stores them against opaque keys.

    Writes only. Nothing in this interface returns a feature value to a caller,
    because nothing in the daily application is allowed to read one.
    """

    feature_set: str
    engine_version: str

    def compute(self, session_uid: str, date: str) -> Dict[str, float]: ...


# --------------------------------------------------------------------- registry
class Adapter:
    def __init__(self, key: str, status: str, blocked_by: str = "", note: str = ""):
        self.key = key
        self.status = status
        self.blocked_by = blocked_by
        self.note = note

    def as_dict(self) -> dict:
        return {"key": self.key, "status": self.status,
                "blocked_by": self.blocked_by, "note": self.note}

    @property
    def ready(self) -> bool:
        return self.status == READY

    def require(self):
        if not self.ready:
            raise NotImplementedContract(
                f"{self.key} is {self.status}"
                + (f" ({self.blocked_by})" if self.blocked_by else ""))


REGISTRY = {
    "10AM_REFERENCE_IMPL": Adapter(
        "10AM_REFERENCE_IMPL", BLOCKED_ON_STRATEGY_SPEC,
        blocked_by="exact 10AM rule specification, owned by its own project",
        note="Interface defined; no implementation. Rules will not be reconstructed "
             "from memory or inferred from another lane — a guessed reference "
             "produces a confidently wrong USER_VALUE_ADDED.",
    ),
    "BROKER_IMPORTER": Adapter(
        "BROKER_IMPORTER", BLOCKED_ON_SAMPLE,
        blocked_by="a real broker export sample, and the broker choice itself",
        note="Generic CSV mapping profile implemented and tested against a "
             "synthetic export. No broker-specific assumption has been made. "
             "Read-only by construction: the journal never holds order authority.",
    ),
    "SIGNAL_IMPORTER": Adapter(
        "SIGNAL_IMPORTER", NOT_IMPLEMENTED,
        blocked_by="signal source format (TradingView alerts / DORB engine logs)",
        note="`signal` and `opportunity` tables accept engine-detected setups today; "
             "until an importer exists, opportunities are human_logged and coverage "
             "is reported as incomplete.",
    ),
    "MARKET_CONTEXT_ENRICHER": Adapter(
        "MARKET_CONTEXT_ENRICHER", NOT_IMPLEMENTED,
        blocked_by="a local market data source",
        note="Schema and UI are ready. Until this exists, MFE/MAE are optionally "
             "human-reported and derived from those reported prices, never guessed.",
    ),
    "ASTRO_BLINDED_ENRICHER": Adapter(
        "ASTRO_BLINDED_ENRICHER", NOT_IMPLEMENTED,
        blocked_by="astro engine interface from its owning project",
        note="Blinded storage plumbing is live and tested: features can be written "
             "against opaque keys today and are unreadable from every daily view. "
             "No personal-astro analysis has been executed.",
    ),
}


def status_report() -> List[dict]:
    return [a.as_dict() for a in REGISTRY.values()]


def require(key: str) -> Adapter:
    adapter = REGISTRY.get(key)
    if adapter is None:
        raise KeyError(f"unknown integration contract '{key}'")
    adapter.require()
    return adapter


# --------------------------------------------------------------------- placeholder
class TenAmReferencePlaceholder:
    """Interface-complete, deliberately non-functional.

    Calling evaluate() raises. It does not return a plausible number, because a
    plausible number would flow into USER_VALUE_ADDED and be indistinguishable
    from a real one.
    """

    strategy_id = "10AM_MODEL"
    version = "1.0"
    status = BLOCKED_ON_STRATEGY_SPEC

    def evaluate(self, signal: dict, market: dict) -> dict:
        raise NotImplementedContract(
            "10AM_REFERENCE_IMPL is BLOCKED_ON_STRATEGY_SPEC. The exact rules must "
            "come from the strategy's owning project; they will not be reconstructed here."
        )
