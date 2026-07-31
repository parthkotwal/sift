# AWS Deployment Status and Decisions

> **Purpose:** Shared handoff for every agent working on Sift's AWS lane.
>
> **Authority:** `.agents/AWS_DEPLOYMENT_PLAN.md` remains the deployment ground
> truth. This file records execution status, evidence, decisions made while
> implementing that plan, and the exact next action.
>
> **Issue ledger:** `.agents/AWS_ISSUES.md` records deployment failures, residual
> risks, and operating traps that should survive the handoff.
>
> **Last updated:** 2026-07-31 on branch `aws`.

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
- There is no missing application interface blocking the AWS lane. The cloud
  latency miss and cold-start behavior are recorded as coding-agent follow-ups
  in `AWS_ISSUES.md` (AWS-I1 and AWS-I2).
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
| Managed cache | One private Valkey 8 `cache.t4g.medium` node, using the Redis protocol over TLS. Schema-7 publication used 1.04 GiB locally, which ruled out the earlier micro and left too little headroom on small. No replica, Multi-AZ failover, or snapshots. |
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

Schema-7 deployment revalidation on 2026-07-31:

- merged `origin/main` through `fd3cede` in merge commit `0d1a923` without
  changing core semantics;
- `uv sync --frozen` succeeded;
- all 215 tests passed, Ruff passed, and strict mypy passed across 70 source
  files;
- packaged immutable generation `20260731T192746Z-0d1a923`, tied to the full
  merge SHA, with Redis schema 7, 29 files, and 379,777,487 bytes;
- every packaged size and SHA-256 digest verified;
- built and loaded the exact Linux AMD64 image selected by Terraform. It is
  295,691,986 bytes locally, runs as non-root user `sift`, retains the artifact
  entrypoint, and pins one Uvicorn worker;
- inside that image, a clean Redis publication activated schema 7 with the
  expected entity counts, and the skew check passed over 100 pairs / 7,300
  feature values;
- `/health` returned 200 and cold plus immediate-warm `/recommend` requests
  each returned ten results. Under AMD64 emulation on the ARM developer machine,
  application time was 799.273 ms cold and 86.497 ms warm. These numbers prove
  the path, not the Fargate latency contract;
- the temporary smoke containers/network and response files were deleted. The
  verified private generation and image remain local for publication only after
  foundation approval.

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

The earlier developer image was Linux ARM64. The current deployment image is
the revalidated Linux AMD64 build recorded above, matching the Terraform
runtime-platform choice and the standard GitHub-hosted build architecture.

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
- a configurable `cache.t4g.medium` node, with exactly one cache node,
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

Choice and compatibility evidence, updated 2026-07-31:

- the live ElastiCache API in `us-west-2` reported Valkey versions 7.2, 8.0,
  8.1, 8.2, 9.0, and 9.1;
- the exact schema-7 AMD64 container publication used 1,115,428,248 bytes
  (1.04 GiB), peaked at 1.06 GiB, and created 1,234,043 Redis keys. The planned
  0.5-GiB `cache.t4g.micro` cannot hold it and was rejected before apply;
- the 2026-07-31 AWS Price List record gives `cache.t4g.small` 1.37 GiB and
  `cache.t4g.medium` 3.09 GiB. The measured state would consume 75.8% of the
  small's physical memory before managed-service/Redis overhead, versus 33.6%
  of the medium, so medium is the smallest defensible initial node;
- current `us-west-2` Valkey on-demand prices are `$0.0256` per small node-hour
  and `$0.0520` per medium node-hour, effective 2026-07-01. Deterministic
  calculation gives `$0.6144` versus `$1.2480` for 24 hours, a `$0.6336`
  one-day difference before other services and transfer. Re-check immediately
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
- the planned cache has Valkey 8.0, `cache.t4g.medium`, one node, zero snapshot
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

### Phase 3 — ECS IAM boundary

Implemented on the current `aws` branch without applying it:

