#!/usr/bin/env bash
# Installs the post-commit cross-review hook. Run once after cloning.
# .git/hooks/ is never tracked by git, so this stub has to be (re)installed manually.
set -euo pipefail

HOOKS_DIR="$(git rev-parse --git-path hooks)"
HOOK="$HOOKS_DIR/post-commit"
MARKER="Installed by scripts/install-hooks.sh"

if [ -e "$HOOK" ] && ! grep -qF "$MARKER" "$HOOK"; then
  echo "error: $HOOK already exists and wasn't installed by this script." >&2
  echo "It would be overwritten and its existing automation lost. Move it aside or merge" >&2
  echo "scripts/cross-review.sh into it by hand, then re-run this installer." >&2
  exit 1
fi

cat > "$HOOK" <<EOF
#!/usr/bin/env bash
# $MARKER — do not edit directly, edit scripts/cross-review.sh instead.
REPO_ROOT="\$(git rev-parse --show-toplevel)"
SHA="\$(git rev-parse HEAD)"
nohup "\$REPO_ROOT/scripts/cross-review.sh" "\$SHA" >/dev/null 2>&1 &
disown
EOF

chmod +x "$HOOK"
echo "Installed post-commit hook -> $HOOK"
