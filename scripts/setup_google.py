"""Plan private Sheets setup or inspect its schema without mutating data."""

from __future__ import annotations

import argparse
import json

from career_radar.runtime import live_book
from career_radar.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if not args.verify:
        print(
            json.dumps(
                {
                    "status": "PLAN",
                    "steps": [
                        "Create a new blank private Google Sheet in your own account.",
                        "Enable the standard Sheets API in your own Google project; no broad Drive scope or paid service is needed.",
                        "Prefer keyless GitHub WIF: run bootstrap_gcp_wif.sh --plan then --apply after reviewing exact private repo/main trust.",
                        "Share only the target Sheet with the Sheets service identity.",
                        "Set GOOGLE_SHEET_ID privately; use ADC/WIF or an explicitly chosen base64 service-account fallback.",
                        "Run career-radar bootstrap-sheet, then career-radar doctor --require scheduled --verify-write before scheduling.",
                    ],
                    "google_scope": "https://www.googleapis.com/auth/spreadsheets",
                    "gmail": "WIF/service accounts are Sheets-only credentials.",
                },
                sort_keys=True,
            )
        )
        return
    try:
        book = live_book(Settings())
        errors = book.verify()
    except Exception:
        print(json.dumps({"status": "AUTH_OR_SHEET_UNAVAILABLE", "mutation": False}))
        raise SystemExit(1) from None
    print(
        json.dumps(
            {
                "status": "SCHEMA_VERIFIED" if not errors else "SCHEMA_REPAIR_REQUIRED",
                "error_count": len(errors),
                "mutation": False,
            },
            sort_keys=True,
        )
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
