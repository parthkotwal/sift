# AWS_ISSUES.md — deployment failures, risks, and operating traps

This is the deployment-specific companion to `ISSUES.md`. It records problems
encountered while publishing Sift to AWS, including resolved failures whose
diagnosis is likely to matter again. Application/model issues still belong in
`ISSUES.md`; deployment execution evidence belongs in
`AWS_DEPLOYMENT_STATUS.md`. The showcase has been deleted; open entries below
are historical application/deployment findings, not descriptions of a live
endpoint.

**Last updated:** 2026-08-01 on branch `aws`.

Format: `### AWS-I<n> — <title>   [open | fixed | accepted]`

Do not put credentials, private endpoints, Terraform state, saved plans, Yelp
record contents, or artifact bundles in this file.

---

## Open

### AWS-I1 — The selected Fargate topology meets concurrency 2, not 4   [open]

A controlled matrix used the same immutable image and artifact generation, the
corrected whole-request `server_ms` clock, and 1,000 requests per binding p99.
The selected one-worker, 2-vCPU / 4-GiB task with
`OPENBLAS_NUM_THREADS=1` measured 34.80 ms server p99 at concurrency 1 and
71.94 ms at concurrency 2 with closing connections. At concurrency 4 it missed
at 173.88 ms. Persistent connections produced the same conclusion: 34.44,
73.93, and 171.75 ms respectively. All six runs completed without transport
errors.

The unpinned one-worker baseline exposed a two-thread OpenBLAS pool and missed
from concurrency 2 onward. Two warmed Uvicorn workers with one BLAS thread each
were slower at every concurrency, doubled memory use, and regressed the
single-concurrency ranking tripwire. The optional two-task cell was therefore
not run: the review required it only if the worker experiment was promising.

A corrected open-loop run held 20.0 requests/s and passed at 77.06 ms server
p99. At 30 requests/s the service accumulated seconds of queueing and the
bounded client eventually fell behind its own schedule, so that run establishes
overload rather than a valid 30 requests/s capacity result. Standard ECS metrics
showed service CPU peaking near 100% while memory stayed below 7% for the selected
cell; those metrics are one-minute task/service aggregates, not per-core or
per-process evidence.

**Impact:** the deployed topology is a measured improvement and a valid
concurrency-2 service, but the desktop concurrency-4 promise is not established
on this Fargate size.

**Next investigation:** the application lane falsified both shared DuckDB
connection locking and JSON encoding as explanations. `feature.encode_rows`
is small and nearly load-insensitive; DuckDB statement execution is the
load-sensitive ceiling. Reduce statements per request before revisiting worker
or task counts.

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

The latest HTTPS rollout reproduced the mechanism more precisely: the first
request was 673.144 ms in the application, including 479.36 ms of
`feature.catalog_relations` and 64.42 ms of `rerank.inputs`; the immediate warm
request was 40.001 ms.

### AWS-I3 — The deployed base image has unresolved ECR scan findings   [open]

ECR basic scanning completed for the selected immutable image and reported 3
critical, 5 high, and 3 medium findings. Ten findings are against Debian `perl`
`5.36.0-7+deb12u3`: CVE-2026-48961, CVE-2026-7017, CVE-2026-57432,
CVE-2026-13221, CVE-2026-48959, CVE-2026-57433, CVE-2026-7010,
CVE-2025-15649, CVE-2026-48962, and CVE-2026-12087. The remaining medium
finding is CVE-2026-13595 in `util-linux` `2.38.1-5+deb12u3`. ECR did not report
a fixed package version for these findings. Sift does not invoke Perl and runs
as a non-root user, which reduces exposure but does not make the findings
disappear.

**Impact:** this is accepted residual risk for the short-lived showcase, not a
clean security scan or a production-ready image assessment.

Teardown deleted the ECR repository and image. The CVEs were not fixed; removal
ended the live deployment exposure while preserving this finding as historical
evidence.

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

### AWS-I6 — GitHub Actions currently emits a Node runtime deprecation warning   [open]

The successful deployment run completed, but GitHub annotated actions that target
Node.js 20 and forced them onto Node.js 24. This did not affect the deployment.

**Next action:** update action versions when their supported releases remove the
warning, keeping the workflow definition on `main` and `aws` synchronized. Rerun
the complete manual workflow after the update; do not change a working deployment
solely to suppress an annotation without that validation.

### AWS-I17 — Fresh TLS connections amplify the CloudFront tail   [open]

At a fixed 20.0 requests/s, 1,000 requests creating a new TLS connection each
time held schedule but missed at 120.55 ms server p99, with 32/1000 requests
over 100 ms. The same public CloudFront endpoint with connection reuse passed at
95.41 ms server p99, with 8/1000 over 100 ms and no transport errors. A direct
in-VPC Fargate control using the same immutable image, 100-user cycle, rate, and
benchmark SG passed at 42.14 ms server p99 with 0/1000 over budget.

