# Cross-review tooling

Claude Code and Codex review each other's commits automatically, and can apply
each other's findings back to the working tree for you to approve.

## Setup

```bash
scripts/install-hooks.sh
```

Installs two git hooks (`.git/hooks/` isn't tracked, so this needs to be
re-run after every fresh clone):

- `prepare-commit-msg` — stamps a `Claude-Session-Id` trailer on commits made
  from inside an active `claude` session, so a later fix can be applied back
  into that same session's context.
- `post-commit` — fires `cross-review.sh` in the background after every
  commit.

## What happens automatically

Every commit's `Co-Authored-By` trailer says who wrote it (Claude tags its
own; `.agents/AGENTS.md` tells Codex to do the same). The *other* tool then
reviews it, inside a throwaway `git worktree` checked out at that exact
commit — isolated from your live working tree, so it's safe even if another
agent is actively editing at the same time. Read-only: neither reviewer gets
`Write`/`Edit`.

If the review turns anything up, it's handed straight to
`apply-review.sh`, which edits the working tree accordingly — never
commits. You get a macOS notification either way; the review `.md` opens
automatically; check `git diff` before committing whatever landed.

Skipped automatically for: commits with neither trailer (manual commits),
commits that only touch `scripts/` (this tooling itself), or when
`git config sift.cross-review-enabled false` is set.

## Manual use

```bash
scripts/cross-review.sh <sha> codex     # force Codex to review any past commit
scripts/cross-review.sh <sha> claude    # force Claude to review any past commit
scripts/apply-review.sh <sha> <codex|claude>   # apply an existing review's findings
```

Manual review runs don't auto-apply — an old commit's fix may not make sense
against the current tree anymore, so `apply-review.sh` is a separate,
deliberate step.
