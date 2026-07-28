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
# Each reviewer runs inside its own throwaway `git worktree` checked out at
# <sha> (detached HEAD, deleted when the review finishes). This is what makes
# it safe to run while another agent is actively editing this repo: whatever
# commands the reviewer decides to run -- grep, pytest, ls, whatever -- see
# only the frozen, historical state of the reviewed commit, never your live
# working tree. Neither reviewer is given Write/Edit either, so even inside
# its own disposable copy it can only read and report, never modify.
#
# Every run opens the resulting .md (macOS `open`, default handler) and
# fires a notification. In the automatic (hook-triggered) path, a successful
# review is also handed straight to apply-review.sh -- so review AND fix
# happen unattended. It still never commits: you get a notification, the
# working tree has the (uncommitted) fix, and you inspect with `git diff`
# before committing. Manual `scripts/cross-review.sh <sha> <reviewer>` runs
# don't auto-apply, since an old commit's fix may no longer make sense
# against the current tree -- run apply-review.sh yourself if you want it.
#
# Toggle automatic (hook-triggered) review on/off without touching the hook
# -- e.g. if you just don't want the notification/cost right now:
#   git config sift.cross-review-enabled false   # turn off
#   git config --unset sift.cross-review-enabled  # back to default (on)
# This only gates the hook's auto-detect path -- an explicit
# `scripts/cross-review.sh <sha> <reviewer>` always runs regardless.
set -euo pipefail

FORCE_REVIEWER="${2:-}"
if [ -z "$FORCE_REVIEWER" ] && [ "$(git config --bool sift.cross-review-enabled 2>/dev/null || echo true)" = "false" ]; then
  exit 0
fi

SHA="${1:-$(git rev-parse HEAD)}"
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

auto_apply() {
  local reviewer="$1"
  local log="$REVIEW_DIR/${SHA}-${reviewer}-apply.log"
  if "$REPO_ROOT/scripts/apply-review.sh" "$SHA" "$reviewer" > "$log" 2>&1; then
    notify "Fix applied for $SHA" "Inspect with git diff, then commit yourself"
    echo "cross-review: auto-applied $reviewer's review of $SHA — see $log, then git diff" >&2
  else
    echo "cross-review: auto-apply skipped for $SHA (see $log) — dirty tree, missing CLI, or nothing to apply" >&2
  fi
}

make_worktree() {
  local dir
  dir="$(mktemp -d "${TMPDIR:-/tmp}/sift-review-${SHA}.XXXXXX")"
  rmdir "$dir"  # git worktree add wants to create the leaf dir itself
  git worktree add --detach --quiet "$dir" "$SHA" >&2
  echo "$dir"
}

cleanup_worktree() {
  git worktree remove --force "$1" >/dev/null 2>&1 || rm -rf "$1"
}

run_codex_review() {
  local out="$REVIEW_DIR/${SHA}-codex.md"
  local jsonl="$REVIEW_DIR/${SHA}-codex.jsonl"
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v codex >/dev/null 2>&1; then
    echo "cross-review: codex CLI not found, skipping review of $SHA" >&2
    return 1
  fi
  local worktree
  worktree="$(make_worktree)"
  trap 'cleanup_worktree "$worktree"' RETURN

  codex exec -C "$worktree" review --commit "$SHA" --title "$SUBJECT" -o "$out" \
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
  local worktree
  worktree="$(make_worktree)"
  trap 'cleanup_worktree "$worktree"' RETURN

  # Isolated to its own worktree now, so this can safely have full Bash/Read
  # (to grep, run tests, etc., matching what Codex's review does) -- only
  # Write/Edit stay blocked, since a review should never modify, even its
  # own disposable copy.
  (cd "$worktree" && claude -p "Review the changes introduced by commit $SHA in this git repository (subject: \"$SUBJECT\"). Run 'git show $SHA' to see the full diff; feel free to run tests or grep around this checkout for context. Write a concise review: a 1-2 sentence summary, then specific, actionable findings with file:line references, ranked most severe first. If nothing significant, say so briefly. Do not modify any files." \
    --model sonnet \
    --permission-mode dontAsk \
    --disallowedTools "Write Edit" \
    --output-format json) \
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
    # Commits that only touch this cross-review tooling itself aren't worth
    # auto-reviewing — force a reviewer explicitly (see usage above) if you
    # ever want one anyway.
    CHANGED_FILES="$(git diff-tree --no-commit-id --name-only -r "$SHA")"
    if [ -n "$CHANGED_FILES" ] && ! grep -qv '^scripts/' <<<"$CHANGED_FILES"; then
      exit 0
    fi
    TRAILERS="$(git log -1 --format=%B "$SHA" | git interpret-trailers --parse)"
    if echo "$TRAILERS" | grep -qi "^Co-Authored-By: *Claude"; then
      run_codex_review && auto_apply codex
    elif echo "$TRAILERS" | grep -qi "^Co-Authored-By: *Codex"; then
      run_claude_review && auto_apply claude
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
