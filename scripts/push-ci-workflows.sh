#!/usr/bin/env bash
# GitHub refuses a push that adds .github/workflows/* unless the token carries the `workflow`
# scope, which the gh login on this machine lacks. The CI commit therefore sits on a local
# branch. Run this once to grant the scope, push the branch and open its pull request.
set -euo pipefail
cd "$(dirname "$0")/.."
branch="${1:-chore/ci-workflows}"
git rev-parse -q --verify "$branch" >/dev/null || { echo "no local branch $branch" >&2; exit 1; }
if ! gh auth status 2>&1 | grep -q "workflow"; then
  gh auth refresh -h github.com -s workflow
fi
git push -u origin "$branch"
gh pr create --base main --head "$branch" --title "ci: add test and release workflows" \
  --body "Adds ci.yml (ruff + pytest on pushes and PRs) and release.yml (build and attach artifacts when a v* tag is pushed)." || true
