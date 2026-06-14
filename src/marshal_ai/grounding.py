"""Knowledge grounding for the reasoning loop.

A Grounding turns a query into retrieved passages with citations. The loop feeds
these into each worker's prompt so answers are grounded in real knowledge, not
just the model's parametric memory.

Phase 1 ships LocalGrounding: offline keyword-overlap retrieval over a corpus the
UI supplies (the project's own cards and notes). No model call, so it is free and
works with any provider, including the local Claude-subscription test path.

Foundry IQ (the Microsoft IQ layer that makes a submission eligible) and a live
web source plug in later as additional Grounding implementations behind this same
interface, so nothing in the loop has to change to adopt them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class Passage:
    text: str
    source: str
    score: float = 0.0


class Grounding:
    """Base: a no-op grounding that retrieves nothing."""

    enabled = False
    label = ""

    def retrieve(self, query: str, top_k: int = 3) -> list[Passage]:
        return []


class NullGrounding(Grounding):
    pass


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return {w for w in _WORD.findall((s or "").lower()) if len(w) > 2}


class LocalGrounding(Grounding):
    """Offline keyword-overlap retrieval over a supplied corpus (no model, no cost)."""

    enabled = True
    label = "project notes"

    def __init__(self, docs: list[dict]):
        # docs: [{"text": ..., "source": ...}]; split each into paragraph-ish chunks.
        self.chunks: list[Passage] = []
        for d in docs or []:
            src = d.get("source") or "project"
            for para in re.split(r"\n\s*\n|(?<=[.!?])\s{2,}", (d.get("text") or "").strip()):
                para = para.strip()
                if len(para) >= 24:
                    self.chunks.append(Passage(text=para, source=src))

    def retrieve(self, query: str, top_k: int = 3) -> list[Passage]:
        q = _tokens(query)
        if not q or not self.chunks:
            return []
        scored: list[tuple[float, Passage]] = []
        for c in self.chunks:
            ct = _tokens(c.text)
            overlap = len(q & ct)
            if overlap:
                # length-normalised overlap so long chunks don't dominate
                score = overlap / ((len(q) ** 0.5) + (len(ct) ** 0.5))
                scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Passage(text=c.text[:600], source=c.source, score=round(s, 3))
            for s, c in scored[:top_k]
        ]


class FoundryIQGrounding(Grounding):
    """Grounding via a Foundry IQ knowledge base (Azure AI Search agentic retrieval).

    This is the Microsoft IQ integration that makes a submission eligible. It calls
    the knowledge base's retrieve action, which plans queries, searches the attached
    sources in parallel, and returns ranked grounding documents with citations.

    Needs `azure-search-documents` (preview: `pip install --pre azure-search-documents`),
    a search service endpoint, a knowledge base name, and Azure credentials
    (DefaultAzureCredential, e.g. `az login`). Imports are lazy so the package loads
    without the SDK installed.
    """

    enabled = True
    label = "Foundry IQ"

    def __init__(self, endpoint: str, knowledge_base: str, credential=None):
        from azure.identity import DefaultAzureCredential
        from azure.search.documents.knowledgebases import KnowledgeBaseRetrievalClient

        self.endpoint = endpoint
        self.knowledge_base = knowledge_base
        self._client = KnowledgeBaseRetrievalClient(
            endpoint=endpoint,
            knowledge_base_name=knowledge_base,
            credential=credential or DefaultAzureCredential(),
        )

    def retrieve(self, query: str, top_k: int = 3) -> list[Passage]:
        from azure.search.documents.knowledgebases.models import (
            KnowledgeBaseMessage,
            KnowledgeBaseMessageTextContent,
            KnowledgeBaseRetrievalRequest,
        )

        request = KnowledgeBaseRetrievalRequest(
            messages=[
                KnowledgeBaseMessage(
                    role="user",
                    content=[KnowledgeBaseMessageTextContent(text=query)],
                )
            ]
        )
        return self._to_passages(self._client.retrieve(request), top_k)

    @staticmethod
    def _to_passages(result, top_k: int) -> list[Passage]:
        # The grounding payload is a JSON string at response[0].content[0].text:
        # [{"ref_id": "0", "title": ..., "terms": ..., "content": ...}, ...]
        try:
            text = result.response[0].content[0].text
        except (AttributeError, IndexError, TypeError):
            return []
        if not text:
            return []
        try:
            docs = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return [Passage(text=text[:600], source="Foundry IQ")]
        out: list[Passage] = []
        for d in docs if isinstance(docs, list) else []:
            if not isinstance(d, dict):
                continue
            content = (d.get("content") or d.get("text") or "").strip()
            if not content:
                continue
            title = d.get("title") or d.get("terms") or f"ref {d.get('ref_id', '?')}"
            out.append(Passage(text=content[:600], source=str(title)))
        return out[:top_k]


def make_grounding(knowledge=None, foundry_iq=None) -> Grounding:
    """Build the grounding for a run.

    Prefers a Foundry IQ knowledge base when `foundry_iq` is configured (the
    Microsoft IQ layer). Falls back to offline LocalGrounding over a UI-supplied
    corpus, then to NullGrounding. If the Azure SDK or service is unavailable, it
    degrades gracefully to the local/null path rather than failing the run.

    `knowledge`: a string or list of {"text", "source"} dicts (the UI corpus).
    `foundry_iq`: {"endpoint": ..., "knowledge_base": ...} or None.
    """
    if foundry_iq and foundry_iq.get("endpoint") and foundry_iq.get("knowledge_base"):
        try:
            return FoundryIQGrounding(foundry_iq["endpoint"], foundry_iq["knowledge_base"])
        except Exception:
            # SDK missing or client construction failed — degrade to local/null.
            pass
    if not knowledge:
        return NullGrounding()
    if isinstance(knowledge, str):
        knowledge = [{"text": knowledge, "source": "project"}]
    docs = [d for d in knowledge if (d or {}).get("text")]
    return LocalGrounding(docs) if docs else NullGrounding()
