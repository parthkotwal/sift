# Sift showcase infrastructure

This directory is the Terraform root for the short-lived Sift AWS showcase.
`.agents/AWS_DEPLOYMENT_PLAN.md` defines the architecture and
`.agents/AWS_DEPLOYMENT_STATUS.md` records implementation status and evidence.

## Safety boundary

- The state backend is intentionally local for this one-day environment.
- State and plan files are gitignored. Keep local state until `terraform destroy`
  succeeds and teardown has been verified.
- `terraform init`, `fmt`, and `validate` do not create AWS resources.
- Inspect a saved plan before any `terraform apply`.
- Never put credentials, Yelp data, derived artifacts, or private endpoints in
  Terraform variables or tracked files.

## Foundation commands

```bash
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform validate
```

Copy `terraform.tfvars.example` only when a non-default value is needed. The
local copy is ignored.

## Planned resource slices

Resources will be added in reviewable slices, not as one uninspectable apply:

1. network (implemented, not applied): VPC, internet gateway, two public and two
   private subnets, route tables, and narrowly scoped security groups;
2. storage and registry (implemented, not applied): private versioned S3
   artifact bucket and private ECR;
3. Redis and observability (implemented, not applied): single-node ElastiCache
   and CloudWatch log groups;
4. IAM: separate ECS execution and task roles with resource-scoped policies;
5. ECS and ALB: cluster, task definitions, one-task service, target group,
   listener, and one-off materialization command support;
6. GitHub OIDC: repository-scoped build/push/deploy role.

No NAT Gateway, autoscaling, Multi-AZ Redis, public Redis, EKS, CodePipeline, or
other service outside the deployment plan should appear in these slices.

## Network contract

The VPC defaults to `10.42.0.0/20`, divided into two public and two private
`/24` subnets across the first two available AZs. The private route table has no
internet route. Public subnets do not assign public IPs by default; the later
ECS service must opt its Fargate ENIs into public IP assignment explicitly.

Security-group relationships are deliberately directional:

- the public CIDR input reaches only ALB port 80;
- the ALB reaches only ECS task port 8000;
- ECS tasks reach public HTTPS endpoints and Redis port 6379;
- Redis accepts port 6379 only from the ECS task security group.

The public HTTPS egress is required because this no-NAT design uses public-IP
Fargate tasks to pull from ECR, download artifacts from S3, and publish logs.

## Artifact storage and registry contract

The S3 bucket name includes the AWS account ID and remains private through all
four Block Public Access settings, bucket-owner-enforced ownership, and a
TLS-only bucket policy. Versioning and AES-256 encryption are enabled. Lifecycle
rules abort incomplete multipart uploads after one day and expire only
noncurrent object versions after seven days; current immutable generation
objects do not expire automatically.

The ECR repository uses AES-256 encryption and immutable tags. Basic scanning is
enabled per repository on push. Registry-level scanning would alter behavior for
unrelated repositories in the account, so this intentionally uses the scoped
repository setting. The lifecycle expires untagged images after one day and
keeps the ten newest tagged images.

`allow_asset_deletion` defaults to `false`. A destroy therefore refuses to
empty a populated bucket or repository until Phase 8 explicitly chooses
deletion and changes that input. The artifacts and images are reproducible, but
their deletion should still be an intentional teardown decision.

## Rebuildable cache and task-log contract

ElastiCache uses one Valkey 8 node in the private subnet group. Valkey preserves
the Redis protocol used by Sift while avoiding a legacy Redis OSS choice. The
default `cache.t4g.micro` is a low-cost starting point, not a performance claim;
change `elasticache_node_type` if materialization or ALB measurements require
more memory or sustained CPU.

The replication group has exactly one node, no replica, no Multi-AZ failover,
and no snapshots. At-rest encryption and required in-transit encryption are
enabled. ECS must therefore pass a `rediss://` URL through `SIFT_REDIS_URL`.
Access remains network-authorized through the existing ECS-to-Redis security
group relationship; no cache password is stored in Terraform.

Separate API and materialization CloudWatch log groups use standard log storage
with three-day retention by default. They are intentionally deleted during
Terraform teardown after non-sensitive evidence has been preserved.
