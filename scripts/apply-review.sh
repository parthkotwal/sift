#!/usr/bin/env bash
# Apply a cross-review's findings to the working tree — for you to inspect and
# commit yourself. Never commits or pushes on its own.
#
# Usage: scripts/apply-review.sh <sha> <codex|claude>
#   <codex|claude> selects which review to apply, i.e. .agents/reviews/<sha>-<reviewer>.md
#   (run scripts/cross-review.sh first if that file doesn't exist yet).
#
# The FIXER is always the *other* tool — the one that authored <sha> in the
# first place, since it has context for its own prior reasoning that the
# reviewer doesn't. A codex review gets applied by claude; a claude review
# gets applied by codex. Mirrors the cross-review.sh author/reviewer pairing.
set -euo pipefail

SHA="${1:?usage: scripts/apply-review.sh <sha> <codex|claude>}"
REVIEWER="${2:?usage: scripts/apply-review.sh <sha> <codex|claude>}"
REPO_ROOT="$(git rev-parse --show-toplevel)"

if ! git rev-parse --verify --quiet "${SHA}^{commit}" >/dev/null; then
  echo "apply-review: '$SHA' is not a commit in this repo" >&2
  exit 1
fi
SHA="$(git rev-parse "$SHA")"
REVIEW_FILE="$REPO_ROOT/.agents/reviews/${SHA}-${REVIEWER}.md"

if [ ! -f "$REVIEW_FILE" ]; then
  echo "apply-review: no review at $REVIEW_FILE — run 'scripts/cross-review.sh $SHA $REVIEWER' first" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "apply-review: working tree isn't clean — commit or stash first so the diff this produces is easy to read" >&2
  exit 1
fi

REVIEW="$(cat "$REVIEW_FILE")"
PROMPT="$(cat <<PROMPT_EOF
A review of commit $SHA (which you authored) found the following. Apply the
findings that are real and still apply to the code as it stands now; skip
anything stale, stylistic, or low-value, and say why you skipped it. Edit the
working tree directly. Do NOT run 'git commit' or 'git push' — leave the
changes unstaged so the author can inspect and commit them. End with a short
summary of what you changed and what you skipped.

--- Review ---
$REVIEW
PROMPT_EOF
)"

case "$REVIEWER" in
  codex)
    # Codex reviewed a Claude commit -> Claude (the author) applies the fix.
    command -v claude >/dev/null 2>&1 || { echo "apply-review: claude CLI not found" >&2; exit 1; }
    claude -p "$PROMPT" \
      --model sonnet \
      --permission-mode dontAsk \
      --disallowedTools "Bash(git commit:*) Bash(git push:*)"
    ;;
  claude)
    # Claude reviewed a Codex commit -> Codex (the author) applies the fix.
    # No CLI-level "disallow this command" flag exists for `codex exec`, so
    # the no-commit rule here is instruction-only, not tool-enforced like the
    # Claude branch above. Check `git status` afterward before trusting it.
    export PATH="$HOME/.local/bin:$PATH"
    command -v codex >/dev/null 2>&1 || { echo "apply-review: codex CLI not found" >&2; exit 1; }
    codex exec "$PROMPT" -c model_reasoning_effort="medium"
    ;;
  *)
    echo "apply-review: unknown reviewer '$REVIEWER' (use 'codex' or 'claude')" >&2
    exit 1
    ;;
esac

echo
echo "apply-review: done. Inspect with 'git diff', then commit yourself if it looks right."
git status --short
