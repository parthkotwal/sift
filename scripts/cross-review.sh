#!/usr/bin/env bash
# Cross-review: whichever agent didn't author a commit reviews it.
#
# Triggered by .git/hooks/post-commit (installed locally, see scripts/install-hooks.sh).
# Detection is by Co-Authored-By trailer (see .agents/AGENTS.md, "Working style"):
# a commit with neither trailer is a manual commit and is skipped.
set -euo pipefail

SHA="${1:-$(git rev-parse HEAD)}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
REVIEW_DIR="$REPO_ROOT/.agents/reviews"
mkdir -p "$REVIEW_DIR"

MSG="$(git log -1 --format=%B "$SHA")"
SUBJECT="$(git log -1 --format=%s "$SHA")"

notify() {
  osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
}

if echo "$MSG" | grep -qi "Co-Authored-By: Claude"; then
  REVIEWER="codex"
  OUT="$REVIEW_DIR/${SHA}-codex.md"
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v codex >/dev/null 2>&1; then
    echo "cross-review: codex CLI not found, skipping review of $SHA" >&2
    exit 0
  fi
  codex exec review --commit "$SHA" --title "$SUBJECT" -o "$OUT" \
    > "$REVIEW_DIR/${SHA}-codex.log" 2>&1
  notify "Codex reviewed $SHA" "$SUBJECT"

elif echo "$MSG" | grep -qi "Co-Authored-By: Codex"; then
  REVIEWER="claude"
  OUT="$REVIEW_DIR/${SHA}-claude.md"
  if ! command -v claude >/dev/null 2>&1; then
    echo "cross-review: claude CLI not found, skipping review of $SHA" >&2
    exit 0
  fi
  claude -p "Review the changes introduced by commit $SHA in this git repository (subject: \"$SUBJECT\"). Run 'git show $SHA' to see the full diff. Write a concise review: a 1-2 sentence summary, then specific, actionable findings with file:line references, ranked most severe first. If nothing significant, say so briefly. Do not modify any files." \
    --permission-mode dontAsk \
    --allowedTools "Read Bash(git show:*) Bash(git log:*) Bash(git diff:*) Bash(git blame:*)" \
    > "$OUT" 2>"$REVIEW_DIR/${SHA}-claude.log"
  notify "Claude reviewed $SHA" "$SUBJECT"

else
  # Manual commit, not authored by either agent — nothing to do.
  exit 0
fi

echo "cross-review: $REVIEWER review of $SHA written to $OUT" >&2
