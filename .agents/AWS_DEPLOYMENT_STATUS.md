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
> **Last updated:** 2026-08-01 on branch `aws`.

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
  concurrency-4 miss and cold-start behavior remain coding-agent follow-ups in
  `AWS_ISSUES.md` (AWS-I1 and AWS-I2).
- Current `main` through `2361293` is merged. The deployed image includes the
  exact-k response contract, corrected middleware `server_ms`, fixed-rate load
  generation, opt-in sub-stage timings, and the separate
  `feature.encode_rows` span. The merged branch passed 246 tests, Ruff, strict
  mypy, Terraform validation, and the hosted Linux AMD64 container build.

## Fixed deployment decisions

| Topic | Decision |
| --- | --- |
| AWS identity | Keep the existing identity: account `442042531996`, IAM user ARN `arn:aws:iam::442042531996:user/parth`. |
| Region | `us-west-2`. |
| Infrastructure as code | Terraform. Installed CLI: `v1.15.8` on `darwin_arm64`. The CLI has no usage charge; provisioned AWS resources do. |
| Environment lifetime | Short-lived showcase. Visible expiry: 2026-08-01 18:00 PDT, followed by user-authorized verified teardown. Hard lifetime ceiling `$10`; operational stop `$8` to reserve `$2` for billing lag. |
| Runtime | One Linux container image for both the API and one-off Redis materialization commands. |
| Compute | ECS on Fargate, one 2-vCPU / 4-GiB API task, one Uvicorn worker, and `OPENBLAS_NUM_THREADS=1`, selected by the controlled matrix rather than assumed. |
| Network | Restricted CloudFront HTTPS endpoint in front of an ALB in public subnets. The viewer `/32` is enforced at the edge; CloudFront-to-ALB requests require both AWS's origin-facing prefix list and a random origin header. Direct ALB access retains the same `/32`; benchmark clients retain their restricted security group. ECS API tasks use public IPs for AWS egress; ElastiCache is private; no NAT Gateway. |
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

The corrected serving revision was subsequently deployed by workflow run
`30677517131` from immutable Git SHA
`aae459ece593868dd2cc323d5ebdaf4b01d7eccc`. All quality gates, OIDC exchange,
Linux AMD64 build/push, ECS stability wait, and target-health verification
passed. ECR resolves that tag to digest
`sha256:4e2d264552e3ca6648089f074ca2b3d58c46e2fb04709ea64fe32e5c26f567ba`.
Terraform was reconciled to the same SHA before any runtime experiment.

### Phase 7 — cloud validation and measured envelope

A runtime probe established the actual Fargate boundary before measurement. The
2-vCPU task sees two logical CPUs; its unpinned NumPy/OpenBLAS pool selected two
threads, while `OPENBLAS_NUM_THREADS=1` produced an observed one-thread pool.
Access logs include process IDs. Before the two-worker cell, 101 discarded
warmup requests reached both workers (41 and 60 respectively).

Every binding cell used the same immutable image and artifact generation,
1,000 deterministic requests, the corrected whole-request `server_ms` clock,
and both closing and persistent connections. Values are server p99 / achieved
closed-loop throughput:

| Runtime cell | c1 close | c2 close | c4 close | c1 keep-alive | c2 keep-alive | c4 keep-alive |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 worker, BLAS auto (2) | 81.05 ms / 16.6/s | 175.32 ms / 18.2/s | 317.24 ms / 21.0/s | 79.78 ms / 16.9/s | 174.37 ms / 18.4/s | 329.42 ms / 21.1/s |
| 1 worker, BLAS 1 | 34.80 ms / 37.7/s | 71.94 ms / 47.9/s | 173.88 ms / 45.0/s | 34.44 ms / 38.5/s | 73.93 ms / 47.3/s | 171.75 ms / 44.8/s |
| 2 warmed workers, BLAS 1 | 57.39 ms / 22.3/s | 110.05 ms / 29.2/s | 239.74 ms / 29.5/s | 55.62 ms / 23.7/s | 97.55 ms / 28.5/s | 244.23 ms / 29.9/s |

