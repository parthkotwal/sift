#!/usr/bin/env bash
# Installs the post-commit cross-review hook. Run once after cloning.
# .git/hooks/ is never tracked by git, so this stub has to be (re)installed manually.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK="$REPO_ROOT/.git/hooks/post-commit"

cat > "$HOOK" <<'EOF'
#!/usr/bin/env bash
# Installed by scripts/install-hooks.sh — do not edit directly, edit scripts/cross-review.sh instead.
REPO_ROOT="$(git rev-parse --show-toplevel)"
SHA="$(git rev-parse HEAD)"
nohup "$REPO_ROOT/scripts/cross-review.sh" "$SHA" >/dev/null 2>&1 &
disown
EOF

chmod +x "$HOOK"
echo "Installed post-commit hook -> $HOOK"
