"""Reject tracked private artifacts and obvious secrets without printing their content."""

from __future__ import annotations

import re
import subprocess

BAD_PATH = re.compile(
    r"(^|/)(\.private|\.demo|backups|logs|node_modules|\.venv|credentials|tokens|browser-profile)(/|$)|\.(pdf|sqlite3?|db|har|pem|key)$|(^|/)\.env($|\.)",
    re.I,
)
SECRET = re.compile(
    r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b|\bAIza[A-Za-z0-9_-]{30,}\b|"private_key"\s*:\s*"[^"\s]{20}',
    re.I,
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SAFE_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "test.invalid",
    "example.test",
    "example.invalid",
    "test.example",
    "invalid.example",
    "noreply.github.com",
    "users.noreply.github.com",
}


def check_path(path: str, content: bytes) -> list[str]:
    reasons = []
    if BAD_PATH.search(path) and path != ".env.example":
        reasons.append("PRIVATE_PATH")
    if path.endswith(".eml") and not path.startswith("tests/fixtures/"):
        reasons.append("PRIVATE_EMAIL_FILE")
    if b"\0" in content:
        if not path.endswith((".png", ".jpg", ".webp")):
            reasons.append("UNREVIEWED_BINARY")
        return reasons
    text = content.decode("utf-8", errors="replace")
    if SECRET.search(text):
        reasons.append("POSSIBLE_SECRET")
    for email in EMAIL.findall(text):
        domain = email.rsplit("@", 1)[1].lower()
        if (
            domain not in SAFE_EMAIL_DOMAINS
            and not domain.endswith(".iam.gserviceaccount.com")
            and not re.fullmatch(r"[a-zA-Z0-9._+-]+@[a-f0-9]{40}", email)
        ):
            reasons.append("NON_SYNTHETIC_EMAIL")
            break
    return reasons


def tracked_files() -> list[str]:
    return (
        subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
        .stdout.decode()
        .split("\0")[:-1]
    )


def main() -> None:
    paths = tracked_files()
    if not paths:
        raise SystemExit("No Git index to verify; stage the reviewed code first.")
    failed = []
    for name in paths:
        # Inspect the exact staged blob, not a potentially different working copy.
        data = subprocess.run(["git", "show", ":" + name], check=True, capture_output=True).stdout
        reasons = check_path(name, data)
        if reasons:
            failed.append((name, sorted(set(reasons))))
    if failed:
        for name, reasons in failed:
            print(name + ": " + ",".join(reasons))
        raise SystemExit(1)
    print(f"PASS: {len(paths)} tracked files; no private artifacts or detected secrets.")


if __name__ == "__main__":
    main()
