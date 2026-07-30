# AWS Deployment Status and Decisions

> **Purpose:** Shared handoff for every agent working on Sift's AWS lane.
>
> **Authority:** `.agents/AWS_DEPLOYMENT_PLAN.md` remains the deployment ground
> truth. This file records execution status, evidence, decisions made while
> implementing that plan, and the exact next action.
>
> **Last updated:** 2026-07-30 on branch `aws`.

## Update rule

Update this file whenever a major deployment slice is completed, a deployment
decision changes, AWS resources are created or destroyed, a measurement is
recorded, or another agent needs an application interface. Do not mark a phase
complete from code inspection alone; include the command or observable result
that proved it.

Do not put secrets, Yelp-derived record contents, Terraform state, artifact
bundles, private endpoints, or credentials in this file.

## Ownership and coordination

- The AWS lane owns containerization, deployment artifact lifecycle, Terraform,
  AWS publication/deployment, CI/CD, cloud observability, validation, and
  teardown.
- Core Sift development continues separately. Treat `src/sift/**` as read-only
  in the AWS lane.
- If deployment needs a missing application interface, record the exact
  interface and intended caller here, then ask the core coding agent to provide
  it. Do not patch around the boundary in `src/sift/**`.
- There is currently **no outstanding request for the core coding agent**.
- Coordination note: `origin/main` was observed at `fd3cede` after this branch
  diverged. It includes D31 (`5b6dcb2`): Redis schema 7, catalog-wide item and
  business records, and a measured desktop supported concurrency of 8. The
  network slice is independent of that change. Before publishing an artifact
  generation or deploying ECS, integrate current `main` and rerun the complete
  package/container/skew smoke against schema 7.

## Fixed deployment decisions

| Topic | Decision |
| --- | --- |
| AWS identity | Keep the existing identity: account `442042531996`, IAM user ARN `arn:aws:iam::442042531996:user/parth`. |
| Region | `us-west-2`. |
| Infrastructure as code | Terraform. Installed CLI: `v1.15.8` on `darwin_arm64`. The CLI has no usage charge; provisioned AWS resources do. |
| Environment lifetime | Short-lived showcase, approximately one day, followed by verified teardown. |
| Runtime | One Linux container image for both the API and one-off Redis materialization commands. |
| Compute | ECS on Fargate, one API task initially, one Uvicorn worker per task. Size from measurement rather than inheriting the plan's 0.5 vCPU / 1 GB example. |
| Network | ALB in public subnets; ECS API tasks use public IPs for AWS egress; ElastiCache is private; no NAT Gateway. |
| Durable artifacts | Private S3 immutable generation prefixes. Redis remains rebuildable and non-durable. |
| Managed cache | One private Valkey 8 node, initially `cache.t4g.micro`, using the Redis protocol over TLS. No replica, Multi-AZ failover, or snapshots. Resize if measurement requires it. |
| Registry | Private ECR repository with Git-SHA image tags. |
| AWS credentials in tasks | ECS task-role credentials through boto3's default credential chain. No static AWS keys in images or task configuration. |
| CI/CD | GitHub Actions with AWS OIDC; no CodePipeline or CodeBuild. |
| Artifact selection | `SIFT_ARTIFACT_BUCKET` and `SIFT_ARTIFACT_GENERATION`, set together. Redis uses `SIFT_REDIS_URL`. |
| Health checks | `/health` is the ALB health endpoint. It intentionally does not initialize the serving catalog. |

## Application performance contract carried into AWS

- D28 introduced an end-to-end p99 target of `< 100 ms` at up to four
  concurrent requests **per process**, measured on four performance cores.
  D31 subsequently moved the desktop `SUPPORTED_CONCURRENCY` to 8 after
  catalog-wide state removed 1,000 invariant per-request Redis records.
- A Fargate task cannot inherit that result. Phase 7 must report the task size,
  offered concurrency, p50/p95/p99 through the ALB, and Sift's per-stage timing
  where available.
- The serving process retains a real cold cost: the first `/recommend` builds
  immutable catalog relations for the generation. Before D31, the 19.6 MB ALS
  relation alone was measured around 215 ms at the application layer. Schema 7
  adds catalog-wide item and business relations, so the old number is historical
  evidence rather than a prediction for the merged deployment.
- Because `/health` does not touch the store, a fresh target can become healthy
  before its first recommendation pays that cost. Report the first request
  separately; do not classify it as a steady-state regression.
