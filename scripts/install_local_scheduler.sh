#!/usr/bin/env bash
# Print/install a private local launchd/cron alternative after GitHub scheduling is disabled.
set -euo pipefail
mode="${1:---plan}"
if [[ "$mode" != '--plan' && "$mode" != '--apply' ]]; then
  echo 'Usage: SCHEDULER=local GITHUB_SCHEDULE_DISABLED_ATTESTED=true ./scripts/install_local_scheduler.sh --plan|--apply' >&2
  exit 2
fi
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(dirname "$script_dir")"
if [[ "$mode" == '--apply' && ( "${SCHEDULER:-disabled}" != 'local' || "${GITHUB_SCHEDULE_DISABLED_ATTESTED:-false}" != 'true' ) ]]; then
  echo 'Refusing local scheduling until SCHEDULER=local and GitHub schedule disablement is attested' >&2
  exit 2
fi
# Generate a shell-quoted launcher, keeping all credentials in a private environment file.
export CAREER_SCHEDULER_PROJECT_DIR="$project_dir"
export CAREER_SCHEDULER_INSTALL_MODE="$mode"
uv run python - <<'PY'
import os, pathlib, shlex, subprocess
project = pathlib.Path(os.environ['CAREER_SCHEDULER_PROJECT_DIR'])
apply = os.environ['CAREER_SCHEDULER_INSTALL_MODE'] == '--apply'
launcher = project / '.private' / 'local-scheduler.sh'
python = project / '.venv' / 'bin' / 'python'
# A local single writer checks schedule due times every minute; flock is available on Linux.
# macOS users use launchd with this same dispatcher and its advisory fcntl lock.
command = f'* * * * * {shlex.quote(str(python))} {shlex.quote(str(project / "scripts" / "local_dispatch.py"))} >/dev/null 2>&1'
if not apply:
    print('Plan: one local minute dispatcher; timezone and due-slot idempotency handled by Python.')
    print(command)
else:
    if not (project / 'scripts' / 'local_dispatch.py').exists():
        raise SystemExit('SETUP_REQUIRED: local_dispatch.py missing')
    current = subprocess.run(['crontab', '-l'], capture_output=True, text=True, check=False)
    if current.returncode not in (0, 1):
        raise SystemExit('Cannot inspect existing crontab safely')
    lines = current.stdout.splitlines()
    marker = '# career-command-center local dispatcher'
    if marker not in lines:
        subprocess.run(['crontab', '-'], input=current.stdout.rstrip() + '\n' + marker + '\n' + command + '\n', text=True, check=True)
    print('Local dispatcher installed without replacing unrelated crontab entries.')
PY
