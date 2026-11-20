// Only an identity whose token carries the refund:create scope may start a
// refund. No scope -> no matching permit -> denied by default (no forbid needed).
// Replace <GATEWAY_ARN> with your gateway's ARN (GatewayDemoGatewayArnOutput).
permit(
    principal is AgentCore::OAuthUser,
    action == AgentCore::Action::"refunds___start_refund",
    resource == AgentCore::Gateway::"<GATEWAY_ARN>"
)
when {
    principal.hasTag("scope") && principal.getTag("scope") like "*refund:create*"
};
