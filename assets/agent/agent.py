# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Local Strands agent — interactive REPL client for the CLI-provisioned gateway.

Run from inside `toollayer/` after `agentcore deploy` (same as Demo1/README.md):

    source ../demo.env      # GATEWAY_URL, saved at the end of Demo 1 Step A
    export GATEWAY_TOKEN=$(python ../assets/identity/mint_token.py representative)
    python ../assets/agent/agent.py

Type prompts at the `you>` prompt; responses stream token-by-token. Ctrl-C or
'exit' to quit.

The gateway uses JWT auth from Demo 1, so set GATEWAY_TOKEN to a Cognito JWT
(mint one with assets/identity/mint_token.py) — it's sent as a Bearer header.
"""

import os
import sys

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
GATEWAY_URL = os.environ["GATEWAY_URL"]           # saved to demo.env in Demo 1
TOKEN = os.environ.get("GATEWAY_TOKEN")           # Cognito JWT (required — auth)

headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else None
mcp_client = MCPClient(url=GATEWAY_URL, headers=headers)
model = BedrockModel(model_id=MODEL_ID, region_name=REGION, streaming=True)


class ReplyPrinter:
    """Strands callback handler: stream reply text to the terminal, readably.

    The default handler prints text deltas back to back, so text the model
    writes before and after a tool call runs together ("...the refund!Here
    are..."). This one prints a short tool line for each call and starts the
    text after it on a new paragraph. Strands calls it with the same event
    dicts Agent.stream_async() yields: text under "data", a tool call under
    event.contentBlockStart.start.toolUse (repeated input partials under
    "current_tool_use", deduped by toolUseId), and result=... once per turn.
    """

    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.dim = self.out.isatty()
        self._reset()

    def _reset(self):
        self.seen = set()          # toolUseIds already announced this turn
        self.tail = ""             # last two characters written
        self.pending_break = False

    def _write(self, text):
        self.out.write(text)
        self.out.flush()
        self.tail = (self.tail + text)[-2:]

    def _blank_line(self):
        """Top the output up so the next write starts after one blank line."""
        if self.tail:
            have = len(self.tail) - len(self.tail.rstrip("\n"))
            self._write("\n" * (2 - have))

    def __call__(self, **kwargs):
        if "result" in kwargs:     # end of turn
            if self.tail and not self.tail.endswith("\n"):
                self._write("\n")
            self._reset()
            return
        start = (kwargs.get("event") or {}).get("contentBlockStart", {}).get("start", {})
        tool = start.get("toolUse") or kwargs.get("current_tool_use")
        if isinstance(tool, dict) and tool.get("name"):
            key = tool.get("toolUseId") or tool["name"]
            if key not in self.seen:
                self.seen.add(key)
                self._blank_line()
                line = f"→ calling {tool['name']}"
                self._write(f"\033[2m{line}\033[0m\n" if self.dim else line + "\n")
                self.pending_break = True
            return
        text = kwargs.get("data")
        if isinstance(text, str) and text:
            if self.pending_break:
                text = text.lstrip("\n")
                if not text:
                    return
                self._blank_line()
                self.pending_break = False
            self._write(text)


def main():
    with mcp_client:
        tools = mcp_client.list_tools_sync()
        print("Discovered tools:", [t.tool_name for t in tools])

        agent = Agent(model=model, tools=tools, callback_handler=ReplyPrinter())

        print("\nChat with the agent — type a prompt (Ctrl-C or 'exit' to quit).")
        while True:
            try:
                prompt = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if prompt.lower() in ("exit", "quit"):
                return
            if prompt:
                agent(prompt)          # streams the response to the terminal


if __name__ == "__main__":
    main()
