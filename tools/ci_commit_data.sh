#!/usr/bin/env bash
# Commit data/ changes and push with pull --rebase retries for concurrent Action runs.
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

# Concurrent collect/backfill may have advanced origin/main; rebase and retry push.
MAX_ATTEMPTS=5
attempt=1
while true; do
  if git push origin HEAD; then
    echo "Pushed successfully"
    exit 0
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "Push failed after ${MAX_ATTEMPTS} attempts" >&2
    exit 1
  fi
  echo "Push rejected (attempt ${attempt}); pulling with rebase..."
  git pull --rebase origin "$(git rev-parse --abbrev-ref HEAD)" || {
    echo "Rebase failed; aborting" >&2
    git rebase --abort 2>/dev/null || true
    exit 1
  }
  attempt=$((attempt + 1))
done