The selected topology is one worker with one BLAS thread. It preserves the
concurrency-1 stage tripwires, passes the whole-request contract through
concurrency 2, and is materially faster than two workers. The optional two
1-vCPU-task cell was not run because the agreed matrix made it conditional on a
promising two-worker result. This avoided increasing the daily run rate without
evidence.

The exact UTC matrix windows were 01:34:53–01:40:19 for the unpinned baseline,
01:44:41–01:47:03 for the selected cell, and 01:52:40–01:56:27 for two workers.
Ordinary `AWS/ECS` service metrics showed peak CPU of 98.68%, 99.99%, and 98.13%
respectively; peak memory was 5.79%, 6.29%, and 10.94%. These are useful
one-minute service/task aggregates with one desired task, but they cannot show
individual cores or processes.

Corrected direct-ALB fixed-rate scheduling on the selected topology sustained 20.0
requests/s with 77.06 ms server p99 and a client that kept its schedule. At a
requested 30/s, queueing grew into seconds and the bounded client eventually
fell 500 ms behind, so it proves overload rather than a valid 30/s capacity.
A 100-request diagnostic sample placed the largest median sub-stages in the
DuckDB feature query (15.69 ms), relation loading (8.09 ms), and LightGBM
prediction (18.06 ms). The corresponding p99s were 28.38, 14.89, and 32.39 ms.

The h11 backend, 65-second backend keep-alive, and full-body closing-client
regression path remained intact. All measured requests were decoded completely;
there were no transport errors. The ALB's former `0.0.0.0/0` rule was replaced
with one authorized `/32`, and Terraform now has no public default. A dedicated
benchmark security group reaches only ALB port 80 and AWS HTTPS endpoints.
The user-facing endpoint is now HTTPS; the authenticated CloudFront-to-ALB hop
remains HTTP inside AWS and is an explicitly accepted short-lived residual risk.

Terraform owns the selected image and topology at API task revision 14. The
target is healthy, the runtime probe reports one worker and one BLAS thread, and
a final refresh plan reports `No changes`.

### Restricted HTTPS follow-up

The original external timeout was not an unhealthy ALB or a missing route.
Protocol-specific egress checks showed this environment leaving HTTPS as the
authorized `/32` but plain HTTP through relay addresses. A short-lived,
reject-only VPC Flow Log then recorded the controlled port-80 attempts arriving
at the ALB nodes from those relay addresses and being rejected by the intended
security-group rule. The diagnostic flow log, log group, and IAM role/policy
were deleted immediately after the result; Terraform owns none of them now.

Terraform now adds CloudFront distribution `E3AMJXNGP1HCIP` and the published
viewer-request function `sift-showcase-viewer-allowlist`. The public endpoint is
`https://d1q85kfkwjx9tf.cloudfront.net`. Caching is disabled, all API methods
are allowed, IPv6 is disabled because the approved identity is an IPv4 `/32`,
and the function returns 403 before origin processing for any other viewer IP.
A live function test returned 403 for an unrelated test address and passed the
authorized address through. The ALB security group admits CloudFront only via
AWS's origin-facing managed prefix list, and its listener forwards those
requests only when CloudFront supplies the random state-held origin header. The
default listener response is 403; the existing direct `/32` and benchmark-SG
paths have explicit forwarding rules.

GitHub workflow run `30683908583` passed all 246 tests, Ruff, strict mypy, OIDC,
the Linux AMD64 build/push, ECS stability, and target-health verification for
immutable image `ab2c4cf80f40ce0a1007dc2f1f0e298bccb19f5c`, digest
`sha256:89214309b3d05e6cb1e4e41c47abbc14eb4de725aba0a98bd3662ba6182ab7a1`.
Terraform reconciliation registered API revision 14 and materialization
revision 8. ECR scanning completed with the same 3 critical, 5 high, and 3
medium findings already recorded in AWS-I3.

