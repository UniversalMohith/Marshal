"""FastAPI server: runs Marshal and streams the swarm's events to the live UI.

Modes:
- live: needs PROJECT_ENDPOINT + az login; runs the real loop on Foundry.
- demo: runs the real loop and governor with a stubbed model (marshal_ai.demo),
  so the UI works with no connection and serves as a reliable fallback demo.

Run:  python -m uvicorn marshal_ai.server:app --app-dir src --port 8000
"""
from __future__ import annotations

import asyncio
import math
import mimetypes
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from .config import settings

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

app = FastAPI(title="Marshal")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "endpoint_configured": bool(settings.project_endpoint),
        "foundry_iq_configured": bool(settings.search_endpoint and settings.knowledge_base),
    }


# ===== Run on localhost — serve a built site's files statically at a real local URL. =====
# Writes the given files to a temp dir (gitignored) and serves them at /run/<id>/. STATIC ONLY:
# files are served as-is, never executed, so a backend file (server.js, routes/web.php) is served as
# text, not run. This covers static sites and single-page apps; it does not run arbitrary server code.
_RUNS_DIR = Path(__file__).resolve().parents[2] / ".marshal_runs"


def _safe_join(base: Path, rel: str):
    """Join rel onto base, returning None if it escapes base (path-traversal guard)."""
    try:
        p = (base / rel).resolve()
        p.relative_to(base.resolve())
        return p
    except (ValueError, OSError):
        return None


def _prune_runs(keep: int = 24) -> None:
    """Keep only the most recent run dirs so served sites don't grow without bound."""
    try:
        if not _RUNS_DIR.is_dir():
            return
        dirs = sorted((d for d in _RUNS_DIR.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[keep:]:
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


@app.post("/serve")
def serve_site(req: dict) -> dict:
    """Write a set of files to a temp dir and serve them at /run/<id>/. Static only, no code execution."""
    raw = req.get("files") or []
    clean: list[tuple[str, str]] = []
    for f in raw:
        path = str((f or {}).get("path") or "").strip().lstrip("/")
        if not path or any(seg in ("..", "") for seg in path.split("/")):
            continue
        clean.append((path, (f.get("content") or "")))
    if not clean:
        return {"ok": False, "error": "No files with content to serve. Draft or paste some code first."}
    run_id = secrets.token_hex(6)
    run_dir = _RUNS_DIR / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        for path, content in clean:
            dest = _safe_join(run_dir, path)
            if dest is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    paths = [p for p, _ in clean]
    entry = "index.html" if "index.html" in paths else next(
        (p for p in paths if p.lower().endswith((".html", ".htm"))), paths[0]
    )
    _prune_runs()
    return {"ok": True, "url": f"/run/{run_id}/{entry}", "run": run_id, "entry": entry, "count": len(clean)}


@app.get("/run/{run_id}/{file_path:path}")
def serve_run_file(run_id: str, file_path: str):
    """Serve a previously written file for a run. 404 on unknown run, bad id, or path traversal."""
    if not re.fullmatch(r"[0-9a-f]{6,16}", run_id or ""):
        return Response("Not found", status_code=404)
    base = _RUNS_DIR / run_id
    rel = file_path or "index.html"
    dest = _safe_join(base, rel)
    if dest is None or not dest.is_file():
        idx = _safe_join(base, (rel.rstrip("/") + "/index.html").lstrip("/")) if rel else _safe_join(base, "index.html")
        if idx is not None and idx.is_file():
            dest = idx
        else:
            return Response("Not found", status_code=404)
    ctype = mimetypes.guess_type(str(dest))[0] or "application/octet-stream"
    return FileResponse(str(dest), media_type=ctype)


def _list_deployments(endpoint: str):
    """Return the deployment names for a Foundry project, or raise (auth/connection failure)."""
    from .foundry import Foundry

    f = Foundry(endpoint)
    return [getattr(d, "name", None) for d in f.project.deployments.list() if getattr(d, "name", None)]


@app.get("/config")
def get_config() -> dict:
    """Current Foundry connection state for the in-app 'Connect Foundry' screen."""
    out = {
        "connected": bool(settings.project_endpoint),
        "project_endpoint": settings.project_endpoint,
        "orchestrator_model": settings.orchestrator_model,
        "worker_model": settings.worker_model,
        "critic_model": settings.critic_model,
        "synthesiser_model": settings.synthesiser_model,
        "search_endpoint": settings.search_endpoint,
        "knowledge_base": settings.knowledge_base,
        "foundry_iq_configured": bool(settings.search_endpoint and settings.knowledge_base),
        "foundry_iq": {
            "configured": bool(settings.search_endpoint and settings.knowledge_base),
            "search_endpoint": settings.search_endpoint,
            "knowledge_base": settings.knowledge_base,
        },
        "model_source": settings.model_source,
        "subagents_enabled": bool(settings.subagents_enabled),
        "ai_can_spawn_subagents": bool(settings.ai_can_spawn_subagents),
        "total_spend_usd": round(float(settings.total_spend_usd or 0.0), 6),
        "total_runs": int(settings.total_runs or 0),
        "auth_ok": False,
        "auth_error": None,
        "models": [],
    }
    if settings.project_endpoint:
        try:
            out["models"] = _list_deployments(settings.project_endpoint)
            out["auth_ok"] = True
        except Exception as exc:
            out["auth_error"] = f"{type(exc).__name__}: {exc}"
    return out


@app.get("/spend")
def get_spend() -> dict:
    """Lifetime spend monitor: cumulative USD across all runs, plus the per-question budget."""
    return {
        "total_spend_usd": round(float(settings.total_spend_usd or 0.0), 6),
        "total_runs": int(settings.total_runs or 0),
        "budget_usd": float(settings.budget_usd),
        "reserve_frac": float(settings.budget_reserve_frac),
        "model_source": settings.model_source or "foundry",
        "subagents_enabled": bool(settings.subagents_enabled),
        "ai_can_spawn_subagents": bool(settings.ai_can_spawn_subagents),
    }


@app.post("/deployments")
def deployments(req: dict) -> dict:
    """List a candidate project's deployments for the Connect dropdown — does NOT persist."""
    endpoint = (req.get("project_endpoint") or "").strip()
    if not endpoint:
        return {"ok": False, "error": "A Foundry project endpoint is required."}
    try:
        models = _list_deployments(endpoint)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "auth_hint": "If this is a sign-in error, run `az login` in your terminal, then try again.",
        }
    chat = [m for m in models if "embed" not in m.lower()]
    return {"ok": True, "models": models, "chat_models": chat}


@app.post("/connect")
def connect(req: dict) -> dict:
    """Connect a Foundry project at runtime: validate, apply over .env, and persist.

    Auth is via the machine's `az login` (DefaultAzureCredential); a sign-in failure here means
    the user needs to run `az login`. No secrets are stored by this endpoint.
    """
    from .config import save_runtime_config

    endpoint = (req.get("project_endpoint") or "").strip()
    if not endpoint:
        return {"ok": False, "error": "A Foundry project endpoint is required."}
    try:
        models = _list_deployments(endpoint)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "auth_hint": "If this is a sign-in error, run `az login` in your terminal, then try again.",
        }

    worker = (req.get("worker_model") or "").strip()
    chat_models = [m for m in models if "embed" not in m.lower()]
    if worker and worker not in models:
        return {"ok": False, "error": f"Deployment '{worker}' not found. Available: {', '.join(models) or '(none)'}"}
    if not worker:
        worker = chat_models[0] if chat_models else (models[0] if models else "")
    if not worker:
        return {"ok": False, "error": "No model deployments found in that project. Deploy a model in Foundry first."}

    # One reasoning model drives the whole loop; the chat model picker overrides per chat.
    settings.project_endpoint = endpoint
    settings.orchestrator_model = settings.worker_model = settings.critic_model = settings.synthesiser_model = worker
    if req.get("search_endpoint") is not None:
        settings.search_endpoint = (req.get("search_endpoint") or "").strip()
    if req.get("knowledge_base") is not None:
        settings.knowledge_base = (req.get("knowledge_base") or "").strip()

    # Rebuild caches so the new endpoint/model take effect immediately.
    global _FOUNDRY_HELPER, _HELPER_AGENTS
    _FOUNDRY_HELPER = None
    _HELPER_AGENTS = set()

    save_runtime_config({
        "project_endpoint": endpoint, "worker_model": worker, "orchestrator_model": worker,
        "critic_model": worker, "synthesiser_model": worker,
        "search_endpoint": settings.search_endpoint, "knowledge_base": settings.knowledge_base,
    })
    return {"ok": True, "models": models, "worker_model": worker}