- separate task-execution and application task roles;
- both trust only the `ecs-tasks.amazonaws.com` service principal;
- both trust policies require `aws:SourceAccount` to equal account
  `442042531996` and `aws:SourceArn` to be an ECS ARN in `us-west-2` for that
  account, following AWS's confused-deputy guidance;
- a custom execution policy replaces the broader managed execution policy:
  it obtains an ECR authorization token, pulls only from the planned Sift ECR
  repository, and writes only to streams under the two planned log groups;
- the application role can only call `s3:GetObject` below
  `sift/artifacts/*` in the planned private bucket;
- the application role cannot list the bucket and neither role has ElastiCache
  API permissions;
- outputs expose only the two role ARNs required by later task definitions.

Saved-plan audit:

- the combined plan reported `39 to add, 0 to change, 0 to destroy`, exactly
  four additions beyond the previous plan: two roles and two inline policies;
- there are no wildcard allow-actions;
- the only wildcard allow-resource is `*` for
  `ecr:GetAuthorizationToken`, whose authorization token is not
  repository-resource-scopable;
- ECR image actions reference only `aws_ecr_repository.api.arn`;
- CloudWatch write actions reference only `aws_cloudwatch_log_group.tasks`;
- `s3:GetObject` references only the artifact bucket ARN plus
  `local.artifact_prefix`;
- each inline policy is attached to its intended role, with no cross-role
  attachment;
- the saved plan was deleted after JSON inspection so it cannot become a stale
  apply artifact;
- no `terraform apply` was run and no AWS resources were created.

IAM roles and policies have no separate hourly AWS resource charge. Their
permissions authorize later billable service activity but do not create it.

### Phase 3 — Application Load Balancer

Implemented on the current `aws` branch without applying it:

- one internet-facing IPv4 Application Load Balancer spanning only the two
  public subnets and attaching only the existing ALB security group;
- invalid HTTP header fields are dropped and deletion protection is disabled
  for the short-lived showcase;
- one HTTP port 80 listener forwarding to one HTTP port 8000 target group;
- target type `ip`, as required by Fargate tasks using `awsvpc`;
- `/health` checks every 15 seconds with a five-second timeout, two-success
  healthy threshold, three-failure unhealthy threshold, and HTTP 200 matcher;
- 30-second target deregistration delay;
- outputs expose the ALB ARN/DNS/hosted-zone ID, listener ARN, and target-group
  ARN needed by ECS and validation.

The health path intentionally does not initialize the catalog. A target can
become healthy before its first `/recommend` pays the catalog cold cost; that
first request remains a separately reported Phase 7 measurement.

Saved-plan audit:

- the combined plan reported `42 to add, 0 to change, 0 to destroy`, exactly
  three additions beyond the previous plan: ALB, listener, and target group;
- the ALB references only `aws_subnet.public` and
  `aws_security_group.alb`;
- the listener has one forward action referencing only the API target group;
- the target group is HTTP port 8000 with target type `ip` and the exact
  `/health` contract above;
- no NAT resource appeared and the only public CIDR rules remain ALB port 80
  ingress and ECS port 443 egress;
- the saved plan was deleted after JSON inspection so it cannot become a stale
  apply artifact;
- no `terraform apply` was run and no AWS resources were created.

Once applied, the ALB adds load-balancer hours or partial hours, LCUs, and
service-managed public IPv4 address hours across its enabled zones, plus any
applicable data transfer. Re-check current pricing immediately before apply.

### Phase 3 — ECS cluster, tasks, and service

Implemented on the current `aws` branch without applying it:

- one ECS cluster with paid Container Insights explicitly disabled;
- nullable immutable image-tag and artifact-generation inputs that must be set
  together and cannot use `latest` or unsafe generation paths;
- task CPU/memory validation covering valid Fargate combinations, with an
  initial x86-64, 1-vCPU, 2-GiB measurement envelope chosen to match standard
  GitHub-hosted builders and leave more startup headroom than the old example;
