"""CLI entry point.

    python -m marshal_ai "your hard question" [--budget 0.50]

Runs the reasoning loop and prints the swarm working live. This headless view
doubles as a reliable fallback demo if the web UI is unavailable.
"""
from __future__ import annotations

import argparse
import sys

from .config import settings
from .loop import Marshal

_GRADE_MARK = {"strong": "[strong]", "thin": "[thin]", "blank": "[blank]"}


def _print_event(ev: dict) -> None:
    t = ev.get("type")
    if t == "start":
        print(f"\n>>> Question: {ev['question']}")
        b = ev["budget"]
        print(f"    Budget: ${b['budget_usd']} (reserve ${b['reserve_usd']} held back)")
    elif t == "refused":
        print(f"!!! Refused: {ev['reason']}")
    elif t == "decompose_start":
        print(f"\n[orchestrator] decomposing (tier {ev['tier']}, up to {ev['allotment']} sub-tasks)...")
    elif t == "decomposed":
        for s in ev["subtasks"]:
            print(f"    - {s['id']}: {s['title']}")
    elif t == "workers_start":
        print(f"\n[workers] dispatching {ev['count']} in parallel x{ev['parallel']} (tier {ev['tier']})...")
    elif t == "worker_done":
        flag = " BLANK" if ev["blank"] else ""
        print(f"    {ev['id']} done ({ev['chars']} chars){flag}")
    elif t == "graded":
        print(f"[critic] {ev['id']} -> {_GRADE_MARK.get(ev['grade'], ev['grade'])} (by {ev['by']})")
    elif t == "rescope_start":
        print(f"\n[self-correct] re-scoping {', '.join(ev['ids'])}...")
    elif t == "degrade":
        print(f"[governor] degrading: skipping {ev['stage']} to protect the budget")
    elif t == "synthesise_start":
        print(f"\n[synthesiser] fusing {ev['using']} answer(s)...")
    elif t == "done":
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marshal_ai", description="Marshal reasoning loop")
    parser.add_argument("question", nargs="*", help="the question to reason about")
    parser.add_argument("--budget", type=float, default=None, help="USD budget for this question")
    args = parser.parse_args(argv)

    question = " ".join(args.question).strip() or input("Question: ").strip()

    if not settings.project_endpoint:
        print("PROJECT_ENDPOINT is not set. Put it in .env or the environment.", file=sys.stderr)
        return 2

    from .foundry import Foundry  # imported here so --help works without Azure

    marshal = Marshal(Foundry(settings.project_endpoint), emit=_print_event)
    result = marshal.answer(question, budget_usd=args.budget)

    print("\n" + "=" * 64)
    print("FINAL ANSWER")
    print("=" * 64)
    print(result["answer"])

    b = result["budget"]
    print(
        f"\n[budget] spent ${b['spent_usd']} of ${b['budget_usd']} "
        f"| final tier {b['tier']} | {b['charges']} model calls"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
