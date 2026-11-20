# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
The manager approval — release the human-in-the-loop gate.

The workflow parked its task token in DynamoDB. We look it up by order ID and
call SendTaskSuccess, which resumes the paused execution.

    python approve_refund.py 12345
"""

import json
import os
import sys

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
APPROVALS_TABLE = "refund-approvals"

order_id = sys.argv[1] if len(sys.argv) > 1 else "12345"

ddb = boto3.client("dynamodb", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)

# Look up the task token the workflow saved for this order.
item = ddb.get_item(TableName=APPROVALS_TABLE, Key={"orderId": {"S": order_id}})["Item"]
task_token = item["taskToken"]["S"]

# Resume the workflow. `output` becomes the ManagerApproval state's result.
sfn.send_task_success(taskToken=task_token, output=json.dumps({"approved": True}))

print(f"Approved refund for order {order_id}. The workflow will now complete.")