- D31 resolved I31 in the application lane. Do not debug or redesign its
  materialization format through ECS; integrate it, validate schema compatibility,
  and measure the merged image.

## Completed work and evidence

### Phase 0 — local readiness

The core branch supplied the deployment interfaces and dependency boundary in
commits `4132a6f` through `c68e34e`, including the serving ranker, D28 benchmark,
runtime-safe ALS artifact constants, request generation pinning, and the
training/runtime dependency split.

After integration:

- repository tests passed;
- Ruff passed;
- strict mypy passed;
- the API, online materializer, and skew checker ran without `torch` or
  `implicit` installed in the serving image.

### Phase 1 — containerization

Commit `60d6496` (`deploy: package API and bootstrap in one runtime image`):

- uses the locked Python 3.12 environment;
- installs serving dependencies only;
- runs as user `sift` / UID 10001;
- uses one Uvicorn worker;
- supports alternate task commands;
- serves `/health`;
- ran locally against isolated Redis;
- materialized Redis, passed the skew check, and returned a representative
  recommendation.

The current image, after adding the artifact entrypoint, is Linux ARM64,
286,228,616 bytes, runs as `sift`, and retains the one-worker API command.
ECR/ECS architecture must match the image produced by CI; do not assume this
developer-machine ARM64 build determines the final task architecture.

### Phase 2 — immutable artifact packaging

Commit `6dbd779` (`deploy: package immutable serving artifact generations`):

- packages only the API and Redis-materialization inputs;
- excludes raw Yelp data, training sets, rejected two-tower artifacts, and
  unrelated derived files;
- creates a new generation in a temporary sibling and publishes it by rename;
- refuses to overwrite an existing generation;
- records Git commit, creation time, Redis schema, selected models, feature and
  embedding versions, file sizes, and SHA-256 digests.

Real-data proof on 2026-07-30:

- 29 files;
- 379,777,487 bytes;
- every manifest size and SHA-256 digest verified;
- the generation alone materialized Redis;
- the Redis-versus-Parquet skew check sampled 100 pairs / 7,300 feature values
  and passed;
- the API became healthy and returned 10 recommendations.

Observed single-request evidence from the local packaged-generation smoke:

| Request | Application total | Client wall time |
| --- | ---: | ---: |
| First/cold | 372.475 ms | 481.900 ms |
| Immediate warm | 66.675 ms | 68.699 ms |

These are smoke observations, not the Phase 7 Fargate benchmark.

### Phase 2 — S3 startup boundary

Commit `14cbedb` (`deploy: verify S3 artifact generations at task startup`):

- one container entrypoint wraps both the default API command and ECS command
  overrides;
- with no artifact variables, local mounted-data workflows remain unchanged;
- with both artifact variables, it downloads the selected S3 manifest and only
  the objects declared by that manifest;
- it rejects path traversal, duplicate paths, missing required files, missing
  event partitions, incompatible manifest/Redis/model/feature/embedding
  versions, count/size inconsistencies, and SHA-256 mismatches;
- it executes the task command only after verification succeeds;
- it uses bounded SDK retries/timeouts and four transfer threads;
- it needs `s3:GetObject`; it does not list the bucket.

Verification:

- 198 repository tests passed;
- Ruff passed;
- strict mypy passed;
- Docker image build passed;
- local entrypoint passthrough ran as UID 10001 with boto3 installed;
- incomplete artifact environment configuration failed before task execution;
- `/health` passed through the rebuilt image;
- the real packaging command is
  `python -m scripts.package_artifacts`.

Actual S3 transfer against AWS remains intentionally untested until Terraform
creates the private bucket in Phase 3.

### Phase 3 — Terraform foundation

Implemented on the current `aws` branch:

- added `infra/terraform/` as the single Terraform root;
- constrained Terraform to `1.15.x` and the official AWS provider to `6.57.x`;
- locked the installed provider to signed release `6.57.1`;
- fixed the provider region default at `us-west-2` while retaining a validated
  input;
- defined a shared `sift-showcase` naming prefix and required ownership tags;
- allowed additional non-sensitive tags without permitting them to override
  the required tags;
- added local state, plan, override, crash-log, and local variable-file
  exclusions to `.gitignore`;
- documented the local-state safety boundary and the planned resource slices.

Verification:

- `terraform -chdir=infra/terraform init -backend=false` succeeded;
- `terraform -chdir=infra/terraform fmt -check -recursive` succeeded;
- `terraform -chdir=infra/terraform validate` reported that the configuration
  is valid;
