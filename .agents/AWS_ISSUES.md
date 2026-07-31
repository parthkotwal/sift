# AWS_ISSUES.md — deployment failures, risks, and operating traps

This is the deployment-specific companion to `ISSUES.md`. It records problems
encountered while publishing Sift to AWS, including resolved failures whose
diagnosis is likely to matter again. Application/model issues still belong in
`ISSUES.md`; deployment execution evidence belongs in
`AWS_DEPLOYMENT_STATUS.md`.

**Last updated:** 2026-07-31 on branch `aws`.

Format: `### AWS-I<n> — <title>   [open | fixed | accepted]`

Do not put credentials, private endpoints, Terraform state, saved plans, Yelp
record contents, or artifact bundles in this file.

---

## Open

### AWS-I1 — Fargate misses the established steady-state latency contract   [open]

The final one-worker, 2-vCPU / 4-GiB task completed a deterministic 1,000-request
run through the ALB at concurrency 4 with zero transport errors, but application
p99 was 189.56 ms and 777/1,000 requests exceeded the `<100 ms` contract. Client
wall p99 was 353.07 ms. Feature lookup was the largest median stage at 69.98 ms.

A controlled 4-vCPU / 8-GiB run was worse at 217.21 ms application p99 while
Valkey engine CPU remained below 1%, data-memory usage stayed near 45%, and task
memory was low. The service was returned to 2 vCPU / 4 GiB because doubling the
task cost did not solve the bottleneck.

**Impact:** the showcase is healthy and functionally correct, but its cloud
performance result is an explicit `MISS`, not a validated extension of the
desktop envelope.

**Next investigation:** the application lane should profile the feature-store /
DuckDB path, Python scheduling, and per-request work under Linux/Fargate before
another infrastructure resize. Cross-AZ placement can add noise because the
single Valkey node occupies one AZ while ECS can place the task in either public
subnet, but the measurements do not support treating placement as the root cause.

### AWS-I2 — The first recommendation after rollout pays a large cold cost   [open]

`/health` deliberately does not initialize the serving catalog, so an ECS target
can become healthy before its first `/recommend` constructs immutable catalog
relations. After the connection-safe 2-vCPU rollout, the first request measured
556.54 ms in the application and 1.26 s at the client; the immediate warm request
measured 46.52 ms in the application. Other rollout probes showed the same class
of cold/warm split.

**Impact:** the first real user after every replacement sees materially higher
latency even when target health and steady-state behavior are good.

**Next investigation:** decide in the application lane whether to add an explicit
warmup operation, lifecycle hook, or a readiness distinction. Do not silently
make `/health` initialize the store; that changes its current contract and may
interact with the one-task rollout gap in AWS-I4.

### AWS-I3 — The deployed base image has unresolved ECR scan findings   [open]

ECR basic scanning completed for both published images and reported 3 critical,
5 high, and 3 medium package findings, predominantly against Debian's essential
`perl-base`. Sift does not invoke Perl and runs as a non-root user, which reduces
exposure but does not make the findings disappear.

**Impact:** this is accepted residual risk for the short-lived showcase, not a
clean security scan or a production-ready image assessment.

**Next investigation:** rebuild from a patched upstream image when available or
evaluate a smaller compatible runtime image, then rerun package scanning and the
complete container smoke before changing the deployment selection.

### AWS-I4 — One-task deployments can cause a brief availability gap   [accepted]

The showcase uses desired count 1 with ECS deployment percentages 0/100. A
replacement therefore stops the old task before the new one is healthy instead
of temporarily running two paid API tasks. Artifact download and process startup
can make the gap last a few minutes.

**Impact:** manual and GitHub-driven deployments can briefly make the public API
unavailable. This is an intentional one-day-demo cost tradeoff, not zero-downtime
deployment behavior.

**Production change:** use overlapping tasks (for example, minimum healthy 100
and maximum 200), sufficient desired count, and then verify draining and rollback.

### AWS-I5 — Teardown depends on preserving ignored local Terraform state   [open]

Terraform state and its backup are local under `infra/terraform/` and are
gitignored. They currently own the live showcase. Losing them would not delete
AWS resources, but it would make the planned, verified teardown substantially
harder and increase the risk of leaving billable resources behind.

**Guardrail:** preserve `infra/terraform/terraform.tfstate` and its backup; never
commit them; do not run `terraform destroy` until the user explicitly asks to take
the showcase down. Before teardown, inspect the destroy plan, enable the deliberate
asset-deletion switch, apply, and independently verify that paid resources are gone.

### AWS-I6 — GitHub Actions currently emits a Node runtime deprecation warning   [open]

The successful deployment run completed, but GitHub annotated actions that target
Node.js 20 and forced them onto Node.js 24. This did not affect the deployment.

**Next action:** update action versions when their supported releases remove the
warning, keeping the workflow definition on `main` and `aws` synchronized. Rerun
the complete manual workflow after the update; do not change a working deployment
solely to suppress an annotation without that validation.

---

## Fixed — retained because the failure mode can recur

### AWS-I7 — Uvicorn `httptools` truncated ALB responses for closing clients   [fixed]

