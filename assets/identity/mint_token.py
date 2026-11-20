# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Mint a Cognito access token for one identity (client-credentials OAuth2).

    export GATEWAY_TOKEN=$(python mint_token.py representative)   # or: customer

The identities live in the svs342-identity stack (demo-setup/identity/, deployed
by setup.sh). The client ID and token endpoint come from the stack outputs; the
client secret is read from Cognito at runtime and never written to disk.
Prints only the access token, so it works inside $(...).
"""

import base64
import json
import os
import sys
import urllib.parse

import boto3
import urllib3  # ships with botocore; no extra dependency
from botocore.exceptions import ClientError

STACK_NAME = "svs342-identity"

# Identity name -> the stack output holding its app client ID.
IDENTITIES = {
    "customer": "CustomerClientId",
    "representative": "RepresentativeClientId",
}


def _stack_outputs(region):
    """Return the identity stack's outputs as a dict, or raise a clear error."""
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        stack = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    except ClientError as e:
        if "does not exist" in str(e):
            raise RuntimeError(
                f"Identity stack {STACK_NAME} not found in {region}. "
                "Run ./setup.sh from the repo root first."
            ) from e
        raise
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    if not all(k in outputs for k in ("UserPoolId", "TokenEndpoint", *IDENTITIES.values())):
        raise RuntimeError(
            f"Identity stack {STACK_NAME} is {stack['StackStatus']} and missing "
            "outputs. Re-run ./setup.sh from the repo root."
        )
    return outputs


def mint(which="representative"):
    """Return a fresh access token for identity `which` ('customer'|'representative').

    Raises ValueError for an unknown identity and RuntimeError if the identity
    stack isn't deployed (never SystemExit, so callers like the web console can
    show the message instead of exiting).
    """
    if which not in IDENTITIES:
        raise ValueError(
            f"Unknown identity '{which}'. Choose one of: {', '.join(IDENTITIES)}."
        )
    region = os.environ.get("AWS_REGION", "us-west-2")
    outputs = _stack_outputs(region)

    # The secret (and the scopes this client may request) come from Cognito
    # itself, so nothing secret is stored locally.
    client = boto3.client("cognito-idp", region_name=region).describe_user_pool_client(
        UserPoolId=outputs["UserPoolId"], ClientId=outputs[IDENTITIES[which]],
    )["UserPoolClient"]

    # The endpoint comes from our own stack output, but validate it anyway so
    # we only ever post the client secret to a Cognito token endpoint.
    token_endpoint = outputs["TokenEndpoint"]
    parsed = urllib.parse.urlsplit(token_endpoint)
    if (
        parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".amazoncognito.com")
        or parsed.path != "/oauth2/token"
    ):
        raise RuntimeError(
            f"TokenEndpoint {token_endpoint!r} is not a Cognito https://<domain>"
            ".amazoncognito.com/oauth2/token URL"
        )

    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": " ".join(client["AllowedOAuthScopes"]),
    }).encode()
    basic = base64.b64encode(f"{client['ClientId']}:{client['ClientSecret']}".encode()).decode()
    # urllib3 only speaks HTTP(S) (unlike urlopen, which also accepts file://
    # and other schemes) and verifies TLS certificates by default.
    try:
        resp = urllib3.PoolManager().request(  # PoolManager works on urllib3 1.26 and 2.x
            "POST", token_endpoint, body=body, timeout=10, retries=False,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except urllib3.exceptions.HTTPError as e:
        raise RuntimeError(f"Could not reach Cognito token endpoint {parsed.hostname}: {e}") from e
    if resp.status != 200:
        raise RuntimeError(f"Cognito token request failed: HTTP {resp.status} {resp.data[:200]!r}")
    return json.loads(resp.data)["access_token"]


if __name__ == "__main__":
    try:
        print(mint(sys.argv[1] if len(sys.argv) > 1 else "representative"))
    except (ValueError, RuntimeError) as e:
        raise SystemExit(str(e))   # message to stderr, nothing on stdout
