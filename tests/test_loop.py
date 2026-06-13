"""Offline tests for the reasoning loop, using a fake Foundry (no Azure needed).

These prove the orchestration mechanics (decompose, parallel work, grading,
self-correction, synthesis, honest blank/stub guards) independently of the live
service, so going live only tests the Azure connection, not the loop.

Run directly (python tests/test_loop.py) or under pytest.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from marshal_ai.foundry import AgentReply  # noqa: E402
from marshal_ai.loop import Marshal  # noqa: E402

LONG = "## Answer\n" + ("reasoning " * 60) + "\nCONFIDENCE: high, well grounded."
STUB = "Not sure."


class FakeFoundry:
    """Scripted Foundry: orchestrator/critic/synthesiser/worker by agent name."""

    def __init__(self, stub_first: bool = False):
        self.stub_first = stub_first

    def ensure_agent(self, name, model, instructions):
        return None

    def ask(self, agent_name: str, prompt: str, conversation_id=None) -> AgentReply:
        if "orchestrator" in agent_name:
            if "Rewrite each" in prompt:  # this is a re-scope request
                text = json.dumps({"subtasks": [
                    {"id": "s1", "title": "A", "prompt": "STUB_TASK (refined)"},
                ]})
            else:  # initial decomposition
                s1 = "STUB_TASK" if self.stub_first else "GOOD_TASK_A"
                text = json.dumps({"reasoning": "split", "subtasks": [
                    {"id": "s1", "title": "A", "prompt": s1},
                    {"id": "s2", "title": "B", "prompt": "GOOD_TASK_B"},
                ]})
        elif "critic" in agent_name:
            text = json.dumps({"grade": "strong", "needs_redo": False, "rescope": ""})
        elif "synthesiser" in agent_name:
            text = "FINAL: a fused answer.\nCONFIDENCE: medium, limited inputs."
        else:  # worker
            if "(refined)" in prompt:
                text = LONG
            elif "STUB_TASK" in prompt:
                text = STUB
            else:
                text = LONG
        return AgentReply(text=text, input_tokens=100, output_tokens=200)


def test_happy_path_all_strong():
    j = Marshal(FakeFoundry(stub_first=False))
    out = j.answer("What should we do?", budget_usd=0.50)
    assert out["refused"] is False
    assert "FINAL" in out["answer"]
    assert all(r.grade == "strong" for r in out["results"])
    assert all(r.redo_count == 0 for r in out["results"])
    # 1 decompose + 2 workers + 2 critic + 1 synth.
    assert out["budget"]["charges"] == 6


def test_self_correction_recovers_a_thin_answer():
    j = Marshal(FakeFoundry(stub_first=True))
    out = j.answer("What should we do?", budget_usd=0.50)
    assert out["refused"] is False
    by_id = {r.id: r for r in out["results"]}
    assert by_id["s1"].redo_count == 1          # the thin one was re-worked
    assert by_id["s1"].grade == "strong"         # and recovered
    assert by_id["s2"].grade == "strong"
    # 1 decompose + 2 workers + 1 critic(s2) + 1 rescope + 1 worker(s1) + 1 critic(s1) + 1 synth.
    assert out["budget"]["charges"] == 8


def test_empty_question_is_refused():
    j = Marshal(FakeFoundry())
    out = j.answer("   ", budget_usd=0.50)
    assert out["refused"] is True
    assert out["budget"]["charges"] == 0


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
    print("\nall loop tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
