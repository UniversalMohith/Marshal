"""FastAPI server: runs Marshal and streams the swarm's events to the live UI.

Modes:
- live: needs PROJECT_ENDPOINT + az login; runs the real loop on Foundry.
- demo: runs the real loop and governor with a stubbed model (marshal_ai.demo),
  so the UI works with no connection and serves as a reliable fallback demo.

Run:  python -m uvicorn marshal_ai.server:app --app-dir src --port 8000
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

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
        "worker_model": settings.worker_model,
        "search_endpoint": settings.search_endpoint,
        "knowledge_base": settings.knowledge_base,
        "foundry_iq_configured": bool(settings.search_endpoint and settings.knowledge_base),
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
    profile = (req.get("profile") or "").strip()[:6000]  # bound prompt growth from the master file

    grounding = _grounding_for(source, knowledge)
    ground_block, citations = _retrieve_citations(grounding, message)

    # The master file is per-request context (the helper agent caches its system prompt), so it
    # rides in the prompt. It is how the user states a preferred name or form of address, etc.
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
        return {
            "reply": reply.text,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "citations": citations,
            "groundLabel": grounding.label if citations else "",
        }
    except Exception as exc:
        return {"reply": "", "error": f"{type(exc).__name__}: {exc}"}


FILES_SYSTEM = (
    "You are a senior engineer planning a code change. Given a task, list the files most likely to "
    "need creating or changing to complete it. Prefer realistic, conventional paths for the stack implied "
    'by the task. Reply with STRICT JSON only: {"files": [{"path": "...", "why": "short reason"}]}. '
    "Between 3 and 6 files. No prose outside the JSON."
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
    "Output ONLY the file's complete contents — no explanations, no commentary, and no markdown code fences."
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
    "You are planning a project on a kanban board. Given the goal (and any steps already added), produce the "
    "FULL remaining plan as an ordered list of concrete, actionable board cards, each one small and self-contained, "
    "covering the project end to end. Do not repeat steps already added. "
    'Reply with STRICT JSON only: {"steps": [{"title": "short card title", "detail": "one or two sentences of guidance"}]}. '
    "Between 4 and 10 steps. No prose outside the JSON."
)


@app.post("/plan-all")
def plan_all(req: dict) -> dict:
    """Produce the whole plan in one shot, so the user can add every step at once."""
    goal = (req.get("goal") or "").strip()
    if not goal:
        return {"steps": []}
    steps = req.get("steps") or []
    project = req.get("project") or {}
    model = req.get("model")

    from .loop import _extract_json

    added = "\n".join(f"- {s.get('title', '')}" for s in steps) or "(none yet)"
    prompt = (
        f"PROJECT: {project.get('name', '')} — {project.get('desc', '')}\n"
        f"GOAL: {goal}\n\n"
        f"STEPS ALREADY ADDED (do not repeat these):\n{added}\n\n"
        "Produce the full remaining plan. STRICT JSON ONLY."
    )
    try:
        reply = _helper_reply("marshal-plan-all", PLAN_ALL_SYSTEM, prompt, model)
        data = _extract_json(reply.text) or {}
        out = [
            {"title": str(s.get("title", "")).strip(), "detail": str(s.get("detail", "")).strip()}
            for s in (data.get("steps") or [])
            if str(s.get("title", "")).strip()
        ]
        return {"steps": out[:12]}
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
    from .loop import Marshal

    grounding = _grounding_for(source, knowledge)

    if mode == "claude":
        # Local testing on the Claude *subscription* via the Claude Code CLI (no API key).
        from .cli_provider import ClaudeCliProvider

        Marshal(ClaudeCliProvider(model=model), emit=emit, grounding=grounding).answer(question, budget_usd=budget)
    elif mode == "demo" or not settings.project_endpoint:
        from .demo import DemoFoundry

        Marshal(DemoFoundry(), emit=emit, grounding=grounding).answer(question or "Demo question", budget_usd=budget)
    else:
        from .foundry import Foundry

        Marshal(Foundry(settings.project_endpoint), emit=emit, grounding=grounding).answer(question, budget_usd=budget)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        req = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    question = (req.get("question") or "").strip()
    budget = float(req.get("budget") or settings.budget_usd)
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
