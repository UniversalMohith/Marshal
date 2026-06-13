"""Unit tests for the Budget Governor. No Azure dependency: runs offline.

Run directly (python tests/test_budget.py) or under pytest.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from marshal_ai.budget import BudgetGovernor, Tier  # noqa: E402
from marshal_ai.config import pricing_for  # noqa: E402


def _spend(g: BudgetGovernor, usd: float) -> None:
    """Add an exact USD cost by charging output tokens on gpt-5-mini (2.00/Mtok)."""
    out_tokens = int(round(usd / 2.00 * 1_000_000))
    g.record("test", "gpt-5-mini", 0, out_tokens)


def test_pricing_cost():
    p = pricing_for("gpt-5-mini")
    assert abs(p.cost(1_000_000, 1_000_000) - 2.25) < 1e-9


def test_starts_full():
    g = BudgetGovernor(budget_usd=1.00, reserve_frac=0.20)
    assert g.tier() == Tier.FULL
    assert g.allot_workers(5, 5) == 5
    assert g.self_correction_allowed() is True
    assert abs(g.workable_usd - 0.80) < 1e-9


def test_degrades_to_reduced():
    g = BudgetGovernor(budget_usd=1.00, reserve_frac=0.20)
    _spend(g, 0.41)  # workable 0.39, frac ~0.49
    assert g.tier() == Tier.REDUCED
    assert g.allot_workers(5, 5) == 2
    assert g.self_correction_allowed() is True


def test_degrades_to_minimal():
    g = BudgetGovernor(budget_usd=1.00, reserve_frac=0.20)
    _spend(g, 0.65)  # workable 0.15, frac ~0.19
    assert g.tier() == Tier.MINIMAL
    assert g.allot_workers(5, 5) == 1
    assert g.self_correction_allowed() is False


def test_exhausts_and_protects_reserve():
    g = BudgetGovernor(budget_usd=1.00, reserve_frac=0.20)
    _spend(g, 0.85)  # past the workable budget entirely
    assert g.tier() == Tier.EXHAUSTED
    assert g.allot_workers(5, 5) == 0
    # The reserve is the guarantee: workers cannot touch it, the synthesiser can.
    assert g.can_afford("gpt-5-mini", 0, 50_000, use_reserve=False) is False
    assert g.can_afford("gpt-5-mini", 0, 50_000, use_reserve=True) is True
    assert g.remaining_usd > 0


def test_snapshot_keys():
    g = BudgetGovernor(budget_usd=0.50)
    snap = g.snapshot()
    for key in ("budget_usd", "spent_usd", "remaining_usd", "workable_usd", "reserve_usd", "tier", "charges"):
        assert key in snap


def _run_all():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
    if failures:
        print(f"\n{failures} test(s) failed")
        return 1
    print("\nall budget governor tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