**Impact:** ordinary connection-reusing clients meet the 20/s public contract;
clients that create a fresh TLS connection per call do not. The image itself is
not the regression. The remote client schedules connection starts at 20/s; the
measured difference is consistent with variable TLS/edge transit reshaping when
requests reach the single backend task.

**Next investigation:** treat connection reuse as part of the public client
contract. If a fresh-connection SLO is required, test CloudFront connection and
origin behavior separately before changing application workers or Fargate size.

### AWS-I15 — The CloudFront-to-ALB origin hop uses plain HTTP   [accepted]

The unrestricted `0.0.0.0/0` listener rule was removed. Terraform now requires
explicit ingress CIDRs, and the live caller rule is a single authorized `/32`.
Short-lived load clients use a separate security group that can reach only ALB
port 80 and public HTTPS endpoints required for ECR and CloudWatch. Direct ECS
port 8000 and Valkey remain private to their security-group relationships.

The public endpoint is now HTTPS on CloudFront's default certificate. The ALB
listener remains HTTP because this one-day deployment has no approved domain or
ACM certificate. CloudFront origin traffic must come from AWS's managed
origin-facing prefix list and present a random listener header held in protected
Terraform state; the listener default is 403. This materially narrows the
unencrypted hop but does not make it end-to-end TLS or production-ready.

---

## Fixed — retained because the failure mode can recur

### AWS-I18 — Destroy-time asset opt-in did not update prior Terraform state   [fixed]

The first audited destroy plan contained 60 deletes and no create or update
actions, and it was generated with `allow_asset_deletion=true`. It removed the
runtime and all dependent infrastructure, but S3 and ECR rejected their final
deletions as non-empty. The provider used the `force_destroy=false` and
`force_delete=false` values already stored in state rather than persisting the
new deletion behavior from the destroy-only plan.

Recovery used a saved targeted plan that changed only those two flags in place:
0 additions, 2 changes, and 0 deletions. A new saved destroy plan then contained
only the S3 bucket and ECR repository as deletion actions. Applying it removed
both reproducible stores and their contents; Terraform state is empty.

**Guardrail:** before producing the deletion-only plan, first apply a separately
inspected plan that persists `allow_asset_deletion=true` while the full
configuration is still converged. Do not assume passing the value only to
`plan -destroy` changes an existing resource's stored force-delete behavior.

### AWS-I5 — Teardown depended on preserving ignored local Terraform state   [fixed]

Terraform state and its backup were local and gitignored. The protected
mode-0600, checksum-verified pre-destroy copy allowed Terraform to remove the
complete showcase in dependency order and diagnose the final S3/ECR flag issue
without importing or deleting resources ad hoc. The live Terraform state is now
empty; the historical protected backup remains outside the repository.

**Guardrail:** for another short-lived local-state environment, make and verify
an external protected backup before apply and again immediately before teardown.

### AWS-I16 — Plain HTTP egress changed the authorized caller IP   [fixed]

The ALB timed out externally even though its SG allowed the HTTPS-observed
caller `/32`, the NACL was default allow-all, the ALB was active, and its target
was healthy. A protocol comparison found that HTTPS left this environment as
the authorized address while plain HTTP used relay addresses. A temporary
reject-only VPC Flow Log recorded the controlled port-80 attempts reaching the
ALB ENIs from those relay addresses and being rejected by the intended `/32`.
The in-app browser independently blocked the HTTP URL client-side.

Terraform now provides a restricted CloudFront HTTPS endpoint. A viewer-request
function enforces the original `/32`; caching is disabled; the origin is
protected by both the CloudFront prefix list and a random header checked by an
ALB listener rule. The temporary flow log, log group, and IAM resources were
removed after diagnosis, and the final Terraform plan reports no changes.

**Lesson:** do not assume an IP-check result transfers across protocols or
client surfaces. When a restricted ALB looks correct but times out, capture
rejected flows before widening ingress.

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
internet-facing ALB by its public DNS path. Security-group references match
private addresses, not a Fargate task's translated public source address. The
controlled matrix instead used a dedicated benchmark security group and one
private ALB node address, retaining the ALB listener/h11 path without reopening
world ingress.

**Lesson:** choose a probe path that matches the hypothesis. A task-to-public-ALB
timeout is not interchangeable with external client behavior or direct target
behavior.

---

## Operating invariants

- The showcase is deleted and the endpoint is not live. Do not redeploy without
  a new, explicit user request.
- The final conservative cost upper bound is `$5.62`. Cost Explorer's delayed
  final posting is not an operational teardown check.
- Preserve the protected pre-destroy backup and the empty local Terraform state
  as historical evidence; do not commit either state file.
- Delete saved plan files after review/use.
- Keep `src/sift/**` read-only in the AWS lane; hand application performance or
  lifecycle-interface changes to the coding agent.
- For any future deployment, a healthy target, a 200 status, and a completed
  workflow remain separate checks.
