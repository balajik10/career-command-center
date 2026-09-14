"""One local cron dispatcher with a nonblocking advisory lock and durable slot claims."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from career_radar.orchestration.planner import SCHEDULES
from career_radar.settings import Settings


def matches(cron: str, now: datetime) -> bool:
    minute, hours, _, _, weekdays = cron.split()
    weekday = now.isoweekday() % 7
    allowed = (
        range(1, 6)
        if weekdays == "1-5"
        else {0, 6}
        if weekdays == "0,6"
        else {0}
        if weekdays == "0"
        else range(7)
    )
    return (
        now.minute == int(minute)
        and now.hour in {int(hour) for hour in hours.split(",")}
        and weekday in allowed
    )


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    os.chdir(project)
    settings = Settings()
    if settings.scheduler != "local" or not settings.tracker_enabled:
        return
    folder = project / ".private"
    folder.mkdir(exist_ok=True, mode=0o700)
    with (folder / "local-scheduler.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        ledger = folder / "local-scheduler-slots.json"
        state = json.loads(ledger.read_text()) if ledger.exists() else {}
        for name, (cron, _, mode, timeout_minutes) in SCHEDULES.items():
            slot = now.strftime("%Y-%m-%dT%H:%M%z") + ":" + name
            if not matches(cron, now) or slot in state:
                continue
            state[slot] = "CLAIMED"
            # Keep a bounded, non-personal slot ledger. A failed/ambiguous invocation is not retried.
            state = dict(sorted(state.items())[-1000:])
            temporary = ledger.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, sort_keys=True))
            temporary.replace(ledger)
            env = os.environ | {
                "INPUT_MODE": mode,
                "INPUT_DRY_RUN": "false",
                "INPUT_SEND_ALERTS": "true",
                "INPUT_SOURCE": "",
                "SCHEDULER": "local",
                "GITHUB_ACTIONS": "false",
            }
            try:
                run = subprocess.run(
                    [str(Path(sys.executable).with_name("career-radar")), "run-scheduled"],
                    env=env,
                    check=False,
                    timeout=timeout_minutes * 60,
                )
                state[slot] = "SUCCEEDED" if run.returncode == 0 else "FAILED"
            except subprocess.TimeoutExpired:
                state[slot] = "TIMEOUT_REVIEW_REQUIRED"
            temporary.write_text(json.dumps(state, sort_keys=True))
            temporary.replace(ledger)


if __name__ == "__main__":
    main()
