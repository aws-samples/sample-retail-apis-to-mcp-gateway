#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# live-demo-helpers/demo3.sh — walk the Demo 3 live flow as Enter-gated steps.
#
# Run it (don't source it): `./demo3.sh` pauses before each command so you can
# narrate; `./demo3.sh --auto` runs straight through without pausing. cd/export/
# source persist within this single process. Stops on the first failing command.
#
# This script attaches the Cedar policy engine and the two policies, then deploys.
# The customer-vs-representative comparison itself happens in the web console
# (./console.sh): after this runs, click Reconnect and switch
# roles — the customer is denied the refund, the representative is allowed. The
# ARN-templating step writes gitignored .generated.cedar copies; the committed
# .cedar files keep the placeholder.

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
# root, the same depth as Demo3/, so the ../ paths match the Demo READMEs.
cd "$SCRIPT_DIR"

banner "Demo 3 — Scoped Agent Identity (Cedar policy)"

quiet "entering toollayer and loading demo.env (region)" 'cd ../toollayer && source ../demo.env'

step "Attach a Cedar policy engine in ENFORCE mode — the gateway is now deny-by-default."
run 'agentcore add policy-engine --name gatewayauthz --attach-to-gateways demo-gateway --attach-mode enforce'

quiet "capturing the gateway ARN (Cedar pins it when naming specific tools)" 'export GATEWAY_ARN=$(agentcore status --json 2>/dev/null | jq -r '"'"'.resources[] | select(.resourceType == "gateway") | .arn'"'"')'
quiet "checking the gateway ARN was found" 'case "$GATEWAY_ARN" in arn:*) ;; *) echo "could not read the gateway ARN from agentcore status" >&2; false ;; esac'

quiet "substituting the gateway ARN into generated policy copies (committed .cedar keep the placeholder)" 'sed "s|<GATEWAY_ARN>|$GATEWAY_ARN|g" ../assets/policies/permit_read.cedar > ../assets/policies/permit_read.generated.cedar'
quiet "generating permit_start_refund.generated.cedar" 'sed "s|<GATEWAY_ARN>|$GATEWAY_ARN|g" ../assets/policies/permit_start_refund.cedar > ../assets/policies/permit_start_refund.generated.cedar'

step "Permit the read-only tools for any identity."
run 'agentcore add policy --engine gatewayauthz --name permit_read --statement file://../assets/policies/permit_read.generated.cedar'

step "Permit start_refund only when the token carries refund:create."
run 'agentcore add policy --engine gatewayauthz --name permit_start_refund --statement file://../assets/policies/permit_start_refund.generated.cedar'

step "Deploy."
run 'agentcore deploy'
quiet "marking demo stage 3 in demo.env (the console reveals what this stage unlocks)" 'grep -v "^export DEMO_STAGE=" ../demo.env > ../demo.env.tmp 2>/dev/null; mv ../demo.env.tmp ../demo.env; echo "export DEMO_STAGE=3" >> ../demo.env'
