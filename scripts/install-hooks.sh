#!/usr/bin/env bash
# Installs the cross-review git hook. Run once after cloning (and once more
# after upgrading from the old per-commit version of this tooling).
# .git/hooks/ is never tracked by git, so this has to be (re)installed manually.
set -euo pipefail

HOOKS_DIR="$(git rev-parse --git-path hooks)"
MARKER="Installed by scripts/install-hooks.sh"

# Remove hooks from the old per-commit design, but only ones we installed.
for stale in post-commit prepare-commit-msg; do
  hook="$HOOKS_DIR/$stale"
  if [ -e "$hook" ] && grep -qF "$MARKER" "$hook"; then
    rm -f "$hook"
    echo "Removed obsolete $stale hook (superseded by pre-push)"
  fi
done

HOOK="$HOOKS_DIR/pre-push"
if [ -e "$HOOK" ] && ! grep -qF "$MARKER" "$HOOK"; then
  echo "error: $HOOK already exists and wasn't installed by this script." >&2
  echo "It would be overwritten and its existing automation lost. Move it aside or merge" >&2
  echo "the intended behavior into it by hand, then re-run this installer." >&2
  exit 1
fi

# pre-push gets "<local ref> <local sha1> <remote ref> <remote sha1>" lines on
# stdin, one per pushed ref. Backgrounds the review and exits 0 immediately --
# it never delays the push itself.
cat > "$HOOK" <<EOF
#!/usr/bin/env bash
# $MARKER — do not edit directly, edit scripts/cross-review.sh instead.
REPO_ROOT="\$(git rev-parse --show-toplevel)"
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "\$local_sha" = "0000000000000000000000000000000000000000" ] && continue  # deleting a ref
  if [ "\$remote_sha" = "0000000000000000000000000000000000000000" ]; then
    old_sha=""  # new branch, no previous remote tip to diff against
  else
    old_sha="\$remote_sha"
  fi
  nohup "\$REPO_ROOT/scripts/cross-review.sh" "\$local_sha" "" "\$old_sha" >/dev/null 2>&1 &
  disown
done
exit 0
EOF

chmod +x "$HOOK"
echo "Installed pre-push hook -> $HOOK"
