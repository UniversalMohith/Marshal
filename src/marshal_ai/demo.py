"""A stubbed Foundry for the offline demo.

It runs the REAL reasoning loop and budget governor; only the model calls are
faked, with small delays so the live UI shows the swarm working. It is scripted
to return one thin answer, so the self-correction loop is visible in the demo.
"""
from __future__ import annotations

import json
import time

from .foundry import AgentReply

_LONG = (
    "## Findings\n"
    + ("Weighed the trade-offs and reached a clear, defensible position. " * 16)
    + "\n\nCONFIDENCE: high, grounded in the framing above."
)
_STUB = "It depends on the context.\nCONFIDENCE: low, not enough specifics."


class DemoFoundry:
    """Mimics marshal_ai.foundry.Foundry with scripted, delayed replies."""

    def __init__(self, latency: float = 0.6):
        self.latency = latency

    def ensure_agent(self, name, model, instructions):
        return None

    def new_conversation(self) -> str:
        return "conv_demo"

    def ask(self, agent_name: str, prompt: str, conversation_id=None) -> AgentReply:
        time.sleep(self.latency)
        if "orchestrator" in agent_name:
            if "Rewrite each" in prompt:  # a re-scope round
                text = json.dumps({"subtasks": [
                    {"id": "s2", "title": "Counter-arguments (sharpened)", "prompt": "THIN (refined)"},
                ]})
            else:  # initial decomposition into three angles
                text = json.dumps({"reasoning": "split into three angles", "subtasks": [
                    {"id": "s1", "title": "The main case", "prompt": "GOOD A"},
                    {"id": "s2", "title": "Counter-arguments", "prompt": "THIN"},
                    {"id": "s3", "title": "Evidence and examples", "prompt": "GOOD C"},
                ]})
            return AgentReply(text, 820, 240)
        if "critic" in agent_name:
            return AgentReply(json.dumps({"grade": "strong", "needs_redo": False, "rescope": ""}), 640, 70)
        if "synthesiser" in agent_name:
            return AgentReply(
                "## Final answer\n\nA balanced synthesis of the workers' findings, leading "
                "with the direct answer and the reasoning that supports it.\n\n"
                "CONFIDENCE: medium, bounded by this offline demo.",
                1300, 430,
            )
        # worker
        if "(refined)" in prompt:
            return AgentReply(_LONG, 520, 920)
        if "THIN" in prompt:
            return AgentReply(_STUB, 520, 40)
        return AgentReply(_LONG, 520, 920)
