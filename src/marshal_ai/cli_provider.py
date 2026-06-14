"""A provider that runs Marshal's agents through the Claude Code CLI (`claude -p`).

This uses your Claude *subscription* (the CLI's OAuth login) instead of a
pay-as-you-go API key. Each agent call shells out to `claude -p` with the role's
charter as the system prompt (via a temp file, so multiline/JSON content isn't
mangled) and the task as stdin, and parses the JSON result for text + token usage.

Local testing only; the submission runs on Microsoft Foundry.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile

from .foundry import AgentReply


def _find_claude() -> str:
    return (
        os.getenv("CLAUDE_CMD")
        or shutil.which("claude.cmd")
        or shutil.which("claude")
        or "claude"
    )


class ClaudeCliProvider:
    def __init__(self, model: str | None = None, timeout: int = 240):
        self.cmd = _find_claude()
        self.model = model or os.getenv("CLAUDE_MODEL", "sonnet")
        self.timeout = timeout
        self._sys_files: dict[str, str] = {}

    def ensure_agent(self, name: str, model: str, instructions: str):
        f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        f.write(instructions)
        f.close()
        self._sys_files[name] = f.name
        atexit.register(lambda p=f.name: os.path.exists(p) and os.remove(p))
        return None

    def new_conversation(self) -> str:
        return "conv_cli"

    def ask(self, agent_name: str, prompt: str, conversation_id=None) -> AgentReply:
        args = [
            self.cmd, "-p",
            "--model", self.model,
            "--tools", "",                 # pure reasoning, no tool detours
            "--output-format", "json",
            "--no-session-persistence",
        ]
        sysf = self._sys_files.get(agent_name)
        if sysf:
            args += ["--system-prompt-file", sysf]
        proc = subprocess.run(
            args, input=prompt, text=True, capture_output=True, timeout=self.timeout
        )
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out)
            usage = data.get("usage", {}) or {}
            return AgentReply(
                text=data.get("result", "") or "",
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            return AgentReply(text=out or (proc.stderr or "")[:600], input_tokens=0, output_tokens=0)
