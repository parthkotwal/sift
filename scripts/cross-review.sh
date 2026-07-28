#!/usr/bin/env bash
# Cross-review: whichever agent didn't (mostly) author a pushed range of
# commits reviews the whole range in one shot.
#
# Automatic use (triggered by .git/hooks/pre-push, installed via
# scripts/install-hooks.sh): backgrounds itself and returns immediately, so
# it never delays `git push`. Picks the reviewer from the trailer on the tip
# commit being pushed (see .agents/AGENTS.md, "Working style") and reviews
# everything between the previous remote tip and the new one. No trailer on
# the tip commit -> skipped.
#
# Manual use: review any range (or single commit) with a reviewer of choice.
#   scripts/cross-review.sh <new-sha> codex [<old-sha>]   # Codex reviews <old-sha>..<new-sha>
#   scripts/cross-review.sh <new-sha> claude [<old-sha>]  # Claude reviews <old-sha>..<new-sha>
#   scripts/cross-review.sh <new-sha>                     # auto-detect via <new-sha>'s trailer
#   scripts/cross-review.sh                               # auto-detect on HEAD, single commit
# <old-sha> defaults to <new-sha>^ (i.e. just that one commit) when omitted.
#
# The reviewer runs inside its own throwaway `git worktree` checked out at
# <new-sha> (detached HEAD, deleted when the review finishes) -- isolated
# from your live working tree, so it's safe to run while you keep working.
# Neither reviewer is given Write/Edit, so it can only read and report.
#
# Review-only: this never edits anything. Output lands in
# .agents/reviews/<new-sha>-<reviewer>.md plus a notification. Run
# scripts/apply-review.sh yourself if you want the findings applied.
#
# Toggle on/off without touching the hook:
#   git config sift.cross-review-enabled false   # turn off
#   git config --unset sift.cross-review-enabled  # back to default (on)
# This only gates the hook's auto-detect path -- an explicit
# `scripts/cross-review.sh <sha> <reviewer>` always runs regardless.
set -euo pipefail

FORCE_REVIEWER="${2:-}"
if [ -z "$FORCE_REVIEWER" ] && [ "$(git config --bool sift.cross-review-enabled 2>/dev/null || echo true)" = "false" ]; then
  exit 0
fi

NEW_SHA="${1:-$(git rev-parse HEAD)}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
REVIEW_DIR="$REPO_ROOT/.agents/reviews"
mkdir -p "$REVIEW_DIR"

if ! git rev-parse --verify --quiet "${NEW_SHA}^{commit}" >/dev/null; then
  echo "cross-review: '$NEW_SHA' is not a commit in this repo" >&2
  exit 1
fi
NEW_SHA="$(git rev-parse "$NEW_SHA")"
OLD_SHA="${3:-$(git rev-parse "${NEW_SHA}^" 2>/dev/null || echo "")}"
SUBJECT="$(git log -1 --format=%s "$NEW_SHA")"
[ -n "$OLD_SHA" ] && [ "$OLD_SHA" != "$NEW_SHA" ] || OLD_SHA=""

notify() {
  osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv)' -e 'end run' \
    -- "$1" "$2" >/dev/null 2>&1 || true
}

make_worktree() {
  local dir
  dir="$(mktemp -d "${TMPDIR:-/tmp}/sift-review-${NEW_SHA}.XXXXXX")"
  rmdir "$dir"  # git worktree add wants to create the leaf dir itself
  git worktree add --detach --quiet "$dir" "$NEW_SHA" >&2
  echo "$dir"
}

cleanup_worktree() {
  git worktree remove --force "$1" >/dev/null 2>&1 || rm -rf "$1"
}

