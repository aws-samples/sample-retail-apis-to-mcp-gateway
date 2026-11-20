#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# setup.sh — deploy the pre-existing infrastructure for the SVS342 demos.
#
# Deploys the three SAM stacks (Orders API, refund workflow, and the shared
# Cognito identity pool), and writes a gitignored demo.env plus a templated
# assets/specs/orders.generated.json. Safe to re-run: CloudFormation updates each
# stack in place (an unchanged stack is a no-op), so the Cognito client IDs a
# gateway already trusts stay the same. After this runs you can follow the Demo 1,
# 2, and 3 READMEs, then tear everything down with ./cleanup.sh.
#
# Lifecycle:  ./setup.sh  ->  Demo 1, 2, 3  ->  ./cleanup.sh
set -euo pipefail

# Single source of truth for the region. Override by exporting AWS_REGION before
# running, or by editing this default. setup.sh exports it into demo.env so every
# downstream component (the agentcore CLI, the AWS CLI, boto3, the agent) inherits
# the same value — preventing the gateway from landing in another region by accident.
REGION="${AWS_REGION:-us-west-2}"

# Run from the script's own directory so all relative paths are stable.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> SVS342 setup starting (region: $REGION)"

# Step 5 converts assets/specs/orders.yaml to JSON with PyYAML. Check up front so
# a missing venv fails in a second, not after the stacks deploy.
if ! python -c "import yaml" 2>/dev/null; then
  echo "ERROR: PyYAML not found for 'python'. Activate the venv first:" >&2
  echo "  source .venv/bin/activate && uv pip install -r requirements.txt" >&2
  exit 1
fi

# -------------------------------------------------------------------------
# 1) Deploy the "existing" Orders API (API Gateway -> Lambda).
#    --no-fail-on-empty-changeset (on every deploy) lets a re-run succeed when
#    nothing changed instead of exiting non-zero.
# -------------------------------------------------------------------------
echo "==> [1/5] Building + deploying Orders API stack (svs342-orders-api)..."
(
  cd demo-setup/orders-api
  sam build
  sam deploy --no-confirm-changeset --no-fail-on-empty-changeset --resolve-s3 \
    --stack-name svs342-orders-api \
    --capabilities CAPABILITY_IAM \
    --region "$REGION"
)

# -------------------------------------------------------------------------
# 2) Deploy the refund workflow (Step Functions + DynamoDB + thin Lambda).
# -------------------------------------------------------------------------
echo "==> [2/5] Building + deploying refund workflow stack (svs342-refund-workflow)..."
(
  cd demo-setup/refund-workflow
  sam build
  sam deploy --no-confirm-changeset --no-fail-on-empty-changeset --resolve-s3 \
    --stack-name svs342-refund-workflow \
    --capabilities CAPABILITY_IAM \
    --region "$REGION"
)

# -------------------------------------------------------------------------
# 3) Deploy the shared Cognito identity pool the gateway's CUSTOM_JWT
#    authorizer trusts (user pool + domain, the `refunds` resource server, and
#    the customer / representative app clients). No IAM resources, so no
#    --capabilities. Client secrets stay in Cognito — mint_token.py reads them
#    at runtime; nothing secret is written to disk.
# -------------------------------------------------------------------------
echo "==> [3/5] Building + deploying identity stack (svs342-identity)..."
(
  cd demo-setup/identity
  sam build
  sam deploy --no-confirm-changeset --no-fail-on-empty-changeset --resolve-s3 \
    --stack-name svs342-identity \
    --region "$REGION"
)

# -------------------------------------------------------------------------
# 4) Capture the stack outputs the demos need.
#    Output keys come straight from each template's Outputs section:
#      orders-api      -> ApiBaseUrl, ApiKeyId (the ID only; the key value is
#                         fetched at demo time and never written to disk)
#      refund-workflow -> ToolLambdaArn
#      identity        -> DiscoveryUrl, AllowedClients (for `agentcore add gateway`)
# -------------------------------------------------------------------------
echo "==> [4/5] Reading stack outputs..."
API_BASE_URL=$(aws cloudformation describe-stacks \
  --stack-name svs342-orders-api --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiBaseUrl'].OutputValue" --output text)
ORDERS_API_KEY_ID=$(aws cloudformation describe-stacks \
  --stack-name svs342-orders-api --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiKeyId'].OutputValue" --output text)
REFUND_LAMBDA_ARN=$(aws cloudformation describe-stacks \
  --stack-name svs342-refund-workflow --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ToolLambdaArn'].OutputValue" --output text)
DISCOVERY_URL=$(aws cloudformation describe-stacks \
  --stack-name svs342-identity --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DiscoveryUrl'].OutputValue" --output text)
ALLOWED_CLIENTS=$(aws cloudformation describe-stacks \
  --stack-name svs342-identity --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AllowedClients'].OutputValue" --output text)
echo "    ApiBaseUrl     = $API_BASE_URL"
echo "    ApiKeyId       = $ORDERS_API_KEY_ID"
echo "    ToolLambdaArn  = $REFUND_LAMBDA_ARN"
echo "    DiscoveryUrl   = $DISCOVERY_URL"
echo "    AllowedClients = $ALLOWED_CLIENTS"

# -------------------------------------------------------------------------
# 5) Generate demo.env + the templated OpenAPI spec.
#    orders.generated.json is orders.yaml converted to JSON with servers[0].url set to
#    the real ApiBaseUrl. We template it with python to keep the JSON valid.
# -------------------------------------------------------------------------
echo "==> [5/5] Writing demo.env and assets/specs/orders.generated.json..."
API_BASE_URL="$API_BASE_URL" python - <<'PY'
import json, os, yaml
spec = yaml.safe_load(open("assets/specs/orders.yaml"))
spec["servers"][0]["url"] = os.environ["API_BASE_URL"]
json.dump(spec, open("assets/specs/orders.generated.json", "w"), indent=2)
PY

cat > demo.env <<EOF
# Generated by setup.sh — source this before running the demos (gitignored).
# AWS_REGION / AWS_DEFAULT_REGION pin the region for EVERYTHING that reads the
# environment: the agentcore CLI, the AWS CLI, boto3, and the agent.
# Source this BEFORE running 'agentcore create' so the gateway lands in \$AWS_REGION.
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"
export API_BASE_URL="$API_BASE_URL"
# ID of the Orders API key (not the secret value; Demo 1 fetches that at runtime).
export ORDERS_API_KEY_ID="$ORDERS_API_KEY_ID"
export REFUND_LAMBDA_ARN="$REFUND_LAMBDA_ARN"
export DISCOVERY_URL="$DISCOVERY_URL"
export ALLOWED_CLIENTS="$ALLOWED_CLIENTS"
EOF

echo ""
echo "============================================================"
echo "  Setup complete — ready to demo"
echo "  Next: follow Demo1/README.md -> Demo2 -> Demo3"
echo "  (run 'source demo.env' in each new terminal)."
echo "  Tear everything down afterward with ./cleanup.sh"
echo "============================================================"