- separate Linux/Fargate `awsvpc` API and materialization task definitions,
  both using the existing execution/application role separation;
- one essential API container with the exact one-worker Uvicorn command, port
  8000, immutable S3 generation, and private TLS Redis URL;
- one essential materialization container that publishes the selected
  generation and then requires the 100-pair Redis/Parquet skew check to pass;
- separate `awslogs` destinations for API and materialization output;
- one API service using only the public subnets, explicit public-IP assignment,
  only the ECS task security group, and only the ALB `ip` target group;
- desired count constrained to zero or one, no autoscaling, a three-minute
  artifact/process startup health grace, and 0/100 deployment percentages that
  trade brief replacement downtime for never running two paid API tasks;
- activation gating that creates only the cluster when immutable assets are
  unset, creates both task definitions plus a zero-task service once they are
  selected, and starts exactly one task only after `api_desired_count` moves to
  one;
- outputs for cluster identity, both task-definition ARNs, service name, and
  the public-subnet/security-group inputs required by `aws ecs run-task`.

Verification on 2026-07-31:

- the AWS identity remains IAM user `parth` in account `442042531996`;
- Terraform formatting and validation passed;
- negative plans proved that an unpaired image/generation, an invalid Fargate
  CPU/memory combination, or a positive desired count without immutable inputs
  fails validation rather than merely warning;
- the foundation plan has 43 creates, zero changes, and zero destroys, with
  only the ECS cluster from this slice;
- the staged and live plans each have 46 creates, zero changes, and zero
  destroys: cluster, two task definitions, and one service are the four ECS
  resources;
- staged desired count is zero and live desired count is exactly one;
- both task definitions plan `FARGATE`, `awsvpc`, Linux x86-64, 1024 CPU units,
  and 2048 MiB; the service plans Fargate platform `1.4.0`, explicit public IP,
  port 8000, 180-second grace, and 0/100 replacement limits;
- no NAT or application-autoscaling resource appears. The only public CIDR
  rules remain ALB ingress port 80 and ECS egress port 443;
- task container JSON remains unknown before the foundation exists because its
  environment contains the not-yet-created bucket ID and cache endpoint. Its
  static configuration was inspected now and must be re-audited in the
  activation plan after the foundation apply resolves those values;
- all saved plan files were deleted after inspection;
- no `terraform apply` was run and no AWS resources were created.

The ECS skill's service-specific guidance was applied to Fargate sizing,
`awsvpc`, `ip` targets, task/execution-role separation, explicit platform
version, logging, and immutable ECR use. It did not alter the agreed deployment
architecture.

### Phases 3–5 — live deployment

The user explicitly approved the billable deployment on 2026-07-31. A fresh
foundation plan was inspected before apply and contained exactly 43 creates,
zero changes, and zero destroys. The apply completed successfully. Subsequent
activation plans were kept deliberately small and inspected before each apply.

Live foundation verification:

- the VPC, two public subnets, two private subnets, route tables, internet
  gateway, and three security groups exist in `us-west-2`;
- there is no NAT Gateway and the private subnet route table has no public
  route;
- the S3 artifact bucket has all four Block Public Access controls, versioning,
  `BucketOwnerEnforced`, AES-256 encryption, and the TLS-only deny policy;
- an anonymous object request returned HTTP 403;
- ECR is private, immutable-tagged, AES-256 encrypted, scan-on-push enabled,
  and governed by the intended lifecycle policy;
- the single-node Valkey 8 replication group is available on
  `cache.t4g.medium`, TLS-required, encrypted at rest, private-subnet-only, and
  has no replica, failover, Multi-AZ, or snapshots;
- the ALB is active and the Fargate target is healthy;
- a post-foundation refresh plan reported no changes.

Immutable publication evidence:

- S3 generation `20260731T192746Z-0d1a923` contains exactly 30 objects: the 29
  manifest-declared files plus `manifest.json`;