def _foundry_iq_probe(endpoint: str, knowledge_base: str, query: str = "test", top_k: int = 3) -> dict:
    """Build a Foundry IQ grounding and run one retrieval. Classify failures clearly.

    stage is one of: 'sdk' (preview package not installed), 'client' (auth / endpoint /
    KB name), 'retrieve' (query ran but the service errored), 'empty' (ran, no passages).
    """
    try:
        from .grounding import FoundryIQGrounding
    except Exception as exc:
        return {"ok": False, "stage": "import", "error": f"{type(exc).__name__}: {exc}"}

    try:
        g = FoundryIQGrounding(endpoint, knowledge_base)
    except ModuleNotFoundError:
        return {
            "ok": False, "stage": "sdk",
            "error": "The Foundry IQ SDK is not installed.",
            "hint": "Install the preview package:  pip install --pre azure-search-documents",
        }
    except Exception as exc:
        return {
            "ok": False, "stage": "client",
            "error": f"Could not connect to Azure AI Search: {type(exc).__name__}: {exc}",
            "hint": "Check the Search endpoint URL and that you are signed in (az login). "
                    "DefaultAzureCredential needs a logged-in identity with access to the service.",
        }

    try:
        passages = g.retrieve(query, top_k=top_k)
    except Exception as exc:
        msg = str(exc)
        hint = ("Confirm the knowledge base name exists on that Search service and that your "
                "identity has the Search Index Data Reader role. Run az login if sign-in is stale.")
        return {"ok": False, "stage": "retrieve",
                "error": f"Retrieval failed: {type(exc).__name__}: {msg[:300]}", "hint": hint}

    sample = [{"source": p.source, "text": (p.text or "")[:280]} for p in (passages or [])]
    if not sample:
        return {"ok": False, "stage": "empty",
                "error": "Connected, but the knowledge base returned no passages for a test query.",
                "hint": "The knowledge base is reachable but may be empty or still indexing. "
                        "Add a knowledge source and let indexing finish, then test again."}
    return {"ok": True, "sample": sample}


@app.post("/foundry-iq/connect")
def foundry_iq_connect(req: dict) -> dict:
    """Set or clear the Foundry IQ knowledge base (Azure AI Search agentic retrieval).

    Accepts search_endpoint + knowledge_base. By default it runs a live retrieval probe
    so the user gets a real pass/fail before relying on grounding; pass validate=false to
    save without probing. Sending both blank clears the configuration. No secrets stored:
    auth is DefaultAzureCredential (az login).
    """
    from .config import save_runtime_config

    endpoint = (req.get("search_endpoint") or "").strip()
    kb = (req.get("knowledge_base") or "").strip()
    validate = req.get("validate", True)

    if not endpoint and not kb:
        settings.search_endpoint = ""
        settings.knowledge_base = ""
        save_runtime_config({"search_endpoint": "", "knowledge_base": ""})
        return {"ok": True, "cleared": True, "configured": False}

    if not endpoint or not kb:
        return {
            "ok": False,
            "error": "Both the Search endpoint and the knowledge base name are required.",
            "hint": "Search endpoint looks like https://<service>.search.windows.net ; "
                    "the knowledge base name is what you created in Foundry IQ.",
        }
    if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
        return {"ok": False,
                "error": "The Search endpoint must be a URL like https://<service>.search.windows.net."}

    probe = None
    if validate:
        probe = _foundry_iq_probe(endpoint, kb)
        if not probe.get("ok"):
            return {"ok": False, "error": probe.get("error"), "hint": probe.get("hint"),
                    "stage": probe.get("stage")}

    settings.search_endpoint = endpoint
    settings.knowledge_base = kb
    save_runtime_config({"search_endpoint": endpoint, "knowledge_base": kb})
    out = {"ok": True, "configured": True,
           "search_endpoint": endpoint, "knowledge_base": kb}
    if probe:
        out["sample"] = probe.get("sample")
        out["validated"] = True
    return out


@app.post("/foundry-iq/test")
def foundry_iq_test(req: dict) -> dict:
    """Run one retrieval against the configured (or supplied) Foundry IQ knowledge base.

    Returns the cited passages so the user can confirm grounding works end to end before
    they trust an answer. Used by the Settings panel and the wizard 'Test' button.
    """
    endpoint = (req.get("search_endpoint") or settings.search_endpoint or "").strip()
    kb = (req.get("knowledge_base") or settings.knowledge_base or "").strip()
    query = (req.get("query") or "What is in this knowledge base?").strip()
    if not (endpoint and kb):
        return {"ok": False, "error": "Foundry IQ is not configured yet."}
    probe = _foundry_iq_probe(endpoint, kb, query=query, top_k=int(req.get("top_k") or 3))
    if not probe.get("ok"):
        return {"ok": False, "error": probe.get("error"), "hint": probe.get("hint"),
                "stage": probe.get("stage")}
    return {"ok": True, "passages": probe.get("sample") or [], "label": "Foundry IQ"}


# ===== GitHub (E) — token-based: list repos, read files, push changes. =====
# The fine-grained PAT is held server-side and stored locally (gitignored). It is never
# returned to the browser. The user generates and provides it; the server never asks for a password.
import json as _json  # noqa: E402
import pathlib as _pathlib  # noqa: E402

_GH_TOKEN: str | None = None
_GH_PATH = _pathlib.Path(__file__).resolve().parents[2] / ".marshal.json"


