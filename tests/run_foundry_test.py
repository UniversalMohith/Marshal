"""Run the full Marshal reasoning loop on Microsoft Foundry.

Requires PROJECT_ENDPOINT in .env, deployed model(s), and `az login`. Creates the
four agents in your project and runs one question end to end.

    python tests/run_foundry_test.py "your question"
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

try:  # the Windows console is cp1252; the model's answer may contain arrows etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from marshal_ai.config import settings  # noqa: E402 (loads .env, fixes PATH)
from marshal_ai.foundry import Foundry  # noqa: E402
from marshal_ai.loop import Marshal  # noqa: E402


def emit(ev: dict) -> None:
    t = ev.get("type")
    if t == "decompose_start":
        print(f"[orchestrator] decomposing (tier {ev['tier']}, up to {ev['allotment']})...", flush=True)
    elif t == "decomposed":
        for s in ev["subtasks"]:
            print(f"    - {s.get('id')}: {s.get('title','')}", flush=True)
    elif t == "workers_start":
        print(f"[workers] dispatching {ev['count']} x{ev['parallel']}", flush=True)
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
        "Should a small team adopt Kubernetes for a brand-new product?"
    if not settings.project_endpoint:
        print("PROJECT_ENDPOINT not set in .env.")
        return 2
    print(f"Question: {question}\nModel: {settings.worker_model} (Foundry)\n" + "-" * 60, flush=True)
    try:
        marshal = Marshal(Foundry(settings.project_endpoint), emit=emit)
        result = marshal.answer(question, budget_usd=settings.budget_usd)
    except Exception as exc:
        print(f"\nRun failed: {type(exc).__name__}: {exc}")
        return 2
    print("\n" + "=" * 60 + "\nFINAL ANSWER\n" + "=" * 60)
    print(result["answer"])
    b = result["budget"]
    print(f"\n[budget] spent ${b['spent_usd']} of ${b['budget_usd']} | tier {b['tier']} | {b['charges']} calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
