"""Offline tests for LocalGrounding (phase 1 knowledge grounding). No model, no network."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import importlib.util  # noqa: E402

from marshal_ai.grounding import FoundryIQGrounding, LocalGrounding, NullGrounding, make_grounding  # noqa: E402

DOCS = [
    {"text": "Kubernetes adds operational burden for small teams: version upgrades, RBAC hardening, and networking.", "source": "card: k8s"},
    {"text": "Use Docker Compose or Cloud Run for early-stage products to keep the stack simple and cheap.", "source": "card: alternatives"},
    {"text": "The marketing site needs a clean hero section and fast load times on mobile.", "source": "card: marketing"},
]


def test_null_when_empty():
    g = make_grounding(None)
    assert isinstance(g, NullGrounding)
    assert g.enabled is False
    assert g.retrieve("anything") == []


def test_local_retrieves_relevant_source_first():
    g = make_grounding(DOCS)
    assert isinstance(g, LocalGrounding)
    res = g.retrieve("Should we adopt Kubernetes for a small team?")
    assert res, "expected at least one passage"
    assert res[0].source == "card: k8s"
    assert res[0].score > 0


def test_query_routes_to_marketing():
    res = make_grounding(DOCS).retrieve("hero section load times marketing")
    assert res and res[0].source == "card: marketing"


def test_string_corpus_defaults_source_project():
    res = make_grounding("Kubernetes is complex for small teams.").retrieve("kubernetes small team")
    assert res and res[0].source == "project"


def test_foundry_iq_ignored_when_unconfigured():
    # Empty endpoint/knowledge_base must not trigger Foundry IQ; fall back to local.
    g = make_grounding(DOCS, foundry_iq={"endpoint": "", "knowledge_base": ""})
    assert isinstance(g, LocalGrounding)


def _have_kb_sdk():
    try:
        return importlib.util.find_spec("azure.search.documents.knowledgebases") is not None
    except ModuleNotFoundError:
        return False


def test_foundry_iq_selected_or_degrades_gracefully():
    g = make_grounding(DOCS, foundry_iq={"endpoint": "https://x.search.windows.net", "knowledge_base": "kb"})
    have_sdk = _have_kb_sdk()
    if have_sdk:
        # SDK present: either the Foundry IQ client built, or it degraded to local — both are enabled.
        assert g.label in ("Foundry IQ", "project notes") and g.enabled
    else:
        # SDK absent: must degrade to the offline corpus rather than crash.
        assert isinstance(g, LocalGrounding)


def test_foundry_iq_class_is_lazy():
    # The class must be importable and declare itself enabled even without the Azure SDK.
    assert FoundryIQGrounding.enabled is True
    assert FoundryIQGrounding.label == "Foundry IQ"


if __name__ == "__main__":
    passed = 0
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"  {_name} ok")
            passed += 1
    print(f"all {passed} grounding tests passed")
