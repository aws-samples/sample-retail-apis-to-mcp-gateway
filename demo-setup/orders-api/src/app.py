# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Orders API — a plain serverless REST API (API Gateway -> Lambda).

This is the "existing API you already have" for Demo 1. Nothing here knows
about agents, MCP, or AI. It's an ordinary Lambda handler. The whole point of
the demo is that we DON'T change a line of it to make it agent-ready.
"""

import json

# ---------------------------------------------------------------------------
# Fake data store. In a real service this would be DynamoDB / RDS / etc.
# Kept in-memory so the demo has zero external dependencies.
# ---------------------------------------------------------------------------
ORDERS = {
    "12345": {
        "orderId": "12345",
        "customer": "Ada Lovelace",
        "item": "Mechanical Keyboard",
        "amount": 129.99,
        "currency": "USD",
        "status": "DELIVERED",
    },
    "67890": {
        "orderId": "67890",
        "customer": "Alan Turing",
        "item": "Noise-Cancelling Headphones",
        "amount": 249.00,
        "currency": "USD",
        "status": "SHIPPED",
    },
    "24680": {
        "orderId": "24680",
        "customer": "Grace Hopper",
        "item": "USB-C Hub",
        "amount": 54.99,
        "currency": "USD",
        "status": "PLACED",
    },
}


def _response(status_code, body):
    """Build an API Gateway (proxy integration) HTTP response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    """Route requests. Two operations: list all orders, or get one by ID."""

    # API Gateway REST API (proxy) puts the matched route in `resource`
    # and any path variables in `pathParameters`.
    resource = event.get("resource", "")
    path_params = event.get("pathParameters") or {}

    # GET /orders  -> return the full list
    if resource == "/orders":
        return _response(200, {"orders": list(ORDERS.values())})

    # GET /orders/{orderId}  -> return one order (or 404)
    if resource == "/orders/{orderId}":
        order = ORDERS.get(path_params.get("orderId"))
        if order is None:
            return _response(404, {"message": "Order not found"})
        return _response(200, order)

    return _response(404, {"message": "Not found"})
