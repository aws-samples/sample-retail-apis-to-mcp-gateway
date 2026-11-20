# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tool Layer Console: a local browser chat for the agent that uses your gateway.

A single local web app (stdlib http.server) that runs the Strands/Bedrock/MCP
agent server-side, because a browser can't, and keeps the page a thin chat UI.
Two roles, each with its own agent session and its own Cognito token:

  * Customer Service Representative: agent with the `representative` token
  * Customer: agent with the `customer` token

Start it from the repo root (it starts the server and opens your browser):

    python assets/console/console.py          # http://localhost:8001

Set CONSOLE_PORT to use another port. The server listens on 127.0.0.1 only. It
has no login and acts with your local AWS credentials, so keep it on your own
machine.

The console starts with no AWS dependencies satisfied: no gateway, no demo.env,
no identity stack. Each role has a Reconnect button that rebuilds its session
(picking up a new gateway or newly deployed tools) without restarting the
server, and a Show tools button that lists the MCP tools that role's agent can
actually see.

--- Chat modes: gateway, or model-only until you have a gateway ---------------
The mode depends on whether a gateway URL is configured. With a GATEWAY_URL the
chat is the gateway-backed agent: the role's Cognito token plus the MCP tools
the gateway exposes to that role. With none, the chat is a plain Strands agent
on Bedrock with no tools, no gateway and no token, and the page says so. The
mode is detected when a session is built and again on Reconnect; an existing
session keeps its mode until you click Reconnect.

The gateway URL comes from GATEWAY_URL in the repo-root demo.env (re-read every
time, so Reconnect sees a newly written value), else the GATEWAY_URL environment
variable. The page shows the role switcher only when a gateway is configured.

Optional staged mode: CONSOLE_STAGES=1 (set by live-demo-helpers/console.sh)
makes the mode follow DEMO_STAGE in demo.env, plus a ?stage=N page override.
Readers never need it.

Endpoints:
  GET  /             -> the HTML page
  GET  /config       -> JSON: {"mode", "gateway", "roles", "stages", "stage"}
  GET  /tools        -> JSON: {"connected": bool, "mode": ..., "tools": [names]}
                        for ?persona=
  GET  /chat/stream  -> SSE: stream a role's agent reply token-by-token,
                        with a marker event for each tool call
  POST /chat         -> run a role's agent with a message, return the reply
  POST /reconnect    -> tear down and rebuild a role's session, re-detecting the
                        mode and re-listing tools

Every request is checked by is_local_same_origin() before anything else: the
Host must be this server and any Origin / Sec-Fetch-Site header must be
same-origin, which refuses DNS rebinding and cross-site requests.

--- Lifecycle: connect lazily --------------------------------------------------
Nothing AWS happens at startup. Each role's session (model-only: agent build;
gateway: token mint, MCP connect, tool list, agent build) is created lazily on
the first chat for that role, or rebuilt on an explicit Reconnect. If the gateway
can't be reached the session stays unconnected and chat returns a friendly
message, never a crash. Build failures are never memoized, so a later attempt
succeeds without a restart.

--- Assistant-reply extraction -------------------------------------------------
`agent(prompt)` returns a strands.agent.agent_result.AgentResult (dataclass).
Its __str__ concatenates the text blocks of the final assistant message, so
str(result) is the clean final assistant text with no stdout capture needed.

--- Denial heuristic -----------------------------------------------------------
When the customer tries to start a refund, the gateway's Cedar policy denies it
and the tool invocation raises. We classify the exception as a denial with a
conservative case-insensitive keyword match (DENIAL_KEYWORDS below). It is a
heuristic: tighten it to the real gateway denial string if you need precision.

--- Concurrency model ----------------------------------------------------------
ThreadingHTTPServer serves requests on multiple threads. Each role has at most
ONE persistent session in SESSIONS (model-only or gateway, tagged by .mode),
built lazily and kept open until the next Reconnect. Two locks cooperate:
  * SESSIONS_LOCK guards every mutation of the SESSIONS dict, so two threads
    can't both build the same role or race a build against a reconnect. Slow
    work (mint, connect, list tools, stop) happens without holding it.
  * Each role's own threading.Lock guards ask()/stream() and that role's
    teardown, so same-role requests serialize while different roles still run
    concurrently.
