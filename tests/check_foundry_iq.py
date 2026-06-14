"""Check Foundry IQ grounding: retrieve from the configured knowledge base.

Requires SEARCH_ENDPOINT + KNOWLEDGE_BASE in .env, az login, and the preview SDK
(pip install --pre azure-search-documents).

    python tests/check_foundry_iq.py "your question"
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from marshal_ai.config import settings  # noqa: E402
from marshal_ai.grounding import make_grounding  # noqa: E402


def main() -> int:
    if not (settings.search_endpoint and settings.knowledge_base):
        print("SEARCH_ENDPOINT / KNOWLEDGE_BASE not set in .env.")
        return 2
    g = make_grounding(
        None,
        foundry_iq={"endpoint": settings.search_endpoint, "knowledge_base": settings.knowledge_base},
    )
    print(f"Grounding: {type(g).__name__}  (label={g.label}, enabled={g.enabled})")
    q = " ".join(sys.argv[1:]).strip() or \
        "Why is the Phoenix nighttime street grid so sharply visible from space?"
    print(f"Knowledge base: {settings.knowledge_base}")
    print(f"Query: {q}\n" + "-" * 60)
    try:
        res = g.retrieve(q, top_k=4)
    except Exception as exc:
        print(f"Retrieve failed: {type(exc).__name__}: {exc}")
        return 2
    if not res:
        print("No passages returned (index still ingesting, or a permissions issue).")
        return 1
    for i, p in enumerate(res, 1):
        print(f"[{i}] source={p.source}")
        print("   " + p.text[:240].replace("\n", " ") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
