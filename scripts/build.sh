#!/usr/bin/env bash
# Build the sdist and wheel into dist/. Used locally and by the release workflow.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf dist build ./*.egg-info
python3 -m build
ls -l dist
