#!/usr/bin/env bash
# Cross-review: whichever agent didn't author a commit reviews it.
#
# Automatic use (triggered by .git/hooks/post-commit, installed via
# scripts/install-hooks.sh): detects the author via the Co-Authored-By
# trailer (see .agents/AGENTS.md, "Working style") and picks the other
# agent as reviewer. A commit with neither trailer is skipped.
#
# Manual use: review any past commit with a reviewer of your choice.
#   scripts/cross-review.sh <sha> codex   # Codex reviews commit <sha>
#   scripts/cross-review.sh <sha> claude  # Claude reviews commit <sha>
#   scripts/cross-review.sh <sha>         # auto-detect via trailer (hook default)
#   scripts/cross-review.sh               # auto-detect on HEAD
set -euo pipefail

SHA="${1:-$(git rev-parse HEAD)}"
FORCE_REVIEWER="${2:-}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
REVIEW_DIR="$REPO_ROOT/.agents/reviews"
mkdir -p "$REVIEW_DIR"

if ! git rev-parse --verify --quiet "${SHA}^{commit}" >/dev/null; then
  echo "cross-review: '$SHA' is not a commit in this repo" >&2
  exit 1
fi
SHA="$(git rev-parse "$SHA")"
SUBJECT="$(git log -1 --format=%s "$SHA")"

notify() {
  osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv)' -e 'end run' \
    -- "$1" "$2" >/dev/null 2>&1 || true
}

run_codex_review() {
  local out="$REVIEW_DIR/${SHA}-codex.md"
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v codex >/dev/null 2>&1; then
    echo "cross-review: codex CLI not found, skipping review of $SHA" >&2
    return 1
  fi
  codex exec review --commit "$SHA" --title "$SUBJECT" -o "$out" \
    -c model_reasoning_effort="medium" \
    > "$REVIEW_DIR/${SHA}-codex.log" 2>&1
  notify "Codex reviewed $SHA" "$SUBJECT"
  echo "cross-review: codex review of $SHA written to $out" >&2
}

run_claude_review() {
  local out="$REVIEW_DIR/${SHA}-claude.md"
  if ! command -v claude >/dev/null 2>&1; then
    echo "cross-review: claude CLI not found, skipping review of $SHA" >&2
    return 1
  fi
  claude -p "Review the changes introduced by commit $SHA in this git repository (subject: \"$SUBJECT\"). Run 'git show $SHA' to see the full diff. Write a concise review: a 1-2 sentence summary, then specific, actionable findings with file:line references, ranked most severe first. If nothing significant, say so briefly. Do not modify any files." \
    --model sonnet \
    --permission-mode dontAsk \
    --allowedTools "Read Bash(git show:*) Bash(git log:*) Bash(git diff:*) Bash(git blame:*)" \
    > "$out" 2>"$REVIEW_DIR/${SHA}-claude.log"
  notify "Claude reviewed $SHA" "$SUBJECT"
  echo "cross-review: claude review of $SHA written to $out" >&2
}

case "$FORCE_REVIEWER" in
  codex)
    run_codex_review
    ;;
  claude)
    run_claude_review
    ;;
  "")
    TRAILERS="$(git log -1 --format=%B "$SHA" | git interpret-trailers --parse)"
    if echo "$TRAILERS" | grep -qi "^Co-Authored-By: *Claude"; then
      run_codex_review
    elif echo "$TRAILERS" | grep -qi "^Co-Authored-By: *Codex"; then
      run_claude_review
    else
      # Manual commit, not authored by either agent — nothing to do.
      exit 0
    fi
    ;;
  *)
    echo "cross-review: unknown reviewer '$FORCE_REVIEWER' (use 'codex' or 'claude')" >&2
    exit 1
    ;;
esac
