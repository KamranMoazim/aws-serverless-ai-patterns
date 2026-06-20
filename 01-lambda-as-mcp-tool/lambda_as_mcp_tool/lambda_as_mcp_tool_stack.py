from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_cognito as cognito,
    aws_logs as logs,
    aws_bedrockagentcore as agentcore,
)
from constructs import Construct
from pathlib import Path

# ── Per-project namespace ─────────────────────────────────────────────────────
PROJECT_SLUG = "lambda-as-mcp-tool"


class LambdaAsMcpToolStack(Stack):
    """
    Minimal Lambda-as-MCP-tool pattern:

        MCP Client -> Cognito (auth)
                   -> AgentCore Gateway (MCP endpoint, Cognito-authed)
                   -> Lambda (tool)
                   -> DynamoDB

    No hand-written MCP server. The Gateway speaks MCP; the Lambda is the tool.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        slug = PROJECT_SLUG

        # ── DynamoDB: invoices ────────────────────────────────────────────────
        invoices_table = dynamodb.Table(
            self, "InvoicesTable",
            table_name=f"{slug}-invoices",          # was "Invoices" (account+region unique)
            partition_key=dynamodb.Attribute(
                name="invoice_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,  # demo: tear down cleanly
        )
        # GSI so the tool can query by customer instead of scanning.
        invoices_table.add_global_secondary_index(
            index_name="CustomerIndex",
            partition_key=dynamodb.Attribute(
                name="customer_name",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="invoice_date",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ── Lambda: the tool ──────────────────────────────────────────────────
        invoice_lambda_role = iam.Role(
            self, "InvoiceLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        invoices_table.grant_read_write_data(invoice_lambda_role)

        invoice_lambda = lambda_.Function(
            self, "InvoiceToolsFunction",
            function_name=f"{slug}-invoice-tools",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="invoice_lambda.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/invoice_lambda"),
            role=invoice_lambda_role,
            timeout=Duration.seconds(30),
            environment={
                "INVOICES_TABLE": invoices_table.table_name,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ── Cognito: inbound auth for the Gateway ─────────────────────────────
        user_pool = cognito.UserPool(
            self, "McpUserPool",
            user_pool_name=f"{slug}-users",
            self_sign_up_enabled=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            removal_policy=RemovalPolicy.DESTROY,
        )

        domain_prefix = f"{slug}-poc"
        user_pool.add_domain(
            "McpCognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )

        mcp_resource_server = user_pool.add_resource_server(
            "McpResourceServer",
            identifier="mcp-gateway",
            scopes=[
                cognito.ResourceServerScope(
                    scope_name="invoke",
                    scope_description="Invoke MCP gateway",
                ),
            ],
        )
        mcp_invoke_scope = cognito.OAuthScope.resource_server(
            mcp_resource_server,
            cognito.ResourceServerScope(
                scope_name="invoke",
                scope_description="Invoke MCP gateway",
            ),
        )

        # Public client - browser / Claude.ai (authorization code grant)
        public_client = user_pool.add_client(
            "McpPublicClient",
            user_pool_client_name="mcp-public",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                    mcp_invoke_scope,
                ],
                callback_urls=[
                    "https://claude.ai/api/mcp/auth_callback",
                    "http://localhost:6274/oauth/callback",  # MCP Inspector
                ],
            ),
        )

        # M2M client - easiest path for curl / server-to-server testing
        m2m_client = user_pool.add_client(
            "McpM2MClient",
            user_pool_client_name="mcp-m2m",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[mcp_invoke_scope],
            ),
        )

        # ── AgentCore Gateway: turns the Lambda into an MCP tool ──────────────
        schema_path = str(
            Path(__file__).parent.parent
            / "lambdas" / "invoice_lambda" / "tool_schema.json"
        )
        tool_schema = agentcore.ToolSchema.from_local_asset(schema_path)

        agentcore_gateway = agentcore.Gateway(
            self, "McpGateway",
            gateway_name=f"{slug}-gateway",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_cognito(
                user_pool=user_pool,
                allowed_clients=[m2m_client, public_client],
            ),
        )

        # This single call is the whole pattern: Lambda + schema => MCP tool.
        # Target name is scoped to THIS gateway, so it's safe to keep stable.
        # It also becomes the tool-name prefix: invoice-tools___<tool>.
        agentcore_gateway.add_lambda_target(
            "InvoiceToolsTarget",
            gateway_target_name="invoice-tools",
            description="Invoice analytics tools backed by DynamoDB",
            lambda_function=invoice_lambda,
            tool_schema=tool_schema,
        )

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "InvoicesTableName", value=invoices_table.table_name)
        CfnOutput(self, "InvoiceLambdaArn", value=invoice_lambda.function_arn)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "PublicClientId", value=public_client.user_pool_client_id)
        CfnOutput(self, "M2MClientId", value=m2m_client.user_pool_client_id)
        CfnOutput(self, "MCPGatewayURL", value=agentcore_gateway.gateway_url)
        CfnOutput(
            self, "TokenEndpoint",
            value=f"https://{domain_prefix}.auth.{self.region}.amazoncognito.com/oauth2/token",
        )
        CfnOutput(
            self, "CognitoDiscoveryUrl",
            value=f"https://cognito-idp.{self.region}.amazonaws.com"
                  f"/{user_pool.user_pool_id}/.well-known/openid-configuration",
        )