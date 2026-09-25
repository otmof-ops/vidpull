#!/usr/bin/env bash
# vidpull installer.
#
#   ./install.sh                 install the newest release tag from GitHub
#   ./install.sh --ref v0.2.0    install a specific release tag
#   ./install.sh --from .        install this checkout (untagged commits allowed)
#   ./install.sh --uninstall     remove vidpull (forwards flags to `vidpull uninstall`)
#
# Layout (XDG): $XDG_DATA_HOME/vidpull/gen/<NNNN-tag>/venv is one generation,
# $XDG_DATA_HOME/vidpull/current points at the live one, ~/.local/bin/vidpull runs it.
# A failed install never touches an existing generation.
set -euo pipefail

REPO_URL="${VIDPULL_REPO_URL:-https://github.com/otmof-ops/vidpull}"
PYTHON_BIN="${PYTHON:-python3}"
REF=""
FROM_DIR=""
NO_DENO=0
NO_LAUNCHER=0
UNINSTALL=0
PASSTHRU=()

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --from) FROM_DIR="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --no-deno) NO_DENO=1; shift ;;
    --no-launcher) NO_LAUNCHER=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) PASSTHRU+=("$1"); shift ;;
  esac
done

xdg_dir() { # $1 variable name, $2 default relative to HOME; empty or relative values mean unset
  local value="${!1:-}"
  case "$value" in /*) printf '%s' "$value" ;; *) printf '%s/%s' "$HOME" "$2" ;; esac
}
DATA_DIR="$(xdg_dir XDG_DATA_HOME .local/share)/vidpull"
GEN_ROOT="$DATA_DIR/gen"
BUILD_ROOT="$DATA_DIR/build"
CURRENT="$DATA_DIR/current"
LAUNCHER="$HOME/.local/bin/vidpull"

if [ "$UNINSTALL" = 1 ]; then
  if [ -x "$CURRENT/bin/vidpull" ]; then
    exec "$CURRENT/bin/vidpull" uninstall "${PASSTHRU[@]}"
  fi
  say "no installed generation found; offering the standard directories one by one"
  for dir in "$(xdg_dir XDG_CONFIG_HOME .config)/vidpull" "$(xdg_dir XDG_STATE_HOME .local/state)/vidpull" \
             "$(xdg_dir XDG_CACHE_HOME .cache)/vidpull" "$DATA_DIR"; do
    [ -e "$dir" ] || continue
    if [ "$(basename "$dir")" != vidpull ] || [ -L "$dir" ]; then echo "skip $dir (not a plain vidpull directory)"; continue; fi
    printf 'remove %s? [y/N] ' "$dir"; read -r answer
    [ "$answer" = y ] && rm -rf -- "$dir" && echo "removed $dir"
  done
  if [ -f "$LAUNCHER" ] && grep -q 'vidpull launcher' "$LAUNCHER"; then rm -f -- "$LAUNCHER"; echo "removed $LAUNCHER"; fi
  exit 0
fi

command -v git >/dev/null || die "git is required"
command -v "$PYTHON_BIN" >/dev/null || die "$PYTHON_BIN not found (set PYTHON=/path/to/python3.12)"
"$PYTHON_BIN" - <<'PY' || die "Python 3.12+ with the venv module is required (apt install python3-venv)"
import sys
import venv  # noqa: F401
sys.exit(0 if sys.version_info >= (3, 12) else 1)
PY
command -v ffmpeg >/dev/null || say "warning: ffmpeg not found; merging and audio conversion need it (apt install ffmpeg)"
PYTHON_PATH="$(command -v "$PYTHON_BIN")"

mkdir -p "$GEN_ROOT" "$BUILD_ROOT"
BUILD_DIR=""
GEN_DIR=""
cleanup() { [ -n "$BUILD_DIR" ] && rm -rf -- "$BUILD_DIR"; return 0; }
on_error() {
  cleanup
  [ -n "$GEN_DIR" ] && rm -rf -- "$GEN_DIR"
  die "install failed; any existing generation is untouched"
}
trap on_error ERR

if [ -n "$FROM_DIR" ]; then
  SRC="$(cd "$FROM_DIR" && pwd)"
  [ -f "$SRC/pyproject.toml" ] || die "$SRC is not a vidpull checkout"
  VERSION="$("$PYTHON_BIN" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$SRC/pyproject.toml")"
  COMMIT="$(git -C "$SRC" rev-parse --verify -q HEAD 2>/dev/null | tr -d '[:space:]' || true)"
  [ -n "$COMMIT" ] || COMMIT=unknown
  TAG="$(git -C "$SRC" describe --tags --exact-match 2>/dev/null | tr -d '[:space:]' || true)"
  [ -n "$TAG" ] || TAG="v${VERSION}+${COMMIT:0:12}"
  say "installing from checkout $SRC as $TAG"
else
  if [ -z "$REF" ]; then
    say "looking up the newest release of $REPO_URL"
    # only annotated tags (they list a peeled ^{} entry) are releases
    REF="$(GIT_TERMINAL_PROMPT=0 git ls-remote --tags "$REPO_URL" \
      | sed -n 's#.*refs/tags/\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)^{}$#\1#p' | sort -V | tail -1)"
    [ -n "$REF" ] || die "no release tags on $REPO_URL yet; use --ref TAG or --from DIR"
  fi
  TAG="$REF"
  BUILD_DIR="$BUILD_ROOT/src-install-$$"
  say "fetching $TAG"
  GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --branch "$TAG" --single-branch "$REPO_URL" "$BUILD_DIR"
  SRC="$BUILD_DIR"
  COMMIT="$(git -C "$SRC" rev-parse --verify HEAD | tr -d '[:space:]')"
fi
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.+-]+)?$ ]] || die "unusable tag name: $TAG"

LAST="$(ls "$GEN_ROOT" 2>/dev/null | sed -n 's/^\([0-9]\{4\}\)-.*/\1/p' | sort -n | tail -1)"
SEQ=$((10#${LAST:-0} + 1))
GEN_ID="$(printf '%04d-%s' "$SEQ" "$TAG")"
GEN_DIR="$GEN_ROOT/$GEN_ID"
VENV="$GEN_DIR/venv"

say "building generation $GEN_ID"
"$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check "$SRC"
say "installing the latest yt-dlp"
if [ "$NO_DENO" = 1 ] || ! "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade 'yt-dlp[default,deno]'; then
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade 'yt-dlp[default]'
fi

say "verifying the generation and switching current"
FINALIZE=("$VENV/bin/vidpull" _finalize-install --gen-dir "$GEN_DIR" --tag "$TAG" --commit "$COMMIT" --python "$PYTHON_PATH" --repo-url "$REPO_URL")
[ "$NO_LAUNCHER" = 1 ] && FINALIZE+=(--no-launcher)
"${FINALIZE[@]}"
trap - ERR
cleanup

say "installed vidpull $TAG"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: add \$HOME/.local/bin to your PATH to run 'vidpull' directly" ;;
esac
echo "next: vidpull doctor"