run_codex_review() {
  local out="$REVIEW_DIR/${NEW_SHA}-codex.md"
  local jsonl="$REVIEW_DIR/${NEW_SHA}-codex.jsonl"
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v codex >/dev/null 2>&1; then
    echo "cross-review: codex CLI not found, skipping review of $NEW_SHA" >&2
    return 1
  fi
  local worktree
  worktree="$(make_worktree)"
  trap 'cleanup_worktree "$worktree"' RETURN

  if [ -n "$OLD_SHA" ]; then
    codex exec -C "$worktree" review --base "$OLD_SHA" --title "$SUBJECT" -o "$out" \
      -c model_reasoning_effort="medium" --json \
      > "$jsonl" 2>"$REVIEW_DIR/${NEW_SHA}-codex.log"
  else
    codex exec -C "$worktree" review --commit "$NEW_SHA" --title "$SUBJECT" -o "$out" \
      -c model_reasoning_effort="medium" --json \
      > "$jsonl" 2>"$REVIEW_DIR/${NEW_SHA}-codex.log"
  fi

  local usage tokens
  usage="$(jq -c 'select(.type=="turn.completed") | .usage' "$jsonl" 2>/dev/null | tail -1)"
  tokens="$(jq -r '(.input_tokens // 0) + (.output_tokens // 0)' <<<"${usage:-null}" 2>/dev/null || echo 0)"
  if [ -z "${usage:-}" ] || [ "$usage" = "null" ] || [ "$tokens" = "0" ]; then
    echo "cross-review: codex review of $NEW_SHA written to $out (tokens: n/a — codex-cli's review subcommand doesn't report usage)" >&2
  else
    echo "cross-review: codex review of $NEW_SHA written to $out (tokens: $usage)" >&2
  fi
  notify "Codex reviewed ${NEW_SHA:0:7}" "$SUBJECT"
}

run_claude_review() {
  local out="$REVIEW_DIR/${NEW_SHA}-claude.md"
  local json="$REVIEW_DIR/${NEW_SHA}-claude.json"
  if ! command -v claude >/dev/null 2>&1; then
    echo "cross-review: claude CLI not found, skipping review of $NEW_SHA" >&2
    return 1
  fi
  local worktree
  worktree="$(make_worktree)"
  trap 'cleanup_worktree "$worktree"' RETURN

  local range_desc
  if [ -n "$OLD_SHA" ]; then
    range_desc="the range $OLD_SHA..$NEW_SHA (run 'git diff $OLD_SHA $NEW_SHA' and 'git log $OLD_SHA..$NEW_SHA' to see it)"
  else
    range_desc="commit $NEW_SHA (run 'git show $NEW_SHA' to see it)"
  fi

  (cd "$worktree" && claude -p "Review the changes in $range_desc, in this git repository (subject: \"$SUBJECT\"). Feel free to run tests or grep around this checkout for context. Write a concise review: a 1-2 sentence summary, then specific, actionable findings with file:line references, ranked most severe first. If nothing significant, say so briefly. Do not modify any files." \
    --model sonnet \
    --permission-mode dontAsk \
    --disallowedTools "Write Edit" \
    --output-format json) \
    > "$json" 2>"$REVIEW_DIR/${NEW_SHA}-claude.log"

  jq -r '.result // "(no result — see .claude.log)"' "$json" > "$out" 2>/dev/null \
    || echo "(failed to parse claude output — see $json)" > "$out"

  local input_tok output_tok cost
  input_tok="$(jq -r '.usage.input_tokens + .usage.cache_creation_input_tokens + .usage.cache_read_input_tokens' "$json" 2>/dev/null || echo 0)"
  output_tok="$(jq -r '.usage.output_tokens' "$json" 2>/dev/null || echo 0)"
  cost="$(jq -r '.total_cost_usd' "$json" 2>/dev/null || echo 0)"
  echo "cross-review: claude review of $NEW_SHA written to $out (tokens: ${input_tok:-0} in / ${output_tok:-0} out, cost: \$${cost:-0})" >&2
  notify "Claude reviewed ${NEW_SHA:0:7}" "$SUBJECT"
}

case "$FORCE_REVIEWER" in
  codex)
    run_codex_review
    ;;
  claude)
    run_claude_review
    ;;
  "")
    # A push that only touches this cross-review tooling itself isn't worth
    # auto-reviewing — force a reviewer explicitly (see usage above) if you
    # ever want one anyway.
    CHANGED_FILES="$(git diff-tree --no-commit-id --name-only -r "${OLD_SHA:-$NEW_SHA^}" "$NEW_SHA" 2>/dev/null || git diff-tree --no-commit-id --name-only -r "$NEW_SHA")"
    if [ -n "$CHANGED_FILES" ] && ! grep -qv '^scripts/' <<<"$CHANGED_FILES"; then
      exit 0
    fi
    TRAILERS="$(git log -1 --format=%B "$NEW_SHA" | git interpret-trailers --parse)"
    if echo "$TRAILERS" | grep -qi "^Co-Authored-By: *Claude"; then
      run_codex_review
    elif echo "$TRAILERS" | grep -qi "^Co-Authored-By: *Codex"; then
      run_claude_review
    else
      # Tip commit not authored by either agent — nothing to do.
      exit 0
    fi
    ;;
  *)
    echo "cross-review: unknown reviewer '$FORCE_REVIEWER' (use 'codex' or 'claude')" >&2
    exit 1
    ;;
esac
