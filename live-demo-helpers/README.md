# Live demo helpers

Tooling for presenting **SVS342** on stage. The Demo 1/2/3 READMEs are the
self-guided walkthrough; this folder runs the same steps in a form that's easier
to narrate in front of an audience, with the agent chat in a browser instead of a
terminal.

You don't need anything here to follow the READMEs.

## What's in here

| File | What it does |
|---|---|
| `demo1.sh`, `demo2.sh`, `demo3.sh` | The `agentcore` steps for each demo, one Enter-gated command at a time |
| `demo_runner.sh` | The `banner` / `step` / `run` / `quiet` / `note` helpers the scripts source |
| `console.sh` | Starts the shared web console ([`../assets/console/console.py`](../assets/console/console.py)) in staged mode, so it follows `DEMO_STAGE` |
| `reset_demo.sh` | Rewinds to the state right after `setup.sh`, for another rehearsal |

## Prerequisites

- Everything in the root [`README.md`](../README.md) prerequisites (AgentCore CLI,
  SAM CLI, jq, AWS creds, Bedrock model access, the Python venv).
- **[`../setup.sh`](../setup.sh) has been run once** from the repo root.

## Presenter flow

Use two terminals, both in `live-demo-helpers/` with the venv active.

**Terminal 1 — start the console** (before any demo has run):

```bash
./console.sh
```

It sources `../demo.env` and the venv, serves http://localhost:8001 on `127.0.0.1` and opens it in your browser. Nothing connects to
AWS at startup; each chat connects on first use or on **Reconnect**.

Before Demo 1 the chat already works, as a **model-only** agent: Strands +
Bedrock with no tools, no gateway, and no Cognito token, so it can answer only
from the model's own knowledge. That's the "before" beat. Ask
*"What's the status of order 12345?"* and the model can't know. The Chat tab
shows a small **Model only — no tools** label, and **Show tools** says there are
none.

**Terminal 2 — run the demos in order**, switching to the browser after each one. For the Demo 2 approval, run `python ../Demo2/approve_ui.py` in a **third terminal** (http://localhost:8000) and keep the console open on 8001:

```bash
source ../.venv/bin/activate
./demo1.sh     # then click Reconnect in the console — the chat switches to the gateway tools
./demo2.sh     # then Reconnect — the refund tools appear
./demo3.sh     # then Reconnect — compare customer vs representative
```

Each script loads `../demo.env` itself, so you don't need to source it in
terminal 2. `demo1.sh` writes `GATEWAY_URL` into `demo.env`; that's how the console
and the later scripts find the gateway.

## How the console follows along

Each `demoN.sh` writes `DEMO_STAGE=N` into `demo.env` as its last step. The console
reads it (it only follows the stage when started with `CONSOLE_STAGES=1`, which `console.sh` sets) on page load and after a successful Reconnect (never on a timer), and
only shows what the story has reached:

| Stage | Shows |
|---|---|
| 0 | Plain chat (as the representative), **model only — no tools** |
| 1 | Plain chat (as the representative), backed by the gateway tools |
| 2 | Same as 1, plus the refund tools. Approve the parked refund in `Demo2/approve_ui.py` on port 8000 |
| 3 | Adds the **Customer / Customer Service Representative** role switcher |

Add `?stage=N` to the URL (e.g. http://localhost:8001/?stage=3) to force a stage
if you need to.

In staged mode the stage picks the chat mode, not whether `GATEWAY_URL` is set
(`reset_demo.sh` leaves a stale one in `demo.env`). Without `CONSOLE_STAGES=1` the console is gateway-backed whenever a `GATEWAY_URL` is configured. The page sends its stage with
every chat, Reconnect, and Show tools request. At stage 0 the agent is
model-only; at stage 1+ it uses the gateway. Switching modes starts a fresh
agent, so the conversation history doesn't carry over.

- **Reconnect** re-reads the stage, then rebuilds the active role's session. In
  gateway mode it mints a fresh token, reconnects to the gateway, and re-lists
  tools; at stage 0 it rebuilds the model-only agent. Click it after every
  script. After `demo1.sh` this is what switches the chat from model-only to
  the gateway tools; after `reset_demo.sh` it switches back.
- **Show tools** lists the MCP tools the active role can currently see — handy
  after Demo 2 (new refund tools) and Demo 3 (the customer loses `start_refund`).

## The runner

- Each script prints a narration line and the next command, then waits for
  **Enter** before running it. Plumbing (loading `demo.env`, `cd`, capturing the
  gateway URL) runs quietly with a one-line label.
- `--auto` runs straight through without pausing — use it for rehearsal
  (`./demo1.sh --auto`).
- A script stops on the first failing command with a red `[FAILED]` line.

## Rehearsal loop

```bash
./reset_demo.sh    # tear down the AgentCore layer + Demo 2 state; keep stacks + Cognito
./demo1.sh --auto && ./demo2.sh --auto && ./demo3.sh --auto
```

`reset_demo.sh` removes the gateway, targets, and policies (and deploys that
removal), stops any parked refund executions, clears the approvals table, deletes
`toollayer/` and the generated `.cedar` copies, and drops `DEMO_STAGE` from
`demo.env`. It keeps the three SAM stacks (Cognito included) and `demo.env`, so
the next run starts in minutes.

When you're completely done, run the full teardown from the repo root instead:
`../cleanup.sh`.

## Gotchas

- **Tokens expire after about 60 minutes.** If you started the console well before
  your slot, click **Reconnect** right before presenting so each role has a fresh
  token.
- **The console reads `GATEWAY_URL` from `demo.env` ahead of the shell env, on
  purpose.** The gateway URL changes every time it's recreated (reset + a fresh
  `demo1.sh`), and a stale `GATEWAY_URL` exported in your shell would otherwise
  point the console at a gateway that no longer exists.
- The console uses port 8001 (`CONSOLE_PORT`) and `Demo2/approve_ui.py` uses port 8000 (`APPROVE_PORT`), so both can run at once. Both listen on `127.0.0.1` only.
