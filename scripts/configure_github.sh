#!/usr/bin/env bash
# Secrets remain in the environment and are piped to gh; no plaintext temporary files.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run python "$script_dir/install_github_secrets.py" "$@"
