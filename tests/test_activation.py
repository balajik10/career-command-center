"""Private activation tooling must parse secrets as data and fail before mutation."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from test_schedule import script, workflow


def test_installer_explicit_files_precedence_literal_values_and_no_leak(tmp_path, monkeypatch):
    installer = script("install_github_secrets")
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text('GOOGLE_SHEET_ID="first"\nGMAIL_ADDRESS="candidate@example.com"\n')
    literal = "literal$(do-not-execute)`also-not-executed`${NO_EXPANSION}"
    second.write_text(
        'GOOGLE_SHEET_ID="second"\nGMAIL_SEND_CLIENT_SECRET=' + json.dumps(literal) + "\n"
    )
    monkeypatch.setenv("GOOGLE_SHEET_ID", "process-wins")
    with patch.dict(installer.os.environ, {"GOOGLE_SHEET_ID": "process-wins"}, clear=True):
        env = installer.configuration([first, second])
        assert env["GOOGLE_SHEET_ID"] == "process-wins"
        assert env["GMAIL_SEND_CLIENT_SECRET"] == literal
        with patch.object(installer, "run_gh") as gh:
            plan = installer.install(
                "balajik10/career-command-center-runtime", env_files=[first, second]
            )
            gh.assert_not_called()
        assert plan["secret_count"] == 3 and plan["variable_count"] == 0
        assert literal not in json.dumps(plan) and "candidate@example.com" not in json.dumps(plan)
    with patch.dict(installer.os.environ, {}, clear=True):
        assert installer.configuration([first, second])["GOOGLE_SHEET_ID"] == "second"


@pytest.mark.parametrize("body", ['BROKEN="unterminated', "NO_VALUE", "bad-key=x"])
def test_installer_malformed_files_refuse_before_github(tmp_path, body):
    installer = script("install_github_secrets")
    path = tmp_path / "invalid.env"
    path.write_text(body)
    with (
        patch.object(installer, "run_gh") as gh,
        pytest.raises(SystemExit, match="ENV_FILE_UNREADABLE_OR_MALFORMED"),
    ):
        installer.install("balajik10/career-command-center-runtime", apply=True, env_files=[path])
    gh.assert_not_called()


def test_installer_missing_file_empty_apply_and_private_app_password_refuse(tmp_path):
    installer = script("install_github_secrets")
    with pytest.raises(SystemExit, match="ENV_FILE_UNREADABLE_OR_MALFORMED"):
        installer.configuration([tmp_path / "absent"])
    with patch.dict(installer.os.environ, {}, clear=True), patch.object(installer, "run_gh") as gh:
        assert installer.install("balajik10/career-command-center-runtime")["secret_count"] == 0
        with pytest.raises(SystemExit, match="EMPTY_CONFIGURATION_REFUSING_APPLY"):
            installer.install("balajik10/career-command-center-runtime", apply=True)
        path = tmp_path / "private.env"
        path.write_text('GMAIL_APP_PASSWORD="synthetic-only"\n')
        with pytest.raises(SystemExit, match="APP_PASSWORD_LOCAL_ONLY"):
            installer.install(
                "balajik10/career-command-center-runtime", apply=True, env_files=[path]
            )
        gh.assert_not_called()


def test_installer_apply_uses_named_values_from_files_only_via_stdin(tmp_path):
    installer = script("install_github_secrets")
    path = tmp_path / "private.env"
    path.write_text('GOOGLE_SHEET_ID="synthetic-sheet"\nSHEETS_AUTH_MODE=wif\n')
    calls = []

    def gh(args, value=None):
        calls.append((args, value))
        return "balajik10" if args[1] == "user" else "true" if args[0] == "api" else ""

    with patch.dict(installer.os.environ, {}, clear=True), patch.object(installer, "run_gh", gh):
        result = installer.install(
            "balajik10/career-command-center-runtime", apply=True, env_files=[path]
        )
    assert result["secret_count"] == result["variable_count"] == 1
    assert any(
        args[:3] == ["secret", "set", "GOOGLE_SHEET_ID"] and value == "synthetic-sheet"
        for args, value in calls
    )
    assert all("synthetic-sheet" not in " ".join(args) for args, _ in calls)
    assert calls[2][0][:3] == ["variable", "set", "TRACKER_ENABLED"] and calls[2][1] == "false"


def test_manual_doctor_workflow_is_explicit_and_unscheduled():
    caller = workflow("runtime-template/.github/workflows/scheduled.yml")
    runtime = workflow(".github/workflows/runtime.yml")
    inputs = caller["on"]["workflow_dispatch"]["inputs"]
    assert "doctor" in inputs["mode"]["options"]
    assert inputs["verify_write"]["default"] is False
    assert "workflow_dispatch" in caller["jobs"]["run"]["with"]["verify_write"]
    assert runtime["on"]["workflow_call"]["inputs"]["verify_write"]["default"] is False
    assert all("doctor" not in row["cron"] for row in caller["on"]["schedule"])
    assert "private == true" in runtime["jobs"]["execute"]["if"]
