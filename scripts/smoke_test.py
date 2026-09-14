"""Offline smoke by default; explicit live schema/write verification, never an email send."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-sheet", action="store_true")
    parser.add_argument("--verify-write", action="store_true")
    args = parser.parse_args()
    if args.verify_write and not args.live_sheet:
        raise SystemExit("VERIFY_WRITE_REQUIRES_LIVE_SHEET")
    commands = [
        ["uv", "run", "career-radar", "scan", "--dry-run", "--offline-fixtures"],
        [
            "uv",
            "run",
            "career-radar",
            "digest",
            "--period",
            "morning",
            "--no-send",
            "--offline-fixtures",
        ],
    ]
    if args.live_sheet:
        commands = [["uv", "run", "career-radar", "sheets", "verify"]]
        if args.verify_write:
            commands.append(
                ["uv", "run", "career-radar", "doctor", "--require", "scheduled", "--verify-write"]
            )
    for command in commands:
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], check=False)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
