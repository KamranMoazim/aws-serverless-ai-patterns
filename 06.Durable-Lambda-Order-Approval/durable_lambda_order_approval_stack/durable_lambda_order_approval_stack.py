from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_apigateway as apigw,
)
from constructs import Construct

PROJECT_SLUG = "durable-order-approval"


class DurableLambdaOrderApprovalStack(Stack):
    """
    POST /orders → starter ──async──▶ Durable order fn (invoked via ALIAS — qualified ARN)
                                        step: save PENDING → DynamoDB
                                        create_callback + step: SES approval email
                                        callback.result()  ← suspends (no compute)
    GET /approve → completer → SendDurableExecutionCallbackSuccess → fn resumes → finalize
    """

    def __init__(self, scope: Construct, construct_id: str, sender_email: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG
        STAGE = "prod"

        # ── DynamoDB: orders ──────────────────────────────────────────────────
        orders = dynamodb.Table(
            self, "Orders",
            partition_key=dynamodb.Attribute(
                name="order_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── API Gateway (created FIRST so we can reference its id, not its url) ─
        api = apigw.RestApi(
            self, "Api",
            rest_api_name=f"{slug}-api",
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.REGIONAL]),
            deploy_options=apigw.StageOptions(stage_name=STAGE),
        )
        # Build /approve URL from the API id + stage — NOT api.url. api.url references
        # the Stage, which depends on the methods → starter → alias → order_fn, so using
        # it in the durable fn's env would create a circular dependency.
        approve_url = (
            f"https://{api.rest_api_id}.execute-api.{self.region}.amazonaws.com"
            f"/{STAGE}/approve"
        )

        durable_execution_sdk_layer = lambda_.LayerVersion(
            self, 
            "durable_execution_sdk_layer", 
            layer_version_name="durable_execution_sdk_layer",
            code=lambda_.Code.from_asset("lambda_layers/durable-sdk/durable-sdk.zip")
        )


        # ── The DURABLE function (env baked in at construction, then aliased) ──
        order_fn = lambda_.Function(
            self, "OrderFn",
            function_name=f"{slug}-order-fn",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="order_fn.handler",
            code=lambda_.Code.from_asset("lambdas/order_fn"),
            timeout=Duration.seconds(60),
            memory_size=256,
            durable_config=lambda_.DurableConfig(
                execution_timeout=Duration.days(2),
                retention_period=Duration.days(7),
            ),
            layers=[durable_execution_sdk_layer],
            environment={
                "ORDERS_TABLE": orders.table_name,
                "SENDER_EMAIL": sender_email,
                "APPROVE_URL": approve_url,   # baked in — captured by the published version
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        orders.grant_read_write_data(order_fn)
        order_fn.add_to_role_policy(iam.PolicyStatement(actions=["ses:SendEmail"], resources=["*"]))

        # Durable functions must be invoked via a QUALIFIED ARN (version/alias).
        order_alias = order_fn.add_alias("live")

        # ── Starter: POST /orders → async invoke the ALIAS ────────────────────
        starter = lambda_.Function(
            self, "Starter",
            function_name=f"{slug}-starter",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="starter.handler",
            code=lambda_.Code.from_asset("lambdas/starter"),
            timeout=Duration.seconds(15),
            environment={"ORDER_FN_ARN": order_alias.function_arn},  # ...:order-fn:live
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        order_alias.grant_invoke(starter)

        # ── Completer: GET /approve → resume the durable execution ────────────
        completer = lambda_.Function(
            self, "Completer",
            function_name=f"{slug}-completer",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="completer.handler",
            code=lambda_.Code.from_asset("lambdas/completer"),
            timeout=Duration.seconds(15),
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        completer.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "lambda:SendDurableExecutionCallbackSuccess",
                "lambda:SendDurableExecutionCallbackFailure",
            ],
            resources=["*"],
        ))

        # ── Wire the routes (methods depend on the Lambdas, not vice versa) ───
        api.root.add_resource("orders").add_method("POST", apigw.LambdaIntegration(starter, proxy=True))
        api.root.add_resource("approve").add_method("GET", apigw.LambdaIntegration(completer, proxy=True))

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "OrdersUrl", value=f"{api.url}orders")
        CfnOutput(self, "ApproveUrl", value=f"{api.url}approve")
        CfnOutput(self, "OrdersTable", value=orders.table_name)
        CfnOutput(self, "OrderFnAliasArn", value=order_alias.function_arn)