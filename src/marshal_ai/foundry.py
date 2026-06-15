"""Thin wrapper over Microsoft Foundry's Agent Service (azure-ai-projects 2.x).

Verified against the Microsoft Learn quickstart (doc dated March 2026) and the
live 2.2.0 package: declarative "prompt agents" are created with
PromptAgentDefinition and driven through the OpenAI-compatible Responses API
exposed by project.get_openai_client().

The Azure SDK is imported lazily inside the constructor, so the rest of the
package (the loop, the governor) imports and unit-tests with no Azure installed
and no live connection.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class AgentReply:
    text: str
    input_tokens: int
    output_tokens: int


class Foundry:
    """Creates prompt agents and runs single-shot or multi-turn requests."""

    def __init__(self, endpoint: str):
        if not endpoint:
            raise ValueError(
                "No Foundry project endpoint. Set PROJECT_ENDPOINT to "
                "https://<resource>.ai.azure.com/api/projects/<project>."
            )
        from azure.identity import DefaultAzureCredential
        from azure.ai.projects import AIProjectClient

        self.endpoint = endpoint
        self.project = AIProjectClient(
            endpoint=endpoint,
            credential=DefaultAzureCredential(),
        )
        self.openai = self.project.get_openai_client()

    def ensure_agent(self, name: str, model: str, instructions: str):
        """Create a prompt agent (or add a version to an existing one)."""
        from azure.ai.projects.models import PromptAgentDefinition

        return self.project.agents.create_version(
            agent_name=name,
            definition=PromptAgentDefinition(model=model, instructions=instructions),
        )

    def new_conversation(self) -> str:
        """Open a conversation for multi-turn history. Returns its id."""
        return self.openai.conversations.create().id

    def ask(
        self,
        agent_name: str,
        prompt: str,
        conversation_id: str | None = None,
    ) -> AgentReply:
        """Send one input to a named agent and return its reply plus token usage."""
        kwargs: dict = {
            "input": prompt,
            "extra_body": {
                "agent_reference": {"name": agent_name, "type": "agent_reference"}
            },
        }
        if conversation_id:
            kwargs["conversation"] = conversation_id
        # One retry on a transient failure (rate limit, timeout, 5xx) so a single
        # hiccup rides through rather than blanking a worker or crashing the run.
        response = None
        for attempt in range(2):
            try:
                response = self.openai.responses.create(**kwargs)
                break
            except Exception:
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise
        return AgentReply(
            text=getattr(response, "output_text", "") or "",
            input_tokens=_usage(response, "input_tokens"),
            output_tokens=_usage(response, "output_tokens"),
        )


def _usage(response, field_name: str) -> int:
    """Pull a token count off the response usage object, tolerating naming drift."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    candidates = [
        field_name,
        field_name.replace("input", "prompt").replace("output", "completion"),
    ]
    for attr in candidates:
        val = getattr(usage, attr, None)
        if val is not None:
            return int(val)
    return 0
