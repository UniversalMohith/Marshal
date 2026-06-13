"""Agent roles: load the charters and register them as Foundry prompt agents."""
from __future__ import annotations

from pathlib import Path

from .config import settings

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

ROLE_MODELS = {
    "orchestrator": settings.orchestrator_model,
    "worker": settings.worker_model,
    "critic": settings.critic_model,
    "synthesiser": settings.synthesiser_model,
}


def charter(role: str) -> str:
    return (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")


def agent_name(role: str) -> str:
    return f"{settings.agent_prefix}-{role}"


def ensure_agents(foundry) -> dict[str, str]:
    """Create (or version) all four agents in the Foundry project. Returns role -> name."""
    names: dict[str, str] = {}
    for role, model in ROLE_MODELS.items():
        name = agent_name(role)
        foundry.ensure_agent(name, model, charter(role))
        names[role] = name
    return names
