"""A Claude (Anthropic API) provider for the Marshal reasoning loop.

Drop-in alternative to the Foundry client for LOCAL testing: it exposes the same
ensure_agent()/ask() surface the loop expects, but routes calls to the Anthropic
Messages API. Each "agent" is just a (model, system prompt) pair, since the API is
stateless.

Credentials resolve from ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth
login` profile (see config.py, which loads .env). Model via CLAUDE_MODEL env,
default claude-opus-4-8.

Note: this is for local testing only. The hackathon submission runs on Microsoft
Foundry (which also hosts Claude); this just lets us exercise the real loop before
the Foundry endpoint is wired.
"""
from __future__ import annotations

import os

from .foundry import AgentReply


class AnthropicProvider:
    def __init__(self, model: str | None = None, max_tokens: int = 8000):
        import anthropic  # imported lazily so the package imports without the SDK

        self.client = anthropic.Anthropic()  # resolves key/token/profile from env
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-opus-4-8")
        self.max_tokens = max_tokens
        self._systems: dict[str, str] = {}

    def ensure_agent(self, name: str, model: str, instructions: str):
        # The Foundry model name is ignored; we run every role on the chosen Claude model.
        self._systems[name] = instructions
        return None

    def new_conversation(self) -> str:
        return "conv_local"

    def ask(self, agent_name: str, prompt: str, conversation_id=None) -> AgentReply:
        system = self._systems.get(agent_name, "")
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        usage = resp.usage
        return AgentReply(
            text=text,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
