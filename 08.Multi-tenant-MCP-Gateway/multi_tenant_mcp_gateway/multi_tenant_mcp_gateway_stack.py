from aws_cdk import (
    Stack, Duration, RemovalPolicy, CfnOutput, ArnFormat,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_cognito as cognito,
    aws_bedrockagentcore as agentcore
)
from constructs import Construct
from pathlib import Path

PROJECT_SLUG = "governed-mcp"


class MultiTenantMCPGatewayStack(Stack):
    """
    User → AgentCore Gateway
             ├→ [1] Request Interceptor  : enrich request with tenant/role from the JWT
             ├→ [2] Cedar Policy Engine  : allow/deny InvokeTool (default-deny)
             ├→ [3] Lambda target        : the actual tools
             └→ [4] Response Interceptor : filter ListTools + redact PII
                     → AgentCore Evaluations → CloudWatch

    Ordering matters: the Gateway runs the REQUEST interceptor BEFORE Cedar, so the
    interceptor enriches the context that Cedar then evaluates deterministically.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG
        root = Path(__file__).parent.parent

        # ── Cognito: tenant + role live in the token ──────────────────────────
        pool = cognito.UserPool(
            self, "Users",
            user_pool_name=f"{slug}-users",
            custom_attributes={"tenant_id": cognito.StringAttribute(mutable=True)},
            removal_policy=RemovalPolicy.DESTROY,
        )
        pool.add_domain("Domain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"{slug}-poc"))
        rs = pool.add_resource_server(
            "Rs", identifier="mcp-gateway",
            scopes=[cognito.ResourceServerScope(scope_name="invoke", scope_description="Invoke MCP gateway")]
        )
        scope_invoke = cognito.OAuthScope.resource_server(
            rs, cognito.ResourceServerScope(scope_name="invoke", scope_description="Invoke MCP gateway")
        )

        for g in ("admin", "user"):
            cognito.CfnUserPoolGroup(self, f"Group{g}", user_pool_id=pool.user_pool_id, group_name=g)

        public_client = pool.add_client(
            "Public", 
            generate_secret=True,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.PROFILE, scope_invoke],
                callback_urls=[
                    "https://claude.ai/api/mcp/auth_callback", 
                    "http://localhost:6274/oauth/callback"
                ]
            )
        )

        # ── The tools ─────────────────────────────────────────────────────────
        tools_fn = lambda_.Function(
            self, "Tools",
            function_name=f"{slug}-tools",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="tools.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/tools"),
            timeout=Duration.seconds(30),
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ── Interceptors ──────────────────────────────────────────────────────
        pre_token_fn = lambda_.Function(
            self, "PreToken",
            function_name=f"{slug}-pre-token",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="pre_token.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/pre_token"),
            timeout=Duration.seconds(5),
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        req_fn = lambda_.Function(
            self, "ReqInterceptor",
            function_name=f"{slug}-req-interceptor",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="req_interceptor.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/req_interceptor"),
            timeout=Duration.seconds(10),
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        resp_fn = lambda_.Function(
            self, "RespInterceptor",
            function_name=f"{slug}-resp-interceptor",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="resp_interceptor.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/resp_interceptor"),
            timeout=Duration.seconds(10),
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        pool.add_trigger(
            cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG,
            pre_token_fn,
            lambda_version=cognito.LambdaVersion.V2_0,
        )

        # ── Gateway with BOTH interceptors ────────────────────────────────────
        gateway = agentcore.Gateway(
            self, "Gateway",
            gateway_name=f"{slug}-gateway",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_cognito(user_pool=pool, allowed_clients=[public_client]),
            interceptor_configurations=[
                agentcore.LambdaInterceptor.for_request(req_fn, pass_request_headers=True),
                agentcore.LambdaInterceptor.for_response(resp_fn, pass_request_headers=True),
            ],
        )
        tools_target = gateway.add_lambda_target(
            "ToolsTarget",
            gateway_target_name="orders",
            description="Order + refund tools (tenant-scoped)",
            lambda_function=tools_fn,
            tool_schema=agentcore.ToolSchema.from_local_asset(str(root / "lambdas" / "tools" / "tool_schema.json")),
        )

        # ── Cedar policy engine: default-deny, forbid-wins ────────────────────
        engine = agentcore.CfnPolicyEngine(
            self, "PolicyEngine",
            name=f"{slug.replace('-', '_')}_engine",
            description="Cedar policies governing MCP tool invocation",
        )

        # ── Gateway role needs policy-engine access BEFORE the gateway is created ──
        gw_policy = iam.Policy(
            self, "GatewayPolicyEngineAccess",
            statements=[
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:GetPolicyEngine"],
                    resources=[engine.attr_policy_engine_arn],
                ),
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:AuthorizeAction",
                            "bedrock-agentcore:PartiallyAuthorizeActions"],
                    resources=[
                        engine.attr_policy_engine_arn,
                        # wildcard, NOT gateway.gateway_arn — that closes the cycle
                        Stack.of(self).format_arn(
                            service="bedrock-agentcore",
                            resource="gateway",
                            resource_name="*",
                            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                        ),
                    ],
                ),
            ],
        )
        gw_policy.attach_to_role(gateway.role)

        # ── Wire engine → gateway in ENFORCE mode (L1 escape hatch) ───────────
        # Without this the policies are never consulted.
        cfn_gw = gateway.node.default_child
        cfn_gw.policy_engine_configuration = agentcore.CfnGateway.GatewayPolicyEngineConfigurationProperty(
            arn=engine.attr_policy_engine_arn,
            mode="ENFORCE", #"LOG_ONLY",
        )
        cfn_gw.add_dependency(engine)
        cfn_gw.node.add_dependency(gw_policy)

        gw_arn = gateway.gateway_arn

        # Everyone may list their own orders.
        p1 = agentcore.CfnPolicy(
            self, "PermitListOrders",
            name="permit_list_orders",
            policy_engine_id=engine.attr_policy_engine_id,
            description="Anyone authenticated may list their tenant's orders",
            validation_mode="FAIL_ON_ANY_FINDINGS",     # use LOG_ONLY to dry-run a policy first
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(
                            statement=f"""permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"orders___list_orders",
  resource == AgentCore::Gateway::"{gw_arn}"
) when {{
  principal.hasTag("custom:tenant_id")
}};"""
                ),
            ),
        )

        # Refunds: admins only, and only under $500. The LLM cannot argue with this.
        p2 = agentcore.CfnPolicy(
            self, "PermitRefundAdminsUnder500",
            name="permit_refund_admins_under_500",
            policy_engine_id=engine.attr_policy_engine_id,
            description="Admins may refund, but only under $500",
            validation_mode="FAIL_ON_ANY_FINDINGS",
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(
                    statement = f"""permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"orders___issue_refund",
  resource == AgentCore::Gateway::"{gw_arn}"
) when {{
  principal.hasTag("cognito:groups") &&
  principal.getTag("cognito:groups") like "*admin*" &&
  context.input.amount < 500
}};"""
                ),
            ),
        )

        for p in (p1, p2):
            p.node.add_dependency(tools_target)

        # ── Evaluations → CloudWatch ──────────────────────────────────────────
        # Scores live agent traffic on correctness, tool selection, and safety.
        # Reads the Gateway's traces from CloudWatch (needs Transaction Search on).
        agentcore.OnlineEvaluationConfig(
            self, "Evals",
            online_evaluation_config_name=f"{slug.replace('-', '_')}_evals",
            description="Correctness, tool-selection and safety scoring for gateway traffic",
            data_source=agentcore.DataSourceConfig.from_cloud_watch_logs(
                log_group_names=["aws/spans"],          # AgentCore/ADOT span log group
                service_names=[f"{slug}-gateway"],
            ),
            evaluators=[
                agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.CORRECTNESS),
                agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.TOOL_SELECTION_ACCURACY),
                agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.HARMFULNESS),
            ],
        )

        CfnOutput(self, "GatewayUrl", value=gateway.gateway_url)
        CfnOutput(self, "GatewayArn", value=gw_arn)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "PublicClientId", value=public_client.user_pool_client_id)
        CfnOutput(self, "PolicyEngineId", value=engine.attr_policy_engine_id)