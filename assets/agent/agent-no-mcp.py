# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Local Strands agent — plain interactive chat with a Bedrock model, no tools.

This is the "before" version: just the LLM, no gateway and no MCP tools. It
shows the bare Strands + Bedrock loop before Demo 1 adds a tool-serving gateway.

    python agent-no-mcp.py

Type prompts at the `you>` prompt; responses stream token-by-token. Ctrl-C or
'exit' to quit. Requires AWS credentials with Bedrock access for the region below.
"""

import os

from strands import Agent
from strands.models import BedrockModel

REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = "us.anthropic.claude-sonnet-4-6"

model = BedrockModel(model_id=MODEL_ID, region_name=REGION, streaming=True)


def main():
    # One agent kept across turns → it remembers the conversation.
    # No callback_handler → Strands' default handler streams tokens to stdout.
    agent = Agent(model=model)

    print("Chat with the agent — type a prompt (Ctrl-C or 'exit' to quit).")
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
