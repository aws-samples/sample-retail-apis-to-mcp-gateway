// Read-only tools any authenticated identity may use: order lookups and the
// refund status check. Everything not permitted is denied by default.
// Replace <GATEWAY_ARN> with your gateway's ARN (GatewayDemoGatewayArnOutput).
permit(
    principal is AgentCore::OAuthUser,
    action in [
        AgentCore::Action::"orders",
        AgentCore::Action::"refunds___check_refund_status"
    ],
    resource == AgentCore::Gateway::"<GATEWAY_ARN>"
);
