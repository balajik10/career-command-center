"""Capability-aware Gmail setup plan and read-only token verification."""

from __future__ import annotations

import argparse
import json

from career_radar.integrations.google import gmail_access_token
from career_radar.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--purpose", choices=["read", "send", "both"], default="send")
    args = parser.parse_args()
    if not args.verify:
        print(
            json.dumps(
                {
                    "status": "PLAN",
                    "send": "Separate Desktop OAuth client; authorize with bootstrap_gmail_oauth.py --purpose send --authorize --save-private",
                    "read": "Live ingestion deferred; use owned .eml imports. Optional separate read grant is token validation only.",
                    "mailbox": "Use a dedicated mailbox; label filtering is not an OAuth access boundary.",
                    "local_app_password": "Local SMTP self-notifications require an attested dedicated mailbox. IMAP ingestion is deferred; never upload an app password to GitHub.",
                    "consent": "Testing-mode grants are demo-only. Attest In-production consent only after verifying the Google project configuration.",
                },
                sort_keys=True,
            )
        )
        return
    settings = Settings()
    purposes = ["read", "send"] if args.purpose == "both" else [args.purpose]
    results = {}
    for purpose in purposes:
        try:
            gmail_access_token(settings, purpose)
        except Exception:
            results[purpose] = "AUTH_SCOPE_OR_IDENTITY_FAILURE"
        else:
            results[purpose] = (
                "TOKEN_VERIFIED_INGESTION_DEFERRED"
                if purpose == "read"
                else "TOKEN_VERIFIED_NO_MAIL_SENT"
            )
    print(json.dumps(results, sort_keys=True))
    if any(value == "AUTH_SCOPE_OR_IDENTITY_FAILURE" for value in results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
