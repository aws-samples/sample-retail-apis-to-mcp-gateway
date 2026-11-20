#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# live-demo-helpers/demo2.sh — walk the Demo 2 live flow as Enter-gated steps.
#
# Run it (don't source it): `./demo2.sh` pauses before each command so you can
# narrate; `./demo2.sh --auto` runs straight through without pausing. cd/export/
# source persist within this single process. Stops on the first failing command.
#
# This script adds the refund Lambda target and deploys. The agent chat itself
# happens in the web console (./console.sh): after this runs, click
# Reconnect to pick up the new refund tools.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --auto) export DEMO_AUTO=1 ;;
    *) echo "usage: $0 [--auto]" >&2; exit 2 ;;
  esac
  shift
done

# shellcheck source=./demo_runner.sh
source "$SCRIPT_DIR/demo_runner.sh"

# Relative paths below start from live-demo-helpers/ — one level below the repo
# root, the same depth as Demo2/, so the ../ paths match the Demo READMEs.
cd "$SCRIPT_DIR"

banner "Demo 2 — Wrapping a Step Functions Workflow as a Tool"

quiet "entering toollayer and loading demo.env (REFUND_LAMBDA_ARN, region)" 'cd ../toollayer && source ../demo.env'

step "Add the refund Lambda as a target — its tool schema describes the two tools (a Lambda has no OpenAPI spec)."
run 'agentcore add gateway-target --type lambda-function-arn --name refunds --lambda-arn "$REFUND_LAMBDA_ARN" --tool-schema-file ../assets/specs/refund_tools.json --gateway demo-gateway'

step "Deploy."
run 'agentcore deploy -y'
quiet "marking demo stage 2 in demo.env (the console reveals what this stage unlocks)" 'grep -v "^export DEMO_STAGE=" ../demo.env > ../demo.env.tmp 2>/dev/null; mv ../demo.env.tmp ../demo.env; echo "export DEMO_STAGE=2" >> ../demo.env'
