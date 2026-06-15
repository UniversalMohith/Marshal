"""Self-contained MCP (Model Context Protocol) client for Marshal.

Marshal reasons through Microsoft Foundry on background worker *threads* (see the
``threading.Thread`` worker in ``server.ws``). The official ``mcp`` SDK is
asyncio-based, so this module bridges the two worlds: it exposes plain
*synchronous* functions that each spin up — and tear down — their own asyncio
event loop, run one short-lived MCP session over a stdio subprocess, and return
ordinary Python data. Nothing here imports FastAPI or touches Marshal's request
loop, so it is safe to call from any worker thread.

Public API (synchronous, thread-safe):

    mcp_list_tools(command, args, env=None, timeout=...) -> list[dict]
        Each dict: {"name": str, "description": str, "input_schema": dict}

    mcp_call_tool(command, args, tool_name, tool_args, env=None, timeout=...) -> dict
        Returns: {"ok": bool, "text": str, "error": str}

Both launch the server fresh, do the MCP initialize handshake, perform one
operation, and shut the subprocess down. They are intentionally stateless: a new
process per call keeps the code simple and avoids cross-thread session sharing,
which is what Marshal's per-request worker model wants.

Windows notes (handled below):
  * ``npx`` is shipped as ``npx.cmd``; a bare ``"npx"`` will not launch via the
    raw CreateProcess that asyncio uses. We resolve the real executable with
    ``shutil.which`` and, failing that, append ``.cmd``.
  * asyncio can only spawn subprocesses on a ``ProactorEventLoop``. The default
    loop created by ``new_event_loop()`` *on a non-main thread* is a
    ``SelectorEventLoop``, which raises ``NotImplementedError`` on subprocess
    transport. We therefore construct a ``ProactorEventLoop`` explicitly on
    Windows.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from typing import Any

# The MCP SDK is imported lazily inside the async helpers so that merely
# importing this module never fails if `mcp` is absent — callers get a clear
# error string at call time instead of an ImportError at import time.

# Default wall-clock budget (seconds) for a single end-to-end operation: launch
# the server, handshake, do the work, tear down. npx may need to fetch a package
# on first use, so this is generous.
DEFAULT_TIMEOUT = 60.0


# --------------------------------------------------------------------------- #
# Event-loop plumbing
# --------------------------------------------------------------------------- #
def _new_loop() -> asyncio.AbstractEventLoop:
    """Create a fresh event loop suitable for spawning subprocesses.

    On Windows, subprocess support requires a ProactorEventLoop. A plain
    ``asyncio.new_event_loop()`` called off the main thread yields a
    SelectorEventLoop (no subprocess support), so we build the Proactor loop
    by hand there. We deliberately do NOT use ``asyncio.run`` because Marshal's
    worker threads may not be the place asyncio expects, and constructing the
    loop ourselves lets us pick the right implementation on Windows.
    """
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
    return asyncio.new_event_loop()


def _run_sync(coro, timeout: float):
    """Run *coro* to completion on a private loop, then dispose of the loop.

    Wraps the coroutine in ``asyncio.wait_for`` so a hung server cannot block a
    Marshal worker thread forever. The loop is always closed, even on error.
    """
    loop = _new_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
    finally:
        # Give cancelled subprocess transports a chance to finalize so we don't
        # leak handles or print "Event loop is closed" noise on shutdown.
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()


# --------------------------------------------------------------------------- #
# Command resolution (Windows npx/npm .cmd handling)
# --------------------------------------------------------------------------- #
def _resolve_command(command: str) -> str:
    """Return a launchable executable path for *command*.

    On Windows, Node's launchers (``npx``, ``npm``, ``yarn``...) exist only as
    ``.cmd`` shims; the raw CreateProcess that asyncio uses won't find a bare
    ``npx``. We first try ``shutil.which`` (which honours PATHEXT and returns the
    real ``npx.cmd``); if that misses, we fall back to appending ``.cmd``. On
    POSIX we just trust ``which`` / the original string.
    """
    found = shutil.which(command)
    if found:
        return found
    if sys.platform == "win32" and not command.lower().endswith((".cmd", ".exe", ".bat")):
        found_cmd = shutil.which(command + ".cmd")
        if found_cmd:
            return found_cmd
        return command + ".cmd"
    return command


def _merged_env(env: dict | None) -> dict:
    """Overlay caller-supplied vars on the current environment.

    MCP's ``StdioServerParameters`` defaults to a *scrubbed* minimal env when
    ``env`` is None, which on Windows can drop PATH/SystemRoot and break npx.
    Passing a full, merged environment is the reliable choice.
    """
    merged = dict(os.environ)
    if env:
        merged.update({k: str(v) for k, v in env.items()})
    return merged


# --------------------------------------------------------------------------- #
# Async cores
# --------------------------------------------------------------------------- #
async def _async_list_tools(command: str, args: list[str], env: dict | None) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=_resolve_command(command),
        args=list(args),
        env=_merged_env(env),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            out: list[dict] = []
            for tool in resp.tools:
                out.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema or {},
                    }
                )
            return out


async def _async_call_tool(
    command: str,
    args: list[str],
    tool_name: str,
    tool_args: dict,
    env: dict | None,
) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=_resolve_command(command),
        args=list(args),
        env=_merged_env(env),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, tool_args or {})
            text = _flatten_content(result.content)
            if getattr(result, "isError", False):
                return {"ok": False, "text": text, "error": text or "tool reported an error"}
            return {"ok": True, "text": text, "error": ""}


def _flatten_content(content: Any) -> str:
    """Render an MCP tool result's content blocks into one plain string.

    A CallToolResult carries a list of typed content blocks (text, image,
    embedded resource...). For Marshal's purposes we want the human-readable
    text; we concatenate every ``.text`` we find and note non-text blocks.
    """
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text" or hasattr(block, "text"):
            txt = getattr(block, "text", None)
            if txt is not None:
                parts.append(str(txt))
                continue
        if btype:
            parts.append(f"[{btype} content]")
        else:
            parts.append(str(block))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Public synchronous API
# --------------------------------------------------------------------------- #
def mcp_list_tools(
    command: str,
    args: list[str],
    env: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Launch an MCP stdio server, handshake, and return its tool catalogue.

    Returns a list of ``{"name", "description", "input_schema"}`` dicts. On any
    failure (missing SDK, server crash, timeout) raises ``RuntimeError`` with a
    clear, single-line message — callers in Marshal can surface it to the UI.
    """
    try:
        return _run_sync(_async_list_tools(command, args, env), timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"mcp_list_tools timed out after {timeout:.0f}s launching "
            f"'{command} {' '.join(args)}'"
        ) from None
    except ImportError as exc:
        raise RuntimeError(f"MCP SDK not available: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(
            f"mcp_list_tools failed for '{command} {' '.join(args)}': "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def mcp_call_tool(
    command: str,
    args: list[str],
    tool_name: str,
    tool_args: dict,
    env: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Launch an MCP stdio server, handshake, and call one tool.

    Always returns a dict ``{"ok": bool, "text": str, "error": str}`` — it does
    not raise for normal operational failures (missing SDK, server crash,
    timeout, tool error), so callers can branch on ``ok`` without try/except.
    """
    try:
        return _run_sync(
            _async_call_tool(command, args, tool_name, tool_args, env), timeout
        )
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "text": "",
            "error": f"mcp_call_tool timed out after {timeout:.0f}s calling '{tool_name}'",
        }
    except ImportError as exc:
        return {"ok": False, "text": "", "error": f"MCP SDK not available: {exc}"}
    except Exception as exc:
        return {
            "ok": False,
            "text": "",
            "error": f"mcp_call_tool failed for '{tool_name}': {type(exc).__name__}: {exc}",
        }
