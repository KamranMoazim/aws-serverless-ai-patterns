from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    CfnResource,
    aws_s3 as s3,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_apigateway as apigw,
)
from constructs import Construct

PROJECT_SLUG = "s3-files-demo"


class S3LambdaMountedFilesStack(Stack):
    """
    Client -> API Gateway -> Lambda  <->  S3 bucket (mounted as a filesystem via S3 Files)

    The Lambda mounts the bucket at /mnt/s3 and does plain open()/read()/write() —
    no boto3 transfer code. S3 Files (EFS-backed) handles the NFS layer and syncs
    to S3. Requires a VPC, mount targets, an access point, and bucket versioning.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        slug = PROJECT_SLUG
        mount_path = "/mnt/s3"

        # ── S3 bucket (versioning is REQUIRED by S3 Files) ────────────────────
        bucket = s3.Bucket(
            self, "DataBucket",
            bucket_name=f"s3-mounted-data-bucket-{slug}",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,   # demo: clean teardown
            auto_delete_objects=True,               # runs after the file system is deleted
        )

        # ── VPC: private isolated subnets, no NAT ─────────────────────────────
        # The Lambda only needs NFS (2049) to the mount target inside the VPC.
        # It needs no internet, so we skip the NAT gateway (the costly part).
        vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        # One self-referencing SG shared by the mount targets and the Lambda:
        # members can talk NFS (2049) to each other, nothing else inbound.
        nfs_sg = ec2.SecurityGroup(
            self, "NfsSg",
            vpc=vpc,
            allow_all_outbound=True,
            description="S3 Files NFS access (port 2049)",
        )
        nfs_sg.add_ingress_rule(nfs_sg, ec2.Port.tcp(2049), "NFS between SG members")

        # ── IAM role that S3 Files assumes to read/write the bucket ───────────
        # Note the principal: S3 Files reuses the EFS service principal.
        service_role = iam.Role(
            self, "S3FilesServiceRole",
            assumed_by=iam.ServicePrincipal(
                "elasticfilesystem.amazonaws.com", # it is s3 files principal
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:s3files:{self.region}:{self.account}:file-system/*"
                    },
                },
            ),
        )
        bucket.grant_read_write(service_role)
        service_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "s3:GetBucketLocation",
                "s3:GetBucketVersioning",
                "s3:GetObjectVersion",
                "s3:ListBucketMultipartUploads",
                "s3:ListMultipartUploadParts",
                "s3:AbortMultipartUpload",
            ],
            resources=[bucket.bucket_arn, bucket.arn_for_objects("*")],
        ))

        # ── S3 Files file system (L1 — no CDK L2 module exists yet) ───────────
        file_system = CfnResource(
            self, "FileSystem",
            type="AWS::S3Files::FileSystem",
            properties={
                "Bucket": bucket.bucket_arn,
                "RoleArn": service_role.role_arn,
                "AcceptBucketWarning": True,   # required: acknowledges write coordination
            },
        )
        file_system_id = file_system.get_att("FileSystemId").to_string()

        # ── Mount targets: one per isolated subnet/AZ the Lambda runs in ──────
        mount_targets = []
        subnets = vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
        ).subnets
        for i, subnet in enumerate(subnets):
            mt = CfnResource(
                self, f"MountTarget{i}",
                type="AWS::S3Files::MountTarget",
                properties={
                    "FileSystemId": file_system_id,
                    "SubnetId": subnet.subnet_id,
                    "SecurityGroups": [nfs_sg.security_group_id],
                },
            )
            mt.add_dependency(file_system)
            mount_targets.append(mt)

        # ── Access point: POSIX identity + a writable root dir for Lambda ─────
        # Without CreationPermissions the root is owned by root and the Lambda
        # (UID 1000) can't create files.
        access_point = CfnResource(
            self, "AccessPoint",
            type="AWS::S3Files::AccessPoint",
            properties={
                "FileSystemId": file_system_id,
                "PosixUser": {"Uid": "1000", "Gid": "1000"},
                "RootDirectory": {
                    "Path": "/lambda",
                    "CreationPermissions": {
                        "OwnerUid": "1000",
                        "OwnerGid": "1000",
                        "Permissions": "755",
                    },
                },
            },
        )
        access_point.add_dependency(file_system)
        access_point_arn = access_point.get_att("AccessPointArn").to_string()

        # ── Lambda: mounts the bucket at /mnt/s3 ──────────────────────────────
        fn = lambda_.Function(
            self, "FileApi",
            function_name=f"{slug}-file-api",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="file_api.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/file_api"),
            memory_size=512,                 # 512MB+ recommended for direct S3 reads
            timeout=Duration.seconds(30),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            security_groups=[nfs_sg],
            environment={"MOUNT_PATH": mount_path},
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        # Permissions to mount/write the S3 Files file system + direct S3 reads.
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3files:ClientMount", "s3files:ClientWrite"],
            resources=["*"],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[bucket.arn_for_objects("*")],
        ))

        # Attach the S3 Files mount. The L2 `filesystem=` prop is EFS-typed, so we
        # set FileSystemConfigs directly with the S3 Files access-point ARN.
        cfn_fn = fn.node.default_child
        cfn_fn.add_property_override(
            "FileSystemConfigs",
            [{"Arn": access_point_arn, "LocalMountPath": mount_path}],
        )
        # The function can only mount once the mount targets + access point exist.
        for mt in mount_targets:
            cfn_fn.add_dependency(mt)
        cfn_fn.add_dependency(access_point)

        # ── API Gateway (REST) → Lambda proxy ─────────────────────────────────
        api = apigw.LambdaRestApi(
            self, "Api",
            handler=fn,
            proxy=True,
            rest_api_name=f"{slug}-api",
        )

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "FileSystemId", value=file_system_id)