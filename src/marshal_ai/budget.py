"""The Budget Governor: the reliability spine of Marshal.

It caps spend for answering a single question, decides how aggressively the swarm
may work as the budget burns down, and always holds a reserve back so the
synthesiser can still produce an answer. This is graceful degradation by design:
the system gets quieter and cheaper rather than failing hard.

Pure logic, no Azure dependency, so it is unit-testable on its own.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum

from .config import pricing_for


class Tier(str, Enum):
    """How aggressively the swarm may work, given remaining workable budget."""

    FULL = "full"            # plenty of budget: full swarm, self-correction on
    REDUCED = "reduced"      # past halfway: fewer workers
    MINIMAL = "minimal"      # near the reserve: one worker, no self-correction
    EXHAUSTED = "exhausted"  # reserve only: stop dispatching, synthesise now


@dataclass
class Charge:
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float


class BudgetExhausted(RuntimeError):
    """Raised when a charge is attempted with no workable budget left."""


class BudgetGovernor:
    """Tracks spend against a per-question budget and sets the degradation tier.

    A reserve fraction is held back from dispatch at all times, so the final
    synthesis step is always affordable even after the workers run the budget
    down. That guarantee is what lets the system degrade gracefully instead of
    returning nothing.
    """

    def __init__(self, budget_usd: float, reserve_frac: float = 0.15):
        if budget_usd <= 0:
            raise ValueError("budget_usd must be positive")
        if not 0.0 <= reserve_frac < 1.0:
            raise ValueError("reserve_frac must be in [0, 1)")
        self.budget_usd = budget_usd
        self.reserve_usd = budget_usd * reserve_frac
        self._spent = 0.0
        self._charges: list[Charge] = []
        self._lock = threading.Lock()

    # -- accounting ---------------------------------------------------------
    @property
    def spent_usd(self) -> float:
        return self._spent

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - self._spent

    @property
    def workable_usd(self) -> float:
        """Budget available for dispatch, i.e. above the held-back reserve."""
        return max(0.0, self.budget_usd - self.reserve_usd - self._spent)

    def record(self, agent: str, model: str, input_tokens: int, output_tokens: int) -> Charge:
        """Record actual usage after a call. Always succeeds (accounting is honest)."""
        usd = pricing_for(model).cost(input_tokens, output_tokens)
        with self._lock:
            self._spent += usd
            charge = Charge(agent, model, input_tokens, output_tokens, usd)
            self._charges.append(charge)
        return charge

    # -- estimation and gating ----------------------------------------------
    def estimate(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return pricing_for(model).cost(input_tokens, output_tokens)

    def can_afford(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        use_reserve: bool = False,
    ) -> bool:
        """Whether an estimated call fits. Workers use workable budget; the
        synthesiser may dip into the reserve (use_reserve=True)."""
        cost = self.estimate(model, input_tokens, output_tokens)
        headroom = self.remaining_usd if use_reserve else self.workable_usd
        return cost <= headroom

    # -- degradation policy --------------------------------------------------
    def tier(self) -> Tier:
        budget_after_reserve = self.budget_usd - self.reserve_usd
        if budget_after_reserve <= 0:
            return Tier.EXHAUSTED
        frac = self.workable_usd / budget_after_reserve
        if frac <= 0.0:
            return Tier.EXHAUSTED
        if frac <= 0.2:
            return Tier.MINIMAL
        if frac <= 0.5:
            return Tier.REDUCED
        return Tier.FULL

    def allot_workers(self, requested: int, max_workers: int) -> int:
        """Clamp a requested worker count to what the current tier permits."""
        ceiling = {
            Tier.FULL: max_workers,
            Tier.REDUCED: max(1, max_workers // 2),
            Tier.MINIMAL: 1,
            Tier.EXHAUSTED: 0,
        }[self.tier()]
        return max(0, min(requested, ceiling))

    def self_correction_allowed(self) -> bool:
        """Self-correction is a luxury: only while there is comfortable budget."""
        return self.tier() in (Tier.FULL, Tier.REDUCED)

    def snapshot(self) -> dict:
        """A small dict for logging and the live UI."""
        return {
            "budget_usd": round(self.budget_usd, 4),
            "spent_usd": round(self._spent, 4),
            "remaining_usd": round(self.remaining_usd, 4),
            "workable_usd": round(self.workable_usd, 4),
            "reserve_usd": round(self.reserve_usd, 4),
            "tier": self.tier().value,
            "charges": len(self._charges),
        }
