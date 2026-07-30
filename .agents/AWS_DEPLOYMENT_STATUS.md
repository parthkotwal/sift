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
| Registry | Private ECR repository with Git-SHA image tags. |
| AWS credentials in tasks | ECS task-role credentials through boto3's default credential chain. No static AWS keys in images or task configuration. |
| CI/CD | GitHub Actions with AWS OIDC; no CodePipeline or CodeBuild. |
| Artifact selection | `SIFT_ARTIFACT_BUCKET` and `SIFT_ARTIFACT_GENERATION`, set together. Redis uses `SIFT_REDIS_URL`. |
| Health checks | `/health` is the ALB health endpoint. It intentionally does not initialize the serving catalog. |

## Application performance contract carried into AWS

- D28's end-to-end p99 target is `< 100 ms` at up to four concurrent requests
  **per process**, measured on four performance cores.
- A Fargate task cannot inherit that result. Phase 7 must report the task size,
  offered concurrency, p50/p95/p99 through the ALB, and Sift's per-stage timing
  where available.
- I31 creates a real per-process cold cost: the first `/recommend` builds a
  roughly 19.6 MB catalog relation and was previously measured around 215 ms at
  the application layer.
- Because `/health` does not touch the store, a fresh target can become healthy
  before its first recommendation pays that cost. Report the first request
  separately; do not classify it as a steady-state regression.
- I31 remains an application optimization, not an ECS debugging task. Do not
  change its materialization format or schema from this lane.

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

Implemented on the current `aws` branch and intended to be committed with this
status update:

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

Begin Phase 3 with one reviewable Terraform foundation slice:

1. add `infra/terraform/` with pinned Terraform/provider requirements;
2. configure the `us-west-2` AWS provider, naming inputs, and common tags;
3. add Terraform state and plan-file exclusions to `.gitignore`;
4. declare variables/outputs needed by later resource slices without creating
   speculative services;
5. run `terraform fmt` and `terraform init -backend=false`;
6. run `terraform validate`.

Propose the resource breakdown before adding live infrastructure. Do not run
`terraform apply` until the plan has been inspected for resource count, public
exposure, and expected one-day cost.

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
