"""Parse real workflow cron and timeout limits across every Gregorian calendar layout."""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from career_radar.orchestration.planner import SCHEDULES


def estimate(root: Path = Path(".")) -> dict[str, int]:
    caller: dict[str, Any] = yaml.safe_load(
        (root / "runtime-template/.github/workflows/scheduled.yml").read_text()
    )
    runtime: dict[str, Any] = yaml.safe_load((root / ".github/workflows/runtime.yml").read_text())
    entries = caller["on"]["schedule"]
    expected = {row[0] for row in SCHEDULES.values()}
    if {row["cron"] for row in entries} != expected or len(entries) != len(expected):
        raise ValueError("SCHEDULE_DRIFT_REQUIRES_REVIEW")
    if any(row.get("timezone") != "Asia/Kolkata" for row in entries):
        raise ValueError("EXACTLY_ONE_IST_SCHEDULE_SET_REQUIRED")
    timeout = runtime["jobs"]["execute"]["timeout-minutes"]
    expected_expression = "${{ inputs.mode == 'full' && 10 || inputs.mode == 'maintenance' && 8 || (inputs.mode == 'morning' || inputs.mode == 'evening') && 3 || 4 }}"
    if timeout != expected_expression:
        raise ValueError("TIMEOUT_DRIFT_REQUIRES_REVIEW")
    if len(runtime["jobs"]) != 1 or len(caller["jobs"]) != 1:
        raise ValueError("ONE_RUNTIME_JOB_REQUIRED")
    limits = {"incremental": 4, "full": 10, "morning": 3, "evening": 3, "maintenance": 8}
    mode_by_cron = {row[0]: row[2] for row in SCHEDULES.values()}
    worst = 0
    for year in range(2000, 2400):
        for month in range(1, 13):
            total = 0
            for day in range(1, calendar.monthrange(year, month)[1] + 1):
                weekday = date(year, month, day).isoweekday() % 7
                for entry in entries:
                    _, hour, _, _, weekdays = entry["cron"].split()
                    allowed = (
                        set(range(1, 6))
                        if weekdays == "1-5"
                        else {0, 6}
                        if weekdays == "0,6"
                        else {0}
                        if weekdays == "0"
                        else set(range(7))
                    )
                    if weekday in allowed:
                        total += len(hour.split(",")) * limits[mode_by_cron[entry["cron"]]]
            worst = max(worst, total)
    uses = caller["jobs"]["run"]["uses"]
    sha = caller["jobs"]["run"]["with"]["implementation_sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or uses.rsplit("@", 1)[-1] != sha:
        raise ValueError("IMPLEMENTATION_SHA_MISMATCH")
    result = {
        "scheduled_minutes": worst,
        "manual_reserve_minutes": 200,
        "planned_minutes": worst + 200,
        "public_ci_allowance": 300,
    }
    if worst > 1200 or result["planned_minutes"] > 1600:
        raise ValueError("ACTIONS_BUDGET_EXCEEDS_ZERO_COST_PLAN")
    return result


def main() -> None:
    print(json.dumps(estimate(), sort_keys=True))


if __name__ == "__main__":
    main()
