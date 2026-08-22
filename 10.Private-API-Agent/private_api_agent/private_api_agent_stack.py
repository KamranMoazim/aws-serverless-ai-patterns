from aws_cdk import (
    Stack, Duration, CfnOutput, CustomResource,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_ecr as ecr,
    aws_apigateway as apigw,
    aws_dsql as dsql,
    aws_vpclattice as vpclattice,
    aws_bedrockagentcore as agentcore,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    custom_resources as cr
)
from constructs import Construct

PROJECT_SLUG = "private-api-agent"


class PrivateApiAgentStack(Stack):
    """
    User → API Gateway (PRIVATE) → Lambda (in VPC)
      → AgentCore Runtime (in VPC)
         ├→ Bedrock (interface VPC endpoint - no internet)
         ├→ mock EHR in a SEPARATE VPC via VPC Lattice (no NAT, no peering)
         └⇄ Aurora DSQL (PrivateLink connection endpoint + IAM auth, no password)

    There is NO NAT gateway and NO internet gateway in either VPC.

    ── THREE-PASS DEPLOY ────────────────────────────────────────────────────
    Three values are assigned by AWS at create time and are not CloudFormation
    attributes, so each one has to be read back and fed in as context:

      pass 1  cdk deploy
              → run EhrDnsLookupCmd and DsqlVpceLookupCmd
      pass 2  cdk deploy -c ehr_host=... -c dsql_vpce_service_name=...
              → run DsqlPrivateHostHint
      pass 3  cdk deploy -c ehr_host=... -c dsql_vpce_service_name=... \
                         -c dsql_private_host=...

    Until pass 3 the agent has no route to the database and the schema
    bootstrap does not exist as a resource. In a no-egress VPC nothing works
    by accident.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG

        # ══ APP VPC ═══════════════════════════════════════════════════════════
        vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=0,
            ip_addresses=ec2.IpAddresses.cidr("10.10.0.0/16"),
            subnet_configuration=[
                ec2.SubnetConfiguration(name="private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)
            ],
        )
        private_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)

        endpoint_sg = ec2.SecurityGroup(
            self, "EndpointSg",
            vpc=vpc,
            allow_all_outbound=True,   # includes 169.254.171.0/24 (Lattice link-local)
            description="In-VPC clients to interface endpoints (80/443/5432) and VPC Lattice",
        )
        # Port 80 is for the Lattice hop, not for any endpoint. Omit it and the
        # agent's plain-HTTP call to the EHR is dropped on the way out.
        endpoint_sg.add_ingress_rule(endpoint_sg, ec2.Port.tcp(80), "HTTP to Lattice resources")
        endpoint_sg.add_ingress_rule(endpoint_sg, ec2.Port.tcp(443), "HTTPS in-VPC")
        endpoint_sg.add_ingress_rule(endpoint_sg, ec2.Port.tcp(5432), "Postgres to DSQL endpoint")

        # ── Interface endpoints: the ONLY way out. No internet required. ──────
        for name, svc in [
            ("Bedrock", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("Logs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
            ("Secrets", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("Sfn", ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS),
            ("EcrApi", ec2.InterfaceVpcEndpointAwsService.ECR),
            ("EcrDkr", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER),
            ("Lambda", ec2.InterfaceVpcEndpointAwsService.LAMBDA_),   # cr.Provider -> DbInit
            # Needed only if you use an SSM bastion to run the tests:
            ("Ssm", ec2.InterfaceVpcEndpointAwsService.SSM),
            ("SsmMessages", ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES),
            ("Ec2Messages", ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES),
            ("XRay", ec2.InterfaceVpcEndpointAwsService.XRAY),
            # DSQL_MANAGEMENT (com.amazonaws.<region>.dsql) is deliberately absent:
            # it is the control plane only and carries no Postgres traffic. The
            # data path is the per-cluster connection endpoint further down.
        ]:
            vpc.add_interface_endpoint(
                f"{name}Endpoint", service=svc, subnets=private_subnets, security_groups=[endpoint_sg]
            )

        vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
        vpc.add_interface_endpoint(
            "AgentCoreEndpoint",
            service=ec2.InterfaceVpcEndpointService(f"com.amazonaws.{self.region}.bedrock-agentcore", 443),
            subnets=private_subnets, security_groups=[endpoint_sg], private_dns_enabled=True,
        )

        # Kept out of the loop above: the endpoint object itself is referenced
        # twice below, by the API's endpoint configuration and its policy.
        apigw_endpoint = vpc.add_interface_endpoint(
            "ApiGwEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.APIGATEWAY,
            subnets=private_subnets,
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )

        # ══ EHR VPC (the "internal system" - separate VPC, also no egress) ════
        ehr_vpc = ec2.Vpc(
            self, "EhrVpc",
            max_azs=2,
            nat_gateways=0,
            ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
            subnet_configuration=[
                ec2.SubnetConfiguration(name="private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)
            ],
        )
        ehr_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)

        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=ehr_vpc, description="internal EHR ALB")
        rgw_sg = ec2.SecurityGroup(self, "RgwSg", vpc=ehr_vpc, description="Lattice resource gateway")
        # Inbound to the resource gateway comes from the Lattice data plane -
        # neither the app-VPC CIDR nor a SG you own, so it has to be opened by
        # CIDR. Without this rule the agent gets a silent timeout, not an error.
        rgw_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.all_tcp(), "lattice to rgw")
        alb_sg.add_ingress_rule(rgw_sg, ec2.Port.tcp(80), "from Lattice resource gateway")

        mock_ehr = lambda_.Function(
            self, "MockEhr",
            function_name=f"{slug}-mock-ehr",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="mock_ehr.handler",
            code=lambda_.Code.from_asset("lambdas/mock_ehr"),
            timeout=Duration.seconds(10),
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        alb = elbv2.ApplicationLoadBalancer(
            self, "EhrAlb",
            vpc=ehr_vpc,
            internet_facing=False,
            vpc_subnets=ehr_subnets,
            security_group=alb_sg,
        )
        # open=False: without it CDK adds an 0.0.0.0/0 ingress rule to alb_sg.
        alb\
            .add_listener("Http", port=80, protocol=elbv2.ApplicationProtocol.HTTP, open=False)\
            .add_targets("T", targets=[elbv2_targets.LambdaTarget(mock_ehr)])

        # ══ VPC LATTICE ═══════════════════════════════════════════════════════
        # The resource gateway lives in the TARGET VPC, next to the thing being
        # exposed - not in the app VPC.
        rgw = vpclattice.CfnResourceGateway(
            self, "ResourceGateway",
            name=f"{slug}-rgw",
            vpc_identifier=ehr_vpc.vpc_id,
            subnet_ids=[s.subnet_id for s in ehr_vpc.isolated_subnets],
            security_group_ids=[rgw_sg.security_group_id],
        )

        # resource_configuration_type="ARN" is supported for RDS only; pointing
        # it at an ALB fails at deploy time. SINGLE + DNS works, but unlike ARN
        # it does not inherit port/protocol - both must match the ALB listener.
        ehr = vpclattice.CfnResourceConfiguration(
            self, "EhrResource",
            name=f"{slug}-ehr",
            resource_configuration_type="SINGLE",
            resource_gateway_id=rgw.attr_id,
            port_ranges=["80"],
            protocol_type="TCP",
            resource_configuration_definition=(
                vpclattice.CfnResourceConfiguration.ResourceConfigurationDefinitionProperty(
                    dns_resource=vpclattice.CfnResourceConfiguration.DnsResourceProperty(
                        # Resolved by the gateway INSIDE the EHR VPC, where an
                        # internal ALB resolves to private IPs.
                        domain_name=alb.load_balancer_dns_name,
                        ip_address_type="IPV4",
                    ),
                )
            ),
        )
        ehr.node.add_dependency(alb)

        # auth_type does not gate the EHR hop: service network auth policies do
        # not apply to resource configurations. The VPC association is what
        # authorizes the call, so the agent uses plain urllib, not SigV4.
        network = vpclattice.CfnServiceNetwork(
            self, "ServiceNetwork",
            name=f"{slug}-net",
            auth_type="AWS_IAM"
        )

        vpclattice.CfnServiceNetworkVpcAssociation(
            self, "NetVpcAssoc",
            # This resource uses *_identifier; the one below uses *_id.
            service_network_identifier=network.attr_id,
            vpc_identifier=vpc.vpc_id,
            security_group_ids=[endpoint_sg.security_group_id],
        )

        net_res_assoc = vpclattice.CfnServiceNetworkResourceAssociation(
            self, "NetResAssoc",
            service_network_id=network.attr_id,
            resource_configuration_id=ehr.attr_id,
        )

        # Generated by Lattice at create time; see EhrDnsLookupCmd.
        ehr_host = self.node.try_get_context("ehr_host") or "PENDING-SET-VIA-CONTEXT"

        # ══ Aurora DSQL ═══════════════════════════════════════════════════════
        cluster = dsql.CfnCluster(self, "Dsql", deletion_protection_enabled=False)  # demo

        # Public cluster name. This is the name on the server certificate and
        # the name the IAM auth token is signed against - clients dial it as
        # host= even when the socket lands on a PrivateLink ENI.
        dsql_endpoint = f"{cluster.attr_identifier}.dsql.{self.region}.on.aws"

        # ── DSQL CONNECTION endpoint (the data path) ──────────────────────────
        # Per-cluster endpoint service, assigned at cluster-create time. Its own
        # API, not a field on the cluster:
        #   aws dsql get-vpc-endpoint-service-name --identifier <id>
        dsql_vpce_service_name = self.node.try_get_context("dsql_vpce_service_name")
        # The endpoint's private DNS name is likewise assigned by AWS and is not
        # derivable from the cluster id + service id. Read it back with
        # DsqlPrivateHostHint after the endpoint exists.
        dsql_private_host_ctx = self.node.try_get_context("dsql_private_host")
        dsql_private_host = dsql_private_host_ctx or dsql_endpoint

        if dsql_vpce_service_name:
            dsql_endpoint_res = ec2.InterfaceVpcEndpoint(
                self, "DsqlConnEndpoint",
                vpc=vpc,
                service=ec2.InterfaceVpcEndpointService(dsql_vpce_service_name, 5432),
                subnets=private_subnets,
                security_groups=[endpoint_sg],
                private_dns_enabled=True,
                open=False,
            )
            CfnOutput(self, "DsqlVpceId", value=dsql_endpoint_res.vpc_endpoint_id)
            CfnOutput(
                self, "DsqlPrivateHostHint",
                value=(
                    "aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "
                    f"{dsql_endpoint_res.vpc_endpoint_id} "
                    "--query 'VpcEndpoints[0].DnsEntries[*].DnsName' --output text"
                ),
            )

            # ── Schema bootstrap ──────────────────────────────────────────────
            # Gated on the real private hostname, i.e. pass 3. Created any
            # earlier it fires against a name that does not resolve yet.
            if dsql_private_host_ctx:
                # Built out-of-band by ./build_layer.sh so deploys need no Docker.
                psycopg_layer = lambda_.LayerVersion(
                    self, "PsycopgLayer",
                    layer_version_name=f"{slug}-psycopg",
                    code=lambda_.Code.from_asset("psycopg-layer.zip"),
                    compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
                    compatible_architectures=[lambda_.Architecture.X86_64],
                    description="psycopg for the DSQL schema bootstrap",
                )

                # The cluster ships with an empty `postgres` database; this
                # creates and seeds `patients`.
                db_init = lambda_.Function(
                    self, "DbInit",
                    function_name=f"{slug}-db-init",
                    runtime=lambda_.Runtime.PYTHON_3_13,
                    architecture=lambda_.Architecture.X86_64,   # must match the layer
                    handler="db_init.handler",
                    code=lambda_.Code.from_asset("lambdas/db_init"),
                    layers=[psycopg_layer],
                    timeout=Duration.seconds(120),
                    vpc=vpc,
                    vpc_subnets=private_subnets,
                    security_groups=[endpoint_sg],
                    environment={
                        "DSQL_ENDPOINT": dsql_endpoint,       # host=  : SNI, cert, token
                        "DSQL_HOST": dsql_private_host,       # hostaddr= source
                    },
                    log_retention=logs.RetentionDays.ONE_WEEK,
                )
                db_init.add_to_role_policy(
                    iam.PolicyStatement(
                        actions=["dsql:DbConnectAdmin"],
                        resources=[cluster.attr_resource_arn],
                    )
                )

                # The framework lambda is in the VPC too, so the no-egress claim
                # holds at deploy time. It reaches DbInit over the Lambda
                # interface endpoint and PUTs its response over the S3 gateway.
                provider = cr.Provider(
                    self, "DbInitProvider",
                    on_event_handler=db_init,
                    vpc=vpc,
                    vpc_subnets=private_subnets,
                    security_groups=[endpoint_sg],
                    log_retention=logs.RetentionDays.ONE_WEEK,
                )

                schema = CustomResource(
                    self, "DsqlSchema",
                    service_token=provider.service_token,
                    properties={"Version": "1"},  # bump to force a re-run
                )
                schema.node.add_dependency(cluster)
                # CREATE_COMPLETE on the endpoint does not mean its private DNS
                # has propagated - a first deploy can still lose that race.
                schema.node.add_dependency(dsql_endpoint_res)

        # ── AgentCore Runtime INSIDE the VPC ──────────────────────────────────
        repo = ecr.Repository.from_repository_name(self, "AgentRepo", f"{slug}-agent")
        runtime = agentcore.Runtime(
            self, "Agent",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_ecr_repository(repo, "latest"),
            # The one line that gives the agent an ENI in your subnets. The
            # default, using_public_network(), silently defeats the design.
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_vpc(
                self, vpc=vpc,
                vpc_subnets=private_subnets,
                security_groups=[endpoint_sg]
            ),
            environment_variables={
                # Two different names on purpose: the cert and the token belong
                # to the public one, the route belongs to the private one.
                "DSQL_ENDPOINT": dsql_endpoint,
                "DSQL_HOST": dsql_private_host,
                "EHR_ENDPOINT": f"http://{ehr_host}",
                "AGENT_OBSERVABILITY_ENABLED": "true",
            },
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"]
            )
        )
        # DSQL IAM auth - a token, not a password.
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dsql:DbConnectAdmin", "dsql:DbConnect"],
                resources=[cluster.attr_resource_arn]
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["vpc-lattice-svcs:Invoke"],
                resources=["*"]
            )
        )

        # ── Entry Lambda (also in the VPC) ────────────────────────────────────
        entry = lambda_.Function(
            self, "Entry",
            function_name=f"{slug}-entry",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="entry.handler",
            code=lambda_.Code.from_asset("lambdas/entry"),
            timeout=Duration.seconds(120),
            vpc=vpc,
            vpc_subnets=private_subnets,
            security_groups=[endpoint_sg],
            environment={"AGENT_RUNTIME_ARN": runtime.agent_runtime_arn},
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        runtime.grant_invoke(entry)

        # ── PRIVATE API Gateway: reachable only from inside the VPC ───────────
        api = apigw.RestApi(
            self, "Api",
            rest_api_name=f"{slug}-api",
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.PRIVATE],
                vpc_endpoints=[apigw_endpoint]
            ),
            # The endpoint type alone is not the boundary. Without this
            # condition any account with an execute-api endpoint could reach it.
            policy=iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    principals=[iam.AnyPrincipal()],
                    actions=["execute-api:Invoke"],
                    resources=["execute-api:/*"],
                    conditions={
                        "StringEquals": {
                            "aws:SourceVpce": apigw_endpoint.vpc_endpoint_id
                        }
                    },
                ),
            ]),
            deploy_options=apigw.StageOptions(stage_name="prod"),
        )
        api.root.add_resource("ask").add_method("POST", apigw.LambdaIntegration(entry, proxy=True))

        # ══ Outputs ═══════════════════════════════════════════════════════════
        CfnOutput(self, "PrivateApiUrl", value=f"{api.url}ask")
        CfnOutput(self, "DsqlEndpoint", value=dsql_endpoint)
        CfnOutput(self, "DsqlHostInUse", value=dsql_private_host)
        CfnOutput(self, "AgentRuntimeArn", value=runtime.agent_runtime_arn)
        CfnOutput(self, "ServiceNetworkId", value=network.attr_id)
        CfnOutput(self, "VpcId", value=vpc.vpc_id)
        CfnOutput(self, "EhrVpcId", value=ehr_vpc.vpc_id)
        CfnOutput(self, "EhrAlbDnsName", value=alb.load_balancer_dns_name)  # must NOT be reachable from app VPC
        CfnOutput(self, "EhrResourceConfigId", value=ehr.attr_id)
        CfnOutput(
            self, "DsqlVpceLookupCmd",
            value=(
                f"aws dsql get-vpc-endpoint-service-name --identifier {cluster.attr_identifier} "
                "--query serviceName --output text"
            ),
        )
        CfnOutput(
            self, "EhrDnsLookupCmd",
            value=(
                "aws vpc-lattice get-service-network-resource-association "
                f"--service-network-resource-association-identifier {net_res_assoc.attr_id} "
                "--query dnsEntry.domainName --output text"
            ),
        )