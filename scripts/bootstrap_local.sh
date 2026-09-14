#!/usr/bin/env bash
# Install the committed lock and verify the complete offline path without credentials.
set -euo pipefail
mode="${1:---plan}"
if [[ "$mode" != '--plan' && "$mode" != '--apply' ]]; then
  echo 'Usage: ./scripts/bootstrap_local.sh --plan|--apply' >&2
  exit 2
fi
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(dirname "$script_dir")"
if [[ "$mode" == '--plan' ]]; then
  echo 'Plan: uv sync --locked; doctor without live checks; offline fixture scan; no-send digest.'
  echo 'Normal package-index access is needed only for installation. Existing private files will be preserved.'
  exit 0
fi
command -v uv >/dev/null || { echo 'SETUP_REQUIRED: install uv from its official installer or package manager' >&2; exit 2; }
cd "$project_dir"
uv sync --locked
uv run python -c 'import json; from career_radar.runtime import readiness; from career_radar.settings import Settings; print(json.dumps(readiness(Settings(), verify_live=False), sort_keys=True))'
uv run career-radar scan --dry-run --offline-fixtures
uv run career-radar digest --period morning --no-send --offline-fixtures