- a read-only, unsaved `terraform plan -refresh=false` proposed only the three
  local outputs and explicitly reported no real infrastructure changes;
- the generated `.terraform/` provider directory is ignored while
  `.terraform.lock.hcl` is tracked;
- no `terraform apply` was run and no AWS resources were created.

### Phase 3 — network configuration

Implemented on the current `aws` branch without applying it:

- one `10.42.0.0/20` VPC with DNS support and hostnames enabled;
- two public `/24` subnets and two private `/24` subnets across `us-west-2a`
  and `us-west-2b`;
- one internet gateway and a public default route;
- one explicit private route table with no internet or NAT route;
- public subnets do not assign public IPs automatically; the later Fargate
  service must explicitly enable public IP assignment;
- separate ALB, ECS-task, and Redis security groups;
- ALB ingress is public HTTP port 80 only;
- ECS port 8000 accepts only the ALB security group;
- Redis port 6379 accepts only the ECS task security group;
- ECS egress is limited to public HTTPS port 443 and Redis port 6379;
- outputs expose the VPC, subnet, AZ, and security-group IDs needed later.

Saved-plan audit:

- `22 to add, 0 to change, 0 to destroy`;
- one VPC, one internet gateway, four subnets, two route tables, one public
  route, four route-table associations, three security groups, and six
  security-group rules;
- no NAT Gateway or other NAT resource;
- exactly two `0.0.0.0/0` rules: ALB ingress on 80 and ECS egress on 443;
- the saved plan was deleted after JSON inspection so it cannot become a stale
  apply artifact;
- no `terraform apply` was run and no AWS resources were created.

Cost boundary as of 2026-07-30:

- this network-only plan allocates no public IPv4 address and contains no NAT
  Gateway, running compute, load balancer, or managed cache;
- later billable categories are ALB hours/LCUs and public IPv4 addresses,
  Fargate vCPU/memory and its task public IPv4 address, the ElastiCache node,
  CloudWatch ingestion/storage, and S3/ECR storage and requests;
- data transfer can also accrue once traffic exists. Re-check the inspected
  full plan before apply rather than treating this categorical note as a quote.

### Phase 3 — private artifact storage and registry

Implemented on the current `aws` branch without applying it:

- one account-unique private S3 bucket under the fixed
  `sift/artifacts` application prefix;
- all four S3 Block Public Access controls enabled and ownership fixed to
  `BucketOwnerEnforced`;
- S3 versioning and AES-256 server-side encryption enabled;
- a deny-only bucket policy rejects non-TLS requests and grants no principal
  access;
- lifecycle rules abort incomplete multipart uploads after one day and delete
  noncurrent versions after seven days, with no expiration of current artifact
  objects;
- one private ECR repository with AES-256 encryption, immutable tags, and
  repository-scoped basic scan-on-push;
- ECR lifecycle expires untagged images after one day and retains the ten
  newest tagged images;
- `allow_asset_deletion` defaults to `false`, so Terraform cannot silently
  empty a populated bucket or repository. Phase 8 must explicitly opt in when
  the showcase assets are ready for teardown;
- outputs expose only the artifact bucket name/ARN/prefix and ECR repository
  name/ARN/URL needed by later phases.

Saved-plan audit:

- the combined network, storage, and registry plan reported
  `31 to add, 0 to change, 0 to destroy`;
- exactly nine additions belong to S3 or ECR;
- the planned bucket has all public-access controls enabled, versioning
  enabled, AES-256 encryption, and no current-object expiration;
- the planned ECR repository has `force_delete = false`,
  `image_tag_mutability = "IMMUTABLE"`, and `scan_on_push = true`;
- the generated lifecycle JSON selects all tagged images with `["*"]` and
  expires only those beyond the newest ten;
- the saved plan was deleted after JSON inspection so it cannot become a stale
  apply artifact;
- no `terraform apply` was run and no AWS resources were created.

Cost boundary:

- this slice adds only S3 and ECR storage/request categories once applied;
- it deliberately uses service-managed AES-256 encryption rather than a
  customer-managed KMS key;
- basic ECR scanning is used, not enhanced Inspector scanning;
- actual storage, request, transfer, and scan behavior must be checked after
  publication rather than estimated from empty resources.

### Phase 3 — rebuildable cache and task logs

Implemented on the current `aws` branch without applying it:

- one ElastiCache subnet group referencing only the two private subnets;
- one node-based Valkey 8.0 replication group using the Redis protocol expected
  by Sift;