The initial deployment intermittently returned only part of a mid-sized JSON body
and then reset the connection when a client sent `Connection: close`. Python's
`urllib.request`, used by the benchmark, sends that header. Small health bodies and
ordinary keep-alive requests could succeed, so basic smoke tests hid the defect.
The response declared a larger `Content-Length` but commonly stopped around 740
bytes. Both ALB nodes reproduced it; the same image served directly by local
Uvicorn did not.

The ECS command now selects Uvicorn `h11` and a 65-second backend keep-alive,
longer than the ALB's 60-second idle window. Six repetitions of the formerly
failing request returned their complete bodies and the subsequent 1,000-request
benchmark had zero transport errors.

**Regression check:** exercise `/recommend` through the ALB with an explicit
closing connection and verify the received body matches `Content-Length`; a 200
status alone is insufficient.

### AWS-I8 — The first planned Valkey node was too small for schema 7   [fixed]

The earlier `cache.t4g.micro` example has roughly 0.5 GiB, while local schema-7
publication measured about 1.04 GiB with a 1.06-GiB peak. It was rejected before
apply and Terraform now defaults to one `cache.t4g.medium`. Live memory usage has
stayed near 45%, providing practical headroom without a replica.

**Lesson:** size the cache from a real materialization of the current schema, not
from key counts or an infrastructure example. Re-measure before any schema or
artifact-generation expansion.

### AWS-I9 — A manual workflow cannot be dispatched until it exists on `main`   [fixed]

The first `gh workflow run deploy-aws.yml --ref aws` attempt returned workflow not
found even though the file existed on `aws`. GitHub must first register a
`workflow_dispatch` workflow from the repository's default branch.

The identical workflow definition was added to `main`; its deploy job remains
hard-gated to `refs/heads/aws`. Dispatching the `aws` ref then worked.

**Guardrail:** keep the workflow file synchronized on both branches. The default-
branch copy enables registration; the branch gate prevents a `main` deployment.

### AWS-I10 — GitHub's OIDC subject did not match the initial IAM trust policy   [fixed]

The first hosted workflow passed tests, lint, and type checking, then failed
`AssumeRoleWithWebIdentity` with `AccessDenied`. The trust policy expected the
traditional name-only repository subject. CloudTrail showed that GitHub supplied
an exact subject containing immutable owner and repository IDs.

Terraform now trusts that exact observed subject, the `aws` branch, and audience
`sts.amazonaws.com`. The retry successfully assumed the role and completed the
deployment.

**Guardrail:** diagnose OIDC denial from the actual token/CloudTrail claim. Do not
"fix" it by introducing a repository or branch wildcard; the immutable-ID match
also prevents a renamed or recreated repository from inheriting the role.

### AWS-I11 — GitHub deployment temporarily moved ECS outside Terraform state   [fixed]

The successful workflow registered and deployed a new task definition revision,
so the live image no longer matched the ignored local Terraform selection. The
deployment image tag was advanced to the workflow's immutable Git SHA and an
audited Terraform apply registered reconciled task revisions. A final refresh
plan returned `No changes`.

**Guardrail:** after every workflow deployment, reconcile the immutable image tag
into the local Terraform inputs and run a refresh plan. Otherwise a later apply or
destroy is based on stale ownership and may roll the service backward or obscure
what will be deleted.

### AWS-I12 — An explicit empty OIDC thumbprint list caused plan churn   [fixed]

Setting `thumbprint_list = []` allowed the AWS provider to discover a thumbprint,
then a later plan proposed removing that computed value. The argument was removed
so the optional/computed provider behavior owns it; the final plan is stable.

**Lesson:** do not pin an empty value for optional-and-computed AWS fields merely
to make configuration explicit. Verify the provider schema and confirm a second
refresh plan is clean.

### AWS-I13 — A 1-vCPU task was too small, but vertical scaling was not the fix   [fixed]

An exploratory 1-vCPU / 2-GiB run had 132.34 ms application p99 over 100 requests,
with 46/100 above 100 ms. Moving to 2 vCPU / 4 GiB removed that obviously undersized
configuration, but AWS-I1 remained. A later 4-vCPU / 8-GiB test also missed and was
reverted.

**Lesson:** treat task sizing as a controlled measurement, not a monotonic cure.
Retain the cheaper measured baseline while the actual stage bottleneck is profiled.

### AWS-I14 — A Fargate probe through the public ALB was a misleading diagnostic   [fixed]

A one-off task in the service VPC timed out when it tried to reach the
internet-facing ALB by its public DNS path. That hairpin-style route was not useful
for isolating the client-close reset and did not prove the API target was unhealthy.
The task exited without changing infrastructure; external ALB probes and direct
local-container reproduction were used instead.

**Lesson:** choose a probe path that matches the hypothesis. A task-to-public-ALB
timeout is not interchangeable with external client behavior or direct target
behavior.

---

## Operating invariants

- The showcase is live and billable; keep it running until the user explicitly
  requests teardown.
- Deploy only immutable Git-SHA image tags and immutable S3 generations.
- Preserve local Terraform state, delete saved plan files after review/use, and
  require a clean refresh plan after any out-of-band workflow deployment.
- Keep `src/sift/**` read-only in the AWS lane; hand application performance or
  lifecycle-interface changes to the coding agent.
- A healthy target, a 200 status, and a completed workflow are separate checks:
  validate target health, full response bodies, and Terraform convergence.
