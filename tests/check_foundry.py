"""Quick Microsoft Foundry connectivity check.

Loads your .env itself, creates one prompt agent, and does a tiny ask. Run this
after setting PROJECT_ENDPOINT and signing in (az login), to confirm auth, the
endpoint, and a model deployment all work before running the full loop.

    python tests/check_foundry.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from marshal_ai.config import settings  # noqa: E402 (also loads .env)
from marshal_ai.foundry import Foundry  # noqa: E402


def main() -> int:
    if not settings.project_endpoint:
        print("PROJECT_ENDPOINT is not set. Add it to .env first (see .env.example).")
        return 2

    print(f"Endpoint : {settings.project_endpoint}")
    print(f"Model    : {settings.orchestrator_model}  (the deployment name Marshal will use)")
    print(f"Agent    : {settings.agent_prefix}-check\n")

    try:
        foundry = Foundry(settings.project_endpoint)
    except Exception as exc:
        print(f"Could not create the Foundry client: {type(exc).__name__}: {exc}")
        return 2

    try:
        name = f"{settings.agent_prefix}-check"
        foundry.ensure_agent(
            name,
            settings.orchestrator_model,
            "You are a terse assistant. Answer in one short sentence.",
        )
        reply = foundry.ask(name, "Reply with exactly: Foundry connection OK.")
        text = reply.text.strip()
        print("--- reply ---")
        print(text or "(empty)")
        print(f"tokens: in={reply.input_tokens} out={reply.output_tokens}")
        print("\nConnectivity OK." if text else "\nConnected, but the model returned no text.")
        return 0 if text else 1
    except Exception as exc:
        print(f"Call failed: {type(exc).__name__}: {exc}\n")
        print("Common causes:")
        print("  - endpoint wrong (use the project endpoint: https://<res>.services.ai.azure.com/api/projects/<proj>)")
        print("  - model deployment name mismatch -> set ORCHESTRATOR_MODEL/WORKER_MODEL/CRITIC_MODEL/SYNTHESISER_MODEL in .env")
        print("  - your identity lacks access -> assign the 'Azure AI User' role on the project, then 'az login'")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
