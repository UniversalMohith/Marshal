# Foundry IQ grounding

Marshal grounds its reasoning in a **Foundry IQ knowledge base** — Microsoft's
agentic-retrieval layer on Azure AI Search. This is the Microsoft IQ integration the
Agents League requires for eligibility.

## How it fits the loop

Grounding lives behind one small interface (`src/marshal_ai/grounding.py`):

- `Grounding.retrieve(query) -> [Passage(text, source, score)]`
- `LocalGrounding` — offline keyword retrieval over a corpus the UI supplies (the
  project's own card notes). No model, no cost, works fully offline.
- `FoundryIQGrounding` — calls a Foundry IQ knowledge base's retrieve action, which
  plans queries, searches the attached sources in parallel, and returns ranked,
  cited grounding documents.
- `NullGrounding` — no-op.

The reasoning loop (`loop.py`) grounds **at the worker step**: before a worker
answers a sub-task it calls `grounding.retrieve(prompt)`, prepends the passages to
the prompt, records citations, and emits a `grounded` event the board shows as a
"Grounded · N" badge. Nothing in the loop changes between local and Foundry IQ.

## Selection and graceful degradation

`make_grounding(knowledge, foundry_iq)` (called by `server.py`) chooses:

1. **Foundry IQ** when `SEARCH_ENDPOINT` and `KNOWLEDGE_BASE` are both set.
2. else **LocalGrounding** over the UI-supplied corpus.
3. else **NullGrounding**.

If the Azure SDK is missing or the client can't be built, it degrades to the
local/null path rather than failing the run — so the app always works offline.

## Activating it (your Azure side)

1. Install the preview SDK (knowledge bases are preview):
   `pip install --pre azure-search-documents`
2. In Azure, create an **Azure AI Search** service, then a **Foundry IQ knowledge
   base** (Serverless / Developer tier is fine) with at least one knowledge source
   (Azure Blob, OneLake, SharePoint, a search index, or the web).
3. Sign in so `DefaultAzureCredential` works: `az login`.
4. Set in `.env`:
   - `SEARCH_ENDPOINT=https://<service>.search.windows.net`
   - `KNOWLEDGE_BASE=<your knowledge base name>`
5. Run Marshal. Worker sub-tasks now ground on the knowledge base, and the
   "Grounded · N" badges cite Foundry IQ sources.

## SDK shape (for reference)

`azure.search.documents.knowledgebases.KnowledgeBaseRetrievalClient.retrieve(...)`
returns the grounding payload at `result.response[0].content[0].text` as a JSON
array of `{ref_id, title, terms, content}` items, which `FoundryIQGrounding` maps
to `Passage(text=content, source=title)`.

## Status

- Phase 1 — local grounding + worker grounding pass + `grounded` event: done.
- Phase 2 — `FoundryIQGrounding` wired behind the interface (this doc): done in code;
  activate with the Azure steps above.
- Phase 3 — citations surfaced in chat and the result modal, and a selectable
  source picker (project / web): pending.
