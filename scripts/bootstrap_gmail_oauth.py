"""User-driven Google desktop OAuth with PKCE; never sends mail or reads messages."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
Purpose = Literal["read", "send"]


def scopes(purpose: Purpose) -> set[str]:
    return {
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail." + ("readonly" if purpose == "read" else "send"),
    }


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


def authorization_url(
    client_id: str, redirect_uri: str, state: str, challenge: str, purpose: Purpose
) -> str:
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(sorted(scopes(purpose))),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "false",
            }
        )
    )


def validate_callback(path: str, state: str) -> str:
    parsed = urlsplit(path)
    if parsed.path != "/oauth/callback" or len(path) > 16_384:
        raise ValueError("OAUTH_CALLBACK_PATH_INVALID")
    query = parse_qs(parsed.query, keep_blank_values=True)
    received = query.get("state", [])
    if len(received) != 1 or not hmac.compare_digest(received[0], state):
        raise ValueError("OAUTH_STATE_MISMATCH")
    if "error" in query:
        raise ValueError("OAUTH_CONSENT_DENIED")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise ValueError("OAUTH_CODE_MISSING")
    return codes[0]


def callback_handler(state: str, result: dict[str, str]) -> type[BaseHTTPRequestHandler]:
    class Callback(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)
            self.callback_deadline = threading.Timer(5, self.expire_connection)
            self.callback_deadline.daemon = True
            self.callback_deadline.start()

        def expire_connection(self) -> None:
            with suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)

        def finish(self) -> None:
            self.callback_deadline.cancel()
            super().finish()

        def do_GET(self) -> None:
            try:
                code = validate_callback(self.path, state)
            except ValueError as exc:
                if str(exc) == "OAUTH_CONSENT_DENIED":
                    result["error"] = "OAUTH_CONSENT_DENIED"
                self.send_response(400)
                text = "Authorization callback rejected. Return to the setup terminal."
            else:
                result["code"] = code
                self.send_response(200)
                text = "Authorization received. Return to the setup terminal for verification."
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(text.encode())))
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, format: str, *args: Any) -> None:
            # Callback paths contain the one-time authorization code; never log them.
            return

    return Callback


def exchange(
    code: str,
    verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    expected_email: str,
    purpose: Purpose,
    client: httpx.Client,
) -> dict[str, str]:
    response = client.post(
        TOKEN_URL,
        data={
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
        },
    )
    if response.status_code != 200:
        raise ValueError("OAUTH_EXCHANGE_FAILED")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("OAUTH_RESPONSE_INVALID")
    actual = {
        "email" if item == "https://www.googleapis.com/auth/userinfo.email" else item
        for item in str(payload.get("scope", "")).split()
    }
    if actual != scopes(purpose):
        raise ValueError("OAUTH_SCOPE_MISMATCH")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise ValueError("OAUTH_REFRESH_TOKEN_REQUIRED")
    identity = client.get(USERINFO_URL, headers={"Authorization": "Bearer " + access})
    if identity.status_code != 200:
        raise ValueError("OAUTH_IDENTITY_UNVERIFIED")
    claims = identity.json()
    expected = expected_email.strip().lower()
    if (
        not isinstance(claims, dict)
        or claims.get("email_verified") is not True
        or str(claims.get("email", "")).strip().lower() != expected
    ):
        raise ValueError("OAUTH_ACCOUNT_MISMATCH")
    prefix = "GMAIL_" + purpose.upper() + "_"
    return {
        prefix + "CLIENT_ID": client_id,
        prefix + "CLIENT_SECRET": client_secret,
        prefix + "REFRESH_TOKEN": refresh,
        "GMAIL_ADDRESS": expected,
        "GMAIL_AUTH_MODE": "oauth",
    }


def save_private(values: dict[str, str], purpose: Purpose, directory: Path) -> Path:
    if directory.name != ".private" or directory.is_symlink():
        raise ValueError("SECRETS_REQUIRE_PRIVATE_DIRECTORY")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"gmail-{purpose}.env"
    # Exclusive creation prevents overwriting a working grant or following a symlink.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(
            "\n".join(name + "=" + json.dumps(value) for name, value in sorted(values.items()))
            + "\n"
        )
    return path


def github_save(values: dict[str, str], repo: str) -> None:
    if not re.fullmatch(r"balajik10/career-command-center-runtime(?:-[a-z0-9-]+)?", repo):
        raise ValueError("EXPECTED_PRIVATE_RUNTIME_REPOSITORY")

    def gh(args: list[str], value: str | None = None) -> str:
        result = subprocess.run(
            ["gh", *args], input=value, text=True, capture_output=True, check=False
        )
        if result.returncode:
            raise ValueError("GITHUB_SECRET_INSTALL_FAILED")
        return result.stdout.strip()

    if (
        gh(["api", "user", "--jq", ".login"]) != "balajik10"
        or gh(["api", f"repos/{repo}", "--jq", ".private"]) != "true"
    ):
        raise ValueError("GITHUB_OWNER_OR_PRIVACY_MISMATCH")
    gh(["variable", "set", "TRACKER_ENABLED", "--repo", repo], "false")
    for name, value in values.items():
        if name != "GMAIL_AUTH_MODE":
            gh(["secret", "set", name, "--repo", repo], value)


def authorize(
    purpose: Purpose, *, save_to_file: bool, repo: str | None, wait_seconds: int = 180
) -> dict[str, str]:
    if not save_to_file and not repo:
        raise ValueError("EXPLICIT_SECRET_DESTINATION_REQUIRED")
    if os.environ.get("GMAIL_APP_PASSWORD"):
        raise ValueError("APP_PASSWORD_LOCAL_ONLY_DO_NOT_MIX")
    prefix = "GMAIL_" + purpose.upper() + "_"
    client_id = os.environ.get(prefix + "CLIENT_ID", "")
    client_secret = os.environ.get(prefix + "CLIENT_SECRET", "")
    expected_email = os.environ.get("GMAIL_ADDRESS", "").strip().lower()
    if not client_id or not client_secret or not expected_email:
        raise ValueError("OAUTH_CLIENT_AND_EXPECTED_MAILBOX_REQUIRED")
    other = os.environ.get("GMAIL_" + ("SEND" if purpose == "read" else "READ") + "_CLIENT_ID")
    if other and hmac.compare_digest(other, client_id):
        raise ValueError("READ_SEND_CLIENTS_MUST_BE_SEPARATE")
    private = Path(__file__).resolve().parents[1] / ".private"
    if save_to_file and (private / f"gmail-{purpose}.env").exists():
        raise ValueError("PRIVATE_GRANT_ALREADY_EXISTS_PRESERVE_BEFORE_ROTATING")
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(32)
    result: dict[str, str] = {}
    with HTTPServer(("127.0.0.1", 0), callback_handler(state, result)) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        print(
            "Open this authorization URL in your own regular browser. No mailbox address or credentials are included:"
        )
        print(authorization_url(client_id, redirect, state, challenge, purpose))
        deadline = time.monotonic() + min(600, max(30, wait_seconds))
        while "code" not in result and "error" not in result and time.monotonic() < deadline:
            server.handle_request()
        if "error" in result:
            raise ValueError(result["error"])
        if "code" not in result:
            raise ValueError("OAUTH_CALLBACK_TIMEOUT")
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        values = exchange(
            result["code"],
            verifier,
            redirect,
            client_id,
            client_secret,
            expected_email,
            purpose,
            client,
        )
    if save_to_file:
        path = save_private(values, purpose, private)
        return {
            "status": "VERIFIED_SAVED",
            "purpose": purpose,
            "destination": str(path),
            "mailbox_read": "false",
            "mail_sent": "false",
        }
    if repo is None:
        raise ValueError("EXPLICIT_SECRET_DESTINATION_REQUIRED")
    github_save(values, repo)
    return {
        "status": "VERIFIED_SAVED",
        "purpose": purpose,
        "destination": "PRIVATE_GITHUB_SECRETS",
        "mailbox_read": "false",
        "mail_sent": "false",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purpose", choices=["read", "send"], required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--authorize", action="store_true")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--save-private", action="store_true")
    destination.add_argument("--github-repo")
    parser.add_argument("--wait-seconds", type=int, default=180)
    args = parser.parse_args()
    if not args.authorize:
        print(
            json.dumps(
                {
                    "mode": "plan",
                    "purpose": args.purpose,
                    "scopes": sorted(scopes(args.purpose)),
                    "requirements": [
                        "Google Desktop OAuth client for this purpose",
                        "Expected GMAIL_ADDRESS in private environment",
                        "Explicit .private or private GitHub secret destination",
                    ],
                    "ingestion_status": "DEFERRED_USE_EML"
                    if args.purpose == "read"
                    else "OWNER_ONLY_SEND_SUPPORTED",
                    "next_step": "Re-run --authorize with --save-private or --github-repo; open the printed URL manually",
                },
                sort_keys=True,
            )
        )
        return
    try:
        outcome = authorize(
            args.purpose,
            save_to_file=args.save_private,
            repo=args.github_repo,
            wait_seconds=args.wait_seconds,
        )
    except (ValueError, OSError, httpx.HTTPError) as exc:
        # Provider response bodies, auth codes and token values never enter output.
        code = (
            str(exc)
            if isinstance(exc, ValueError) and re.fullmatch("[A-Z0-9_]+", str(exc))
            else "SETUP_AUTHORIZATION_FAILED"
        )
        raise SystemExit(code) from None
    print(json.dumps(outcome, sort_keys=True))


if __name__ == "__main__":
    main()
