"""Explicit, stdin-only private runtime secret installation. Never enables scheduling."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from dotenv.parser import parse_stream

SECRET_NAMES = (
    "GOOGLE_SHEET_ID",
    "WIF_PROVIDER",
    "WIF_SERVICE_ACCOUNT",
    "GOOGLE_SERVICE_ACCOUNT_JSON_B64",
    "PRIVATE_PROFILE_YAML_B64",
    "PRIVACY_HMAC_KEY",
    "ALERT_RECIPIENT_EMAIL",
    "GMAIL_ADDRESS",
    "GMAIL_READ_CLIENT_ID",
    "GMAIL_READ_CLIENT_SECRET",
    "GMAIL_READ_REFRESH_TOKEN",
    "GMAIL_SEND_CLIENT_ID",
    "GMAIL_SEND_CLIENT_SECRET",
    "GMAIL_SEND_REFRESH_TOKEN",
)
VARIABLE_NAMES = (
    "APP_TIMEZONE",
    "PRIVACY_HMAC_KEY_VERSION",
    "GMAIL_OAUTH_IN_PRODUCTION",
    "GMAIL_LABEL",
    "GMAIL_SENDER_ALLOWLIST",
    "BUDGET_VERIFIED_AT",
    "INCLUDED_PRIVATE_MINUTES_REMAINING",
    "PAID_OVERAGE_DISABLED",
    "ACCOUNT_RESERVE_MINUTES",
    "SHEETS_AUTH_MODE",
)


def run_gh(args: list[str], value: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=value, capture_output=True, text=True, check=False)
    if result.returncode:
        raise SystemExit(
            "GitHub command failed; inspect credentials and repository access manually"
        )
    return result.stdout.strip()


def configuration(env_files: list[Path]) -> dict[str, str]:
    """Parse explicit dotenv files as data; never expand variables or execute shell."""
    result: dict[str, str] = {}
    for path in env_files:
        try:
            with path.open(encoding="utf-8") as stream:
                for binding in parse_stream(stream):
                    if binding.error:
                        raise ValueError("MALFORMED_ENV_FILE")
                    if binding.key is not None:
                        if (
                            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", binding.key)
                            or binding.value is None
                        ):
                            raise ValueError("MALFORMED_ENV_FILE")
                        result[binding.key] = binding.value
        except (OSError, UnicodeError, ValueError):
            raise SystemExit("ENV_FILE_UNREADABLE_OR_MALFORMED") from None
    return result | dict(os.environ)


def install(
    repo: str, *, apply: bool = False, env_files: list[Path] | None = None
) -> dict[str, object]:
    if not re.fullmatch(r"balajik10/career-command-center-runtime(?:-[a-z0-9-]+)?", repo):
        raise SystemExit("Expected a narrowly named private runtime repository under balajik10")
    environment = configuration(env_files or [])
    if (
        environment.get("GMAIL_APP_PASSWORD")
        or environment.get("GMAIL_AUTH_MODE") == "app_password"
    ):
        raise SystemExit("APP_PASSWORD_LOCAL_ONLY: cloud upload rejected")
    values = {name: environment[name] for name in SECRET_NAMES if environment.get(name)}
    variables = {name: environment[name] for name in VARIABLE_NAMES if environment.get(name)}
    summary: dict[str, object] = {
        "mode": "apply" if apply else "plan",
        "secret_count": len(values),
        "variable_count": len(variables),
        "tracker_enabled": False,
        "scheduler": "disabled",
    }
    if not apply:
        return summary
    if not values and not variables:
        raise SystemExit("EMPTY_CONFIGURATION_REFUSING_APPLY")
    if run_gh(["api", "user", "--jq", ".login"]) != "balajik10":
        raise SystemExit("Authenticated GitHub account does not match expected owner")
    if run_gh(["api", f"repos/{repo}", "--jq", ".private"]) != "true":
        raise SystemExit("Runtime repository is not verified private")
    # Disable before making any partial credential/configuration updates.
    run_gh(["variable", "set", "TRACKER_ENABLED", "--repo", repo], "false")
    run_gh(["variable", "set", "SCHEDULER", "--repo", repo], "disabled")
    for name, value in values.items():
        run_gh(["secret", "set", name, "--repo", repo], value)
    for name, value in variables.items():
        run_gh(["variable", "set", name, "--repo", repo], value)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[],
        help="Private dotenv file; repeatable. Later files override earlier files; process environment wins.",
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--apply", action="store_true")
    choice.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(args.repo, apply=args.apply, env_files=args.env_file), sort_keys=True))


if __name__ == "__main__":
    main()
