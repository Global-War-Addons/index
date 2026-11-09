#!/usr/bin/env bash
# commit.sh "<message>" <path>...: commits the paths if they changed and pushes; on a race with another workflow
# rebases and pushes again. Nothing changed - no commit (the ETag of index.json stays, 11-catalog-distribution.md 4.2).
set -euo pipefail
msg="$1"
shift
branch="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
paths=()
for p in "$@"; do
  # a folder that does not exist yet (no addon published) is no error
  if [ -e "$p" ] || git ls-files --error-unmatch -- "$p" > /dev/null 2>&1; then paths+=("$p"); fi
done
if [ ${#paths[@]} -gt 0 ]; then git add -A -- "${paths[@]}"; fi
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
# the identity is needed by the rebase below too (it writes the commit again): the runner has no git user configured
ident=(-c user.name="${BOT_NAME:-gw-catalog[bot]}" -c user.email="${BOT_EMAIL:-gw-catalog-bot@users.noreply.github.com}")
git "${ident[@]}" commit -q -m "$msg"
for attempt in 1 2 3 4 5; do
  if git push -q origin "HEAD:${branch}"; then
    echo "pushed: $msg"
    exit 0
  fi
  echo "push failed (attempt $attempt): rebasing"
  if ! git "${ident[@]}" pull -q --rebase origin "$branch"; then
    git rebase --abort || true
    echo "::error::a rebase conflict with another run: the next run repeats the work"
    exit 1
  fi
done
echo "::error::push failed 5 times"
exit 1
