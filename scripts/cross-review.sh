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
#
# Both reviewers are read-only: Claude's tool allowlist has no Write/Edit,
# and Codex's `review` subcommand only ever produces a written review in
# every observed run, even though its sandbox is workspace-write.
#
# Every run opens the resulting .md (macOS `open`, default handler) and
# fires a notification. To act on a review's findings, see apply-review.sh.
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

open_review() {
  open "$1" >/dev/null 2>&1 || true
}

run_codex_review() {
  local out="$REVIEW_DIR/${SHA}-codex.md"
  local jsonl="$REVIEW_DIR/${SHA}-codex.jsonl"
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v codex >/dev/null 2>&1; then
    echo "cross-review: codex CLI not found, skipping review of $SHA" >&2
    return 1
  fi
  codex exec review --commit "$SHA" --title "$SUBJECT" -o "$out" \
    -c model_reasoning_effort="medium" --json \
    > "$jsonl" 2>"$REVIEW_DIR/${SHA}-codex.log"

  local usage tokens
  usage="$(jq -c 'select(.type=="turn.completed") | .usage' "$jsonl" 2>/dev/null | tail -1)"
  tokens="$(jq -r '(.input_tokens // 0) + (.output_tokens // 0)' <<<"${usage:-null}" 2>/dev/null || echo 0)"
  if [ -z "${usage:-}" ] || [ "$usage" = "null" ] || [ "$tokens" = "0" ]; then
    echo "cross-review: codex review of $SHA written to $out (tokens: n/a — codex-cli's review subcommand doesn't report usage)" >&2
  else
    echo "cross-review: codex review of $SHA written to $out (tokens: $usage)" >&2
  fi
  notify "Codex reviewed $SHA" "$SUBJECT"
  open_review "$out"
}

run_claude_review() {
  local out="$REVIEW_DIR/${SHA}-claude.md"
  local json="$REVIEW_DIR/${SHA}-claude.json"
  if ! command -v claude >/dev/null 2>&1; then
    echo "cross-review: claude CLI not found, skipping review of $SHA" >&2
    return 1
  fi
  claude -p "Review the changes introduced by commit $SHA in this git repository (subject: \"$SUBJECT\"). Run 'git show $SHA' to see the full diff. Write a concise review: a 1-2 sentence summary, then specific, actionable findings with file:line references, ranked most severe first. If nothing significant, say so briefly. Do not modify any files." \
    --model sonnet \
    --permission-mode dontAsk \
    --allowedTools "Read Bash(git show:*) Bash(git log:*) Bash(git diff:*) Bash(git blame:*)" \
    --output-format json \
    > "$json" 2>"$REVIEW_DIR/${SHA}-claude.log"

  jq -r '.result // "(no result — see .claude.log)"' "$json" > "$out" 2>/dev/null \
    || echo "(failed to parse claude output — see $json)" > "$out"

  local input_tok output_tok cost
  input_tok="$(jq -r '.usage.input_tokens + .usage.cache_creation_input_tokens + .usage.cache_read_input_tokens' "$json" 2>/dev/null || echo 0)"
  output_tok="$(jq -r '.usage.output_tokens' "$json" 2>/dev/null || echo 0)"
  cost="$(jq -r '.total_cost_usd' "$json" 2>/dev/null || echo 0)"
  echo "cross-review: claude review of $SHA written to $out (tokens: ${input_tok:-0} in / ${output_tok:-0} out, cost: \$${cost:-0})" >&2
  notify "Claude reviewed $SHA" "$SUBJECT"
  open_review "$out"
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
