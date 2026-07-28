#!/usr/bin/env bash
# Installs the cross-review git hooks. Run once after cloning.
# .git/hooks/ is never tracked by git, so these stubs have to be (re)installed manually.
set -euo pipefail

HOOKS_DIR="$(git rev-parse --git-path hooks)"
MARKER="Installed by scripts/install-hooks.sh"

install_hook() {
  local name="$1" body="$2"
  local hook="$HOOKS_DIR/$name"
  if [ -e "$hook" ] && ! grep -qF "$MARKER" "$hook"; then
    echo "error: $hook already exists and wasn't installed by this script." >&2
    echo "It would be overwritten and its existing automation lost. Move it aside or merge" >&2
    echo "the intended behavior into it by hand, then re-run this installer." >&2
    exit 1
  fi
  printf '%s\n' "$body" > "$hook"
  chmod +x "$hook"
  echo "Installed $name hook -> $hook"
}

install_hook "post-commit" "$(cat <<EOF
#!/usr/bin/env bash
# $MARKER — do not edit directly, edit scripts/cross-review.sh instead.
REPO_ROOT="\$(git rev-parse --show-toplevel)"
SHA="\$(git rev-parse HEAD)"
nohup "\$REPO_ROOT/scripts/cross-review.sh" "\$SHA" >/dev/null 2>&1 &
disown
EOF
)"

# Stamps the authoring Claude Code session's id onto the commit as a trailer
# (when CLAUDE_CODE_SESSION_ID is set, i.e. this commit was made from inside
# an active `claude` session) so a later automated fix can be applied back
# into that same session via `claude --resume --fork-session` instead of a
# stateless new one. No Codex equivalent exists yet -- see apply-review.sh.
install_hook "prepare-commit-msg" "$(cat <<EOF
#!/usr/bin/env bash
# $MARKER — do not edit directly, edit scripts/install-hooks.sh instead.
if [ -n "\${CLAUDE_CODE_SESSION_ID:-}" ]; then
  git interpret-trailers --if-exists doNothing \\
    --trailer "Claude-Session-Id: \${CLAUDE_CODE_SESSION_ID}" \\
    --in-place "\$1"
fi
EOF
)"
