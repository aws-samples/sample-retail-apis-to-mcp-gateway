# Demo 3 — Scoped Agent Identity (Cedar policy)

The gateway already requires a JWT (Demo 1). Now we attach a **Cedar policy
engine** and allow-list what each identity may do. Only an identity whose token
carries the **`refund:create`** scope can start a refund — enforced at the gateway
boundary, before the backend runs.

**Aha:** the engine is **deny-by-default** — we explicitly `permit` what's allowed
(least privilege, fail-closed). The *same* call is allowed for the **customer
service representative** and denied for the **customer**, purely on the token's scope.

## Architecture
![Diagram: a customer service representative agent (scopes refunds/invoke and refunds/refund:create) and a customer agent (refunds/invoke only) each request a token from Amazon Cognito, send the MCP call with the Bearer JWT directly to the AgentCore Gateway, which validates the token against Cognito, and the Cedar policy engine then allows the representative to reach the refund AWS Lambda function while the customer has no matching permit, does not see start_refund, and is denied at the gateway before the backend.](./diagrams/after.png)

Both identities already carry a JWT (Demo 1). The policy engine reads the token's
`scope` and allows/denies each tool call.

## Prerequisites

- **Demos 1 & 2 completed** — `demo-gateway` with the orders + refunds targets,
  JWT auth on.
- The `customer` / `representative` Cognito identities, deployed by
  [`../setup.sh`](../setup.sh) as the `svs342-identity` stack
  (`demo-setup/identity/`). `mint_token.py` fetches each client's secret from
  Cognito at runtime; nothing secret is stored locally.

---

## Step A — Attach the policy engine + allow-list

Continue in the Demo 2 terminal, inside `toollayer/` — `demo.env` and
`GATEWAY_URL` are already loaded there.

> **Fresh terminal?** Start at the repo root and restore the environment first,
> so the `agentcore` CLI targets the same region as your gateway (`AWS_REGION`),
> not its `us-east-1` default:
>
> ```bash
> source .venv/bin/activate
> source demo.env
> cd toollayer
> export GATEWAY_URL=$(agentcore status --type gateway --json 2>/dev/null | jq -r '.. | .gatewayUrl? // empty' | head -1)
> ```

Attach a Cedar policy engine in ENFORCE mode. This makes the gateway
**deny-by-default** — with no policies, *no tools are even listed*:

```bash
agentcore add policy-engine --name gatewayauthz --attach-to-gateways demo-gateway --attach-mode ENFORCE
```

### Put the gateway ARN into generated policy copies (before adding policies)

Both Cedar policies pin your gateway's ARN (Cedar requires it when naming specific
tools). Each committed file has a placeholder line:

```
resource == AgentCore::Gateway::"<GATEWAY_ARN>"
```

**1. Capture your gateway ARN into the environment.**

```bash
export GATEWAY_ARN=$(agentcore status --type gateway --json 2>/dev/null | jq -r '.. | .gatewayArn? // empty' | head -1)
echo "$GATEWAY_ARN"         # sanity check — should print an arn:aws:... value
```

**2. Substitute it into generated copies of both policy files.** We write
`.generated.cedar` copies rather than editing the committed files in place, so
the committed `.cedar` files keep the `<GATEWAY_ARN>` placeholder (the generated
copies are gitignored, same pattern as `orders.generated.json`):

```bash
sed "s|<GATEWAY_ARN>|$GATEWAY_ARN|g" ../assets/policies/permit_read.cedar > ../assets/policies/permit_read.generated.cedar
sed "s|<GATEWAY_ARN>|$GATEWAY_ARN|g" ../assets/policies/permit_start_refund.cedar > ../assets/policies/permit_start_refund.generated.cedar
```

This only swaps the placeholder — the `permit` rules themselves are unchanged.

**3. Permit the read-only tools** (order lookups + refund status) for any identity:

```bash
agentcore add policy --engine gatewayauthz --name permit_read --source ../assets/policies/permit_read.generated.cedar
```

**4. Permit the `start_refund` tool** only when the token carries `refund:create`:

```bash
agentcore add policy --engine gatewayauthz --name permit_start_refund --source ../assets/policies/permit_start_refund.generated.cedar
```

Deploy:

```bash
agentcore deploy -y
```

Anything not permitted above (a new tool, a refund without the scope) is denied by default.

---

## Step B — Same call, two identities

Switch to the web console (http://localhost:8001). If it isn't running, start it
from the repo root with `source demo.env` loaded
(`python assets/console/console.py`). Each role has its own agent session and
token, and the role switcher sits above the chat. **Reconnect is per role**, so
click it once on each role after the policies deploy.

**Customer** has no `refund:create`. Select **Customer**, click **Reconnect**, then
**Show tools**: the customer doesn't even see `start_refund`. Type
*Process a refund for order 12345*. It can't start; the request is denied at the
boundary.

**Customer Service Representative** has `refund:create`. Select it, click
**Reconnect**, then **Show tools**: `start_refund` is listed. Type the *same*
prompt and the refund starts.

Same gateway, same prompt. Only the token's scope differed. (The refund it
starts parks at the manager gate from Demo 2; approve it with
`python Demo2/approve_ui.py` from the repo root, on http://localhost:8000, if you
want to see it finish.)


---

## When you are done

Run `./cleanup.sh` from the repo root to remove everything (see
[Getting started](../README.md#getting-started)).

## Notes / gotchas

- **Deny-by-default is gateway-wide** — every tool needs an explicit `permit`.
  There's deliberately no `permit_all`; that would be fail-open and defeat the
  point. A newly added tool is denied until you permit it.
- **Specific actions require a pinned gateway ARN** in `resource == AgentCore::Gateway::"<arn>"`
  (Cedar rule) — that's why you substitute the ARN into the generated `.cedar`
  copies.
- Policy names allow **letters, digits, underscores only** (no hyphens).
- The scope gates `start_refund` (initiating a refund) — approving is still the
  human's job in Demo 2. The `customer` can look up orders and check refund status.
- The Cedar policy reads the token's `scope` claim as a principal tag
  (`getTag("scope") like "*refund:create*"`).