The first `detail=true` request after reconciliation returned ten results at
673.144 ms application time; 479.36 ms was catalog construction and 64.42 ms
was rerank-input construction. The immediate warm request was 40.001 ms. A
100-request warm diagnostic sample measured p50/p99 of 11.11/13.29 ms for
`feature.duckdb_query`, 6.10/7.29 ms for `feature.load_relations`, 13.03/14.29
ms for `ranking.predict`, and 1.63/2.07 ms for the new
`feature.encode_rows`; one diagnostic keep-alive reset was excluded from those
99 successful responses. The later 1,000-request connection-reuse gate had no
transport errors. This confirms the coding agent's conclusion: encoding is
small; DuckDB statement work is the load-sensitive ceiling.

The public 20/s result depends on connection behavior. With 1,000 fresh TLS
connections, CloudFront held 20.0/s but missed at 120.55 ms server p99 (32/1000
over 100 ms). With connection reuse, the same endpoint passed at 95.41 ms
server p99 (8/1000 over 100 ms) with zero transport errors. A direct in-VPC
Fargate control using the same image, users, rate, and benchmark SG passed at
42.14 ms server p99 with 0/1000 over budget. The one-off clients exited and
their temporary task definitions were deregistered. The image is therefore not
the regression. The measured difference isolates the public edge path and is
consistent with fresh remote TLS connections reshaping arrivals before they
reach the single task.

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
calculation rules used here. Cost Explorer reported only `$0.00044065` of older
S3 usage because current-day deployment charges had not landed. A conservative
deterministic upper bound at 2026-08-01 12:00 PDT is `$5.48`. It advances the
earlier `$2.25` upper bound by the exact elapsed fixed daily rate and adds a
`$0.05` allowance for CloudFront requests/functions, brief flow logging, and
the two diagnostic Fargate clients. That leaves `$2.52` before the operational
`$8` stop and `$4.52` before the hard lifetime `$10` ceiling. Keeping the
selected topology until the 2026-08-01 18:00 PDT expiry projects a conservative
`$6.61` upper bound before unexpected variable usage. There is no AWS Budget;
creating a notification requires an approved destination, and daily billing
lag means it would be warning-only rather than enforcement.

## AWS resource state

The showcase is live and billable. Terraform local state and its backup exist
under `infra/terraform/` and are gitignored. Preserve them until verified
teardown succeeds.

A final post-HTTPS state copy is stored outside the repository at
`~/.codex/aws-state-backups/sift-showcase-20260801T1200PDT.tfstate`; its directory
is mode 0700, the file is mode 0600, and its SHA-256 matches live state at
`a4b6d4658afd818417bd3477565c55a45ffa1c7a07ade6fad61b78825ba2206b`.

Live resources include the Terraform-managed VPC/network rules, dedicated
benchmark client security group, private S3
bucket, private ECR repository, private Valkey node, two short-retention log
groups, ECS cluster/task definitions/service, internet-facing ALB, ECS task and
execution roles, GitHub OIDC provider, GitHub deploy role, restricted CloudFront
distribution, and CloudFront viewer-request function. One 2-vCPU /
4-GiB API task with one Uvicorn worker and one OpenBLAS thread is continuously
running. All one-off benchmark tasks have exited. The live viewer and direct-ALB
caller ingress are one explicit `/32`, not `0.0.0.0/0`. Do not destroy any resource until the user
explicitly asks to take the showcase down.

## Exact next action

Keep the showcase live for the user, but treat 2026-08-01 18:00 PDT as the
teardown decision deadline. Preserve `infra/terraform/terraform.tfstate` and
the checksum-verified mode-0600 backup outside the repository. Do not run
destroy until the user explicitly asks to take the showcase down. When asked,
follow the exact inspected destroy-plan and independent post-destroy checklist
in `infra/terraform/README.md`.

## Remaining phases

- Phase 8 only: after the user asks, preserve final evidence, destroy paid
  resources, and verify teardown and billing views.
