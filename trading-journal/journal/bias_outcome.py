"""Candidate methodologies for judging whether a morning bias was right.

**Nothing here is active.** No methodology is registered as approved, no code
path calls `evaluate`, and `ACTIVE_METHODOLOGY` is None. Running any of these
against real data requires an explicit approval that has not been given, and
`journal.bias.set_outcome` remains the only writer.

Three candidates are implemented rather than described, because the difference
between them is arithmetic and arithmetic is easier to judge when you can run
it. Each takes the same input — the recorded bias plus that day's session OHLC
— and returns a verdict with the numbers that produced it.

What every candidate has in common, deliberately:

  * **It reads price, not results.** A bias is a claim about the market. If it
    were scored against Zack's P&L, a correct read traded badly would count as
    a wrong read, and the two would stop being separable — which is the whole
    reason the bias is captured before the open.
  * **INDETERMINATE is a real answer.** A day that does neither thing gets
    INDETERMINATE, not a nudge toward the nearest verdict. Forcing every day
    into correct-or-wrong is how a coin flip acquires a track record.
  * **Missing data yields None, never a verdict.** Absent evidence is not a
    correct call, and it is not an incorrect one either.
  * **It is computable from data the journal already holds**, so no candidate
    depends on a feed that does not exist.

The differences are the point, and they are set out in
`compare()` at the bottom of this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

# No methodology is approved. Setting this to a key does not switch anything on
# by itself — it is the flag an approval would flip, and every reader checks it.
ACTIVE_METHODOLOGY: Optional[str] = None

CORRECT = "CORRECT"
PARTIALLY_CORRECT = "PARTIALLY_CORRECT"
INCORRECT = "INCORRECT"
INDETERMINATE = "INDETERMINATE"


@dataclass
class SessionPrice:
    """The minimum a candidate may look at: the session's own price envelope.

    Deliberately not the trades. A methodology that can see the trades will
    eventually be tuned until the bias looks right whenever the day was
    profitable, and the two facts stop being independent.
    """

    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    atr: Optional[float] = None          # prior-day ATR, for scaling thresholds

    def complete(self) -> bool:
        return None not in (self.open, self.high, self.low, self.close)


@dataclass
class Verdict:
    """A verdict is never just a label — it carries what produced it."""

    outcome: str
    methodology: str
    inputs: dict = field(default_factory=dict)
    detail: str = ""

    def as_dict(self) -> dict:
        return {"outcome": self.outcome, "methodology": self.methodology,
                "inputs": self.inputs, "detail": self.detail}


def _direction_sign(direction: str) -> Optional[int]:
    return {"BULLISH": 1, "BEARISH": -1}.get(direction)


# =============================================================================
# Candidate A — close relative to open
# =============================================================================
def close_vs_open(bias: dict, price: SessionPrice,
                  neutral_band: float = 0.15) -> Optional[Verdict]:
    """Did the session close in the direction called?

    The simplest defensible rule, and the hardest to argue with after the fact:
    one number, fixed in advance, no path dependence. A band around unchanged
    (default 0.15 ATR) is INDETERMINATE, so a flat day is not scored as a
    marginal win for whichever side it drifted toward by a tick.

    Weakness, stated plainly: it ignores everything that happened between the
    bells. A day that ran 80 points in your direction and gave it all back
    scores the same as a day that never moved.
    """
    if not price.complete():
        return None
    sign = _direction_sign(bias.get("direction", ""))
    if sign is None:
        return None  # NEUTRAL and UNSURE are not directional claims

    move = price.close - price.open
    scale = price.atr or abs(price.high - price.low) or 1.0
    threshold = neutral_band * scale
    inputs = {"open": price.open, "close": price.close, "move": round(move, 4),
              "threshold": round(threshold, 4), "scale": round(scale, 4)}

    if abs(move) < threshold:
        return Verdict(INDETERMINATE, "close_vs_open", inputs,
                       "closed inside the neutral band around the open")
    if (move > 0) == (sign > 0):
        return Verdict(CORRECT, "close_vs_open", inputs,
                       "closed in the direction called")
    return Verdict(INCORRECT, "close_vs_open", inputs,
                   "closed against the direction called")


# =============================================================================
# Candidate B — which side paid first, and by how much
# =============================================================================
def favourable_excursion(bias: dict, price: SessionPrice,
                         ratio: float = 1.5) -> Optional[Verdict]:
    """Did the session offer more room in the called direction than against it?

    Measures both excursions from the open and compares them. A bias that gave
    two points of room for every one against it was a useful read even if the
    close landed nowhere, which matches how a directional read is actually used:
    to decide which side to look for entries on.

    PARTIALLY_CORRECT exists here because it is real. A day that offered decent
    room both ways is genuinely half-right, and collapsing it to a win or a loss
    throws away the distinction.

    Weakness: it cannot see the order of events. A day that dropped hard, then
    rallied harder, reads the same as one that rallied first — and for a trader
    who was at his desk in the morning, the order matters.
    """
    if not price.complete():
        return None
    sign = _direction_sign(bias.get("direction", ""))
    if sign is None:
        return None

    up = price.high - price.open
    down = price.open - price.low
    favourable, adverse = (up, down) if sign > 0 else (down, up)
    inputs = {"favourable": round(favourable, 4), "adverse": round(adverse, 4),
              "ratio_required": ratio}

    if favourable <= 0 and adverse <= 0:
        return Verdict(INDETERMINATE, "favourable_excursion", inputs, "no range")
    if adverse == 0:
        return Verdict(CORRECT, "favourable_excursion", inputs,
                       "no adverse excursion at all")
    measured = favourable / adverse
    inputs["ratio_measured"] = round(measured, 3)

    if measured >= ratio:
        return Verdict(CORRECT, "favourable_excursion", inputs,
                       "materially more room in the called direction")
    if measured >= 1.0:
        return Verdict(PARTIALLY_CORRECT, "favourable_excursion", inputs,
                       "more room in the called direction, but not decisively")
    if measured <= 1.0 / ratio:
        return Verdict(INCORRECT, "favourable_excursion", inputs,
                       "materially more room against the direction called")
    return Verdict(INDETERMINATE, "favourable_excursion", inputs,
                   "the day offered comparable room both ways")


# =============================================================================
# Candidate C — the trader's own invalidation level
# =============================================================================
def invalidation_respected(bias: dict, price: SessionPrice,
                           target_multiple: float = 1.0) -> Optional[Verdict]:
    """Was the level Zack himself named as invalidation respected?

    The only candidate that scores the bias against its author's own stated
    terms rather than an analyst's threshold. If the morning note says "wrong
    below 23,140" then the market breaking 23,140 is a wrong call by
    definition, and nobody has to agree on a band.

    This is the strictest candidate and the most honest one, and it is also the
    one that most often returns None — because it needs a numeric invalidation
    level to have been written down. That is a capture problem, not a scoring
    problem, and it is visible rather than papered over.

    Weakness: it depends on a habit that does not exist yet, and a level chosen
    loosely makes the verdict loose with it.
    """
    if not price.complete():
        return None
    sign = _direction_sign(bias.get("direction", ""))
    level = bias.get("invalidation_level")
    if sign is None or level is None:
        # No level, no verdict. Missing evidence is not a correct call.
        return None

    breached = price.low <= level if sign > 0 else price.high >= level
    distance = abs(price.open - level)
    reached = (price.high - price.open) if sign > 0 else (price.open - price.low)
    inputs = {"invalidation_level": level, "breached": breached,
              "risk_distance": round(distance, 4),
              "favourable_reach": round(reached, 4),
              "target_multiple": target_multiple}

    if breached:
        return Verdict(INCORRECT, "invalidation_respected", inputs,
                       "the market traded through the level called as invalidation")
    if distance > 0 and reached >= target_multiple * distance:
        return Verdict(CORRECT, "invalidation_respected", inputs,
                       "the level held and the market travelled at least as far the other way")
    return Verdict(PARTIALLY_CORRECT, "invalidation_respected", inputs,
                   "the level held but the move did not develop")


# =============================================================================
# Registry — declared, and switched off
# =============================================================================
METHODOLOGIES: Dict[str, Callable] = {
    "close_vs_open": close_vs_open,
    "favourable_excursion": favourable_excursion,
    "invalidation_respected": invalidation_respected,
}

# Every candidate is DORMANT. A methodology becomes ACTIVE only by explicit
# approval, which changes this table and ACTIVE_METHODOLOGY together.
STATUS = {name: "DORMANT_AWAITING_APPROVAL" for name in METHODOLOGIES}


class NotApproved(RuntimeError):
    pass


def evaluate(name: str, bias: dict, price: SessionPrice, **kwargs) -> Optional[Verdict]:
    """Run one candidate. Refuses unless that candidate has been approved.

    Refusing here rather than at the call site means there is no way to get a
    verdict into the database by forgetting a check — including by a future
    version of this project that has stopped remembering why the check existed.
    """
    if name not in METHODOLOGIES:
        raise KeyError(f"unknown methodology '{name}'")
    if ACTIVE_METHODOLOGY != name:
        raise NotApproved(
            f"'{name}' is {STATUS[name]}. No bias outcome methodology has been "
            "approved, so no outcomes are assigned. Use dry_run() to see what it "
            "would say without writing anything.")
    return METHODOLOGIES[name](bias, price, **kwargs)


def dry_run(bias: dict, price: SessionPrice) -> dict:
    """What each candidate would say. Writes nothing, approves nothing.

    This is how the candidates get compared on real days once there are some —
    side by side, on the same input, with the disagreements visible.
    """
    results = {}
    for name, fn in METHODOLOGIES.items():
        verdict = fn(bias, price)
        results[name] = verdict.as_dict() if verdict else None
    verdicts = {k: v["outcome"] for k, v in results.items() if v}
    return {
        "verdicts": results,
        "agreement": len(set(verdicts.values())) <= 1 if verdicts else None,
        "unavailable": [k for k, v in results.items() if v is None],
        "note": "Nothing was written. No methodology is approved.",
    }


def compare() -> dict:
    """The three candidates side by side, for the approval decision."""
    return {
        "active": ACTIVE_METHODOLOGY,
        "status": STATUS,
        "candidates": [
            {
                "name": "close_vs_open",
                "question": "Did the session close in the direction called?",
                "needs": "session open and close",
                "available_today": True,
                "strength": "Unarguable after the fact. One number, fixed in advance.",
                "weakness": "Blind to everything between the bells. A day that ran your "
                            "way and gave it back scores like a day that never moved.",
                "bias_toward": "Understates good reads on reversal days.",
            },
            {
                "name": "favourable_excursion",
                "question": "Did the day offer more room the called way than against it?",
                "needs": "session open, high and low",
                "available_today": True,
                "strength": "Matches how a directional read is actually used — which side "
                            "to hunt entries on. PARTIALLY_CORRECT is a real category.",
                "weakness": "Order-blind. Down-then-up reads identically to up-then-down, "
                            "which matters for someone trading the first two hours.",
                "bias_toward": "Overstates reads on wide-range days that went nowhere.",
            },
            {
                "name": "invalidation_respected",
                "question": "Did the market respect the level Zack named as invalidation?",
                "needs": "a numeric invalidation level in the morning note",
                "available_today": False,
                "strength": "Scores the bias on its author's own stated terms. No "
                            "threshold anyone has to agree on.",
                "weakness": "Returns no verdict without a numeric level, and that habit "
                            "does not exist yet. A loose level makes a loose verdict.",
                "bias_toward": "Silent on most days until capture improves — which is "
                               "visible, and better than a guess.",
            },
        ],
        "recommendation": (
            "Do not approve one yet. Run all three in dry-run over the first 20 complete "
            "real trading days and select from the disagreements."
        ),
        # An earlier draft of this module suggested favourable_excursion as the
        # likeliest choice. That suggestion was reviewed and NOT adopted, and
        # naming a favourite before the observation period would bias the
        # reading of the very evidence meant to settle it. All three candidates
        # are equal here until 20 complete days say otherwise.
        "ranking": "NONE — all three candidates are equal during the observation period",
        "selection_question": (
            "Which method best operationalises the pre-session directional thesis? "
            "Not which method makes the bias look best, and never which method "
            "correlates with profit."
        ),
        "what_none_of_them_do": [
            "None looks at P&L. A correct read traded badly must stay a correct read.",
            "None forces a verdict — INDETERMINATE and None are both real answers.",
            "None runs automatically. No outcome is written until one is approved.",
        ],
    }