- a configurable `cache.t4g.micro` initial node, with exactly one cache node,
  no replicas, automatic failover disabled, and Multi-AZ disabled;
- snapshot retention set to zero with no final snapshot identifier, because S3
  is authoritative and the serving state is rebuilt by the materialization
  task;
- service-managed at-rest encryption and required in-transit encryption;
- no cache password in Terraform. Access is private and limited by the existing
  ECS-to-Redis security-group relationship;
- a sensitive connection output emits a `rediss://` URL for the later
  `SIFT_REDIS_URL` task environment;
- separate standard CloudWatch log groups for the API and materialization task,
  with configurable three-day retention and deletion during teardown.

Choice and compatibility evidence on 2026-07-30:

- the live ElastiCache API in `us-west-2` reported Valkey versions 7.2, 8.0,
  8.1, 8.2, 9.0, and 9.1;
- AWS documents `cache.t4g.micro` as a current-generation Valkey/Redis OSS node
  with 0.5 GiB memory and burstable CPU. This is a low-cost starting envelope,
  not a claim that it will pass materialization or ALB performance validation;
- the AWS Price List API returned one `us-west-2` Valkey
  `cache.t4g.micro` product at `$0.0128` per node-hour or partial hour, about
  `$0.31` for 24 hours before other services and transfer. Re-check immediately
  before apply because pricing can change;
- Sift constructs its client with `Redis.from_url`, so the deployment boundary
  already accepts the required `rediss://` URL. No core application change is
  currently needed.

Saved-plan audit:

- the combined plan reported `35 to add, 0 to change, 0 to destroy`, exactly
  four additions beyond the previous network/storage/registry plan;
- those additions are one subnet group, one single-node replication group, and
  two log groups;
- the cache references only `aws_subnet.private` and
  `aws_security_group.redis`;
- the planned cache has Valkey 8.0, `cache.t4g.micro`, one node, zero snapshot
  retention, both encryption modes enabled, and no auth token;
- each log group has three-day retention and `skip_destroy = false`;
- no NAT resource appeared and the only public CIDR rules remain ALB port 80
  ingress and ECS port 443 egress;
- the cache connection output is marked sensitive;
- the saved plan was deleted after JSON inspection so it cannot become a stale
  apply artifact;
- no `terraform apply` was run and no AWS resources were created.

Cost boundary:

- once applied, the cache is billed by each full or partial node-hour;
- CloudWatch Logs adds ingestion and retained-storage categories once tasks
  emit logs;
- there is no replica, snapshot storage, customer-managed KMS key, or NAT
  Gateway in this slice.

## AWS resource state

No AWS infrastructure has been created by this lane yet:

- no VPC, subnets, route tables, or security groups;
- no S3 artifact bucket;
- no ECR repository or pushed image;
- no ECS cluster, task definition, service, or running task;
- no ALB;
- no ElastiCache node;
- no CloudWatch log group;
- no GitHub OIDC role.

Therefore there are currently no deployment resources to tear down and no
deployment-generated AWS service charges to preserve.

## Exact next action

Add the Phase 3 ECS IAM roles and policies without applying them:

1. create separate ECS task-execution and application task roles, each trusted
   only by `ecs-tasks.amazonaws.com`;
2. let the execution role obtain an ECR authorization token, pull only from the
   planned Sift repository, and write only to the two planned task log groups;
3. let the application task role read only objects below the planned
   `sift/artifacts` S3 prefix;
4. do not grant S3 listing because the artifact entrypoint fetches exact keys
   and does not call `ListBucket`;
5. grant no ElastiCache API permissions because cache access is network-level;
6. expose only the role ARNs needed by the later ECS task definitions;
7. format, validate, and inspect a saved plan for wildcard actions/resources,
   trust principals, cross-role privilege, and unexpected resources.

Do not add the GitHub OIDC role in this slice; its repository/ref trust boundary
belongs with the deployment workflow.

## Remaining phases

- Phase 3: Terraform networking, IAM, private S3/ECR, ElastiCache, ECS, ALB, and
  CloudWatch logs.
- Phase 4: package and upload one immutable generation; run and verify the
  one-off Redis materialization task.
- Phase 5: push the image, deploy one API task, and verify ALB health and
  recommendation traffic.
- Phase 6: GitHub Actions OIDC build/push/deploy workflow.
- Phase 7: ALB benchmark with independently stated task size and offered
  concurrency, plus privacy/network checks.
- Phase 8: preserve non-sensitive evidence, destroy paid resources, and confirm
  teardown and billing views.
