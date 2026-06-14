"""Run the real Marshal reasoning loop locally, on your Claude subscription.

Routes every agent call through the Claude Code CLI (`claude -p`), so no API key
is needed. Optional CLAUDE_MODEL (alias: sonnet/opus/haiku; default sonnet).

    python tests/run_claude_test.py "your hard question"
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from marshal_ai.loop import Marshal  # noqa: E402  (also loads .env via config)
from marshal_ai.cli_provider import ClaudeCliProvider  # noqa: E402


def emit(ev: dict) -> None:
    t = ev.get("type")
    if t == "decompose_start":
        print(f"[orchestrator] decomposing (tier {ev['tier']}, up to {ev['allotment']} sub-tasks)...", flush=True)
    elif t == "decomposed":
        for s in ev["subtasks"]:
            print(f"    - {s.get('id')}: {s.get('title','')}", flush=True)
    elif t == "workers_start":
        print(f"[workers] dispatching {ev['count']} in parallel x{ev['parallel']} (tier {ev['tier']})", flush=True)
    elif t == "worker_done":
        print(f"    {ev['id']} done ({ev['chars']} chars)" + (" BLANK" if ev["blank"] else ""), flush=True)
    elif t == "graded":
        print(f"[critic] {ev['id']} -> {ev['grade']} (by {ev['by']})", flush=True)
    elif t == "rescope_start":
        print(f"[self-correct] re-scoping {', '.join(ev['ids'])}", flush=True)
    elif t == "synthesise_start":
        print(f"[synthesiser] fusing {ev['using']} answer(s)...", flush=True)
    elif t == "refused":
        print(f"[refused] {ev['reason']}", flush=True)
    elif t == "error":
        print(f"[error] {ev.get('message')}", flush=True)


def main() -> int:
    question = " ".join(sys.argv[1:]).strip() or \
        "Should a startup choose a monolith or microservices for its first product?"
    print(f"Question: {question}\n")

    provider = ClaudeCliProvider()
    print(f"Model: {provider.model} (via Claude CLI / subscription)\n" + "-" * 60, flush=True)

    marshal = Marshal(provider, emit=emit)
    try:
        result = marshal.answer(question, budget_usd=2.0)
    except Exception as exc:
        print(f"\nRun failed: {type(exc).__name__}: {exc}")
        return 2

    print("\n" + "=" * 60 + "\nFINAL ANSWER\n" + "=" * 60)
    print(result["answer"])
    b = result["budget"]
    print(f"\n[budget] spent ${b['spent_usd']} of ${b['budget_usd']} | tier {b['tier']} | {b['charges']} model calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
