#!/usr/bin/env bash
# Commit data/ and push with pull --rebase retries (safe under concurrent Action runs).
set -euo pipefail
MSG=${1:-"chore: update data"}

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add data/
if git diff --staged --quiet; then
  echo "No data changes"
  exit 0
fi

git commit -m "${MSG} $(date -u +%Y-%m-%dT%H:%MZ)"

REMOTE="${CI_COMMIT_REMOTE:-origin}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
MAX_ATTEMPTS=5
attempt=1
while true; do
  if git push "$REMOTE" "HEAD:refs/heads/${BRANCH}"; then
    echo "Pushed successfully on attempt ${attempt}"
    exit 0
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "Push failed after ${MAX_ATTEMPTS} attempts" >&2
    exit 1
  fi
  echo "Push rejected (attempt ${attempt}/${MAX_ATTEMPTS}); fetching and rebasing onto ${REMOTE}/${BRANCH}..."
  git fetch "$REMOTE" "$BRANCH"
  if ! git rebase "${REMOTE}/${BRANCH}"; then
    echo "Rebase failed; aborting to avoid leaving conflict markers" >&2
    git rebase --abort 2>/dev/null || true
    exit 1
  fi
  attempt=$((attempt + 1))
done
