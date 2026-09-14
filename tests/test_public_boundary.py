import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "verify_public_repo", Path("scripts/verify_public_repo.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".private/profile.yaml",
        "backups/export.json",
        "resume.pdf",
        "browser.har",
        "runtime.sqlite",
        "inbox.eml",
    ],
)
def test_rejects_private_paths(path):
    assert module.check_path(path, b"private data")


def test_synthetic_examples_allowed():
    assert not module.check_path(".env.example", b"ALERT_RECIPIENT_EMAIL=\n")
    assert not module.check_path("examples/contacts.example.csv", b"candidate@example.com")


def test_secrets_rejected_without_content_output():
    private_key_marker = b"-----BEGIN " + b"PRIVATE KEY-----"
    assert module.check_path("config.json", private_key_marker) == ["POSSIBLE_SECRET"]
    assert module.check_path("notes.txt", b"person@" + b"personal-domain.invalid") == [
        "NON_SYNTHETIC_EMAIL"
    ]


def test_unreviewed_binary_rejected():
    assert module.check_path("data.bin", b"\0secret") == ["UNREVIEWED_BINARY"]


def test_all_tracked_files_are_public_safe():
    # A fresh empty workspace has no tracked private data; CI always has tracked files.
    for name in module.tracked_files():
        assert not module.check_path(name, Path(name).read_bytes()), name


def test_installed_catalogue_matches_reviewed_public_catalogue():
    assert (
        Path("config/companies.yaml").read_bytes()
        == Path("src/career_radar/data/companies.yaml").read_bytes()
    )