- the 29-file payload is 379,777,487 bytes and every local manifest size and
  SHA-256 digest was verified before upload;
- ECR tag `0d1a923424d074dfbfba26ea4c88ab355b1714d6` is immutable and resolves to
  image-index digest
  `sha256:e190a0eef85ed883b4c1a59132cb7bae14bd4dd937c4e63c2374156f30473c63`;
- the Linux AMD64 child digest is
  `sha256:eaf401482ae0a5d8843bce1f406252fcedb0c97ef36724194e5a3a81d7fde8d3`;
- ECR's basic scan completed. It reported 3 critical, 5 high, and 3 medium
  package findings, predominantly against Debian's essential `perl-base`.
  Sift does not invoke Perl, the container is non-root, and this remains a
  recorded short-lived-showcase residual risk rather than a hidden clean scan.

The one-off materialization task downloaded and verified the real S3
generation, activated Redis schema 7 with the expected counts, passed the skew
check over 100 pairs / 7,300 values, and exited 0. The continuously running API
uses the same image and generation. `/health` and representative recommendations
both succeed through the public ALB.

### Phase 6 — GitHub Actions OIDC

Terraform now defines:

- the account's GitHub OIDC provider;
- one deploy role whose trust policy requires audience `sts.amazonaws.com` and
  GitHub's exact immutable-ID subject
  `repo:parthkotwal@98301375/sift@1308243087:ref:refs/heads/aws`;
- an inline policy limited to pushing the Sift ECR repository, registering an
  ECS task definition, updating only the Sift ECS service, passing only the two
  Sift task roles to `ecs-tasks.amazonaws.com`, and reading target health;
- a manual workflow that runs the locked test/lint/type-check gates, builds and
  pushes Linux AMD64 under `github.sha`, registers a new task revision, waits
  for service stability, and requires every registered ALB target to be healthy.

The inspected OIDC plan contained exactly three resource creates, zero changes,
and zero destroys. It applied successfully. The first workflow run passed all
quality gates but AWS denied the initial name-only OIDC subject. CloudTrail
showed that GitHub now supplies immutable owner and repository IDs in `sub`;
the trust policy was corrected to the exact observed claim before retrying.

End-to-end proof:

- workflow run `30664468214` completed successfully in 5m38s from `aws` commit
  `3bc40c42e3776181737b42d08df3015c0c7f0b6d`;
- all 215 tests, Ruff, and strict mypy passed on the hosted runner;
- the corrected exact-subject OIDC exchange succeeded, followed by ECR login;
- GitHub built and pushed one Linux AMD64 image under the full Git SHA;
- the workflow registered API revision 6, updated ECS, waited for stability,
  and required every ALB target to be healthy;
- the new ECR digest is
  `sha256:f117e20a405ea3decd5920f6d6c355349fb818c36808575485b1bd02b3edb9d7`;
- ECR's basic scan completed with the same recorded 3 critical, 5 high, and 3
  medium Debian-package findings as the first image;
- the ignored local deployment selection was advanced to the workflow SHA and
  an audited Terraform apply registered API revision 7 plus materialization
  revision 5. A final refresh plan reports no changes, so local state once
  again owns the image the workflow deployed.

### Phase 7 — cloud validation and measured envelope

An early 1-vCPU / 2-GiB exploratory run established that the task was too small.
The deployed value size is one 2-vCPU / 4-GiB Linux AMD64 task and one Uvicorn
worker. A deterministic 1,000-request run through the public ALB at concurrency
4, on a quiet client host, completed with zero request failures:

| Stage | p50 | p95 | p99 |
| --- | ---: | ---: | ---: |
| Retrieval | 2.83 ms | 40.92 ms | 72.03 ms |
| Feature lookup | 69.98 ms | 104.52 ms | 121.11 ms |
| Ranking | 35.04 ms | 69.21 ms | 81.47 ms |
| Rerank | 1.38 ms | 19.22 ms | 32.63 ms |
| Application total | 125.29 ms | 163.71 ms | 189.56 ms |
| Client wall through ALB | 224.11 ms | 325.29 ms | 353.07 ms |

