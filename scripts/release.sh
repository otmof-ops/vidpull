#!/usr/bin/env bash
# Release helper. Two phases, because every commit goes through a pull request:
#
#   scripts/release.sh prepare X.Y.Z   bump pyproject, move CHANGELOG [Unreleased] into [X.Y.Z],
#                                      commit on release/vX.Y.Z, push, open the PR
#   scripts/release.sh tag X.Y.Z       after that PR merged: fast-forward main, assert version and
#                                      changelog, create the annotated tag vX.Y.Z, push it, and
#                                      create the GitHub release with the changelog section
set -euo pipefail
cd "$(dirname "$0")/.."

cmd="${1:-}"
ver="${2:-}"
[[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || { echo "usage: $0 prepare|tag X.Y.Z" >&2; exit 2; }
tag="v$ver"
today="$(date -u +%Y-%m-%d)"

die() { echo "error: $*" >&2; exit 1; }
need_clean() { [ -z "$(git status --porcelain)" ] || die "working tree is not clean"; }
pyproject_version() { python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'; }
changelog_section() { awk -v h="## [$1]" 'index($0, h) == 1 {f = 1; next} /^## \[/ {f = 0} f' CHANGELOG.md; }

case "$cmd" in
  prepare)
    need_clean
    [ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "run prepare from main"
    git fetch -q origin
    [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "main is not in sync with origin/main"
    if git ls-remote --heads origin 'release/v*' | grep -q .; then die "a release/v* branch already exists on origin; finish that release first"; fi
    git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
    grep -q '^## \[Unreleased\]' CHANGELOG.md || die "CHANGELOG.md has no [Unreleased] section"
    python3 - "$ver" "$today" <<'PY'
import pathlib
import re
import sys

ver, today = sys.argv[1], sys.argv[2]
pyproject = pathlib.Path("pyproject.toml")
text, n = re.subn(r'^version = "[^"]+"$', f'version = "{ver}"', pyproject.read_text(), count=1, flags=re.M)
assert n == 1, "version line not found in pyproject.toml"
pyproject.write_text(text)

changelog = pathlib.Path("CHANGELOG.md")
head, sep, rest = changelog.read_text().partition("## [Unreleased]")
assert sep
match = re.search(r"\n## \[", rest)
section = rest[: match.start()] if match else rest
remainder = rest[match.start():] if match else ""
if not section.strip():
    sys.exit("CHANGELOG.md [Unreleased] is empty; nothing to release")
changelog.write_text(f"{head}## [Unreleased]\n\n## [{ver}] - {today}{section.rstrip()}\n{remainder}")
PY
    git checkout -q -b "release/$tag"
    git add pyproject.toml CHANGELOG.md
    git commit -q -m "release: $tag"
    git push -q -u origin "release/$tag"
    gh pr create --base main --head "release/$tag" --title "release: $tag" \
      --body "Bumps the version to $ver and files the CHANGELOG section. After merging, run: scripts/release.sh tag $ver"
    echo "prepared $tag; merge the PR, then run: scripts/release.sh tag $ver"
    ;;
  tag)
    need_clean
    git checkout -q main
    git fetch -q origin
    git merge -q --ff-only origin/main
    [ "$(pyproject_version)" = "$ver" ] || die "pyproject.toml says $(pyproject_version), not $ver (merge the release PR first)"
    grep -q "^## \[$ver\] - " CHANGELOG.md || die "CHANGELOG.md has no [$ver] section"
    git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists locally"
    git ls-remote --tags origin "refs/tags/$tag" | grep -q . && die "tag $tag already exists on origin"
    sha="$(git rev-parse HEAD)"
    if command -v gh >/dev/null; then
      state="$(gh api "repos/{owner}/{repo}/commits/$sha/status" -q .state 2>/dev/null || echo unknown)"
      case "$state" in success|unknown|pending) ;; *) die "CI status for $sha is '$state'" ;; esac
    fi
    notes="$(changelog_section "$ver")"
    [ -n "$notes" ] || die "empty changelog section for $ver"
    git tag -a "$tag" -m "vidpull $tag" -m "$notes"
    [ "$(git cat-file -t "$tag")" = tag ] || die "tag is not annotated"
    git push -q origin "$tag"
    gh release create "$tag" --title "vidpull $tag" --notes "$notes"
    echo "released $tag at $sha"
    ;;
  *)
    echo "usage: $0 prepare|tag X.Y.Z" >&2
    exit 2
    ;;
esac
