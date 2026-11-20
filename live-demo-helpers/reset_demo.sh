#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# live-demo-helpers/reset_demo.sh — rewind the account to the state RIGHT AFTER
# setup.sh, so you can re-run Demo 1, 2, and 3 from scratch without re-deploying
# the slow bits.
#
# This is the middle ground between setup.sh and cleanup.sh:
#
#   ../setup.sh    -> deploy the SAM stacks, Cognito included (slow, one-time)
#   demo1/2/3.sh   -> creates the AgentCore layer + leaves Demo 2 runtime state
#   ./reset_demo.sh -> tears down ONLY what the demos created  <-- you are here
#   ../cleanup.sh  -> full teardown (stacks + Cognito + everything)
#
# What reset tears down:
#   - the AgentCore layer (gateway/targets/policies deployed by agentcore)
#   - Demo 2 runtime state (parked Step Functions executions + approvals rows)
#   - locally generated live artifacts (toollayer/, *.generated.cedar)
#
# What reset KEEPS (the difference from cleanup.sh):
#   - the three SAM stacks (svs342-orders-api, svs342-refund-workflow,
#     svs342-identity — the Cognito pool, so client IDs stay the same)
#   - setup.sh's generated artifacts (demo.env, orders.generated.json)
#
# For a full teardown instead, run ../cleanup.sh.
#
# NOTE: no `set -e` here on purpose — we use `|| true` so a resource that was
# never created (or already removed) does not stop the whole reset.
set -uo pipefail

# Same region resolution as setup.sh / cleanup.sh. If you sourced demo.env,
# AWS_REGION is already set to the region the demos were deployed in.
REGION="${AWS_REGION:-us-west-2}"

# This script lives in live-demo-helpers/, but everything it resets (toollayer/,
# demo.env, assets/policies/) is at the repo root. Run from the repo root (the
# parent of this script's directory) so all relative paths below are stable.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "==> SVS342 reset starting (region: $REGION)"
echo "    Tears down the AgentCore layer + Demo 2 runtime state."
echo "    KEEPS the SAM stacks and Cognito — run ../cleanup.sh for full teardown."

# -------------------------------------------------------------------------
# 1) Tear down the AgentCore layer from AWS.
#
#    `agentcore remove ...` only edits the LOCAL project config — it does not
#    touch AWS. `agentcore deploy --yes` is what actually deletes the gateway/
#    targets/policies from the account (they are provisioned via CDK). So the
#    order MUST be: remove everything -> deploy -> (step 3) delete toollayer/.
#    Deleting toollayer/ before the deploy would orphan the deployed AWS
#    resources with no local project left to remove them.
# -------------------------------------------------------------------------
if [ -d toollayer ]; then
  echo "==> [1/3] Removing AgentCore policies, targets, credential, and gateway..."
  (
    cd toollayer
    # Empty the project: policies, policy engine, targets, credential, gateway.
    agentcore remove all --yes || true
    # This deploy is what actually deletes the resources from AWS; --yes
    # confirms removing the stack now that the project declares nothing. If it
    # fails (e.g. broken CDK state), the AgentCore resources may need manual
    # removal via the AWS console or `aws bedrock-agentcore-control`. We
    # intentionally continue (|| true) rather than aborting the rest of the reset.
    agentcore deploy --yes || true
  )
else
  echo "==> [1/3] toollayer/ not present — nothing deployed to remove (already reset). Skipping."
fi

# -------------------------------------------------------------------------
# 2) Clear Demo 2 runtime state so a re-run of Demo 2 starts clean.
#
#    Prefer the live names from the refund-workflow stack outputs; fall back to
#    discovery/literals if the describe-stacks call returns nothing. If the
#    stack isn't deployed at all, note it and skip — never fail.
# -------------------------------------------------------------------------
echo "==> [2/3] Clearing Demo 2 runtime state (Step Functions + approvals table)..."

SM_ARN=$(aws cloudformation describe-stacks \
  --stack-name svs342-refund-workflow --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='StateMachineArn'].OutputValue" \
  --output text 2>/dev/null || true)
APPROVALS_TABLE=$(aws cloudformation describe-stacks \
  --stack-name svs342-refund-workflow --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApprovalsTableName'].OutputValue" \
  --output text 2>/dev/null || true)

if [ -z "$SM_ARN" ] || [ "$SM_ARN" = "None" ]; then
  # Stack output empty — fall back to discovering the ARN by the known name.
  SM_ARN=$(aws stepfunctions list-state-machines --region "$REGION" \
    --query "stateMachines[?name=='refund-approval'].stateMachineArn" \
    --output text 2>/dev/null || true)
fi
if [ -z "$APPROVALS_TABLE" ] || [ "$APPROVALS_TABLE" = "None" ]; then
  # Fall back to the literal TableName from the template.
  APPROVALS_TABLE="refund-approvals"
fi

if { [ -z "$SM_ARN" ] || [ "$SM_ARN" = "None" ]; }; then
  echo "    refund-workflow stack not deployed (run ../setup.sh) — skipping Demo 2 reset."
else
  # Stop any in-flight / paused executions — a prior Demo 2 may have left a
  # workflow parked at the human-approval gate. Empty list = no-op.
  echo "    Stopping RUNNING executions of $SM_ARN ..."
  for arn in $(aws stepfunctions list-executions \
      --state-machine-arn "$SM_ARN" --status-filter RUNNING --region "$REGION" \
      --query 'executions[].executionArn' --output text 2>/dev/null); do
    aws stepfunctions stop-execution --execution-arn "$arn" --region "$REGION" || true
  done

  # Clear the approvals table rows (primary key: orderId / String). Empty
  # scan = no-op. The --key JSON is built as a bash double-quoted string with
  # the inner double quotes escaped.
  echo "    Clearing rows from DynamoDB table $APPROVALS_TABLE ..."
  for id in $(aws dynamodb scan --table-name "$APPROVALS_TABLE" --region "$REGION" \
      --projection-expression 'orderId' --query 'Items[].orderId.S' \
      --output text 2>/dev/null); do
    aws dynamodb delete-item --table-name "$APPROVALS_TABLE" --region "$REGION" \
      --key "{\"orderId\": {\"S\": \"$id\"}}" || true
  done
fi

# -------------------------------------------------------------------------
# 3) Remove local generated/live artifacts so Demo 1 starts from a clean
#    `agentcore create`. This runs AFTER the step-1 deploy so the AWS
#    resources are already gone before we drop the local project.
#
#    We deliberately do NOT remove demo.env or assets/specs/orders.generated.json
#    — those are setup.sh artifacts the demos still need. Preserving them is
#    the whole point of reset vs cleanup.
# -------------------------------------------------------------------------
echo "==> [3/3] Removing local live/generated artifacts..."
rm -rf toollayer || true
rm -f assets/policies/*.generated.cedar || true
# Back to DEMO_STAGE 0 so the staged console (console.sh) shows a plain chat again. Only the
# DEMO_STAGE line is removed — the rest of demo.env is setup.sh's and stays.
if [ -f demo.env ]; then
  grep -v "^export DEMO_STAGE=" demo.env > demo.env.tmp 2>/dev/null || true
  mv demo.env.tmp demo.env
fi

echo ""
echo "============================================================"
echo "  Reset complete"
echo "  The SAM stacks and Cognito are still deployed."
echo "  You can now re-run ./demo1.sh -> ./demo2.sh -> ./demo3.sh"
echo "  from scratch (or the Demo READMEs, starting at Demo 1)."
echo "  For a full teardown instead, run ../cleanup.sh"
echo "============================================================"