The cloud run is an honest `MISS` against the desktop `<100 ms` application p99
contract: 777/1,000 requests exceeded 100 ms. A controlled 4-vCPU / 8-GiB test
also missed (217.21 ms application p99), while cache engine CPU stayed below
1%, data-memory usage stayed near 45%, and service memory was low. The service
was therefore returned to 2 vCPU / 4 GiB instead of paying for an ineffective
vertical resize.

The first request after the connection-safe 2-vCPU task rollout measured
556.54 ms inside the funnel and 1.26 s at the client; the immediate warm request
measured 46.52 ms inside the funnel. Cold initialization is intentionally
reported separately.

The original Uvicorn `httptools` backend reproducibly truncated mid-sized ALB
responses when the client sent `Connection: close`, which Python `urllib` does.
The container command now selects Uvicorn `h11` and uses a 65-second backend
keep-alive, longer than the ALB's 60-second idle window. Six consecutive copies
of the formerly failing request then returned their full declared bodies, and
the 1,000-request benchmark had zero transport errors. This is deployment
configuration only; `src/sift/**` remains unchanged.

Final privacy and operability checks:

- direct access to the running task's public IPv4 on port 8000 timed out;
- the ECS security group admits port 8000 only from the ALB security group;
- the Valkey endpoint does not resolve on the public client, direct port 6379
  is blocked, and the Redis security group admits 6379 only from the ECS task
  security group;
- the S3 manifest returned HTTP 403 anonymously;
- the latest API log stream contains 22 events and the materialization stream
  contains 13 events, both in three-day-retention log groups;
- no file below `data/`, Terraform state, saved plan, or deployment auto-tfvars
  is tracked by Git;
- `/health` returns 200 through the ALB, a closed-connection recommendation
  returns all ten results, and its immediate warm application total was
  74.10 ms;
- the final Terraform refresh plan reports `No changes`.

### Current one-day price envelope

Public on-demand prices were queried through the AWS Price List API on
2026-07-31 for `us-west-2`, then calculated deterministically:

- the foundation's fixed 24-hour subtotal is `$2.02800`: Valkey medium
  `$1.24800`, ALB base `$0.54000`, and two ALB public IPv4 addresses `$0.24000`;
- with the deployed 2-vCPU / 4-GiB API task and its public IPv4 address, the
  fixed subtotal is `$4.51776` per 24 hours;
- one continuously consumed ALB LCU would add `$0.19200` per 24 hours. Actual
  LCU, log ingestion, requests, image/artifact storage, and transfer depend on
  usage;
- one-off materialization and controlled sizing tests add only their measured
  Fargate/public-IP runtime.

The billing/cost skill supplied the current-date, Price List, and deterministic
calculation rules used here. No budget or billing configuration was changed.

## AWS resource state

The showcase is live and billable. Terraform local state and its backup exist
under `infra/terraform/` and are gitignored. Preserve them until verified
teardown succeeds.

Live resources include the Terraform-managed VPC/network rules, private S3
bucket, private ECR repository, private Valkey node, two short-retention log
groups, ECS cluster/task definitions/service, internet-facing ALB, ECS task and
execution roles, GitHub OIDC provider, and GitHub deploy role. One 2-vCPU /
4-GiB API task is continuously running. Do not destroy any of them until the
user explicitly asks to take the showcase down.

## Exact next action

Keep the showcase live for the user. Preserve `infra/terraform/terraform.tfstate`
and its backup. Do not run destroy until the user explicitly asks to take the
showcase down. When asked, save any final non-sensitive evidence, enable the
deliberate asset-deletion switch, inspect the destroy plan, destroy, and verify
that every paid resource is gone.

## Remaining phases

- Phase 8 only: after the user asks, preserve final evidence, destroy paid
  resources, and verify teardown and billing views.
