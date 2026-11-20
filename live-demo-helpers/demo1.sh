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
run 'agentcore create --name toollayer'
quiet "entering the toollayer project" 'cd toollayer'

step "Add the gateway with JWT auth — discovery URL and client IDs come from the env, nothing to paste."
quiet "building the JWT authorizer config from DISCOVERY_URL and ALLOWED_CLIENTS" 'export JWT_AUTHORIZER=$(jq -nc --arg url "$DISCOVERY_URL" --arg clients "$ALLOWED_CLIENTS" '"'"'{customJwtAuthorizer: {discoveryUrl: $url, allowedClients: ($clients | split(","))}}'"'"')'
run 'agentcore add gateway --name demo-gateway --protocol-type MCP --authorizer-type CUSTOM_JWT --authorizer-configuration "$JWT_AUTHORIZER"'

step "Add the API key as an outbound credential — Gateway sends it to the Orders API in the x-api-key header. The key is piped in, so it never appears on the command line."
run 'printf "%s" "$ORDERS_API_KEY" | agentcore add credentials api-key --name ordersKey --api-key -'

step "Point the gateway at the generated OpenAPI spec — each operation becomes an MCP tool."
run 'agentcore add gateway-target --gateway demo-gateway --target-configuration file://../assets/targets/orders.json'

step "Deploy — renders and applies CDK; the first gateway deploy takes ~2 min."
run 'agentcore deploy'

quiet "capturing the gateway ARN" 'export GATEWAY_ARN=$(agentcore status --json 2>/dev/null | jq -r '"'"'.resources[] | select(.resourceType == "gateway") | .arn'"'"')'
quiet "looking up the gateway URL (the gateway ID is the last part of the ARN)" 'export GATEWAY_URL=$(agentcore gateway get --id "${GATEWAY_ARN##*/}" --json 2>/dev/null | jq -r .gatewayUrl)'
quiet "checking the gateway URL was found" 'case "$GATEWAY_URL" in https://*) ;; *) echo "could not read the gateway URL from agentcore" >&2; false ;; esac'
quiet "persisting GATEWAY_URL to demo.env (so Demo 2, Demo 3, and the console inherit it)" 'grep -v "^export GATEWAY_URL=" ../demo.env > ../demo.env.tmp 2>/dev/null; mv ../demo.env.tmp ../demo.env; echo "export GATEWAY_URL=\"$GATEWAY_URL\"" >> ../demo.env'
quiet "marking demo stage 1 in demo.env (the console reveals what this stage unlocks)" 'grep -v "^export DEMO_STAGE=" ../demo.env > ../demo.env.tmp 2>/dev/null; mv ../demo.env.tmp ../demo.env; echo "export DEMO_STAGE=1" >> ../demo.env'
