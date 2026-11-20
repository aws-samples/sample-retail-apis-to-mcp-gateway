# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
The thin Lambda behind Demo 2's two tools.

Step Functions is NOT an AgentCore Gateway target type, but a Lambda is. This
small function is what makes the whole workflow callable by an agent:

  - start_refund(orderId)         -> kicks off the workflow, returns immediately
  - check_refund_status(orderId)  -> looks up the order's refund status in DynamoDB

Both tools take ONLY the orderId. The workflow keeps each order's status in the
approvals table (PENDING_APPROVAL -> REFUNDED), so checking status is
a simple lookup by orderId — the agent never tracks an execution ARN.

Gateway tells us WHICH tool was called via the Lambda context (not the event);
the tool's arguments arrive as the event itself.
"""

import json
import os
import re

import boto3

sfn = boto3.client("stepfunctions")
ddb = boto3.client("dynamodb")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
APPROVALS_TABLE = os.environ["APPROVALS_TABLE"]

# Order IDs arrive as agent tool arguments, so treat them as untrusted input:
# they're stored in DynamoDB and shown on the manager's approval page. Accept
# only short IDs of letters, digits, and hyphens (e.g. "12345").
ORDER_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")


def _order_id(args):
    """Return a validated orderId from the tool arguments, or raise ValueError."""
    order_id = args.get("orderId") if isinstance(args, dict) else None
    if isinstance(order_id, int) and not isinstance(order_id, bool):
        order_id = str(order_id)   # a model may send 12345 instead of "12345"
    if not isinstance(order_id, str) or not ORDER_ID_RE.fullmatch(order_id):
        raise ValueError("orderId must be 1-64 letters, digits, or hyphens (e.g. 12345)")
    return order_id


def start_refund(args):
    """Start the workflow for an order (returns before the human approves)."""
    order_id = _order_id(args)
    sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        input=json.dumps({"orderId": order_id}),
    )
    return {
        "orderId": order_id,
        "status": "PENDING_APPROVAL",
        "message": "Refund workflow started. A manager must approve before it completes.",
    }


def check_refund_status(args):
    """Look up the order's refund status in DynamoDB. The workflow keeps it
    current: PENDING_APPROVAL -> (human approves, refund processed) REFUNDED.
    Returns NO_REFUND_FOUND if the order has no refund."""
    order_id = _order_id(args)
    item = ddb.get_item(
        TableName=APPROVALS_TABLE,
        Key={"orderId": {"S": order_id}},
    ).get("Item")
    if not item:
        return {"orderId": order_id, "status": "NO_REFUND_FOUND"}
    return {"orderId": order_id, "status": item["status"]["S"]}


# Map tool name -> function.
TOOLS = {
    "start_refund": start_refund,
    "check_refund_status": check_refund_status,
}


def handler(event, context):
    # Gateway sends the tool name (prefixed with "<target>___") on the context.
    full_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = full_name.split("___", 1)[-1]     # strip the "refunds___" prefix

    try:
        return TOOLS[tool_name](event)            # arguments are the event itself
    except ValueError as e:
        # Bad input: tell the agent what's wrong instead of failing the call.
        return {"error": str(e)}
