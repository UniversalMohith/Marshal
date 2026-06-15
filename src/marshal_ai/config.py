"""Central configuration for Marshal.

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

# Local-dev convenience (Windows): a long-lived server started before the Azure
# CLI was installed can carry a stale PATH, so DefaultAzureCredential can't find
# `az` and auth fails. Ensure the standard install dir is on PATH. Harmless on
# other OSes, and irrelevant in production (Azure hosting uses managed identity).
if os.name == "nt":
    _az_dir = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"
    if os.path.isdir(_az_dir) and _az_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _az_dir + os.pathsep + os.environ.get("PATH", "")


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
    "gpt-5.4-mini": ModelPricing(0.25, 2.00),
    "gpt-5.4-nano": ModelPricing(0.10, 0.80),
    "gpt-5.1-mini": ModelPricing(0.25, 2.00),
    "gpt-5-mini": ModelPricing(0.25, 2.00),
    "gpt-4.1-mini": ModelPricing(0.40, 1.60),
    "gpt-5": ModelPricing(1.25, 10.00),
}
DEFAULT_PRICING = ModelPricing(0.25, 2.00)


def pricing_for(model: str) -> ModelPricing:
    return PRICING.get(model, DEFAULT_PRICING)


@dataclass  # not frozen: the in-app "Connect Foundry" flow updates these at runtime
class Settings:
    # Foundry connection.
    project_endpoint: str = os.getenv("PROJECT_ENDPOINT", "")

    # Foundry IQ grounding (Azure AI Search knowledge base). Both must be set to activate.
    search_endpoint: str = os.getenv("SEARCH_ENDPOINT", "")
    knowledge_base: str = os.getenv("KNOWLEDGE_BASE", "")

    # Model roles (deployment names in your Foundry project).
    orchestrator_model: str = os.getenv("ORCHESTRATOR_MODEL", "gpt-5.4-mini")
    worker_model: str = os.getenv("WORKER_MODEL", "gpt-5.4-mini")
    critic_model: str = os.getenv("CRITIC_MODEL", "gpt-5.4-mini")
    synthesiser_model: str = os.getenv("SYNTHESISER_MODEL", "gpt-5.4-mini")

    # Budget governor, per question.
    budget_usd: float = float(os.getenv("BUDGET_USD", "0.50"))
    budget_reserve_frac: float = float(os.getenv("BUDGET_RESERVE_FRAC", "0.15"))

    # Where Marshal's models run: "foundry" (Azure AI Foundry, the default and
    # recommended path) or "local" (a local Ollama daemon). Local is much slower
    # without a strong PC; the UI warns before switching. Local routing in the
    # reasoning loop is not wired yet, so this is a stored preference for now.
    model_source: str = os.getenv("MODEL_SOURCE", "foundry")

    # Subagents (coordinated with the subagents feature).
    subagents_enabled: bool = os.getenv("SUBAGENTS_ENABLED", "1") not in ("0", "false", "False", "")
    ai_can_spawn_subagents: bool = os.getenv("AI_CAN_SPAWN_SUBAGENTS", "0") in ("1", "true", "True")

    # Cumulative USD spent across ALL runs, accumulated by the server after each
    # run. Not from env; loaded from .marshal.json on startup (apply_runtime_config).
    total_spend_usd: float = 0.0
    total_runs: int = 0

    # Swarm sizing.
    max_workers: int = int(os.getenv("MAX_WORKERS", "5"))
    max_self_corrections: int = int(os.getenv("MAX_SELF_CORRECTIONS", "2"))

    # Quality thresholds, ported from the Nex lab blank/stub guards.
    stub_min_chars: int = int(os.getenv("STUB_MIN_CHARS", "400"))

    # Prefix for agent names created in the Foundry project.
    agent_prefix: str = os.getenv("AGENT_PREFIX", "marshal")


settings = Settings()

# Runtime connection config, written by the in-app "Connect Foundry" flow and applied over .env
# on startup so a user can connect their own Foundry without editing files. Gitignored.
import json  # noqa: E402
import pathlib  # noqa: E402

_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / ".marshal.json"
_RUNTIME_KEYS = (
    "project_endpoint", "orchestrator_model", "worker_model", "critic_model",
    "synthesiser_model", "search_endpoint", "knowledge_base",
    # Settings: budget + model source + subagents.
    "budget_usd", "model_source", "subagents_enabled", "ai_can_spawn_subagents",
    "total_spend_usd", "total_runs",
)

# Keys whose value may legitimately be falsy (0, 0.0, False) and must still be applied.
_RUNTIME_FALSY_OK = ("budget_usd", "subagents_enabled", "ai_can_spawn_subagents",
                     "total_spend_usd", "total_runs")


def apply_runtime_config() -> None:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            for k in _RUNTIME_KEYS:
                if k in data and (data[k] or k in _RUNTIME_FALSY_OK):
                    setattr(settings, k, data[k])
    except Exception:
        pass


def save_runtime_config(data: dict) -> None:
    try:
        existing = {}
        if _CONFIG_PATH.exists():
            existing = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        existing.update({k: v for k, v in data.items() if k in _RUNTIME_KEYS and v is not None})
        _CONFIG_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception:
        pass


# ===== Master file (the user's profile). Server-owned, append-only, never returned verbatim
# to the browser. Marshal auto-appends learnings; the user can only download it. =====
import datetime as _dt  # noqa: E402

_PROFILE_MAX_ENTRIES = 200
_PROFILE_ENTRY_CHARS = 600
_PROFILE_PROMPT_CHARS = 6000  # bound on the text injected into a chat prompt


def _read_config() -> dict:
    try:
        if _CONFIG_PATH.exists():
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_config(data: dict) -> None:
    try:
        _CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def load_profile() -> dict:
    """Return the raw profile dict: {'entries': [...], 'updated_at': str|None}."""
    prof = _read_config().get("profile") or {}
    entries = prof.get("entries") or []
    if not isinstance(entries, list):
        entries = []
    return {"entries": entries, "updated_at": prof.get("updated_at")}


def append_profile_entries(items: list[dict]) -> int:
    """Append learnings to the master file. Each item: {'text': str, 'source': str}.

    De-dupes against recent entries, enforces per-entry and total caps. Returns how many
    were actually added. Never raises (best-effort persistence, like the rest of the file).
    """
    data = _read_config()
    prof = data.get("profile") or {}
    entries = prof.get("entries") if isinstance(prof.get("entries"), list) else []
    recent_norms = {_norm(e.get("text", "")) for e in entries[-50:]}

    added = 0
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for it in items or []:
        text = (it.get("text") or "").strip()[:_PROFILE_ENTRY_CHARS]
        if not text:
            continue
        n = _norm(text)
        if not n or n in recent_norms:
            continue
        entries.append({"ts": now, "source": (it.get("source") or "chat").strip()[:40], "text": text})
        recent_norms.add(n)
        added += 1

    if added:
        if len(entries) > _PROFILE_MAX_ENTRIES:
            entries = entries[-_PROFILE_MAX_ENTRIES:]  # FIFO trim
        prof["entries"] = entries
        prof["updated_at"] = now
        data["profile"] = prof
        _write_config(data)
    return added


def profile_prompt_text() -> str:
    """Newest-first plain text of the master file, bounded for prompt injection."""
    entries = load_profile()["entries"]
    out, used = [], 0
    for e in reversed(entries):  # newest first so the most recent learnings survive the cap
        line = (e.get("text") or "").strip()
        if not line:
            continue
        if used + len(line) + 1 > _PROFILE_PROMPT_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def profile_markdown() -> str:
    """The downloadable .md rendering of the whole master file (chronological)."""
    p = load_profile()
    entries = p["entries"]
    lines = [
        "# Marshal master file",
        "",
        "What Marshal has learned about how you like to work. This file is maintained "
        "automatically by Marshal and is not editable in the app.",
        "",
        f"- Entries: {len(entries)}",
        f"- Last updated: {p['updated_at'] or 'never'}",
        "",
        "## Learnings",
        "",
    ]
    if not entries:
        lines.append("_Empty. Marshal has not learned anything yet. Add context in setup or just start chatting._")
    else:
        for e in entries:
            src = e.get("source") or "chat"
            ts = e.get("ts") or ""
            lines.append(f"- **[{src}]** {e.get('text', '').strip()}  \n  _{ts}_")
    return "\n".join(lines) + "\n"


def profile_status() -> dict:
    """Small summary for the Settings status line. Never exposes entry text."""
    p = load_profile()
    return {"count": len(p["entries"]), "updated_at": p["updated_at"]}


def add_spend(amount_usd: float) -> dict:
    """Accumulate one run's spend into the persisted lifetime total. Returns the new totals.

    Reads the file fresh (not just in-memory settings) so concurrent runs and
    restarts don't clobber the running total. Always succeeds; never raises.
    """
    try:
        amount = float(amount_usd)
        if not (amount == amount) or amount < 0:  # NaN or negative -> ignore
            amount = 0.0
    except (TypeError, ValueError):
        amount = 0.0
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8")) if _CONFIG_PATH.exists() else {}
    except Exception:
        data = {}
    total = float(data.get("total_spend_usd") or 0.0) + amount
    runs = int(data.get("total_runs") or 0) + (1 if amount > 0 else 0)
    settings.total_spend_usd = total
    settings.total_runs = runs
    save_runtime_config({"total_spend_usd": round(total, 6), "total_runs": runs})
    return {"total_spend_usd": round(total, 6), "total_runs": runs}


apply_runtime_config()
