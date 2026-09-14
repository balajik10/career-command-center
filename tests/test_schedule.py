"""Schedule translation, bounded workflow structure, and installation guard contracts."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml
from croniter import croniter

from career_radar.orchestration.planner import (
    BUDGETS,
    SCHEDULES,
    maximum_monthly_budget,
    monthly_budget,
    route_schedule,
    scheduler_ready,
)

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    specification = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def workflow(path):
    return yaml.safe_load((ROOT / path).read_text())


def occurrences(cron, start, end):
    iterator = croniter(cron, start)
    result = []
    while True:
        current = iterator.get_next(datetime)
        if current >= end:
            return result
        result.append(current.astimezone(UTC))


@pytest.mark.parametrize("row", list(SCHEDULES.values()))
def test_ist_utc_equivalence_for_every_2026_occurrence(row):
    ist, utc, mode, _ = row
    start = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
    end = datetime(2027, 1, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert occurrences(ist, start, end) == occurrences(
        utc, start.astimezone(UTC), end.astimezone(UTC)
    )
    assert route_schedule(ist) == route_schedule(utc) == mode


def test_routing_weekend_sunday_rollover_and_invalid_mode():
    sunday = datetime(2026, 9, 13, 3, 43, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert sunday.astimezone(UTC).weekday() == 5
    assert croniter.match(SCHEDULES["maintenance"][0], sunday)
    assert croniter.match(SCHEDULES["maintenance"][1], sunday.astimezone(UTC))
    with pytest.raises(ValueError, match="UNKNOWN"):
        route_schedule("* * * * *")
    assert scheduler_ready("github", True)
    assert scheduler_ready("local", False)
    assert not scheduler_ready("local", True)
    assert not scheduler_ready("disabled", False)
    assert BUDGETS["incremental"].requests == 80 and BUDGETS["full"].requests == 500


def test_monthly_calendar_valid_cost_and_workflow_drift(tmp_path):
    assert monthly_budget(2026, 2)["scheduled_minutes"] < 1176
    expected = {
        "scheduled_minutes": 1176,
        "manual_reserve_minutes": 200,
        "planned_minutes": 1376,
        "public_ci_allowance": 300,
    }
    assert maximum_monthly_budget() == expected
    budget = script("estimate_actions_budget")
    assert budget.estimate(ROOT) == expected
    paths = ["runtime-template/.github/workflows/scheduled.yml", ".github/workflows/runtime.yml"]
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / path).read_text())
    target = tmp_path / paths[1]
    target.write_text(target.read_text().replace("== 'full' && 10", "== 'full' && 12"))
    with pytest.raises(ValueError, match="TIMEOUT_DRIFT"):
        budget.estimate(tmp_path)
    target.write_text((ROOT / paths[1]).read_text())
    target = tmp_path / paths[0]
    target.write_text(target.read_text().replace("timezone: Asia/Kolkata", "timezone: UTC"))
    with pytest.raises(ValueError, match="SCHEDULE_SET"):
        budget.estimate(tmp_path)


def test_workflow_ownership_sha_secrets_and_one_queued_writer():
    runtime = workflow(".github/workflows/runtime.yml")
    caller = workflow("runtime-template/.github/workflows/scheduled.yml")
    assert set(runtime["on"]) == {"workflow_call"}
    assert caller["concurrency"] == {"group": "career-command-center-production", "queue": "max"}
    assert "concurrency" not in runtime
    assert len(caller["jobs"]) == len(runtime["jobs"]) == 1
    job = caller["jobs"]["run"]
    assert "timeout-minutes" not in job and "runs-on" not in job
    sha = job["with"]["implementation_sha"]
    assert re.fullmatch("[0-9a-f]{40}", sha) and job["uses"].endswith("@" + sha)
    assert isinstance(job["secrets"], dict) and "inherit" not in job["secrets"]
    assert "private == true" in job["if"]
    runtime_job = runtime["jobs"]["execute"]
    assert "refs/heads/main" in runtime_job["if"] and "private == true" in runtime_job["if"]
    checkout = next(
        step
        for step in runtime_job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["repository"] == "balajik10/career-command-center"
    assert checkout["with"]["ref"] == "${{ inputs.implementation_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert any("git rev-parse HEAD" in step.get("run", "") for step in runtime_job["steps"])
    assert all("${{ inputs." not in step.get("run", "") for step in runtime_job["steps"])
    assert not any("upload-artifact" in step.get("uses", "") for step in runtime_job["steps"])
    assert "GMAIL_APP_PASSWORD" not in json.dumps(runtime)
    assert runtime["permissions"] == {"contents": "read", "id-token": "write"}
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        data = yaml.safe_load(path.read_text())
        assert "schedule" not in data["on"]
        for task in data["jobs"].values():
            for step in task.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])


def test_secret_installer_plan_never_calls_github_and_rejects_app_password(monkeypatch):
    installer = script("install_github_secrets")
    with patch.object(installer, "run_gh") as gh:
        plan = installer.install("balajik10/career-command-center-runtime")
        assert plan["tracker_enabled"] is False
        gh.assert_not_called()
    for repo in ["attacker/runtime", "balajik10/public"]:
        with pytest.raises(SystemExit):
            installer.install(repo)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "synthetic-do-not-upload")
    with pytest.raises(SystemExit, match="APP_PASSWORD_LOCAL_ONLY"):
        installer.install("balajik10/career-command-center-runtime", apply=True)


def test_secret_installer_only_private_correct_owner_stdin(monkeypatch):
    installer = script("install_github_secrets")
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("GMAIL_AUTH_MODE", "oauth")
    monkeypatch.setenv("ALERT_RECIPIENT_EMAIL", "candidate@example.com")
    with (
        patch.object(installer, "run_gh", side_effect=["other-user"]),
        pytest.raises(SystemExit, match="account"),
    ):
        installer.install("balajik10/career-command-center-runtime", apply=True)
    with (
        patch.object(installer, "run_gh", side_effect=["balajik10", "false"]),
        pytest.raises(SystemExit, match="private"),
    ):
        installer.install("balajik10/career-command-center-runtime", apply=True)
    calls = []

    def run(args, value=None):
        calls.append((args, value))
        return "balajik10" if args[1] == "user" else "true" if args[0] == "api" else ""

    with patch.object(installer, "run_gh", side_effect=run):
        installer.install("balajik10/career-command-center-runtime", apply=True)
    secret_calls = [(args, value) for args, value in calls if args[:2] == ["secret", "set"]]
    assert any(
        args[2] == "ALERT_RECIPIENT_EMAIL" and value == "candidate@example.com"
        for args, value in secret_calls
    )
    assert all("candidate@example.com" not in " ".join(args) for args, _ in calls)
    with (
        patch.object(
            installer.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="sensitive"),
        ),
        pytest.raises(SystemExit, match="command failed"),
    ):
        installer.run_gh(["secret", "set", "X"], "value")


def test_local_due_slots_and_mutual_exclusion():
    local = script("local_dispatch")
    for ist, _, _, _ in SCHEDULES.values():
        iterator = croniter(ist, datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Kolkata")))
        for _ in range(7):
            now = iterator.get_next(datetime)
            assert local.matches(ist, now)
            assert not local.matches(ist, now.replace(minute=(now.minute + 1) % 60))
    with patch.object(
        local, "Settings", return_value=SimpleNamespace(scheduler="github", tracker_enabled=True)
    ):
        local.main()
    installer = ROOT / "scripts/install_local_scheduler.sh"
    result = subprocess.run(
        ["bash", str(installer), "--apply"],
        env={"PATH": "/usr/bin:/bin", "SCHEDULER": "github"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2 and "Refusing local scheduling" in result.stderr


def test_setup_shell_syntax_and_wif_readonly_plan():
    scripts = ["bootstrap_gcp_wif.sh", "configure_github.sh", "install_local_scheduler.sh"]
    for name in scripts:
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)
    wif = ROOT / "scripts/bootstrap_gcp_wif.sh"
    result = subprocess.run(
        ["bash", str(wif), "--plan"],
        env={"PATH": "/usr/bin:/bin", "GCP_PROJECT_ID": "example-project"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "refs/heads/main" in result.stdout and "Plan:" in result.stdout
    body = wif.read_text()
    assert "assertion.repository" in body and "assertion.ref" in body
    assert "ref:refs/heads/main" in body and "roles/iam.workloadIdentityUser" in body
    assert "Existing provider trust differs" in body
    assert " delete " not in body
