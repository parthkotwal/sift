# Repository scripts

## Serving artifact generations

`package_artifacts.py` copies only the files required by the API and Redis
materializer into a new immutable generation. It writes a manifest containing
the source commit, serving schema/model versions, byte sizes, and SHA-256
digests:

```bash
python scripts/package_artifacts.py \
  --source-data data \
  --output-root data/deployment \
  --generation "$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD)"
```

The destination generation must not already exist. Packaging happens in a
temporary sibling directory and is renamed into place only after every file and
the manifest have been written, so a failed run does not expose a partial
generation.

## Cross-review tooling

Claude Code and Codex review each other's pushed work automatically.

## Setup

```bash
scripts/install-hooks.sh
```

Installs a `pre-push` hook (`.git/hooks/` isn't tracked, so this needs to be
re-run after every fresh clone). It backgrounds the review and returns
immediately — pushing is never delayed.

## What happens automatically

On `git push`, the hook looks at the trailer on the tip commit being pushed
(`Co-Authored-By: Claude` or `Co-Authored-By: Codex` — Claude tags its own
commits; `.agents/AGENTS.md` tells Codex to do the same) and has the *other*
tool review everything between the previous remote tip and the new one, in
one shot — not commit-by-commit, so it doesn't fire on every local commit
while you're still iterating.

The review runs inside a throwaway `git worktree` checked out at the pushed
tip (detached HEAD, deleted when it finishes) — isolated from your live
working tree, so it's safe to keep working while it runs. Neither reviewer
has the `Write`/`Edit` tools, so it can only read and report — though for
Claude that means specifically those two tools are blocked, not that it's
sandboxed to read-only filesystem access (it still has Bash). For Codex,
`review` mode has only ever produced a written review in every observed run,
but nothing stops it at the tool level the way Claude's tool block does.

Output lands in `.agents/reviews/<sha>-<reviewer>.md` (gitignored) plus a
quiet macOS notification — nothing opens automatically.

Skipped automatically for: a tip commit with neither trailer (a manual
push), a push that only touches `scripts/` (this tooling itself), or when
`git config sift.cross-review-enabled false` is set.

## Acting on a review

Nothing applies itself — read `.agents/reviews/<sha>-<reviewer>.md` and
decide. To have the original author apply the findings:

```bash
scripts/apply-review.sh <sha> <codex|claude>
```

Hands the review to whichever tool *wrote* `<sha>` (it has the context),
has it edit the working tree, but never commits or pushes — Claude's side
is tool-blocked from `git commit`/`git push`, Codex's side is
instruction-only. Always a fresh session (an earlier version tried forking
the original authoring session for full context; in practice it surfaced as
a live turn in that conversation instead of running invisibly, and its
permission flags didn't behave predictably — not solid enough to keep).
Requires a clean working tree, since the point is a diff you can read.
Inspect with `git diff`, then commit yourself.

## Manual use

```bash
scripts/cross-review.sh <sha> codex             # force Codex to review <sha>^..<sha>
scripts/cross-review.sh <sha> claude             # force Claude to review <sha>^..<sha>
scripts/cross-review.sh <new-sha> codex <old-sha>   # force a review of a range
scripts/apply-review.sh <sha> <codex|claude>     # apply an existing review's findings
```
