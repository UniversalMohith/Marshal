"""Run the full Marshal loop on Foundry, grounded on a project-notes corpus.

This mirrors what the board's Run Marshal does with source='project': it feeds the
project's own cards/notes as the knowledge corpus, so workers retrieve and cite them.
It surfaces the `grounded` events (the Grounded badge in the UI) and the per-sub-task
citations, then prints the synthesised answer and the budget.

    python tests/run_grounded_demo.py "your question"
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from marshal_ai.config import settings  # noqa: E402 (loads .env, fixes PATH)
from marshal_ai.foundry import Foundry  # noqa: E402
from marshal_ai.grounding import make_grounding  # noqa: E402
from marshal_ai.loop import Marshal  # noqa: E402

# A small project's own notes — the same shape the UI sends as `knowledge`.
KNOWLEDGE = [
    {"text": "Marshal is a local-first, single-user assistant. Its reasoning loop runs on "
             "Microsoft Foundry using a gpt-5-mini deployment.", "source": "project"},
    {"text": "The loop decomposes a question into sub-tasks, runs workers in parallel, a critic "
             "grades each answer, weak ones are re-scoped and re-run, then a synthesiser fuses "
             "the strong answers into one final answer.", "source": "card: reasoning loop"},
    {"text": "Every model call is metered by the BudgetGovernor: a per-question USD budget with a "
             "reserve fraction held back so the final synthesis is always affordable. As the budget "
             "tightens it drops to cheaper tiers and fewer parallel workers.", "source": "card: budget governor"},
    {"text": "Grounding feeds retrieved passages into each worker's prompt so answers cite real "
             "knowledge. LocalGrounding does keyword-overlap retrieval over the project's own notes; "
             "Foundry IQ is the Microsoft IQ layer via Azure AI Search.", "source": "card: grounding"},
]


def emit(ev: dict) -> None:
    t = ev.get("type")
    if t == "decompose_start":
        print(f"[orchestrator] decomposing (tier {ev['tier']}, up to {ev['allotment']})...", flush=True)
    elif t == "decomposed":
        for s in ev["subtasks"]:
            print(f"    - {s.get('id')}: {s.get('title','')}", flush=True)
    elif t == "workers_start":
        print(f"[workers] dispatching {ev['count']} x{ev['parallel']}", flush=True)
    elif t == "grounded":
        srcs = ", ".join(s.get("source", "?") for s in ev.get("sources", []))
        print(f"  [grounded] {ev['id']} on {ev['label']} -> {srcs}", flush=True)
    elif t == "worker_done":
        print(f"    {ev['id']} done ({ev['chars']} chars)" + (" BLANK" if ev['blank'] else ""), flush=True)
    elif t == "graded":
        print(f"[critic] {ev['id']} -> {ev['grade']} (by {ev['by']})", flush=True)
    elif t == "rescope_start":
        print(f"[self-correct] re-scoping {', '.join(ev['ids'])}", flush=True)
    elif t == "synthesise_start":
        print(f"[synthesiser] fusing {ev['using']} answer(s)...", flush=True)
    elif t == "error":
        print(f"[error] {ev.get('message')}", flush=True)


def main() -> int:
    question = " ".join(sys.argv[1:]).strip() or \
        "How does Marshal keep its multi-agent reasoning both grounded and within budget?"
    if not settings.project_endpoint:
        print("PROJECT_ENDPOINT not set in .env.")
        return 2
    grounding = make_grounding(KNOWLEDGE, None)  # source='project' (the reconciled default)
    print(f"Question: {question}\nModel: {settings.worker_model} (Foundry)\n"
          f"Grounding: {grounding.label} ({len(KNOWLEDGE)} notes)\n" + "-" * 60, flush=True)
    try:
        marshal = Marshal(Foundry(settings.project_endpoint), emit=emit, grounding=grounding)
        result = marshal.answer(question, budget_usd=settings.budget_usd)
    except Exception as exc:
        print(f"\nRun failed: {type(exc).__name__}: {exc}")
        return 2

    print("\n" + "=" * 60 + "\nFINAL ANSWER\n" + "=" * 60)
    print(result["answer"])

    print("\n" + "-" * 60 + "\nCITATIONS PER SUB-TASK")
    for r in result["results"]:
        cites = ", ".join(c.get("source", "?") for c in r.citations) or "(none)"
        print(f"  {r.id} [{r.grade}]: {cites}")

    b = result["budget"]
    print(f"\n[budget] spent ${b['spent_usd']} of ${b['budget_usd']} | tier {b['tier']} | {b['charges']} calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