"""

import asyncio
import json
import os
import pathlib
import re
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = "us.anthropic.claude-sonnet-4-6"


def _port_from_env():
    """CONSOLE_PORT (default 8001), validated as a TCP port; exits cleanly if not."""
    raw = os.environ.get("CONSOLE_PORT", "8001").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        sys.exit(f"CONSOLE_PORT must be a port number from 1 to 65535 (got {raw!r}).")
    return int(raw)


PORT = _port_from_env()
# Loopback only. The console acts with your AWS credentials and has no login.
BIND_HOST = "127.0.0.1"
# Optional staged mode (see module docstring). Off unless CONSOLE_STAGES=1.
STAGES_ENABLED = os.environ.get("CONSOLE_STAGES") == "1"

# The chat personas and the Cognito identity key each one mints a token for.
PERSONAS = ("customer", "representative")

# Conservative denial classifier — see module docstring. Tighten once the real
# gateway denial string has been seen live.
DENIAL_KEYWORDS = (
    "denied", "not authorized", "unauthorized", "forbidden",
    "access denied", "permission", "403",
)

# Reuse the readers' token minter (assets/identity/mint_token.py) in-process
# rather than duplicating it. Importing it has no AWS side effects: the stack
# lookup + secret fetch happen only when mint() is called.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "identity"))
from mint_token import mint  # noqa: E402

# demo.env lives at the repo root. The Demo 1 README step appends GATEWAY_URL to
# it; we parse it on demand (never shell out / source it).
DEMO_ENV_PATH = pathlib.Path(__file__).resolve().parents[2] / "demo.env"

# Matches `GATEWAY_URL=...` with an optional `export ` prefix; value captured raw
# then stripped of surrounding quotes/whitespace by resolve_gateway_url().
_GATEWAY_LINE = re.compile(r'^\s*(?:export\s+)?GATEWAY_URL\s*=\s*(.+)$')


def resolve_gateway_url():
    """Resolve the AgentCore gateway URL on demand, tolerant of pre-deploy state.

    Precedence: demo.env wins over the process env, on purpose.
      (a) the repo-root demo.env: the LAST line matching
          `[export ]GATEWAY_URL=<value>` (as `source` would resolve it), quotes
          and whitespace stripped -> "demo.env"
      (b) else os.environ["GATEWAY_URL"] if set and non-empty -> "env"
      (c) else (no file/line, empty value, nothing in env) -> (None, None)

    Why demo.env first: the file is re-read on every call, so Reconnect picks up
    a gateway you just created or recreated. A GATEWAY_URL exported in the shell
    that launched this server is fixed at launch and can't change, so it would
    otherwise shadow the newer value in the file and point at a gateway that no
    longer exists.

    Returns a (url_or_None, source) tuple. Pure stdlib string parsing: never
    shells out, never raises.
    """
    found = None
    try:
        with DEMO_ENV_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                m = _GATEWAY_LINE.match(line)
                if not m:
                    continue
                value = m.group(1).strip()
                # Strip a single matching pair of surrounding quotes.
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                    value = value[1:-1].strip()
                found = value   # last line wins; an empty value clears it
    except OSError:
        pass
    if found:
        return found, "demo.env"

    env_val = os.environ.get("GATEWAY_URL")
    if env_val and env_val.strip():
        return env_val.strip(), "env"

    return None, None


def _gateway_url():
    """Thin wrapper returning only the resolved URL (or None)."""
    return resolve_gateway_url()[0]


# Staged mode only (CONSOLE_STAGES=1): live-demo-helpers/demoN.sh writes
# `export DEMO_STAGE=N` into demo.env and reset_demo.sh removes it. In that mode
# DEMO_STAGE=0 is model-only, 1-2 gateway with the representative only, 3 adds the
# role switcher. Readers never use this.
_STAGE_LINE = re.compile(r'^\s*(?:export\s+)?DEMO_STAGE\s*=\s*["\']?(\d+)["\']?\s*$')
MAX_STAGE = 3


def resolve_demo_stage():
    """Return the stage (0-3) from demo.env (last matching line); 0 if absent."""
    stage = 0
    try:
        with DEMO_ENV_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                m = _STAGE_LINE.match(line)
                if m:
                    stage = max(0, min(MAX_STAGE, int(m.group(1))))
    except OSError:
        pass
    return stage


# ---------------------------------------------------------------------------
# Token minting: the same mint() readers call via mint_token.py. Client ID and
# token endpoint come from the svs342-identity stack outputs, and the client
# secret is fetched from Cognito at runtime. Runs in-process when a session is
# built. The token lives in memory only. Raises a clear error (not SystemExit)
# so a lazy build fails gracefully.
# ---------------------------------------------------------------------------
def mint_token(which):
    """Mint a Cognito access token for identity `which` ('customer'|'representative')."""
    try:
        return mint(which)
    except (ValueError, RuntimeError) as e:
        raise RuntimeError(str(e)) from e


# ---------------------------------------------------------------------------
# Chat mode: gateway-backed when a gateway URL is configured, otherwise a plain
# model chat with no tools. (Staged mode takes it from the stage instead.)
# ---------------------------------------------------------------------------
MODE_MODEL_ONLY = "model-only"
MODE_GATEWAY = "gateway"
_STAGE_PARAM = re.compile(r"^\s*[0-3]\s*$")


def mode_for_stage(stage):
    """Staged mode: DEMO_STAGE=0 means model-only; 1-3 means gateway."""
    return MODE_MODEL_ONLY if stage == 0 else MODE_GATEWAY


def parse_stage(raw):
    """Validate a client-sent stage (query string or JSON) as an int 0-3.

    None / "" -> resolve_demo_stage(). Anything else that is not 0-3 raises
    ValueError.
    """
    if raw is None or raw == "":
        return resolve_demo_stage()
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= MAX_STAGE:
        return raw
    if isinstance(raw, str) and _STAGE_PARAM.match(raw):
        return int(raw)
    raise ValueError("stage must be an integer 0-3")


def current_mode(raw_stage=None):
    """The mode a NEW session should have right now. Re-reads demo.env each call.

    Normal use: gateway if a gateway URL is configured, else model-only.
    Staged mode (CONSOLE_STAGES=1): follows the stage. Raises ValueError for
    a malformed stage.
    """
    if STAGES_ENABLED:
        return mode_for_stage(parse_stage(raw_stage))
    return MODE_GATEWAY if _gateway_url() else MODE_MODEL_ONLY


def requested_mode(raw_stage=None):
    """Mode a request insists on, or None to reuse the role's existing session.

    Only staged mode forces a mode (the page's stage); normally an existing
    session keeps its mode until Reconnect re-detects it.
    """
    return mode_for_stage(parse_stage(raw_stage)) if STAGES_ENABLED else None


# ---------------------------------------------------------------------------
# Per-persona agent sessions. Built lazily (on first chat or Reconnect), kept open
# until the next Reconnect or mode switch, each guarded by its own lock.
# ---------------------------------------------------------------------------
class _AgentSession:
    """Shared turn logic (ask/stream) for both session modes."""
    mode = None

    def __init__(self, which):
        self.which = which
        self.lock = threading.Lock()
        self.tool_names = []

    def ask(self, message):
        """Run the agent for one turn under this persona's lock; return the text.
        Used by the non-streaming /chat fallback."""
        with self.lock:
            result = self._agent(message)
            # AgentResult.__str__ concatenates the final assistant message's text
            # blocks — this is the clean final reply.
            return str(result).strip()

    def stream(self, message):
        """Yield ("token", text) / ("tool", name) pairs for one turn under this
        persona's lock.

        Strands' Agent.stream_async() is an async generator yielding event dicts
        (text deltas under "data", tool calls via contentBlockStart/toolUse — see
        reply_events()). We bridge that async generator to this sync generator by
        driving it on a fresh event loop in the current request thread, pulling one
        event at a time so we can yield each chunk as it arrives.

        Exceptions (including a mid-stream gateway policy denial once the refund
        tool is invoked) propagate to the caller, which emits a terminal SSE event.
        """
        with self.lock:
            loop = asyncio.new_event_loop()

            def raw_events():
                agen = self._agent.stream_async(message)
                while True:
                    try:
                        yield loop.run_until_complete(agen.__anext__())
                    except StopAsyncIteration:
                        return

            try:
                yield from reply_events(raw_events())
            finally:
                loop.close()

    def close(self):
        """Release external resources. Nothing to release by default."""


class ModelOnlySession(_AgentSession):
    """A plain Strands agent on Bedrock: no tools, no gateway, no Cognito
    token, no MCP connection. Only the model's own knowledge, and the agent's
    default behavior (no system prompt claiming tools or access)."""
    mode = MODE_MODEL_ONLY

    def __init__(self, which):
        super().__init__(which)
        model = BedrockModel(model_id=MODEL_ID, region_name=REGION, streaming=True)
        self._agent = Agent(model=model)


class PersonaSession(_AgentSession):
    """The role's agent with the gateway's MCP tools, authorized by that role's
    Cognito token."""
    mode = MODE_GATEWAY

    def __init__(self, which):
        super().__init__(which)
        gateway_url = _gateway_url()
        if not gateway_url:
            raise RuntimeError(
                "No gateway is configured. Add GATEWAY_URL to demo.env "
                "(see the Demo 1 README), then click Reconnect."
            )
        token = mint_token(which)
        self._mcp = MCPClient(
            url=gateway_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        # Open the MCP connection and keep it open (same work __enter__ does) —
        # do NOT close it per request. list_tools_sync() runs once here.
        self._mcp.start()
        tools = self._mcp.list_tools_sync()
        # Cache the discovered tool names so /tools can surface them without ever
        # re-listing (and so, once new tools are deployed, a reconnected session
        # shows them).
        self.tool_names = [t.tool_name for t in tools]
        # streaming=True so we can relay text deltas to the browser token-by-token
        # (the /chat/stream SSE endpoint). The non-streaming /chat fallback still
        # works: agent(prompt) returns a single AgentResult we stringify.
        model = BedrockModel(model_id=MODEL_ID, region_name=REGION, streaming=True)
        self._agent = Agent(model=model, tools=tools)

    def close(self):
        """Tear down the MCP client cleanly (used by Reconnect).

        SDK teardown is MCPClient.stop(None, None, None) — the non-context-manager
        path (__exit__ just forwards to stop(); start()'s own error path calls
        stop(None, None, None)). It is defensive about partial initialization and
        resets the instance's fields, so it is safe even if start() never fully
        succeeded. We discard this instance afterward and build a fresh one.
        Wrapped in try/except so a noisy teardown can't block the rebuild.
        """
        try:
            self._mcp.stop(None, None, None)
        except Exception as e:  # noqa: BLE001 - teardown must never block the rebuild
            print(f"[console] ignoring MCP client teardown error: {e!r}", file=sys.stderr)


# persona -> ModelOnlySession|PersonaSession. One session per persona at a time;
# its .mode says which kind it is. Built lazily; mutations guarded by SESSIONS_LOCK.
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

# Browsers let any open web page send requests to http://localhost. This app
# acts with your AWS credentials, so only serve requests that come from its
# own page: the Host must be this server (blocks DNS rebinding), and the
# browser's Origin / Sec-Fetch-Site headers, when present, must be same-origin
# (blocks cross-site request forgery). Non-browser clients (curl) send neither.
LOCAL_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}


def is_local_same_origin(headers):
    """True if the request targets this local server and isn't cross-site."""
    if headers.get("Host", "") not in LOCAL_HOSTS:
        return False
    origin = headers.get("Origin")
    if origin is not None and origin not in {f"http://{h}" for h in LOCAL_HOSTS}:
        return False
    return headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")


def build_session(persona, mode):
    """Build a fresh session of `mode` for `persona` (slow; call without locks)."""
    if mode == MODE_MODEL_ONLY:
        return ModelOnlySession(persona)   # never mints, never opens MCP
    return PersonaSession(persona)


def retire_session(sess):
    """Tear down a session that is no longer in SESSIONS. Takes its own lock
    first, so an in-flight turn finishes before the MCP client is stopped."""
    if sess is not None:
        with sess.lock:
            sess.close()


def install_session(persona, session):
    """Make `session` the persona's session, replacing (and tearing down) any
    other one another thread installed meanwhile."""
    with SESSIONS_LOCK:
        loser = SESSIONS.get(persona)
        SESSIONS[persona] = session
    if loser is not None and loser is not session:
        retire_session(loser)
    return session


def get_or_build_session(persona, mode=None):
    """Return a live session for `persona`, building it lazily.

    mode=None (normal use): reuse the persona's existing session whatever its
    mode; with none, build one of current_mode(). An existing session keeps its
    mode until Reconnect re-detects it.
    mode given (staged mode): reuse a session of that mode; one of the OTHER
    mode is popped and torn down like Reconnect does.

    Double-checked locking: check under SESSIONS_LOCK, build OUTSIDE it (the slow
    work), then insert under SESSIONS_LOCK unless another thread already
    installed a same-mode one (then close ours and return the winner). A failed
    build is never stored, so a later call retries cleanly.

    Raises ValueError for an unknown persona and propagates any build/connect
    error (callers turn it into a friendly message).
    """
    if persona not in PERSONAS:
        raise ValueError(f"unknown persona '{persona}'")

    stale = None
    with SESSIONS_LOCK:
        existing = SESSIONS.get(persona)
        if existing is not None:
            if mode is None or existing.mode == mode:
                return existing
            stale = SESSIONS.pop(persona)
    retire_session(stale)

    if mode is None:
        mode = current_mode()
    # Slow work outside the lock; may raise (no gateway, mint or connect failed).
    session = build_session(persona, mode)

    with SESSIONS_LOCK:
        winner = SESSIONS.get(persona)
        if winner is not None and winner.mode == mode:
            # Another thread built one first, so discard ours.
            session.close()
            return winner
    return install_session(persona, session)


def _is_denial(err_text):
    low = err_text.lower()
    return any(kw in low for kw in DENIAL_KEYWORDS)


def _explain(exc):
    """Flatten an exception (including ExceptionGroup / asyncio TaskGroup wrappers)
    into a readable one-line message. The MCP client raises a bare
    'unhandled errors in a TaskGroup (1 sub-exception)' that hides the real cause
    (usually an HTTP 401/403 from the gateway, a connection error, or a protocol
    failure), so we recurse into .exceptions and surface the innermost messages."""
    subs = getattr(exc, "exceptions", None)
    if subs:
        return "; ".join(_explain(s) for s in subs)
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _friendly_connect_error(exc):
    """Turn a connection failure into guidance. The most common cause is a STALE
    gateway URL: the gateway identifier changes every time the gateway is
    recreated, so a demo.env written for an earlier gateway points at one that no
    longer exists. Catch that specific case and say what to do."""
    flat = _explain(exc)
    low = flat.lower()
    if "does not exist" in low or "resource not found" in low:
        return ("The gateway in demo.env no longer exists (the saved GATEWAY_URL "
                "is stale). Update GATEWAY_URL in demo.env to your current "
                f"gateway's URL, then click Reconnect. [{flat}]")
    return flat


def _not_ready_message(mode, exc):
    """Message for a session that could not be built, worded for its mode."""
    if mode == MODE_MODEL_ONLY:
        return "Could not start the model. " + _explain(exc)
    return "Not connected yet. " + _friendly_connect_error(exc)


# ---------------------------------------------------------------------------
# Reply shaping — turn raw Strands stream events into what the page renders.
#
# Event shapes relied on (strands-agents 1.61.1; Agent.stream_async yields
# event.as_dict() for every callback event, agent/agent.py:1448-1451):
#   * text delta:   {"data": "<text>", "delta": ...}           (types/_events.py:159)
#   * raw chunk:    {"event": {"contentBlockStart": {"start": {"toolUse":
#                    {"toolUseId": ..., "name": ...}}}}}        (types/_events.py:113,
#                    event_loop/streaming.py:198-202) — once per tool call
#   * tool input:   {"type": "tool_use_stream", "current_tool_use":
#                    {"toolUseId": ..., "name": ..., "input": "<partial>"}}
#                    (types/_events.py:151) — REPEATED per input delta
# A tool call is seen first via contentBlockStart; the repeated tool_use_stream
# partials (and models that only put the name in the delta,
# event_loop/streaming.py:233-236) are deduped by toolUseId.
# ---------------------------------------------------------------------------
def _tool_use_of(event):
    """Return (name, dedupe_key) if `event` announces a tool call, else (None, None)."""
    start = ((event.get("event") or {}).get("contentBlockStart") or {}).get("start") or {}
    tool = start.get("toolUse")
    if not (isinstance(tool, dict) and tool.get("name")):
        tool = event.get("current_tool_use")
    if isinstance(tool, dict) and tool.get("name"):
        return tool["name"], tool.get("toolUseId") or tool["name"]
    return None, None


def reply_events(events):
    """Map Strands stream events to ("token", text) / ("tool", name) pairs.

    * One ("tool", name) per tool invocation (partials deduped by toolUseId).
    * When a tool call separates two text segments of the same reply, the next
      segment is prefixed so the text reads as a new paragraph ("\\n\\n"); if the
      earlier text already ends in newline(s) it is only topped up to two. No
      break is ever added at the very start of a reply.
    """
    seen = set()
    wrote_text = False
    pending_break = False
    tail = ""   # last two characters of text emitted so far
    for event in events:
        if not isinstance(event, dict):
            continue
        name, key = _tool_use_of(event)
        if name:
            if key not in seen:
                seen.add(key)
                pending_break = wrote_text
                yield ("tool", name)
            continue
        text = event.get("data")
        if not (isinstance(text, str) and text):
            continue
        if pending_break:
            text = text.lstrip("\n")
            if not text:
                continue   # a bare newline chunk — keep waiting for real text
            have = len(tail) - len(tail.rstrip("\n"))
            text = "\n" * (2 - have) + text
            pending_break = False
        wrote_text = True
        tail = (tail + text)[-2:]
        yield ("token", text)


# ---------------------------------------------------------------------------
# The page. Plain HTML + JS, no framework/build step.
# ---------------------------------------------------------------------------
PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Tool Layer Console</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #f2f3f3; }
  header { background: #232f3e; color: #fff; padding: 16px 24px; font-size: 20px; font-weight: 600; }
  main { padding: 24px; max-width: 720px; margin: 0 auto; }
  .panel { display: none; }
  .panel.active { display: block; }
  button { background: #1b8a2f; color: #fff; border: 0; border-radius: 6px;
           padding: 10px 20px; font-size: 15px; cursor: pointer; }
  button:disabled { background: #a5b0b5; cursor: default; }
  button.reconnect { background: #37475a; }
  button.tools { background: #5a4b8a; }
  .chat { background: #fff; border: 1px solid #d5dbdb; border-radius: 8px; padding: 16px; }
  .messages { height: 420px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px;
              padding: 4px; margin-bottom: 12px; }
  .bubble { max-width: 80%; padding: 10px 14px; border-radius: 12px; font-size: 14px;
            line-height: 1.4; white-space: pre-wrap; }
  .user { align-self: flex-end; background: #232f3e; color: #fff; border-bottom-right-radius: 4px; }
  .assistant { align-self: flex-start; background: #eef1f3; color: #16191f; border-bottom-left-radius: 4px; }
  /* Agent replies are rendered from (escaped) markdown, so HTML handles spacing. */
  .assistant.md { white-space: normal; }
  .md p, .md ul, .md ol { margin: 0 0 8px; }
  .md ul, .md ol { padding-left: 20px; }
  .md > :last-child, .md .seg > :last-child { margin-bottom: 0; }
  .md code { font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
             background: #dde3e8; border-radius: 4px; padding: 1px 4px; }
  /* Headings stay modest inside a chat bubble. */
  .md .md-h { font-weight: 700; font-size: 14px; margin: 4px 0 6px; }
  .md .md-h1 { font-size: 16px; }
  .md .md-h2 { font-size: 15px; }
  /* Pipe tables: compact, and wide ones scroll inside the bubble. */
  .md .md-table { max-width: 100%; overflow-x: auto; margin: 0 0 8px; }
  .md table { border-collapse: collapse; font-size: 14px; }
  .md th, .md td { border: 1px solid #d5dbdb; padding: 4px 8px; text-align: left;
                   vertical-align: top; }
  .md th { background: #dde3e8; font-weight: 600; }
  .md th.md-ac, .md td.md-ac { text-align: center; }
  .md th.md-ar, .md td.md-ar { text-align: right; }
  .toolcall { width: fit-content; margin: 6px 0; padding: 2px 8px; border-radius: 10px;
              background: #e4e0f1; color: #5a4b8a; font-size: 12px;
              font-family: SFMono-Regular, Menlo, Consolas, monospace; }
  .hint { align-self: flex-start; background: #fff3cd; color: #7a5b00; font-style: italic; }
  .denied { align-self: flex-start; background: #f8d7da; color: #842029;
            border: 1px solid #f1aeb5; font-weight: 600; }
  .warn { align-self: flex-start; background: #fff3cd; color: #7a5b00; border: 1px solid #ffe69c; }
  .roles { display: inline-flex; border: 1px solid #d5dbdb; border-radius: 8px;
           overflow: hidden; margin-bottom: 12px; background: #fff; }
  .role { background: #fff; color: #232f3e; border: 0; border-radius: 0;
          padding: 8px 16px; font-size: 14px; font-weight: 600; }
  .role + .role { border-left: 1px solid #d5dbdb; }
  .role.active { background: #232f3e; color: #fff; }
  .mode-label { font-size: 12px; color: #6c757d; margin: 0 0 8px; }
  .messages.hidden, .roles.hidden, .mode-label.hidden { display: none; }
  .composer { display: flex; gap: 8px; }
  .composer input { flex: 1; padding: 10px 12px; border: 1px solid #d5dbdb; border-radius: 6px;
                    font-size: 14px; }
</style>
</head>
<body>
<header>Tool Layer Console</header>
<main>
  <section id="panel-chat" class="panel active">
    <div class="roles hidden" id="roles" role="group" aria-label="Chat as">
      <button class="role active" data-role="representative" aria-pressed="true"
              onclick="setRole('representative')">Customer Service Representative</button>
      <button class="role" data-role="customer" aria-pressed="false"
              onclick="setRole('customer')">Customer</button>
    </div>
    <!-- Shown only when no gateway is configured: a plain model chat, no tools. -->
    <div class="mode-label hidden" id="mode-label">Model only &mdash; no gateway configured yet, so no tools</div>
    <div class="chat">
      <!-- One transcript per role: each role has its own agent session. -->
      <div class="messages" id="msgs-representative"></div>
      <div class="messages hidden" id="msgs-customer"></div>
      <div class="composer">
        <input id="in-chat" aria-label="Message" placeholder="Ask the agent&hellip;"
               onkeydown="if(event.key==='Enter')send(activeRole)">
        <button onclick="send(activeRole)">Send</button>
        <button class="reconnect" onclick="reconnect(activeRole, this)">Reconnect</button>
        <button class="tools" onclick="showTools(activeRole)">Show tools</button>
      </div>
    </div>
  </section>

</main>
<script>
// --- Role switcher (Chat tab) -------------------------------------------
// Send / Reconnect / Show tools act on the active role. Each role keeps its
// own transcript because each has its own agent session on the server.
let activeRole = 'representative';
function setRole(role) {
  activeRole = role;
  document.querySelectorAll('.role').forEach(function(b) {
    const on = b.dataset.role === role;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  ['representative', 'customer'].forEach(function(r) {
    document.getElementById('msgs-' + r).classList.toggle('hidden', r !== role);
  });
  document.getElementById('in-chat').focus();
}

// --- Config -------------------------------------------------------------
// /config says whether a gateway is configured (mode), whether to show the role
// switcher, and, in the optional staged mode only, the stage. It is applied
// on page load and after a successful Reconnect, never on a timer.
let config = {mode: 'model-only', roles: false, stages: false, stage: 0};
function stageOverride() {
  const v = new URLSearchParams(window.location.search).get('stage');
  return (v !== null && /^[0-3]$/.test(v)) ? v : null;
}
function applyConfig(c) {
  config = c;
  document.getElementById('roles').classList.toggle('hidden', !c.roles);
  document.getElementById('mode-label').classList.toggle('hidden', c.mode !== 'model-only');
  // Without the switcher, chat as the representative: its token works for
  // every tool.
  if (!c.roles && activeRole !== 'representative') setRole('representative');
}
// Fetch the server's current view without applying it. Falls back to the
// current config if the server is unreachable.
async function fetchConfig() {
  const forced = stageOverride();
  try {
    return await (await fetch('/config' + (forced !== null ? '?stage=' + forced : ''))).json();
  } catch (_) {
    return config;
  }
}
async function loadConfig() {
  applyConfig(await fetchConfig());
}
// Query-string suffix for requests: only staged mode sends a stage.
function stageParam() {
  return config.stages ? '&stage=' + config.stage : '';
}

// --- Reconnect ----------------------------------------------------------
// Tears down + rebuilds a role's session so the chat picks up a newly
// configured gateway or newly deployed tools. The server re-reads demo.env, so
// the mode is detected again. Always HTTP 200; the body says whether it worked.
async function reconnect(persona, btn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Reconnecting\\u2026';
  try {
    const cfg = await fetchConfig();
    const body = {persona: persona};
    if (cfg.stages) body.stage = cfg.stage;
    const r = await (await fetch('/reconnect', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    })).json();
    if (r.connected) {
      bubble(persona, r.mode === 'gateway'
        ? '\\u2705 Reconnected. Try Show tools to see what this role can use.'
        : '\\u2705 Reconnected. Still model-only: no gateway is configured yet.', 'hint');
      applyConfig(cfg);
    } else {
      bubble(persona, '\\u26a0\\ufe0f Could not connect: ' + (r.error || 'unknown error'), 'warn');
    }
  } catch (_) {
    bubble(persona, '\\u26a0\\ufe0f Could not reach the server to reconnect.', 'warn');
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

// --- Chat (customer / representative) -----------------------------------
function bubble(persona, text, cls) {
  const box = document.getElementById('msgs-' + persona);
  const div = document.createElement('div');
  div.className = 'bubble ' + cls;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}
function scrollDown(persona) {
  const box = document.getElementById('msgs-' + persona);
  box.scrollTop = box.scrollHeight;
}
// --- Show tools ---------------------------------------------------------
// Fetches the MCP tools this role's agent can see and prints them as a bubble
// in that chat. Tools appear once the gateway has them and the session has
// (re)connected, so after deploying new tools click Reconnect, then Show tools.
async function showTools(persona) {
  try {
    const r = await (await fetch('/tools?persona=' + encodeURIComponent(persona)
                                 + stageParam())).json();
    if (r.mode === 'model-only') {
      bubble(persona, 'No tools: no gateway is configured yet, so the agent is using only the model\\u2019s own knowledge. Add GATEWAY_URL to demo.env, then click Reconnect.', 'hint');
      return;
    }
    if (!r.connected) {
      bubble(persona, '\\u26a0\\ufe0f Not connected yet. Send a message or click Reconnect first.', 'warn');
      return;
    }
    const tools = r.tools || [];
    if (tools.length === 0) {
      bubble(persona, 'This role can\\u2019t see any MCP tools yet.', 'hint');
    } else {
      bubble(persona, 'MCP tools (' + tools.length + '):\\n\\u2022 ' + tools.join('\\n\\u2022 '), 'hint');
    }
  } catch (_) {
    bubble(persona, '\\u26a0\\ufe0f Could not reach the server to list tools.', 'warn');
  }
}

// --- Minimal, safe markdown for agent replies ---------------------------
// The model answers in markdown. HTML is escaped FIRST, then a small subset is
// turned into tags: **bold**, *italic* / _italic_, `code`, - / * bullet lists,
// 1. numbered lists, # headings, GitHub-style pipe tables, paragraphs and line
// breaks. Model text never reaches innerHTML unescaped. (Regexes use [*] / [ ]
// / [|] classes instead of backslash escapes because this page lives inside a
// Python string.)
function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
}
function renderInline(s) {
  // s is already escaped. Pull code spans out first so their contents stay literal.
  const codes = [];
  s = s.replace(/`([^`]+)`/g, function(_, c) {
    codes.push(c);
    return '\\u0000' + (codes.length - 1) + '\\u0000';
  });
  s = s.replace(/[*][*]([^* ](?:[^*]*[^* ])?)[*][*]/g, '<strong>$1</strong>');
  s = s.replace(/[*]([^* ](?:[^*]*[^* ])?)[*]/g, '<em>$1</em>');
  // _italic_ only at word edges, so snake_case and orders___getOrder stay intact.
  s = s.replace(/(^|[^A-Za-z0-9_])_([^_ ](?:[^_]*[^_ ])?)_(?![A-Za-z0-9_])/g, '$1<em>$2</em>');
  return s.replace(/\\u0000([0-9]+)\\u0000/g, function(_, i) {
    return '<code>' + codes[parseInt(i, 10)] + '</code>';
  });
}
// --- Pipe tables ---
// A table is a header row, then a delimiter row (---, :---, ---:, :---:), then
// body rows until a blank line or a line without a pipe. Without a complete
// delimiter row it is not a table, so a streaming table shows as text until
// that row arrives. Escaped pipes stay literal: they become \\u0001 before
// splitting and turn back into | after the cell is rendered.
const TABLE_DELIM = /^[ \\t]*[|]?[ \\t]*:?-{3,}:?[ \\t]*([|][ \\t]*:?-{3,}:?[ \\t]*)*[|]?[ \\t]*$/;
function hasPipe(line) {
  return line.split('\\\\|').join('').indexOf('|') !== -1;
}
function splitRow(line) {
  let s = line.split('\\\\|').join('\\u0001').trim();
  if (s.charAt(0) === '|') s = s.slice(1);
  if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
  return s.split('|').map(function(c) { return c.trim(); });
}
function isTableStart(lines, i) {
  return i + 1 < lines.length && hasPipe(lines[i]) && hasPipe(lines[i + 1])
      && TABLE_DELIM.test(lines[i + 1])
      && splitRow(lines[i]).length === splitRow(lines[i + 1]).length;
}
function renderCell(tag, text, align) {
  // align comes only from the fixed set below; text is escaped already.
  const cls = align === 'center' ? ' class="md-ac"' : (align === 'right' ? ' class="md-ar"' : '');
  return '<' + tag + cls + '>' + renderInline(text).replace(/\\u0001/g, '|') + '</' + tag + '>';
}
function renderTable(header, delim, rows) {
  const aligns = splitRow(delim).map(function(d) {
    const l = d.charAt(0) === ':', r = d.charAt(d.length - 1) === ':';
    return (l && r) ? 'center' : (r ? 'right' : 'left');
  });
  function row(cells, tag) {
    return '<tr>' + aligns.map(function(a, k) {
      return renderCell(tag, k < cells.length ? cells[k] : '', a);   // pad / drop
    }).join('') + '</tr>';
  }
  let html = '<div class="md-table"><table><thead>' + row(splitRow(header), 'th') + '</thead>';
  if (rows.length) {
    html += '<tbody>' + rows.map(function(r) { return row(splitRow(r), 'td'); }).join('') + '</tbody>';
  }
  return html + '</table></div>';
}
function renderMarkdown(raw) {
  const lines = escapeHtml(raw.replace(/[\\u0000\\u0001]/g, '').replace(/\\r/g, '')).split('\\n');
  let html = '';
  let para = [];
  let list = null;   // {tag: 'ul' or 'ol', start: n, items: [...]}
  function flushPara() {
    if (para.length) html += '<p>' + para.map(renderInline).join('<br>') + '</p>';
    para = [];
  }
  function flushList() {
    if (!list) return;
    const start = (list.tag === 'ol' && list.start !== 1) ? ' start="' + list.start + '"' : '';
    html += '<' + list.tag + start + '>'
          + list.items.map(function(it) { return '<li>' + renderInline(it) + '</li>'; }).join('')
          + '</' + list.tag + '>';
    list = null;
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const t = line.replace(/[ ]+$/, '');
    let m;
    if (t.trim() === '') { flushPara(); flushList(); continue; }
    if (isTableStart(lines, i)) {
      flushPara(); flushList();
      const rows = [];
      let j = i + 2;
      while (j < lines.length && lines[j].trim() !== '' && hasPipe(lines[j])) rows.push(lines[j++]);
      html += renderTable(lines[i], lines[i + 1], rows);
      i = j - 1;
      continue;
    }
    if ((m = /^[ ]{0,3}(#{1,6})[ ]+(.*?)(?:[ ]+#+)?$/.exec(t))) {
      flushPara(); flushList();
      const level = m[1].length;
      html += '<div class="md-h md-h' + level + '">' + renderInline(m[2]) + '</div>';
      continue;
    }
    if ((m = /^[ ]*[-*+][ ]+(.*)$/.exec(t))) {
      flushPara();
      if (!list || list.tag !== 'ul') { flushList(); list = {tag: 'ul', start: 1, items: []}; }
      list.items.push(m[1]);
      continue;
    }
    if ((m = /^[ ]*([0-9]+)[.)][ ]+(.*)$/.exec(t))) {
      flushPara();
      if (!list || list.tag !== 'ol') {
        flushList();
        list = {tag: 'ol', start: parseInt(m[1], 10), items: []};
      }
      list.items.push(m[2]);
      continue;
    }
    if (list && /^[ ]{2,}/.test(line)) {   // indented continuation of a list item
      list.items[list.items.length - 1] += ' ' + t.trim();
      continue;
    }
    flushList();
    para.push(t);
  }
  flushPara();
  flushList();
  return html;
}

// Streaming chat over Server-Sent Events. The assistant bubble is created empty
// and grows token-by-token; a terminal event finalizes it. The bubble holds
// text segments (markdown, re-rendered from their raw text on every token) and,
// between them, a tool-call marker for each tool the agent invokes. Partial text
// and markers that arrived before a mid-stream denial stay visible (the agent
// visibly tries, then gets blocked), with the red denial block appended after.
function send(persona) {
  const input = document.getElementById('in-chat');
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  bubble(persona, message, 'user');

  const reply = bubble(persona, '', 'assistant md');   // grows as events arrive
  let seg = null;      // current text segment element
  let segRaw = '';     // its accumulated raw markdown
  function hasContent() { return reply.childNodes.length > 0; }
  const url = '/chat/stream?persona=' + encodeURIComponent(persona)
            + '&message=' + encodeURIComponent(message)
            + stageParam();
  const es = new EventSource(url);

  es.addEventListener('token', function(e) {
    if (!seg) {
      seg = document.createElement('div');
      seg.className = 'seg';
      reply.appendChild(seg);
      segRaw = '';
    }
    segRaw += JSON.parse(e.data).text;
    seg.innerHTML = renderMarkdown(segRaw);   // escaped first — see renderMarkdown
    scrollDown(persona);
  });
  es.addEventListener('tool', function(e) {
    seg = null;   // the next token starts a new segment after this marker
    const chip = document.createElement('div');
    chip.className = 'toolcall';
    chip.textContent = '\\ud83d\\udd27 ' + JSON.parse(e.data).name;
    reply.appendChild(chip);
    scrollDown(persona);
  });
  es.addEventListener('denied', function() {
    if (!hasContent()) reply.remove();   // nothing streamed — drop the empty bubble
    bubble(persona, '\\ud83d\\udeab Denied by gateway policy — this token does not '
         + 'carry the required scope to do that.', 'denied');
    es.close();
  });
  es.addEventListener('error', function(e) {
    // Distinguish an application error event (has data) from a transport drop.
    if (e.data) {
      if (!hasContent()) reply.remove();
      let msg = 'the agent hit an error';
      try { msg = JSON.parse(e.data).message || msg; } catch (_) {}
      bubble(persona, '\\u26a0\\ufe0f ' + msg, 'warn');
    } else if (!hasContent()) {
      reply.textContent = '\\u26a0\\ufe0f Could not reach the server.';
      reply.className = 'bubble warn';
    }
    es.close();
  });
  es.addEventListener('done', function() {
    if (!hasContent()) reply.remove();   // empty completion — don't leave a blank bubble
    es.close();
  });
}

loadConfig();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not is_local_same_origin(self.headers):
            return self._send(403, json.dumps({"error": "cross-site request refused"}))
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path.startswith("/config"):
            return self._config()
        elif self.path.startswith("/tools"):
            return self._tools()
        elif self.path.startswith("/chat/stream"):
            return self._chat_stream()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if not is_local_same_origin(self.headers):
            return self._send(403, json.dumps({"error": "cross-site request refused"}))
        if self.path == "/chat":
            return self._chat()
        if self.path == "/reconnect":
            return self._reconnect()
        return self._send(404, json.dumps({"error": "not found"}))

    def _config(self):
        """GET /config: what the page needs to decide what to show, read fresh
        on every call so a newly written GATEWAY_URL is noticed on Reconnect.
        {"mode": "gateway"|"model-only", "gateway": bool, "roles": bool,
         "stages": bool, "stage": N}. "roles" says whether to show the role
        switcher; "stages"/"stage" matter only in the optional staged mode.
        Never exposes the gateway URL itself."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            stage = parse_stage((params.get("stage") or [None])[0]) if STAGES_ENABLED else 0
            mode = current_mode(stage if STAGES_ENABLED else None)
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}))
        self._send(200, json.dumps({
            "mode": mode,
            "gateway": mode == MODE_GATEWAY,
            "roles": mode == MODE_GATEWAY and (not STAGES_ENABLED or stage >= 3),
            "stages": STAGES_ENABLED,
            "stage": stage,
        }))

    def _tools(self):
        """GET /tools?persona=... : the MCP tool names the role's agent can
        currently see. Read-only: reports the names cached when the session
        connected; never builds a session, never raises. Returns
        {"connected": bool, "mode": ..., "tools": [names]}. Model-only mode is
        always "connected" with no tools. Gateway mode with no session yet ->
        connected false (the page says to send a message or click Reconnect)."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        persona = (params.get("persona") or [""])[0]
        if persona not in PERSONAS:
            return self._send(404, json.dumps({"error": "unknown persona"}))
        try:
            forced = requested_mode((params.get("stage") or [None])[0])
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}))
        with SESSIONS_LOCK:
            sess = SESSIONS.get(persona)
        if forced is None:
            # Report what this role's own session sees. With no session yet,
            # fall back to what a new one would be.
            mode = sess.mode if sess is not None else current_mode()
        else:
            mode = forced
        if mode == MODE_MODEL_ONLY:
            return self._send(200, json.dumps(
                {"connected": True, "mode": MODE_MODEL_ONLY, "tools": []}))
        if sess is None or sess.mode != MODE_GATEWAY:
            return self._send(200, json.dumps(
                {"connected": False, "mode": MODE_GATEWAY, "tools": []}))
        self._send(200, json.dumps(
            {"connected": True, "mode": MODE_GATEWAY, "tools": list(sess.tool_names)}))

    def _reconnect(self):
        """POST /reconnect {"persona": ...} : tear down the role's existing
        session (if any) and build a fresh one, re-detecting the mode from
        demo.env (gateway: mint a token, re-list tools). Always HTTP 200; the
        body reports success or failure and the mode.
        """
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
            persona = payload["persona"]
            mode = current_mode(payload.get("stage") if STAGES_ENABLED else None)
        except Exception as e:
            return self._send(200, json.dumps({"connected": False, "error": f"bad request: {e}"}))

        if persona not in PERSONAS:
            return self._send(200, json.dumps(
                {"connected": False, "error": "unknown persona"}))

        # Pop the old session under the dict lock, then tear it down (under its
        # own lock, so an in-flight chat on that role finishes first) and
        # rebuild. A model-only rebuild never opens an MCP connection.
        with SESSIONS_LOCK:
            old = SESSIONS.pop(persona, None)

        try:
            retire_session(old)
            install_session(persona, build_session(persona, mode))
            self._send(200, json.dumps({"connected": True, "mode": mode}))
        except Exception as e:
            # Print the full traceback to the server console for diagnosis, and
            # return the unwrapped (TaskGroup-flattened) message to the page.
            traceback.print_exc()
            self._send(200, json.dumps({"connected": False, "mode": mode,
                                        "error": _friendly_connect_error(e)}))

    def _chat(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
            persona = payload["persona"]
            message = payload["message"]
            mode = requested_mode(payload.get("stage"))
        except Exception as e:
            return self._send(400, json.dumps({"error": f"bad request: {e}"}))

        try:
            session = get_or_build_session(persona, mode)
        except ValueError as e:
            return self._send(404, json.dumps({"error": str(e)}))
        except Exception as e:
            # Gateway not resolvable / stale / mint / connect failed (or, in
            # model-only mode, the model client could not be built): a friendly
            # message, never a 500. _friendly_connect_error distinguishes a stale
            # gateway from a missing URL.
            return self._send(200, json.dumps({
                "reply": "\u26a0\ufe0f " + _not_ready_message(requested_mode(payload.get("stage")) or current_mode(), e),
                "error": True,
            }))

        try:
            reply = session.ask(message)
            self._send(200, json.dumps({"reply": reply}))
        except Exception as e:
            # The agent error is shown as a chat bubble, never a dead 500 — always
            # HTTP 200 so the UI can render it. No gateway in model-only mode, so
            # a Bedrock AccessDenied there is a plain error, not a policy denial.
            err = str(e)
            if session.mode == MODE_GATEWAY and _is_denial(err):
                self._send(200, json.dumps({
                    "reply": "\U0001f6ab Denied by gateway policy — this identity's "
                             "token doesn't carry the required scope to do that.",
                    "denied": True,
                }))
            else:
                self._send(200, json.dumps({
                    "reply": f"\u26a0\ufe0f The agent hit an error: {err}",
                    "error": True,
                }))

    def _sse(self, event, payload):
        """Write one Server-Sent Event frame and flush it to the browser."""
        frame = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
        self.wfile.write(frame.encode())
        self.wfile.flush()

    def _chat_stream(self):
        """SSE endpoint: GET /chat/stream?persona=...&message=...

        EventSource is GET-only, so the message rides in the query string
        (chat prompts are short). Emits:
          event: token  {"text": "<chunk>"}   — repeated, as deltas arrive
          event: tool   {"name": "<tool>"}    — once per tool invocation, in order
          event: denied {}                     — terminal, gateway policy denial
          event: error  {"message": "..."}     — terminal, any other failure
          event: done   {}                     — terminal, normal completion
        Partial 'token' text that arrived before a mid-stream denial stays on
        screen (the agent visibly tries, then gets blocked).
        """
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        persona = (params.get("persona") or [""])[0]
        message = (params.get("message") or [""])[0]
        raw_stage = (params.get("stage") or [None])[0]

        # SSE headers — no Content-Length (open-ended stream); disable buffering.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        if not message:
            self._sse("error", {"message": "empty message"})
            return
        try:
            mode = requested_mode(raw_stage)
            report_mode = mode or current_mode()
        except ValueError as e:
            self._sse("error", {"message": str(e)})
            return

        try:
            session = get_or_build_session(persona, mode)
        except ValueError:
            self._sse("error", {"message": "unknown persona"})
            return
        except Exception as e:
            # Gateway not resolvable / stale / mint / connect failed (or the
            # model client could not be built): a friendly terminal error event.
            self._sse("error", {"message": _not_ready_message(report_mode, e)})
            return

        try:
            for kind, value in session.stream(message):
                if kind == "tool":
                    self._sse("tool", {"name": value})
                else:
                    self._sse("token", {"text": value})
            self._sse("done", {})
        except BrokenPipeError:
            # Browser navigated away / closed the tab mid-stream — nothing to do.
            return
        except Exception as e:
            # Classify a gateway policy denial (may surface mid-stream, once the
            # refund tool is invoked) vs any other error. Same conservative
            # heuristic as /chat; tighten after seeing a live denial string.
            # Model-only mode has no gateway: show the real error instead.
            if session.mode == MODE_GATEWAY and _is_denial(str(e)):
                self._sse("denied", {})
            else:
                self._sse("error", {"message": str(e)})

    def log_message(self, *args):
        pass   # keep the console quiet; request lines can carry chat text


if __name__ == "__main__":
    # No AWS preconditions: the console comes up whether or not a gateway, a
    # demo.env or the identity stack exists. Each role's session connects lazily
    # on first use (or an explicit Reconnect).
    print("Starting the Tool Layer Console\u2026 (sessions connect lazily)")

    url = f"http://localhost:{PORT}"
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"Tool Layer Console running at {url}  (listening on {BIND_HOST} only; Ctrl-C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()   # open the browser
    server.serve_forever()