def _gh_token() -> str:
    global _GH_TOKEN
    if _GH_TOKEN is None:
        try:
            _GH_TOKEN = (_json.loads(_GH_PATH.read_text(encoding="utf-8")).get("github_token") or "") if _GH_PATH.exists() else ""
        except Exception:
            _GH_TOKEN = ""
    return _GH_TOKEN


def _set_gh_token(tok: str) -> None:
    global _GH_TOKEN
    _GH_TOKEN = tok or ""
    try:
        data = _json.loads(_GH_PATH.read_text(encoding="utf-8")) if _GH_PATH.exists() else {}
        if tok:
            data["github_token"] = tok
        else:
            data.pop("github_token", None)
        _GH_PATH.write_text(_json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.get("/github/status")
def github_status() -> dict:
    tok = _gh_token()
    if not tok:
        return {"connected": False}
    try:
        from . import github
        return {"connected": True, **github.whoami(tok)}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


@app.post("/github/connect")
def github_connect(req: dict) -> dict:
    tok = (req.get("token") or "").strip()
    if not tok:
        return {"ok": False, "error": "A personal access token is required."}
    try:
        from . import github
        who = github.whoami(tok)
    except Exception as exc:
        return {"ok": False, "error": str(exc),
                "hint": "Create a fine-grained token with Contents: Read and write for your repos, then paste it here."}
    _set_gh_token(tok)
    return {"ok": True, **who}


@app.post("/github/disconnect")
def github_disconnect() -> dict:
    _set_gh_token("")
    return {"ok": True}


@app.get("/github/repos")
def github_repos() -> dict:
    tok = _gh_token()
    if not tok:
        return {"error": "Not connected."}
    try:
        from . import github
        return {"repos": github.list_repos(tok)}
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/github/tree")
def github_tree(req: dict) -> dict:
    tok = _gh_token()
    if not tok:
        return {"error": "Not connected."}
    repo = (req.get("repo") or "").strip()
    if not repo:
        return {"error": "repo required"}
    try:
        from . import github
        return github.get_tree(tok, repo, (req.get("ref") or "").strip() or None)
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/github/file")
def github_file(req: dict) -> dict:
    tok = _gh_token()
    if not tok:
        return {"error": "Not connected."}
    repo = (req.get("repo") or "").strip()
    path = (req.get("path") or "").strip()
    if not (repo and path):
        return {"error": "repo and path required"}
    try:
        from . import github
        return github.get_file(tok, repo, path, (req.get("ref") or "").strip() or None)
    except Exception as exc:
        return {"error": str(exc)}


def _norm_repo(raw: str | None) -> str:
    """Accept a full GitHub URL or owner/repo and return a clean slug, or '' if malformed."""
    import re

    s = (raw or "").strip()
    s = re.sub(r"^https?://github\.com/", "", s, flags=re.I)
    s = re.sub(r"\.git$", "", s, flags=re.I).strip("/")
    return s if re.fullmatch(r"[\w.-]+/[\w.-]+", s) else ""


@app.post("/github/public-tree")
def github_public_tree(req: dict) -> dict:
    """Public-repo file tree, no token. Anonymous GitHub API (rate-limited)."""
    repo = _norm_repo(req.get("repo"))
    if not repo:
        return {"error": "Enter a public repository as owner/repo."}
    try:
        from . import github
        return github.get_public_tree(repo, (req.get("ref") or "").strip() or None)
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/github/public-file")
def github_public_file(req: dict) -> dict:
    """Public-repo single file, no token. Anonymous GitHub API (rate-limited)."""
    repo = _norm_repo(req.get("repo"))
    path = (req.get("path") or "").strip()
    if not repo:
        return {"error": "Enter a public repository as owner/repo."}
    if not path:
        return {"error": "path required"}
    try:
        from . import github
        return github.get_public_file(repo, path, (req.get("ref") or "").strip() or None)
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/github/push")
def github_push(req: dict) -> dict:
    tok = _gh_token()
    if not tok:
        return {"ok": False, "error": "Not connected."}
    repo = (req.get("repo") or "").strip()
    files = [
        {"path": str(f.get("path", "")).strip(), "content": f.get("content") or ""}
        for f in (req.get("files") or []) if f.get("path")
    ]
    if not repo:
        return {"ok": False, "error": "repo required"}
    if not files:
        return {"ok": False, "error": "no files with content to push"}
    try:
        from . import github
        res = github.push_files(
            tok, repo, files,
            (req.get("message") or "Update from Marshal").strip(),
            (req.get("branch") or "").strip() or None,
            (req.get("base") or "").strip() or None,
        )
        return {"ok": True, **res}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/set-models")
def set_models(req: dict) -> dict:
    """Set the model deployment per agent role (orchestrator / worker / critic / synthesiser)."""
    from .config import save_runtime_config

    roles = ("orchestrator_model", "worker_model", "critic_model", "synthesiser_model")
    updates = {}
    for r in roles:
        v = (req.get(r) or "").strip()
        if v:
            setattr(settings, r, v)
            updates[r] = v
    if updates:
        save_runtime_config(updates)
        global _FOUNDRY_HELPER, _HELPER_AGENTS  # noqa: F824
        _FOUNDRY_HELPER = None
        _HELPER_AGENTS = set()
    return {"ok": True, "roles": {r: getattr(settings, r) for r in roles}}


@app.post("/settings")
def update_settings(req: dict) -> dict:
    """Update budget / model source / subagent toggles. All fields optional; only sent ones change."""
    from .config import save_runtime_config

    updates: dict = {}

    if "budget_usd" in req and req["budget_usd"] is not None:
        try:
            b = float(req["budget_usd"])
            if math.isfinite(b) and 0 < b <= 100:
                settings.budget_usd = b
                updates["budget_usd"] = b
        except (TypeError, ValueError):
            pass

    if "model_source" in req:
        src = (req.get("model_source") or "").strip().lower()
        if src in ("foundry", "local"):
            settings.model_source = src
            updates["model_source"] = src

    for key in ("subagents_enabled", "ai_can_spawn_subagents"):
        if key in req:
            val = bool(req[key])
            setattr(settings, key, val)
            updates[key] = val

    # Coherence guard: AI cannot spawn subagents if subagents are disabled entirely.
    if not settings.subagents_enabled and settings.ai_can_spawn_subagents:
        settings.ai_can_spawn_subagents = False
        updates["ai_can_spawn_subagents"] = False

    if req.get("reset_spend") is True:
        settings.total_spend_usd = 0.0
        settings.total_runs = 0
        updates["total_spend_usd"] = 0.0
        updates["total_runs"] = 0

    if updates:
        save_runtime_config(updates)

    return {
        "ok": True,
        "budget_usd": float(settings.budget_usd),
        "model_source": settings.model_source,
        "subagents_enabled": bool(settings.subagents_enabled),
        "ai_can_spawn_subagents": bool(settings.ai_can_spawn_subagents),
        "total_spend_usd": round(float(settings.total_spend_usd or 0.0), 6),
        "total_runs": int(settings.total_runs or 0),
    }


# ===== Master file (profile) — server-owned, hidden from the UI, download-only. =====
# The browser can read a status summary and download the .md, but never the raw text.
from fastapi.responses import PlainTextResponse  # noqa: E402


@app.get("/profile/status")
def profile_status_endpoint() -> dict:
    """Counts and last-updated only. No entry text crosses the wire here."""
    from .config import profile_status

    return profile_status()


@app.get("/profile/download")
def profile_download():
    """The only way to see the master file: download it as Markdown."""
    from .config import profile_markdown

    return PlainTextResponse(
        profile_markdown(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="marshal-master-file.md"'},
    )


@app.post("/profile/append")
def profile_append(req: dict) -> dict:
    """Append user-supplied context to the master file (e.g. the wizard paste step).

    Body: {"text": "...", "source": "setup:ChatGPT"}  OR
          {"items": [{"text": "...", "source": "..."}, ...]}.
    Returns only the new status summary, never the stored text.
    """
    from .config import append_profile_entries, profile_status

    items = req.get("items")
    if not isinstance(items, list):
        items = [{"text": req.get("text") or "", "source": req.get("source") or "setup"}]
    added = append_profile_entries(items)
    return {"ok": True, "added": added, **profile_status()}


# ===== Marshal Workshop — self-upgrading capabilities. =====
# A capability is a specialised agent Marshal designs for itself from a plain-language
# description: a charter (system prompt) it can then run on demand. Stored in .marshal.json
# (gitignored) and registered as a Foundry agent so it is usable the moment it is created.
# Safe by construction: it generates agent *instructions*, never code that executes.
WORKSHOP_SYSTEM = (
    "You are Marshal's Workshop. From the user's description you design ONE new capability for "
    "Marshal, a self-governing multi-agent reasoning assistant. A capability is a specialised "
    "agent with a focused charter. Reply with STRICT JSON only, no prose, no code fences: "
    '{"name": "kebab-case-id", "title": "Short Title", "description": "one sentence on what it does", '
    '"charter": "the full system prompt for this capability, written in the second person, telling it '
    'exactly how to behave and what to output", "example": "one example request it handles"}. '
    "House style for the charter: British English, no em dashes, direct and practical."
)


def _capabilities() -> list:
    try:
        data = _json.loads(_GH_PATH.read_text(encoding="utf-8")) if _GH_PATH.exists() else {}
        return data.get("capabilities") or []
    except Exception:
        return []


def _save_capabilities(caps: list) -> None:
    try:
        data = _json.loads(_GH_PATH.read_text(encoding="utf-8")) if _GH_PATH.exists() else {}
        data["capabilities"] = caps
        _GH_PATH.write_text(_json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.get("/workshop/list")
def workshop_list() -> dict:
    return {"capabilities": _capabilities()}


@app.post("/workshop/create")
def workshop_create(req: dict) -> dict:
    """Marshal designs itself a new capability from a description, then registers it for use."""
    desc = (req.get("prompt") or "").strip()
    if not desc:
        return {"ok": False, "error": "Describe the capability you want Marshal to have."}
    from .loop import _extract_json

    try:
        reply = _helper_reply(
            "marshal-workshop", WORKSHOP_SYSTEM,
            f"Design a capability for this request:\n{desc}\n\nSTRICT JSON ONLY.", req.get("model"),
        )
        spec = _extract_json(reply.text) or {}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    raw = str(spec.get("name") or spec.get("title") or "capability")
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw.lower().strip()).strip("-")[:48] or "capability"
    cap = {
        "name": safe,
        "title": str(spec.get("title") or safe).strip(),
        "description": str(spec.get("description") or "").strip(),
        "charter": str(spec.get("charter") or "").strip(),
        "example": str(spec.get("example") or "").strip(),
    }
    if not cap["charter"]:
        return {"ok": False, "error": "Marshal could not design that capability. Try describing it differently."}
    caps = [c for c in _capabilities() if c.get("name") != safe]
    caps.append(cap)
    _save_capabilities(caps)
    try:  # best-effort registration; invoke also (re)creates the agent on demand
        if settings.project_endpoint:
            from .foundry import Foundry

            global _FOUNDRY_HELPER
            if _FOUNDRY_HELPER is None:
                _FOUNDRY_HELPER = Foundry(settings.project_endpoint)
            _FOUNDRY_HELPER.ensure_agent(f"marshal-skill-{safe}", settings.worker_model, cap["charter"])
            _HELPER_AGENTS.add(f"marshal-skill-{safe}")
    except Exception:
        pass
    return {"ok": True, "capability": cap}


@app.post("/workshop/invoke")
def workshop_invoke(req: dict) -> dict:
    """Run a created capability on some input: Marshal using a skill it gave itself."""
    name = (req.get("name") or "").strip()
    user_input = (req.get("input") or "").strip()
    cap = next((c for c in _capabilities() if c.get("name") == name), None)
    if not cap:
        return {"ok": False, "error": "Capability not found."}
    if not user_input:
        return {"ok": False, "error": "Enter something for the capability to work on."}
    try:
        reply = _helper_reply(f"marshal-skill-{name}", cap["charter"], user_input, req.get("model"))
        return {"ok": True, "output": reply.text,
                "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.post("/workshop/delete")
def workshop_delete(req: dict) -> dict:
    _save_capabilities([c for c in _capabilities() if c.get("name") != (req.get("name") or "").strip()])
    return {"ok": True}


# ===== Subagents — user-created persistent named agents with a charter. =====
# A subagent reuses the Workshop pattern: a charter (system prompt) registered as a Foundry agent
# and run via _helper_reply. Unlike a Workshop capability (which Marshal designs), the USER authors
# a subagent (name + role/charter). Stored in .marshal.json under "subagents" (gitignored). Governed
# by the two opt-in flags persisted via /settings (settings.subagents_enabled / ai_can_spawn_subagents).
SUBAGENT_CHARTER_SYSTEM = (
    "You are Marshal's subagent designer. From a short role description, write a clear, focused "
    "system prompt (a charter) for a single specialised agent. Write it in the second person, "
    "telling the agent exactly how to behave, its scope, and what to output. Reply with the charter "
    "text ONLY, no preamble, no code fences, no JSON. House style: British English, no em dashes, "
    "direct and practical, 4 to 10 sentences."
)

SUBAGENT_PICK_SYSTEM = (
    "You decide which specialised subagents would help with a project task, from a provided roster. "
    "You may also propose ONE brand-new subagent if a clear gap exists. Be conservative: most tasks "
    "need zero or one subagent. Reply with STRICT JSON only, no prose, no code fences: "
    '{"use": ["existing-subagent-name", ...], '
    '"new": {"title": "Short Title", "role": "one-line role", "charter": "full charter, second person"} | null}. '
    "House style for any charter: British English, no em dashes, direct and practical."
)


def _sa_sanitise(raw: str) -> str:
    """kebab-ish id safe for a Foundry agent name (no dots etc.), matching the Workshop scheme."""
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(raw or "").lower().strip())
    return s.strip("-")[:48] or "subagent"


def _subagents() -> list:
    try:
        data = _json.loads(_GH_PATH.read_text(encoding="utf-8")) if _GH_PATH.exists() else {}
        return data.get("subagents") or []
    except Exception:
        return []


def _save_subagents(items: list) -> None:
    try:
        data = _json.loads(_GH_PATH.read_text(encoding="utf-8")) if _GH_PATH.exists() else {}
        data["subagents"] = items
        _GH_PATH.write_text(_json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _sa_guard() -> dict | None:
    """Return a refusal dict if the subsystem is off, else None. Every endpoint calls this first."""
    if not settings.subagents_enabled:
        return {"ok": False, "error": "Subagents are disabled. Enable them in Settings first."}
    return None


def _sa_register(name: str, charter: str) -> None:
    """Best-effort: register the subagent as a Foundry prompt agent so it is usable immediately."""
    try:
        if settings.project_endpoint and charter:
            from .foundry import Foundry

            global _FOUNDRY_HELPER
            if _FOUNDRY_HELPER is None:
                _FOUNDRY_HELPER = Foundry(settings.project_endpoint)
            aname = f"marshal-subagent-{name}"
            _FOUNDRY_HELPER.ensure_agent(aname, settings.worker_model, charter)
            _HELPER_AGENTS.add(aname)
    except Exception:
        pass


@app.get("/subagents")
def subagents_list() -> dict:
    """List subagents and the two governing flags (so the UI can render even when disabled)."""
    return {
        "enabled": bool(settings.subagents_enabled),
        "ai_spawn": bool(settings.ai_can_spawn_subagents),
        "subagents": _subagents() if settings.subagents_enabled else [],
    }


@app.post("/subagents/create")
def subagents_create(req: dict) -> dict:
    """Create (or overwrite by name) a user-authored subagent.

    The user supplies a name and a role/charter. If only a short role is given, Marshal expands it
    into a full charter via _helper_reply (best-effort; falls back to the role text itself).
    """
    g = _sa_guard()
    if g:
        return g
    name = _sa_sanitise(req.get("name") or req.get("title") or "")
    title = str(req.get("title") or req.get("name") or name).strip()
    role = str(req.get("role") or "").strip()
    charter = str(req.get("charter") or "").strip()
    if not (title or role or charter):
        return {"ok": False, "error": "Give the subagent a name and a role or charter."}
    if not charter:
        if not role:
            return {"ok": False, "error": "Describe the subagent's role."}
        try:
            reply = _helper_reply(
                "marshal-subagent-design", SUBAGENT_CHARTER_SYSTEM,
                f"Role: {role}\n\nWrite the charter.", req.get("model"),
            )
            charter = (reply.text or "").strip() or role
        except Exception:
            charter = role  # degrade gracefully; the role is a usable charter on its own
    item = {
        "name": name,
        "title": title or name,
        "role": role or title or name,
        "charter": charter,
        "created": int(time.time()),
    }
    items = [s for s in _subagents() if s.get("name") != name]  # overwrite on name clash
    items.append(item)
    _save_subagents(items)
    _sa_register(name, charter)
    return {"ok": True, "subagent": item}


@app.post("/subagents/spawn")
def subagents_spawn(req: dict) -> dict:
    """Run ONE subagent on a prompt: the user (or the AI) putting a named agent to work on a task."""
    g = _sa_guard()
    if g:
        return g
    name = _sa_sanitise(req.get("name") or "")
    prompt = (req.get("prompt") or req.get("input") or "").strip()
    sub = next((s for s in _subagents() if s.get("name") == name), None)
    if not sub:
        return {"ok": False, "error": "Subagent not found."}
    if not prompt:
        return {"ok": False, "error": "Give the subagent a task to work on."}
    try:
        reply = _helper_reply(f"marshal-subagent-{name}", sub["charter"], prompt, req.get("model"))
        return {"ok": True, "name": name, "title": sub.get("title", name), "output": reply.text,
                "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.post("/subagents/delete")
def subagents_delete(req: dict) -> dict:
    g = _sa_guard()
    if g:
        return g
    _save_subagents([s for s in _subagents() if s.get("name") != _sa_sanitise(req.get("name") or "")])
    return {"ok": True}


@app.get("/subagents/export")
def subagents_export() -> dict:
    """Return all subagents as a portable JSON document (the browser downloads it as a file)."""
    g = _sa_guard()
    if g:
        return g
    return {"ok": True, "version": 1, "subagents": _subagents()}


@app.post("/subagents/import")
def subagents_import(req: dict) -> dict:
    """Import subagents from an uploaded JSON document. Merges by name (imported wins on clash)."""
    g = _sa_guard()
    if g:
        return g
    incoming = req.get("subagents")
    if incoming is None and isinstance(req.get("data"), dict):  # tolerate {data:{subagents:[...]}}
        incoming = req["data"].get("subagents")
    if not isinstance(incoming, list):
        return {"ok": False, "error": "Expected a JSON document with a 'subagents' array."}
    existing = {s.get("name"): s for s in _subagents() if s.get("name")}
    added = 0
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        name = _sa_sanitise(raw.get("name") or raw.get("title") or "")
        charter = str(raw.get("charter") or raw.get("role") or "").strip()
        if not charter:
            continue  # skip empty/garbage entries
        existing[name] = {
            "name": name,
            "title": str(raw.get("title") or name).strip() or name,
            "role": str(raw.get("role") or raw.get("title") or name).strip(),
            "charter": charter,
            "created": int(raw.get("created") or time.time()),
        }
        _sa_register(name, charter)
        added += 1
    items = list(existing.values())
    _save_subagents(items)
    return {"ok": True, "imported": added, "subagents": items}


@app.post("/subagents/auto")
def subagents_auto(req: dict) -> dict:
    """Opt-in: given a task, suggest which subagents to use (and optionally one new one).

    Returns suggestions only; the caller spawns them explicitly. Refuses unless BOTH the subsystem
    and AI-spawn are enabled, so the AI can never run subagents without the user opting in.
    """
    if not settings.subagents_enabled:
        return {"ok": False, "error": "Subagents are disabled."}
    if not settings.ai_can_spawn_subagents:
        return {"ok": False, "error": "AI spawning of subagents is turned off.", "ai_spawn": False}
    task = (req.get("task") or req.get("goal") or "").strip()
    if not task:
        return {"ok": False, "error": "No task given."}
    from .loop import _extract_json

    roster = _subagents()
    roster_block = "\n".join(f"- {s.get('name')}: {s.get('role') or s.get('title')}" for s in roster) or "(none yet)"
    prompt = (
        f"TASK:\n{task}\n\n"
        f"EXISTING SUBAGENTS:\n{roster_block}\n\n"
        "Decide. STRICT JSON ONLY."
    )
    try:
        reply = _helper_reply("marshal-subagent-pick", SUBAGENT_PICK_SYSTEM, prompt, req.get("model"))
        spec = _extract_json(reply.text) or {}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    have = {s.get("name") for s in roster}
    use = [_sa_sanitise(n) for n in (spec.get("use") or []) if _sa_sanitise(n) in have]
    new = None
    raw_new = spec.get("new")
    if isinstance(raw_new, dict) and str(raw_new.get("charter") or "").strip():
        nm = _sa_sanitise(raw_new.get("title") or raw_new.get("role") or "subagent")
        new = {
            "name": nm,
            "title": str(raw_new.get("title") or nm).strip(),
            "role": str(raw_new.get("role") or "").strip(),
            "charter": str(raw_new.get("charter") or "").strip(),
        }
    return {"ok": True, "use": use, "new": new}


@app.get("/models")
def models() -> dict:
    """Real model data for the Models view.

    Foundry: the model deployments actually present in the project (via the projects
    API), with the one(s) the loop is using right now marked. Ollama: whatever models
    are installed on a local Ollama daemon, if one is running. No hardcoded catalogue.
    """
    from .config import PRICING

    roles = {
        "orchestrator": settings.orchestrator_model,
        "worker": settings.worker_model,
        "critic": settings.critic_model,
        "synthesiser": settings.synthesiser_model,
    }
    active = sorted({m for m in roles.values() if m})
    foundry = {"configured": bool(settings.project_endpoint), "active": active, "roles": roles,
               "deployments": [], "error": None}
    if settings.project_endpoint:
        try:
            from .foundry import Foundry

            global _FOUNDRY_HELPER
            if _FOUNDRY_HELPER is None:
                _FOUNDRY_HELPER = Foundry(settings.project_endpoint)
            for d in _FOUNDRY_HELPER.project.deployments.list():
                name = getattr(d, "name", None)
                if not name:
                    continue
                model_name = getattr(d, "model_name", None) or name
                using = [r for r, m in roles.items() if m == name]
                p = PRICING.get(name)
                foundry["deployments"].append({
                    "name": name,
                    "model": model_name,
                    "publisher": getattr(d, "model_publisher", None),
                    "kind": "embedding" if "embed" in (model_name or "").lower() else "chat",
                    "in_use": bool(using),
                    "roles": using,
                    "price_est": {"in": p.input_per_mtok, "out": p.output_per_mtok} if p else None,
                })
        except Exception as exc:
            foundry["error"] = f"{type(exc).__name__}: {exc}"

    ollama = {"running": False, "models": [], "error": None}
    try:
        import json as _json
        import urllib.request

        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.5) as r:
            data = _json.loads(r.read().decode())
        ollama["running"] = True
        ollama["models"] = [m.get("name") for m in (data.get("models") or []) if m.get("name")]
    except Exception as exc:
        ollama["error"] = type(exc).__name__

    return {"foundry": foundry, "ollama": ollama}


CHAT_SYSTEM = (
    "You are Marshal, a concise, helpful reasoning assistant inside a project workspace. "
    "Use British English spelling and idiom. Do not use em dashes anywhere; use commas, full stops, "
    "or parentheses instead. Do not assume the user's gender and do not use gendered honorifics; "
    "address the user neutrally. If the user's master file gives a preferred name or form of address, "
    "use that. Be direct and practical: short paragraphs or tight bullet lists, no filler. If a request "
    "is genuinely ambiguous, ask one clarifying question rather than guessing."
)

LEARN_SYSTEM = (
    "You maintain a long-term memory of durable facts and standing preferences about ONE user, "
    "for an assistant called Marshal. Read the latest exchange and extract only information worth "
    "remembering for future conversations: stable preferences (tone, format, spelling), the user's "
    "role and domains, ongoing projects, tools and stacks, and any explicit instruction to remember "
    "something. Ignore one-off task details, transient context, anything ephemeral, and anything the "
    "user did not actually state about themselves. Reply with STRICT JSON only, no prose: "
    '{"learnings": ["short third-person statement", ...]}. '
    "Between 0 and 3 items. Each under 200 characters. British English. If nothing is worth "
    'remembering, return {"learnings": []}.'
)


# Lightweight endpoints (chat, suggestions, drafts, planner) run on Microsoft Foundry
# when it is configured (the primary backend, using your deployment); they fall back to
# the local Claude subscription via the CLI for offline dev.
_FOUNDRY_HELPER = None
_HELPER_AGENTS: set = set()


def _helper_reply(agent_name: str, system: str, prompt: str, req_model: str | None = None):
    """One prompt to a named helper agent. Foundry when PROJECT_ENDPOINT is set, else the CLI."""
    global _FOUNDRY_HELPER
    if settings.project_endpoint:
        from .foundry import Foundry

        if _FOUNDRY_HELPER is None:
            _FOUNDRY_HELPER = Foundry(settings.project_endpoint)
        # Honour a user-picked deployment: the default model reuses the base agent; any other
        # deployment gets its own per-model agent so the choice actually takes effect.
        model = req_model or settings.worker_model
        safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in model)  # agent ids: no dots etc.
        aname = agent_name if model == settings.worker_model else f"{agent_name}-{safe}"
        if aname not in _HELPER_AGENTS:
            _FOUNDRY_HELPER.ensure_agent(aname, model, system)
            _HELPER_AGENTS.add(aname)
        return _FOUNDRY_HELPER.ask(aname, prompt)

    from .cli_provider import ClaudeCliProvider

    m = req_model or "sonnet"
    prov = ClaudeCliProvider(model=m)
    prov.ensure_agent(agent_name, m, system)
    return prov.ask(agent_name, prompt)


def _grounding_for(source: str | None, knowledge=None):
    """Build the grounding for a request, honouring an explicit source choice.

    The source follows the project: the default ('project') grounds on the UI-supplied
    corpus (the project's own cards and notes). 'foundry_iq' uses the Microsoft IQ
    knowledge base when configured (and degrades gracefully if it is unavailable).
    'none' disables grounding. This is what keeps a real project grounded on its own
    knowledge rather than a sample corpus.
    """
    from .grounding import make_grounding

    if source == "none":
        return make_grounding(None, None)
    if source == "foundry_iq":
        return make_grounding(
            knowledge,
            foundry_iq={"endpoint": settings.search_endpoint, "knowledge_base": settings.knowledge_base},
        )
    return make_grounding(knowledge, None)  # default: project notes


def _retrieve_citations(grounding, query: str, top_k: int = 3):
    """Retrieve passages and shape them as a (prompt block, citations) pair. Never raises."""
    if not getattr(grounding, "enabled", False):
        return "", []
    try:
        passages = grounding.retrieve(query, top_k=top_k)
    except Exception:
        passages = []
    if not passages:
        return "", []
    block = "\n\n".join(f"[{i+1}] ({p.source}) {p.text}" for i, p in enumerate(passages))
    ground_block = (
        f"KNOWLEDGE (retrieved from {grounding.label}; cite as [n] where you use it):\n"
        f"{block}\n\n---\n\n"
    )
    citations = [
        {"source": p.source, "score": p.score, "text": (p.text or "")[:280]} for p in passages
    ]
    return ground_block, citations


@app.post("/chat")
def chat(req: dict) -> dict:
    """Chat for the UI. Runs on Microsoft Foundry when configured, else the local subscription.

    Grounds on the chosen source ('project' by default) and returns the citations used so
    the UI can show where an answer came from.
    """
    message = (req.get("message") or "").strip()
    if not message:
        return {"reply": ""}
    history = req.get("history") or []
    model = req.get("model")
    source = req.get("source")
    knowledge = req.get("knowledge")

    # The master file is server-owned now: the browser never holds it. Inject it here so it rides
    # in the prompt (preferred name, tone, ongoing context) without ever being returned to the UI.
    from .config import profile_prompt_text

    profile = profile_prompt_text()

    grounding = _grounding_for(source, knowledge)
    ground_block, citations = _retrieve_citations(grounding, message)

    profile_block = (
        "USER MASTER FILE (the user's stated preferences and context; honour any preferred name "
        f"or form of address stated here):\n{profile}\n\n---\n\n"
        if profile else ""
    )

    lines = []
    for h in history[-12:]:
        who = "User" if h.get("role") == "user" else "Marshal"
        lines.append(f"{who}: {h.get('content', '')}")
    lines.append(f"User: {message}")
    lines.append("Marshal:")

    try:
        reply = _helper_reply("marshal-chat", CHAT_SYSTEM, profile_block + ground_block + "\n".join(lines), model)
        # Auto-update the master file from this exchange, off the request path so the reply is not slowed.
        threading.Thread(target=_learn_from_chat, args=(message, reply.text, model), daemon=True).start()
        return {
            "reply": reply.text,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "citations": citations,
            "groundLabel": grounding.label if citations else "",
        }
    except Exception as exc:
        return {"reply": "", "error": f"{type(exc).__name__}: {exc}"}


def _learn_from_chat(message: str, reply_text: str, model: str | None) -> None:
    """Best-effort: extract durable learnings from one exchange and append to the master file.

    Runs in a daemon thread after the reply is built. Never raises into the request path.
    """
    from .config import append_profile_entries
    from .loop import _extract_json

    try:
        prompt = (
            "LATEST EXCHANGE:\n"
            f"User: {message}\n"
            f"Marshal: {reply_text}\n\n"
            "Extract durable learnings. STRICT JSON ONLY."
        )
        out = _helper_reply("marshal-learn", LEARN_SYSTEM, prompt, model)
        data = _extract_json(out.text) or {}
        items = [
            {"text": str(s).strip(), "source": "chat"}
            for s in (data.get("learnings") or [])
            if str(s).strip()
        ]
        if items:
            append_profile_entries(items[:3])
    except Exception:
        pass  # learning is best-effort; it must never break chat


FILES_SYSTEM = (
    "You are a senior engineer planning a code change. Given a task, list the files most likely to "
    "need creating or changing to complete it. IMPORTANT: each file is later drafted independently and "
    "must be self-contained, so prefer the SMALLEST coherent set of files and do not split tightly "
    "coupled code across files. For a simple static site or a small page, a single self-contained "
    "index.html (inline CSS and JS, no external files) is ideal, suggest just that. Only propose a "
    "multi-file structure when the task genuinely needs one, and do not assume a server framework "
    "unless the task names a stack. "
    'Reply with STRICT JSON only: {"files": [{"path": "...", "why": "short reason"}]}. '
    "Between 1 and 5 files, the fewest that do the job. No prose outside the JSON."
)


@app.post("/suggest-files")
def suggest_files(req: dict) -> dict:
    """Suggest the files a task will likely touch. Runs locally on the Claude subscription."""
    title = (req.get("title") or "").strip()
    if not title:
        return {"files": []}
    notes = req.get("notes") or ""
    project = req.get("project") or {}
    model = req.get("model")

    from .loop import _extract_json

    prompt = (
        f"PROJECT: {project.get('name', '')} — {project.get('desc', '')}\n"
        f"TASK: {title}\n"
        f"NOTES: {notes}\n\n"
        "List the files. STRICT JSON ONLY."
    )
    try:
        reply = _helper_reply("marshal-files", FILES_SYSTEM, prompt, model)
        data = _extract_json(reply.text) or {}
        files = [
            {"path": str(f.get("path", "")).strip(), "why": str(f.get("why", "")).strip()}
            for f in (data.get("files") or [])
            if f.get("path")
        ]
        return {"files": files[:8]}
    except Exception as exc:
        return {"files": [], "error": f"{type(exc).__name__}: {exc}"}


CODE_SYSTEM = (
    "You are a senior engineer. Write or update the single file at the given path to accomplish the task. "
    "Output ONLY the file's complete contents — no explanations, no commentary, and no markdown code fences. "
    "IF AND ONLY IF the app genuinely needs AI (generating or rewriting text, answering questions, "
    "summarising, classifying, etc.), do NOT hardcode an external API or key: call Marshal's built-in AI "
    "from the browser by POSTing same-origin to /api/ai with JSON body {\"prompt\": \"...\"} (optionally "
    "{\"system\": \"...\"}) and reading the JSON reply's `text` field. It runs on the user's connected "
    "Foundry models with no key in the page. Make the call async, show a loading state, and degrade "
    "gracefully with a friendly message if the request fails (for example when opened without the Marshal "
    "server). For apps that need no AI, add nothing of the sort."
)

APP_AI_SYSTEM = (
    "You are an AI feature embedded inside a small web app the user built with Marshal. Answer the app's "
    "request directly and usefully. Return plain text unless the request asks for a specific format. Keep "
    "it concise and do not add meta commentary about being an AI."
)


def _strip_fences(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    return s


@app.post("/draft-code")
def draft_code(req: dict) -> dict:
    """Draft or update one file's code for a task. Runs locally on the Claude subscription."""
    path = (req.get("path") or "").strip()
    if not path:
        return {"code": ""}
    task = req.get("task") or {}
    project = req.get("project") or {}
    existing = req.get("existing") or ""
    model = req.get("model")

    prompt = (
        f"FILE: {path}\n"
        f"TASK: {task.get('title', '')}\n"
        f"NOTES: {task.get('notes', '')}\n"
        f"PROJECT: {project.get('name', '')} — {project.get('desc', '')}\n\n"
    )
    if existing.strip():
        prompt += f"CURRENT CONTENTS:\n{existing}\n\nUpdate the file for the task. Output the full file."
    else:
        prompt += "Write the file from scratch for the task. Output the full file."

    try:
        reply = _helper_reply("marshal-code", CODE_SYSTEM, prompt, model)
        return {"code": _strip_fences(reply.text)}
    except Exception as exc:
        return {"code": "", "error": f"{type(exc).__name__}: {exc}"}


_AI_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"}


@app.options("/api/ai")
def app_ai_preflight() -> Response:
    """CORS preflight so apps in the sandboxed (opaque-origin) in-app Preview can call /api/ai."""
    return Response(status_code=204, headers=_AI_CORS)


@app.post("/api/ai")
def app_ai(req: dict) -> JSONResponse:
    """AI proxy for apps Marshal builds: they call this same-origin and it runs on Marshal's Foundry
    models, so a generated app gets real AI with no API key in its code. Prompt length is bounded so a
    served app cannot run unbounded spend. CORS-open so the sandboxed Preview can use it too. Returns
    {ok, text}."""
    prompt = (req.get("prompt") or "").strip()[:6000]
    if not prompt:
        return JSONResponse({"ok": False, "error": "A prompt is required."}, headers=_AI_CORS)
    # The helper agent is created once per name, so a per-request system prompt would be ignored after
    # the first call. Fold the app's instructions into the user message so they take effect every time.
    app_system = (req.get("system") or "").strip()[:2000]
    full = (app_system + "\n\n---\n\n" + prompt) if app_system else prompt
    try:
        reply = _helper_reply("marshal-app-ai", APP_AI_SYSTEM, full, req.get("model"))
        return JSONResponse({"ok": True, "text": reply.text,
                             "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens},
                            headers=_AI_CORS)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, headers=_AI_CORS)


PLAN_SYSTEM = (
    "You are planning a project on a kanban board one step at a time, with a human who approves each step. "
    "Given the goal and the steps already added (and any the user skipped), propose the SINGLE next concrete, "
    "actionable step as a board card. Keep steps small. When the plan is essentially complete, set done to true. "
    'Reply with STRICT JSON only: {"done": false, "title": "short card title", "detail": "one or two sentences of guidance"}.'
)


@app.post("/plan-next")
def plan_next(req: dict) -> dict:
    """Propose the next step for the step-by-step planner. Runs on the Claude subscription."""
    goal = (req.get("goal") or "").strip()
    if not goal:
        return {"done": True}
    steps = req.get("steps") or []
    skipped = req.get("skipped") or []
    project = req.get("project") or {}
    model = req.get("model")

    from .loop import _extract_json

    added = "\n".join(f"- {s.get('title', '')}" for s in steps) or "(none yet)"
    skip = "\n".join(f"- {t}" for t in skipped) or "(none)"
    prompt = (
        f"PROJECT: {project.get('name', '')} — {project.get('desc', '')}\n"
        f"GOAL: {goal}\n\n"
        f"STEPS ALREADY ADDED:\n{added}\n\n"
        f"STEPS THE USER SKIPPED (do not propose these again):\n{skip}\n\n"
        "Propose the next step. STRICT JSON ONLY."
    )
    try:
        reply = _helper_reply("marshal-plan", PLAN_SYSTEM, prompt, model)
        data = _extract_json(reply.text) or {}
        return {
            "done": bool(data.get("done")),
            "title": str(data.get("title", "")).strip(),
            "detail": str(data.get("detail", "")).strip(),
        }
    except Exception as exc:
        return {"done": False, "title": "", "detail": "", "error": f"{type(exc).__name__}: {exc}"}


PLAN_ALL_SYSTEM = (
    "You are planning a project on a kanban board. Given the goal (and any cards already added), propose "
    "the most important remaining cards as a batch the user can pick from. Each card is concrete, actionable, "
    "small and self-contained. Order the batch MOST IMPORTANT FIRST, so the user can add the top few and stop. "
    "Aim for about 5 cards; never more than 8. Quality and ordering matter more than coverage: do not pad the "
    "list to hit a number, and do not repeat cards already added. "
    "If a FOCUS direction is given, treat it as the user's steer: generate new cards that advance that direction "
    "AND suggest a few cards that naturally support it, still ordered most important first. "
    'Reply with STRICT JSON only: {"steps": [{"title": "short card title", "detail": "one or two sentences of guidance"}]}. '
    "No prose outside the JSON."
)


@app.post("/plan-all")
def plan_all(req: dict) -> dict:
    """An importance-ordered batch of cards in one shot. Optional `focus` steers it."""
    goal = (req.get("goal") or "").strip()
    if not goal:
        return {"steps": []}
    steps = req.get("steps") or []
    focus = (req.get("focus") or "").strip()[:600]
    project = req.get("project") or {}
    model = req.get("model")

    from .loop import _extract_json

    added = "\n".join(f"- {s.get('title', '')}" for s in steps) or "(none yet)"
    focus_block = (
        f"FOCUS (the user's steer; generate cards that advance this and suggest cards that support it):\n{focus}\n\n"
        if focus else ""
    )
    prompt = (
        f"PROJECT: {project.get('name', '')} — {project.get('desc', '')}\n"
        f"GOAL: {goal}\n\n"
        f"CARDS ALREADY ADDED (do not repeat these):\n{added}\n\n"
        f"{focus_block}"
        "Propose the batch, most important first. STRICT JSON ONLY."
    )
    try:
        reply = _helper_reply("marshal-plan-all", PLAN_ALL_SYSTEM, prompt, model)
        data = _extract_json(reply.text) or {}
        out = [
            {"title": str(s.get("title", "")).strip(), "detail": str(s.get("detail", "")).strip()}
            for s in (data.get("steps") or [])
            if str(s.get("title", "")).strip()
        ]
        return {"steps": out[:8]}
    except Exception as exc:
        return {"steps": [], "error": f"{type(exc).__name__}: {exc}"}


def _run(
    question: str,
    budget: float,
    mode: str,
    emit,
    model: str | None = None,
    knowledge=None,
    source: str | None = None,
) -> None:
    from .config import add_spend
    from .loop import Marshal

    grounding = _grounding_for(source, knowledge)

    if mode == "claude":
        # Local testing on the Claude *subscription* via the Claude Code CLI (no API key).
        from .cli_provider import ClaudeCliProvider

        result = Marshal(ClaudeCliProvider(model=model), emit=emit, grounding=grounding).answer(question, budget_usd=budget)
    elif mode == "demo" or not settings.project_endpoint:
        from .demo import DemoFoundry

        result = Marshal(DemoFoundry(), emit=emit, grounding=grounding).answer(question or "Demo question", budget_usd=budget)
    else:
        from .foundry import Foundry

        result = Marshal(Foundry(settings.project_endpoint), emit=emit, grounding=grounding).answer(question, budget_usd=budget)

    # Accumulate this run's spend into the persisted lifetime total, then tell the UI.
    try:
        spent = float((result or {}).get("budget", {}).get("spent_usd") or 0.0)
    except (AttributeError, TypeError, ValueError):
        spent = 0.0
    totals = add_spend(spent)
    emit({"type": "spend_total", **totals})


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        req = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    question = (req.get("question") or "").strip()
    try:
        budget = float(req.get("budget") or settings.budget_usd)
    except (TypeError, ValueError):
        budget = settings.budget_usd
    if not math.isfinite(budget) or budget <= 0:
        budget = settings.budget_usd
    mode = req.get("mode") or ("live" if settings.project_endpoint else "demo")
    model = req.get("model")
    knowledge = req.get("knowledge")
    source = req.get("source")

    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    END = object()

    def emit(event: dict) -> None:
        # emit is called from the loop's worker threads; hop back to the event loop.
        loop.call_soon_threadsafe(events.put_nowait, event)

    def worker() -> None:
        try:
            _run(question, budget, mode, emit, model, knowledge, source)
        except Exception as exc:  # surface failures honestly to the UI
            emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            loop.call_soon_threadsafe(events.put_nowait, END)

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            event = await events.get()
            if event is END:
                break
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
