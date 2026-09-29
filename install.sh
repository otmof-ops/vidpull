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
ASSUME_YES=0
PASSTHRU=()

usage() {
  cat <<'USAGE'
vidpull installer

  ./install.sh                 install the newest release
  ./install.sh --ref v0.2.0    install a specific release tag
  ./install.sh --from .        install this checkout (untagged commits allowed)
  ./install.sh --uninstall     remove vidpull (forwards flags to `vidpull uninstall`)

Options: --yes (install missing git/python/ffmpeg without asking)   --repo URL
         --python PATH   --no-deno   --no-launcher
USAGE
}
ffmpeg_hint() {
  if command -v apt-get >/dev/null; then echo "sudo apt install ffmpeg"
  elif command -v dnf >/dev/null; then echo "sudo dnf install ffmpeg"
  elif command -v pacman >/dev/null; then echo "sudo pacman -S ffmpeg"
  elif command -v zypper >/dev/null; then echo "sudo zypper install ffmpeg"
  elif command -v brew >/dev/null; then echo "brew install ffmpeg"
  else echo "install ffmpeg with your package manager"; fi
}
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
    -y|--yes) ASSUME_YES=1; shift ;;
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

# ---- preflight: say what is missing, what would be installed and from where; install on yes
pm_detect() {
  if command -v apt-get >/dev/null; then PM=apt; PM_CMD="apt-get install -y"
  elif command -v dnf >/dev/null; then PM=dnf; PM_CMD="dnf install -y"
  elif command -v pacman >/dev/null; then PM=pacman; PM_CMD="pacman -S --noconfirm --needed"
  elif command -v zypper >/dev/null; then PM=zypper; PM_CMD="zypper --non-interactive install"
  elif command -v brew >/dev/null; then PM=brew; PM_CMD="brew install"
  else PM=""; PM_CMD=""; fi
  SUDO=""
  if [ -n "$PM" ] && [ "$PM" != brew ] && [ "$(id -u)" != 0 ]; then SUDO="sudo"; fi
}
python_ok() { command -v "$1" >/dev/null && "$1" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; }
pkg_for() { # $1 = git|python|ffmpeg -> package name(s) for the detected manager
  case "$1:$PM" in
    git:*) echo git ;;
    ffmpeg:*) echo ffmpeg ;;
    python:apt) if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then echo python3-venv; else echo "python3.12 python3.12-venv"; fi ;;
    python:dnf) echo python3.12 ;;
    python:pacman) echo python ;;
    python:zypper) echo python312 ;;
    python:brew) echo python@3.12 ;;
  esac
}
pm_detect
missing=()
command -v git >/dev/null || missing+=(git)
if ! python_ok "$PYTHON_BIN"; then
  if python_ok python3.12; then PYTHON_BIN=python3.12; else missing+=(python); fi
fi
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
if [ ${#missing[@]} -gt 0 ]; then
  echo
  echo "vidpull needs a few things this machine does not have yet:"
  for item in "${missing[@]}"; do
    case "$item" in
      git) why="fetches releases and updates" ;;
      python) why="Python 3.12+ with the venv module runs vidpull" ;;
      ffmpeg) why="merges video with audio and converts to MP3, Opus, FLAC and WAV" ;;
    esac
    if [ -n "$PM" ]; then
      printf '  %-8s %s\n' "$item" "$why"
      printf '  %-8s -> %s %s %s   (%s package%s)\n' "" "$SUDO" "$PM_CMD" "$(pkg_for "$item")" "$PM" "${SUDO:+, asks for your password}"
    else
      printf '  %-8s %s (no package manager found; install it yourself)\n' "$item" "$why"
    fi
  done
  echo
  [ -n "$PM" ] || die "install the items above and run this again"
  if [ "$ASSUME_YES" = 1 ]; then answer=y
  elif [ -t 0 ]; then printf 'Download and install them now? [Y/n] '; read -r answer; answer="${answer:-y}"
  else die "not running in a terminal; rerun with --yes to install them, or install them yourself"; fi
  case "$answer" in
    y|Y|yes|YES) ;;
    *) die "cannot continue without them" ;;
  esac
  pkgs=()
  for item in "${missing[@]}"; do read -r -a parts <<<"$(pkg_for "$item")"; pkgs+=("${parts[@]}"); done
  say "installing: ${pkgs[*]}"
  # shellcheck disable=SC2086
  $SUDO $PM_CMD "${pkgs[@]}" || die "package installation failed; see the output above"
  command -v git >/dev/null || die "git is still missing"
  python_ok "$PYTHON_BIN" || { python_ok python3.12 && PYTHON_BIN=python3.12; } || die "Python 3.12+ with venv is still missing (on Ubuntu 22.04: add the deadsnakes PPA, then apt install python3.12 python3.12-venv)"
  command -v ffmpeg >/dev/null || die "ffmpeg is still missing"
  say "dependencies installed"
fi
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
  # tracked files changed since that commit: this is neither the release nor the commit
  if [ -n "$(git -C "$SRC" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    TAG="v${VERSION}+${COMMIT:0:12}.dirty"
  fi
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
  *":$HOME/.local/bin:"*)
    echo "run:  vidpull            (or: vidpull <link>)"
    ;;
  *)
    echo "add this line to your shell profile (~/.bashrc or ~/.zshrc), open a new terminal, then run vidpull:"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    ;;
esac
