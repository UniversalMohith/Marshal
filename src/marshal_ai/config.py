"""Central configuration for Hackathon Jarvis.

Values are read from environment variables (see .env.example) with sensible
defaults, so the system runs out of the box once a Foundry endpoint is supplied.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv is optional: the pure-logic modules import without it,
    # so the budget governor stays unit-testable with no dependencies installed.
    pass


@dataclass(frozen=True)
class ModelPricing:
    """Approximate USD price per 1,000,000 tokens.

    These figures drive the budget governor's maths in the demo. They are
    estimates: confirm the live numbers on the Azure pricing page and override
    them if you need exact accounting. The governor's logic does not depend on
    the absolute values, only on relative cost between model tiers.
    """

    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            (input_tokens / 1_000_000) * self.input_per_mtok
            + (output_tokens / 1_000_000) * self.output_per_mtok
        )


# Approximate pricing, clearly marked as estimates. Keyed by model deployment name.
PRICING: dict[str, ModelPricing] = {
    "gpt-5.1-mini": ModelPricing(0.25, 2.00),
    "gpt-5-mini": ModelPricing(0.25, 2.00),
    "gpt-4.1-mini": ModelPricing(0.40, 1.60),
    "gpt-5": ModelPricing(1.25, 10.00),
}
DEFAULT_PRICING = ModelPricing(0.25, 2.00)


def pricing_for(model: str) -> ModelPricing:
    return PRICING.get(model, DEFAULT_PRICING)


@dataclass(frozen=True)
class Settings:
    # Foundry connection.
    project_endpoint: str = os.getenv("PROJECT_ENDPOINT", "")

    # Model roles (deployment names in your Foundry project).
    orchestrator_model: str = os.getenv("ORCHESTRATOR_MODEL", "gpt-5.1-mini")
    worker_model: str = os.getenv("WORKER_MODEL", "gpt-5.1-mini")
    critic_model: str = os.getenv("CRITIC_MODEL", "gpt-5.1-mini")
    synthesiser_model: str = os.getenv("SYNTHESISER_MODEL", "gpt-5.1-mini")

    # Budget governor, per question.
    budget_usd: float = float(os.getenv("BUDGET_USD", "0.50"))
    budget_reserve_frac: float = float(os.getenv("BUDGET_RESERVE_FRAC", "0.15"))

    # Swarm sizing.
    max_workers: int = int(os.getenv("MAX_WORKERS", "5"))
    max_self_corrections: int = int(os.getenv("MAX_SELF_CORRECTIONS", "2"))

    # Quality thresholds, ported from the Nex lab blank/stub guards.
    stub_min_chars: int = int(os.getenv("STUB_MIN_CHARS", "400"))

    # Prefix for agent names created in the Foundry project.
    agent_prefix: str = os.getenv("AGENT_PREFIX", "marshal")


settings = Settings()
