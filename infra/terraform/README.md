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
4. IAM (implemented, not applied): separate ECS execution and task roles with
   resource-scoped policies;
5. ALB (implemented, not applied) and ECS: public load balancer, target group,
   listener, cluster, task definitions, one-task service, and one-off
   materialization command support;
6. GitHub OIDC: repository-scoped build/push/deploy role.

No NAT Gateway, autoscaling, Multi-AZ Redis, public Redis, EKS, CodePipeline, or
other service outside the deployment plan should appear in these slices.

## Network contract

The VPC defaults to `10.42.0.0/20`, divided into two public and two private
`/24` subnets across the first two available AZs. The private route table has no
internet route. Public subnets do not assign public IPs by default; the later
ECS service must opt its Fargate ENIs into public IP assignment explicitly.

Security-group relationships are deliberately directional. `alb_ingress_cidrs`
has no default and must be set explicitly; use the authorized caller's public
`/32` unless unrestricted publication has been separately approved:

- the explicit caller CIDR input reaches only ALB port 80;
- short-lived benchmark tasks use their own security group, which reaches only
  ALB port 80 and public HTTPS endpoints needed for ECR and CloudWatch;
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
default `cache.t4g.medium` follows a real schema-7 container smoke: publication
used 1.04 GiB and peaked at 1.06 GiB, so the earlier 0.5-GiB micro could not hold
the serving state. A 1.37-GiB small would leave too little space after AWS/Redis
overhead; the 3.09-GiB medium leaves useful capacity and burstable-CPU headroom.
Change `elasticache_node_type` again only when cloud measurement supports it.

The replication group has exactly one node, no replica, no Multi-AZ failover,
and no snapshots. At-rest encryption and required in-transit encryption are
enabled. ECS must therefore pass a `rediss://` URL through `SIFT_REDIS_URL`.
Access remains network-authorized through the existing ECS-to-Redis security
group relationship; no cache password is stored in Terraform.

Separate API and materialization CloudWatch log groups use standard log storage
with three-day retention by default. They are intentionally deleted during
Terraform teardown after non-sensitive evidence has been preserved.

## ECS IAM contract

The execution and application roles are separate. Both trust only
`ecs-tasks.amazonaws.com`, constrained to this AWS account and ECS source ARNs
in the configured region.

The execution role can obtain the account-scoped ECR authorization token, pull
layers only from the Sift ECR repository, and create/write streams only below
the two planned task log groups. The application role can only call
`s3:GetObject` below `sift/artifacts/*`. It cannot list the bucket or call
ElastiCache APIs because startup fetches exact artifact keys and cache access is
network-level.

## Load-balancer contract

The internet-facing IPv4 ALB spans only the two public subnets and attaches only
the ALB security group. Its single HTTP port 80 listener forwards to an `ip`
target group on port 8000, which is required for Fargate's `awsvpc` tasks.

Target health uses `GET /health`, expects HTTP 200, checks every 15 seconds, and
requires two successes or three failures. This deliberately leaves catalog
initialization on the first `/recommend`; health checks must not disguise that
cold cost. Deletion protection is off for verified showcase teardown.

## ECS activation contract

The ECS cluster is part of the foundation plan. Task definitions and the API
service are deliberately absent until both `deployment_image_tag` and
`artifact_generation` select immutable assets. Those inputs must be supplied
together; the image tag must be a lowercase Git SHA and can never be `latest`.

Both task definitions use Linux Fargate with `awsvpc` networking, separate
execution and application roles, one essential container, and the existing S3
and TLS Redis environment contract. The API task pins one Uvicorn worker and
writes to the API log group. The one-off materialization task runs publication
and the 100-pair skew check in sequence and writes to its own log group.

The controlled runtime matrix selected one Uvicorn worker and
`openblas_num_threads = 1`, which are now the defaults. Two workers and a null
automatic-pool value remain available only to reproduce the experiment. A
startup probe records the task-visible logical CPU count, the BLAS environment,
and NumPy's runtime report. Access logs include the serving process ID so a
multi-worker run can prove that every worker received discarded warmup traffic
before its measured 1,000 requests. Keep the `h11` backend, 65-second keep-alive,
and full-body closing-connection regression check in every cell.

The selected measured envelope is x86-64, 2 vCPU, 4 GiB, one Uvicorn worker,
and one OpenBLAS thread. Architecture, CPU, memory, worker count, and native
thread count remain validated inputs so a future experiment is explicit. The
API service uses public subnets and an explicit public IP only for outbound AWS
access; port 8000 still accepts traffic solely from the ALB security group.

Activation has three deliberate states:

1. leave both immutable inputs null for a foundation-only plan;
2. set both inputs with `api_desired_count = 0` to register the task definitions
   and service without starting the API;
3. after the one-off task materializes Redis and its skew check passes, set
   `api_desired_count = 1` to start the baseline task. Two tasks are allowed only
   for the controlled 1-vCPU / 2-GiB-per-task matrix cell.

The service has no autoscaling. Deployment percentages are 0/100 so replacing
the one-task demo may cause brief downtime but does not run two paid API tasks.
The 180-second ALB health grace covers artifact download and process startup;
it does not hide the catalog initialization paid by the first recommendation.

`ecs_cluster`, `ecs_deployment`, `ecs_run_task_network`, and
`benchmark_run_task_network` expose the identifiers needed for deployment,
materialization, and reproducible short-lived benchmark clients.

## Inspected teardown procedure

Teardown is user-authorized work. The expiry tag is a reminder, not permission
for an automatic deletion script. Run these steps only after the user asks to
take the showcase down.

1. Confirm the AWS caller is account `442042531996`, preserve final
   non-sensitive evidence, and make a new mode-0600 copy of
   `terraform.tfstate` outside the repository. Record and verify its SHA-256.
2. Keep the ignored deployment selection intact. Generate the destroy plan with
   deliberate deletion of the reproducible S3/ECR assets enabled:

   ```bash
   terraform -chdir=infra/terraform plan \
     -destroy \
     -var='allow_asset_deletion=true' \
     -out=destroy.tfplan
   ```

3. Inspect both the human plan and `terraform show -json destroy.tfplan`.
   Every managed action must be deletion-only. Confirm it includes the ECS
   service/tasks, ALB/listener/target group, Valkey replication group, S3
   bucket, ECR repository, log groups, public networking, IAM roles/policies,
   and GitHub OIDC provider. Stop on any create/update action or missing paid
   resource.
4. Apply that exact saved plan, then delete `destroy.tfplan`. Do not substitute
   ad-hoc AWS deletion commands; they would create Terraform drift and make a
   partial teardown harder to diagnose.
5. Verify independently after provider completion and eventual-consistency
   settling:

   - `terraform state list` is empty;
   - ECS lists no running or pending Sift tasks and no active Sift service;
   - the Sift ALB/target group and their network interfaces are absent;
   - the Valkey replication group is absent;
   - the S3 bucket and ECR repository are absent;
   - the two Sift CloudWatch log groups are absent;
   - no NAT Gateway exists and the showcase VPC/subnets/security groups are
     absent;
   - the Sift ECS/GitHub IAM roles and GitHub OIDC provider are absent;
   - Resource Groups Tagging API returns no remaining resources tagged
     `Project=sift` and `Environment=showcase` after a second check.

Cost Explorer and AWS Budgets lag behind deletion, so a nonzero or missing
same-day billing value is not a teardown verdict. Preserve the empty state and
protected pre-destroy backup until the independent checks are recorded; decide
separately when to remove the historical backup.
