"""The reasoning loop: decompose, work in parallel, grade, self-correct, synthesise.

Every model call runs through a Foundry agent and is metered by the BudgetGovernor.
Each stage emits an event so a UI (or the CLI) can show the swarm working live.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from . import agents
from .budget import BudgetGovernor
from .config import settings
from .grounding import Grounding, NullGrounding

Emit = Callable[[dict], None]


def _noop(event: dict) -> None:
    pass


@dataclass
class SubResult:
    id: str
    title: str
    prompt: str
    answer: str = ""
    grade: str = "pending"          # pending | strong | thin | blank
    confidence: str = ""            # high | medium | low
    rescope: str = ""               # critic's note on how to sharpen, if weak
    redo_count: int = 0
    citations: list = field(default_factory=list)   # grounding sources used, if any


# -- small, honest helpers (no token cost) ---------------------------------
def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(text[i : j + 1])
    except json.JSONDecodeError:
        return None


def _looks_blank(text: str) -> bool:
    return not (text or "").strip()


def _looks_stub(text: str) -> bool:
    return len((text or "").strip()) < settings.stub_min_chars


def _confidence_of(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if "CONFIDENCE" in line.upper():
            low = line.lower()
            for level in ("high", "medium", "low"):
                if level in low:
                    return level
    return ""


def guardrail(question: str) -> str | None:
    """Return a refusal message if the question should not be processed, else None."""
    q = (question or "").strip()
    if len(q) < 3:
        return "The question is empty or too short. Please ask something specific."
    return None


class Marshal:
    """Drives the orchestrator, workers, critic and synthesiser through one question."""

    def __init__(self, foundry, emit: Emit = _noop, grounding: Grounding | None = None):
        self.foundry = foundry
        self.emit = emit
        self.grounding = grounding or NullGrounding()
        self.names = agents.ensure_agents(foundry)

    def answer(self, question: str, budget_usd: float | None = None) -> dict:
        gov = BudgetGovernor(
            budget_usd or settings.budget_usd, settings.budget_reserve_frac
        )
        self.emit({"type": "start", "question": question, "budget": gov.snapshot()})

        refusal = guardrail(question)
        if refusal:
            self.emit({"type": "refused", "reason": refusal})
            return {"answer": refusal, "refused": True, "budget": gov.snapshot(), "results": []}

        # 1. Decompose into scoped sub-tasks.
        subtasks = self._decompose(question, gov)
        results = [
            SubResult(s.get("id", f"s{i+1}"), s.get("title", s.get("id", "")), s["prompt"])
            for i, s in enumerate(subtasks)
        ]

        # 2. Work them in parallel.
        self._run_workers(results, gov)

        # 3. Grade, and self-correct the weak ones while budget allows.
        for round_no in range(settings.max_self_corrections + 1):
            self._grade(results, gov)
            weak = [r for r in results if r.grade in ("thin", "blank")]
            if not weak or round_no == settings.max_self_corrections:
                break
            if not gov.self_correction_allowed():
                self.emit({"type": "degrade", "stage": "self_correction", "budget": gov.snapshot()})
                break
            self._rescope(question, weak, gov)
            self._run_workers(weak, gov, redo=True)

        # 4. Synthesise the final answer (paid from the reserve the governor held back).
        final = self._synthesise(question, results, gov)
        self.emit({"type": "done", "answer": final, "budget": gov.snapshot()})
        return {"answer": final, "refused": False, "results": results, "budget": gov.snapshot()}

    # -- stages -------------------------------------------------------------
    def _decompose(self, question: str, gov: BudgetGovernor) -> list[dict]:
        allot = max(1, gov.allot_workers(settings.max_workers, settings.max_workers))
        self.emit({"type": "decompose_start", "allotment": allot, "tier": gov.tier().value})
        prompt = (
            f"QUESTION:\n{question}\n\n"
            f"WORKER BUDGET THIS ROUND: {allot} sub-tasks.\n"
            "Decompose now. STRICT JSON ONLY."
        )
        reply = self.foundry.ask(self.names["orchestrator"], prompt)
        gov.record("orchestrator", settings.orchestrator_model, reply.input_tokens, reply.output_tokens)
        plan = _extract_json(reply.text) or {}
        subtasks = [s for s in (plan.get("subtasks") or []) if s.get("prompt")]
        if not subtasks:
            # Defensive fallback: treat the whole question as one task.
            subtasks = [{"id": "s1", "title": "Answer the question", "prompt": question}]
        subtasks = subtasks[:allot]
        self.emit({
            "type": "decomposed",
            "subtasks": [{"id": s.get("id"), "title": s.get("title", "")} for s in subtasks],
            "budget": gov.snapshot(),
        })
        return subtasks

    def _run_workers(self, results: list[SubResult], gov: BudgetGovernor, redo: bool = False) -> None:
        pending = [r for r in results if (redo or not r.answer)]
        if not pending:
            return
        parallel = max(1, gov.allot_workers(len(pending), settings.max_workers))
        self.emit({"type": "workers_start", "count": len(pending), "parallel": parallel, "tier": gov.tier().value})

        def run_one(r: SubResult) -> None:
            self.emit({"type": "worker_start", "id": r.id, "title": r.title})
            prompt = r.prompt
            if self.grounding.enabled:
                try:
                    passages = self.grounding.retrieve(r.prompt)
                except Exception:
                    passages = []  # a grounding failure degrades to ungrounded, never blanks the worker
                if passages:
                    block = "\n\n".join(f"[{i+1}] ({p.source}) {p.text}" for i, p in enumerate(passages))
                    prompt = (
                        f"KNOWLEDGE (retrieved from {self.grounding.label}; cite as [n] where you use it):\n"
                        f"{block}\n\n---\n\n{r.prompt}"
                    )
                    r.citations = [
                        {"source": p.source, "score": p.score, "text": p.text[:280]} for p in passages
                    ]
                    self.emit({"type": "grounded", "id": r.id, "label": self.grounding.label, "sources": r.citations})
            reply = self.foundry.ask(self.names["worker"], prompt)
            gov.record("worker", settings.worker_model, reply.input_tokens, reply.output_tokens)
            r.answer = reply.text
            r.confidence = _confidence_of(reply.text)
            if redo:
                r.redo_count += 1
            self.emit({
                "type": "worker_done",
                "id": r.id,
                "chars": len(r.answer.strip()),
                "blank": _looks_blank(r.answer),
                "budget": gov.snapshot(),
            })

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [pool.submit(run_one, r) for r in pending]
            for _ in as_completed(futures):
                pass

    def _grade(self, results: list[SubResult], gov: BudgetGovernor) -> None:
        for r in results:
            if r.grade == "strong":
                continue
            # Cheap local guards first: honest failure handling at zero token cost.
            if _looks_blank(r.answer):
                r.grade = "blank"
                self.emit({"type": "graded", "id": r.id, "grade": "blank", "by": "guard"})
                continue
            if _looks_stub(r.answer):
                r.grade = "thin"
                self.emit({"type": "graded", "id": r.id, "grade": "thin", "by": "guard"})
                continue
            # Otherwise let the critic judge the reasoning.
            prompt = (
                f"SUB-TASK:\n{r.prompt}\n\nWORKER ANSWER:\n{r.answer}\n\n"
                "Grade it. STRICT JSON ONLY."
            )
            reply = self.foundry.ask(self.names["critic"], prompt)
            gov.record("critic", settings.critic_model, reply.input_tokens, reply.output_tokens)
            verdict = _extract_json(reply.text) or {}
            grade = str(verdict.get("grade", "strong")).lower()
            if grade not in ("strong", "thin", "blank"):
                grade = "strong"
            r.grade = grade
            r.rescope = str(verdict.get("rescope", ""))
            self.emit({"type": "graded", "id": r.id, "grade": grade, "by": "critic", "budget": gov.snapshot()})

    def _rescope(self, question: str, weak: list[SubResult], gov: BudgetGovernor) -> None:
        notes = "\n".join(f"- {r.id} ({r.title}) graded {r.grade}: {r.rescope}" for r in weak)
        self.emit({"type": "rescope_start", "ids": [r.id for r in weak]})
        prompt = (
            f"ORIGINAL QUESTION:\n{question}\n\n"
            "These sub-tasks came back weak. Rewrite each into a sharper, narrower, "
            "self-contained prompt. Keep the same ids.\n"
            f"{notes}\n\n"
            "Return STRICT JSON with the same shape: "
            '{"subtasks": [{"id": ..., "title": ..., "prompt": ...}]}'
        )
        reply = self.foundry.ask(self.names["orchestrator"], prompt)
        gov.record("orchestrator", settings.orchestrator_model, reply.input_tokens, reply.output_tokens)
        plan = _extract_json(reply.text) or {}
        by_id = {s["id"]: s for s in (plan.get("subtasks") or []) if s.get("id") and s.get("prompt")}
        for r in weak:
            if r.id in by_id:
                r.prompt = by_id[r.id]["prompt"]
                r.title = by_id[r.id].get("title", r.title)
            r.answer = ""        # clear so the worker re-runs
            r.grade = "pending"
        self.emit({"type": "rescoped", "ids": list(by_id.keys()), "budget": gov.snapshot()})

    def _synthesise(self, question: str, results: list[SubResult], gov: BudgetGovernor) -> str:
        strong = [r for r in results if r.grade == "strong"]
        used = strong or results
        blocks = "\n\n".join(
            f"### {r.title} (grade: {r.grade}, confidence: {r.confidence or 'n/a'})\n{r.answer.strip()}"
            for r in used
            if r.answer.strip()
        )
        self.emit({"type": "synthesise_start", "using": len(used)})
        prompt = (
            f"ORIGINAL QUESTION:\n{question}\n\n"
            f"GRADED WORKER ANSWERS:\n{blocks or '(no usable answers were produced)'}\n\n"
            "Synthesise the final answer now."
        )
        reply = self.foundry.ask(self.names["synthesiser"], prompt)
        gov.record("synthesiser", settings.synthesiser_model, reply.input_tokens, reply.output_tokens)
        return reply.text
