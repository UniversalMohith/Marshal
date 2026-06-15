"""Provider-agnostic prompt-based tool-use loop.

Works with ANY text-in/text-out reply function (Foundry .ask, Claude CLI,
Anthropic API, DemoFoundry) — no native function-calling required. The model is
handed the tool catalogue and instructed to emit STRICT JSON: either
{"tool": <name>, "args": {...}} to call a tool, or {"final": <answer>} to stop.
The server executes the tool, appends the observation, and re-prompts, looping
(with a hard cap) until the model finalises. This keeps the provider abstraction
(text only) intact, so the reasoning loop and every provider stay untouched.

    helper_reply_fn(system, user) -> str   # returns model text
    call_tool_fn(name, args) -> Any        # executes one tool, returns JSON-able result
    tools: list of {"name", "description", "input_schema" (JSON schema)}
"""
from __future__ import annotations

import json
from typing import Any, Callable


def _first_json_object(text: str) -> dict | None:
    """Extract the first balanced {...} JSON object from arbitrary model text.

    Tolerant of ```json fences, prose before/after, and nested braces/strings.
    Returns None if no parseable object is found.
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):                      # strip a ```json fence if present
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
    # Fast path: the whole thing is one JSON object.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Scan for the first balanced object, respecting strings/escapes.
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    start = -1   # keep scanning for a later well-formed object
    return None


def _tools_catalog(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        schema = json.dumps(t.get("input_schema") or t.get("parameters") or {"type": "object", "properties": {}})
        lines.append(f"- {t['name']}: {(t.get('description') or '').strip()}\n    args schema: {schema}")
    return "\n".join(lines) if lines else "(no tools available)"


TOOL_SYSTEM_TEMPLATE = """You are a reasoning agent that can call external tools to answer the user.

You have these tools:
{catalog}

PROTOCOL — every reply MUST be a single STRICT JSON object, nothing else:
  - To call a tool:  {{"tool": "<tool_name>", "args": {{ ...matching the args schema... }}}}
  - When you are done: {{"final": "<your complete answer to the user>"}}

Rules:
  - Reply with ONE JSON object only. No prose, no markdown, no code fences.
  - Call at most one tool per reply. After a tool runs, you will be shown its result
    (as an OBSERVATION) and asked to continue.
  - Only use tools listed above, with args that match their schema.
  - When you have enough information, STOP calling tools and return {{"final": ...}}.

ORIGINAL SYSTEM INSTRUCTIONS (follow these for the task itself):
{base_system}
"""


def run_tool_loop(
    helper_reply_fn: Callable[[str, str], str],
    system: str,
    user_question: str,
    tools: list[dict],
    call_tool_fn: Callable[[str, dict], Any],
    max_iters: int = 6,
) -> dict:
    """Drive a model through a prompt-based tool-use loop until it returns a final answer.

    Returns {"final": <str>, "trace": [<step>...], "iterations": <int>, "stopped": <reason>}.
    Robust to non-JSON output: if a reply has no JSON object, it is treated as the final answer.
    """
    sys_prompt = TOOL_SYSTEM_TEMPLATE.format(
        catalog=_tools_catalog(tools), base_system=system or "(none)"
    )
    tool_names = {t["name"] for t in tools}
    # The running conversation we re-send each turn (the text interface is stateless).
    convo = f"USER QUESTION:\n{user_question}\n\nBegin. Reply with STRICT JSON only."
    trace: list[dict] = []

    for i in range(max_iters):
        raw = helper_reply_fn(sys_prompt, convo) or ""
        obj = _first_json_object(raw)

        # No JSON at all -> treat the raw text as the final answer (fail-open, never hang).
        if obj is None:
            return {"final": raw.strip(), "trace": trace,
                    "iterations": i + 1, "stopped": "non_json_final"}

        # Explicit finish.
        if "final" in obj:
            final = obj["final"]
            final = final if isinstance(final, str) else json.dumps(final)
            return {"final": final, "trace": trace,
                    "iterations": i + 1, "stopped": "final"}

        # Tool call.
        if "tool" in obj:
            name = str(obj.get("tool", ""))
            args = obj.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            if name not in tool_names:
                observation = f"ERROR: unknown tool '{name}'. Valid tools: {sorted(tool_names)}."
                result_repr, ok = observation, False
            else:
                try:
                    result = call_tool_fn(name, args)
                    result_repr = result if isinstance(result, str) else json.dumps(result, default=str)
                    observation, ok = result_repr, True
                except Exception as exc:
                    result_repr = f"{type(exc).__name__}: {exc}"
                    observation, ok = f"ERROR running tool '{name}': {result_repr}", False

            trace.append({"step": i + 1, "tool": name, "args": args, "ok": ok, "result": result_repr[:2000]})
            # Append the call + observation and re-prompt for the next decision.
            convo += (
                f"\n\nASSISTANT CALLED TOOL: {json.dumps({'tool': name, 'args': args})}"
                f"\nOBSERVATION ({name}):\n{observation[:4000]}"
                f"\n\nContinue. Reply with STRICT JSON only: another tool call, or {{\"final\": ...}}."
            )
            continue

        # JSON that's neither tool nor final -> nudge once, then it likely finalizes.
        trace.append({"step": i + 1, "tool": None, "args": None, "ok": False,
                      "result": "malformed protocol object"})
        convo += ("\n\nYour last reply was JSON but had neither \"tool\" nor \"final\". "
                  "Reply again with STRICT JSON: {\"tool\":...} or {\"final\":...}.")

    # Hit the cap: ask once for a best-effort final answer from what's been gathered.
    closing = helper_reply_fn(
        sys_prompt,
        convo + "\n\nYou have reached the tool-call limit. Reply now with {\"final\": ...} only.",
    ) or ""
    obj = _first_json_object(closing) or {}
    final = obj.get("final") if isinstance(obj.get("final"), str) else closing.strip()
    return {"final": final, "trace": trace, "iterations": max_iters, "stopped": "max_iters"}
