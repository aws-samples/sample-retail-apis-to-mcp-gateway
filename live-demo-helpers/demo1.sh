#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# live-demo-helpers/demo1.sh — walk the Demo 1 live flow as Enter-gated steps.
#
# Run it (don't source it): `./demo1.sh` pauses before each command so you can
# narrate; `./demo1.sh --auto` runs straight through without pausing. cd/export/
# source persist within this single process. Stops on the first failing command.
#
# This script creates toollayer/, the gateway, and the orders target, deploys,
# and writes GATEWAY_URL into demo.env. The agent chat itself happens in the
# web console (./console.sh, or assets/console/console.py): after this runs,
# click Reconnect.

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
# root, the same depth as Demo1/, so the ../ paths match the Demo READMEs.
cd "$SCRIPT_DIR"

banner "Demo 1 — Serverless API -> MCP Tool via OpenAPI"

quiet "loading demo.env (region, discovery URL, client IDs)" 'source ../demo.env'

step "This is the existing Orders API — a plain API Gateway -> Lambda service, protected by an API key."
run 'curl -s -o /dev/null -w "without a key: HTTP %{http_code}\n" "$API_BASE_URL/orders/12345"'
quiet "fetching the Orders API key value (kept in memory, never written to disk)" 'export ORDERS_API_KEY=$(aws apigateway get-api-key --api-key "$ORDERS_API_KEY_ID" --include-value --query value --output text)'
run 'curl -s -H "x-api-key: $ORDERS_API_KEY" "$API_BASE_URL/orders/12345" | jq .'

step "Create the toollayer project."
quiet "moving to the repo root" 'cd ..'
run 'agentcore create --project-name toollayer --no-agent'
quiet "entering the toollayer project" 'cd toollayer'

step "Add the gateway with JWT auth — discovery URL and client IDs come from the env, nothing to paste."
run 'agentcore add gateway --name demo-gateway --protocol-type MCP --authorizer-type CUSTOM_JWT --discovery-url "$DISCOVERY_URL" --allowed-clients "$ALLOWED_CLIENTS" --no-semantic-search'

step "Add the API key as an outbound credential — Gateway sends it to the Orders API in the x-api-key header."
run 'agentcore add credential --type api-key --name ordersKey --api-key "$ORDERS_API_KEY"'

step "Point the gateway at the generated OpenAPI spec — each operation becomes an MCP tool."
run 'agentcore add gateway-target --type open-api-schema --name orders --schema ../assets/specs/orders.generated.json --gateway demo-gateway --outbound-auth api-key --credential-name ordersKey'

step "Deploy — renders and applies CDK; the first gateway deploy takes ~2 min."
run 'agentcore deploy -y'

quiet "capturing the gateway URL" 'export GATEWAY_URL=$(agentcore status --type gateway --json 2>/dev/null | jq -r '"'"'.. | .gatewayUrl? // empty'"'"' | head -1)'
quiet "persisting GATEWAY_URL to demo.env (so Demo 2, Demo 3, and the console inherit it)" 'grep -v "^export GATEWAY_URL=" ../demo.env > ../demo.env.tmp 2>/dev/null; mv ../demo.env.tmp ../demo.env; echo "export GATEWAY_URL=\"$GATEWAY_URL\"" >> ../demo.env'
quiet "marking demo stage 1 in demo.env (the console reveals what this stage unlocks)" 'grep -v "^export DEMO_STAGE=" ../demo.env > ../demo.env.tmp 2>/dev/null; mv ../demo.env.tmp ../demo.env; echo "export DEMO_STAGE=1" >> ../demo.env'
