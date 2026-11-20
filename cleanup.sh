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
#    `agentcore remove all` empties the local project (policies, policy engine,
#    targets, credential, gateway); `agentcore deploy --yes` then deletes them
#    from AWS (--yes confirms removing the stack).
# -------------------------------------------------------------------------
if [ -d toollayer ]; then
  echo "==> [1/3] Removing AgentCore policies, targets, credential, and gateway..."
  (
    cd toollayer
    agentcore remove all --yes || true
    agentcore deploy --yes || true
  )
  # The API-key credential provider lives in the AgentCore Identity token vault,
  # outside the CDK stack, so make sure it is gone; it holds the Orders API key.
  # The CLI names it <project>_<target>_<credential>.
  aws bedrock-agentcore-control delete-api-key-credential-provider \
    --name toollayer_default_ordersKey --region "$REGION" >/dev/null 2>&1 || true
else
  echo "==> [1/3] toollayer/ not present — skipping AgentCore teardown."
fi

# -------------------------------------------------------------------------
# 2) Delete the SAM stacks (refund workflow, then orders API, then the
#    Cognito identity pool last — the gateway that trusted it is gone by now).
# -------------------------------------------------------------------------
echo "==> [2/3] Deleting SAM stacks..."
# Stop refunds still parked at the approval gate (Demo 2 and Demo 3 can leave
# one). Step Functions keeps a state machine in DELETING until its running
# executions end, so a parked refund would stall the stack delete for up to an
# hour (the workflow's timeout).
SM_ARN=$(aws stepfunctions list-state-machines --region "$REGION" \
  --query "stateMachines[?name=='refund-approval'].stateMachineArn" \
  --output text 2>/dev/null || true)
if [ -n "$SM_ARN" ] && [ "$SM_ARN" != "None" ]; then
  for arn in $(aws stepfunctions list-executions \
      --state-machine-arn "$SM_ARN" --status-filter RUNNING --region "$REGION" \
      --query 'executions[].executionArn' --output text 2>/dev/null); do
    aws stepfunctions stop-execution --execution-arn "$arn" --region "$REGION" >/dev/null || true
  done
fi
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
