"""Prove installed CLI behavior with no credentials and socket networking denied."""

from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any


def deny_network(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("OFFLINE_COMMAND_ATTEMPTED_NETWORK")


def main() -> None:
    # This is a standalone disposable process, never imported by a live runtime.
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/contacts_synthetic.csv"
    os.environ.clear()
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.getaddrinfo = deny_network
    socket.create_connection = deny_network

    from typer.testing import CliRunner

    import career_radar
    from career_radar.cli import app

    runner = CliRunner()
    commands = [
        ["doctor", "--json"],
        ["bootstrap-sheet", "--offline"],
        ["scan", "--dry-run", "--offline-fixtures"],
        ["digest", "--period", "morning", "--no-send"],
        ["source", "check", "--all"],
        ["demo"],
    ]
    with tempfile.TemporaryDirectory(prefix="career-offline-") as directory:
        os.chdir(directory)
        for command in commands:
            result = runner.invoke(app, command)
            if result.exit_code:
                raise AssertionError(
                    "OFFLINE_COMMAND_FAILED: " + " ".join(command)
                ) from result.exception
            if command[0] == "demo":
                data = json.loads(result.stdout)
                assert data["status"] == "PASS"
                assert data["first"]["canonical_jobs"] == 9
                assert all(
                    data["identical_rerun"][field] == 0
                    for field in ["new_jobs", "new_drafts", "alerts"]
                )
        # A synthetic HMAC key configures local contact identifiers only; no account grant.
        os.environ["PRIVACY_HMAC_KEY"] = "test" * 8
        command = ["import", "contacts", str(fixture), "--format", "linkedin-connections"]
        result = runner.invoke(app, command)
        if result.exit_code:
            raise AssertionError("SYNTHETIC_CONTACT_IMPORT_FAILED") from result.exception
        assert "STAGED_PRIVATE" in result.stdout
    print(
        json.dumps(
            {
                "status": "PASS",
                "cli_commands": len(commands) + 1,
                "network": "DENIED_BY_SOCKET_GUARD",
                "account_credentials": "ABSENT",
                "installed_package": str(Path(career_radar.__file__).resolve()),
            }
        )
    )


if __name__ == "__main__":
    main()
