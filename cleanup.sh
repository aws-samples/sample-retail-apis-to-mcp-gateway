#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# cleanup.sh — tear down everything setup.sh (and the demos) created.
#
# Removes the AgentCore gateway/targets/policies, deletes the three SAM stacks
# (including the Cognito identity pool), and removes the generated artifacts. Every step
# tolerates already-gone resources: one missing resource must not abort the
# rest of the teardown.
#
# Lifecycle:  ./setup.sh  ->  Demo 1, 2, 3  ->  ./cleanup.sh
#
# NOTE: no `set -e` here on purpose — we use `|| true` so a resource that was
# never created (or already removed) does not stop the whole teardown.
set -uo pipefail

# Same region resolution as setup.sh. If you sourced demo.env, AWS_REGION is
# already set to the region the stacks were deployed in.
REGION="${AWS_REGION:-us-west-2}"

# Run from the script's own directory so all relative paths are stable.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> SVS342 cleanup starting (region: $REGION)"

# -------------------------------------------------------------------------
# 1) AgentCore teardown. Only possible if toollayer/ was created live (Demo 1).
#    Commands mirror the Cleanup sections of the three demo READMEs, run in
#    reverse dependency order (policies -> engine -> targets -> gateway).
# -------------------------------------------------------------------------
if [ -d toollayer ]; then
  echo "==> [1/3] Removing AgentCore policies, targets, and gateway..."
  (
    cd toollayer
    # Demo 3 — policies + policy engine
    agentcore remove policy --engine gatewayauthz --name permit_start_refund || true
    agentcore remove policy --engine gatewayauthz --name permit_read || true
    agentcore remove policy-engine --name gatewayauthz || true
    # Demo 2 — refund target
    agentcore remove gateway-target --name refunds -y || true
    # Demo 1 — orders target, its outbound credential, and the gateway
    agentcore remove gateway-target --name orders -y || true
    agentcore remove credential --name ordersKey -y || true
    agentcore remove gateway --name demo-gateway -y || true
    agentcore deploy -y || true
  )
  # The API-key credential provider lives in the AgentCore Identity token vault,
  # outside the CDK stack, so `deploy` doesn't always delete it. Remove it
  # directly; it holds the Orders API key.
  aws bedrock-agentcore-control delete-api-key-credential-provider \
    --name ordersKey --region "$REGION" >/dev/null 2>&1 || true
else
  echo "==> [1/3] toollayer/ not present — skipping AgentCore teardown."
fi

# -------------------------------------------------------------------------
# 2) Delete the SAM stacks (refund workflow, then orders API, then the
#    Cognito identity pool last — the gateway that trusted it is gone by now).
# -------------------------------------------------------------------------
echo "==> [2/3] Deleting SAM stacks..."
sam delete --stack-name svs342-refund-workflow --no-prompts --region "$REGION" || true
sam delete --stack-name svs342-orders-api --no-prompts --region "$REGION" || true
sam delete --stack-name svs342-identity --no-prompts --region "$REGION" || true
# Lambda log groups are created on first invoke, outside the stacks.
aws logs delete-log-group --log-group-name /aws/lambda/orders-api --region "$REGION" 2>/dev/null || true
aws logs delete-log-group --log-group-name /aws/lambda/refund-tools --region "$REGION" 2>/dev/null || true

# -------------------------------------------------------------------------
# 3) Remove generated artifacts + the Demo 1 project.
# -------------------------------------------------------------------------
echo "==> [3/3] Removing generated artifacts..."
rm -f demo.env || true
rm -f assets/specs/orders.generated.json || true
rm -f assets/policies/*.generated.cedar || true
rm -rf toollayer || true

echo ""
echo "============================================================"
echo "  Cleanup complete"
echo "============================================================"
