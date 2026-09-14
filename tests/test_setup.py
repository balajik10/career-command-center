"""Setup tooling never authorizes, changes accounts, or sends mail during tests."""

import base64
import hashlib
import importlib.util
import io
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    specification = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_pkce_auth_url_scopes_and_no_mailbox_leak():
    oauth = script("bootstrap_gmail_oauth")
    verifier, challenge = oauth.pkce()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    url = oauth.authorization_url(
        "example-client-id", "http://127.0.0.1:32100/oauth/callback", "state", challenge, "send"
    )
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "accounts.google.com"
    assert query["scope"][0].split() == sorted(oauth.scopes("send"))
    assert query["code_challenge_method"] == ["S256"] and query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["false"]
    assert (
        "login_hint" not in query
        and "client_secret" not in query
        and "candidate@example.com" not in url
    )
    assert oauth.scopes("read") != oauth.scopes("send")


@pytest.mark.parametrize(
    "path",
    [
        "/wrong?state=s&code=c",
        "/oauth/callback?state=wrong&code=c",
        "/oauth/callback?state=s&state=s&code=c",
        "/oauth/callback?state=s&error=access_denied",
        "/oauth/callback?state=s",
        "/oauth/callback?state=s&code=",
    ],
)
def test_oauth_callback_state_path_and_denial(path):
    oauth = script("bootstrap_gmail_oauth")
    with pytest.raises(ValueError):
        oauth.validate_callback(path, "s")
    assert oauth.validate_callback("/oauth/callback?state=s&code=c", "s") == "c"


def test_oauth_callback_returns_only_generic_text_and_suppresses_logs():
    oauth = script("bootstrap_gmail_oauth")
    result = {}
    cls = oauth.callback_handler("s", result)
    handler = object.__new__(cls)
    handler.path = "/oauth/callback?state=s&code=code-not-for-logs"
    handler.wfile = io.BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.do_GET()
    assert result["code"] == "code-not-for-logs"
    assert b"code-not-for-logs" not in handler.wfile.getvalue()
    assert handler.send_response.call_args.args == (200,)
    handler.path = "/bad"
    handler.do_GET()
    assert handler.send_response.call_args.args == (400,)
    assert handler.log_message("sensitive %s", "callback-code") is None


def test_oauth_exchange_exact_scopes_verified_expected_account_and_minimal_persistence():
    oauth = script("bootstrap_gmail_oauth")
    requests = []
    token = {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "scope": "openid email https://www.googleapis.com/auth/gmail.send",
    }
    identity = {"email": "candidate@example.com", "email_verified": True}

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200, json=token if request.url.host == "oauth2.googleapis.com" else identity
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        values = oauth.exchange(
            "code",
            "verifier",
            "http://127.0.0.1:32100/oauth/callback",
            "client",
            "secret",
            "Candidate@Example.com",
            "send",
            client,
        )
    assert values["GMAIL_SEND_REFRESH_TOKEN"] == "synthetic-refresh"
    assert values["GMAIL_ADDRESS"] == "candidate@example.com"
    assert "access_token" not in values
    assert (
        len(requests) == 2
        and requests[0].method == "POST"
        and requests[1].url.host == "openidconnect.googleapis.com"
    )
    assert b"code_verifier=verifier" in requests[0].content
    for changes, expected in [
        ({"scope": "openid email https://mail.google.com/"}, "SCOPE_MISMATCH"),
        ({"refresh_token": ""}, "REFRESH_TOKEN_REQUIRED"),
    ]:
        with (
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda _, changes=changes: httpx.Response(200, json=token | changes)
                )
            ) as client,
            pytest.raises(ValueError, match=expected),
        ):
            oauth.exchange(
                "c", "v", "redirect", "client", "secret", "candidate@example.com", "send", client
            )
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client,
        pytest.raises(ValueError, match="EXCHANGE_FAILED"),
    ):
        oauth.exchange(
            "c", "v", "redirect", "client", "secret", "candidate@example.com", "send", client
        )
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=token
                    if request.url.host == "oauth2.googleapis.com"
                    else {"email": "candidate@example.com", "email_verified": False},
                )
            )
        ) as client,
        pytest.raises(ValueError, match="ACCOUNT_MISMATCH"),
    ):
        oauth.exchange(
            "c", "v", "redirect", "client", "secret", "candidate@example.com", "send", client
        )


def test_private_secret_storage_chmod_exclusive_and_no_symlinks(tmp_path):
    oauth = script("bootstrap_gmail_oauth")
    folder = tmp_path / ".private"
    path = oauth.save_private({"GMAIL_SEND_REFRESH_TOKEN": "synthetic-refresh"}, "send", folder)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert "synthetic-refresh" in path.read_text()
    with pytest.raises(FileExistsError):
        oauth.save_private({"GMAIL_SEND_REFRESH_TOKEN": "different"}, "send", folder)
    assert "different" not in path.read_text()
    with pytest.raises(ValueError, match="PRIVATE_DIRECTORY"):
        oauth.save_private({}, "send", tmp_path / "public")
    symlink = tmp_path / "sub" / ".private"
    symlink.parent.mkdir()
    symlink.symlink_to(folder, target_is_directory=True)
    with pytest.raises(ValueError, match="PRIVATE_DIRECTORY"):
        oauth.save_private({}, "send", symlink)


def test_github_secret_values_never_enter_command_arguments():
    oauth = script("bootstrap_gmail_oauth")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        return SimpleNamespace(
            returncode=0,
            stdout="balajik10" if args[2] == "user" else "true" if args[1] == "api" else "",
        )

    with patch.object(oauth.subprocess, "run", side_effect=run):
        oauth.github_save(
            {"GMAIL_SEND_REFRESH_TOKEN": "synthetic-refresh", "GMAIL_AUTH_MODE": "oauth"},
            "balajik10/career-command-center-runtime",
        )
    assert any(value == "synthetic-refresh" for _, value in calls)
    assert all("synthetic-refresh" not in " ".join(args) for args, _ in calls)
    with pytest.raises(ValueError, match="EXPECTED_PRIVATE"):
        oauth.github_save({}, "other/public")
    with (
        patch.object(
            oauth.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="sensitive"),
        ),
        pytest.raises(ValueError, match="GITHUB_SECRET_INSTALL_FAILED"),
    ):
        oauth.github_save({}, "balajik10/career-command-center-runtime")


def test_authorization_preflight_refuses_missing_credentials_destinations_or_mixed_clients(
    monkeypatch,
):
    oauth = script("bootstrap_gmail_oauth")
    for name in [
        "GMAIL_APP_PASSWORD",
        "GMAIL_SEND_CLIENT_ID",
        "GMAIL_SEND_CLIENT_SECRET",
        "GMAIL_ADDRESS",
        "GMAIL_READ_CLIENT_ID",
    ]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="DESTINATION"):
        oauth.authorize("send", save_to_file=False, repo=None)
    with pytest.raises(ValueError, match="CLIENT_AND_EXPECTED"):
        oauth.authorize("send", save_to_file=True, repo=None)
    monkeypatch.setenv("GMAIL_SEND_CLIENT_ID", "same-client")
    monkeypatch.setenv("GMAIL_READ_CLIENT_ID", "same-client")
    monkeypatch.setenv("GMAIL_SEND_CLIENT_SECRET", "synthetic")
    monkeypatch.setenv("GMAIL_ADDRESS", "candidate@example.com")
    with pytest.raises(ValueError, match="SEPARATE"):
        oauth.authorize("send", save_to_file=True, repo=None)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "never-upload")
    with pytest.raises(ValueError, match="APP_PASSWORD"):
        oauth.authorize("send", save_to_file=True, repo=None)


def test_setup_plans_are_offline_and_smoke_never_sends(monkeypatch, capsys):
    for name in ["setup_google", "setup_gmail", "bootstrap_gmail_oauth"]:
        module = script(name)
        argv = [name, "--plan"] + (["--purpose", "send"] if name == "bootstrap_gmail_oauth" else [])
        monkeypatch.setattr("sys.argv", argv)
        with patch.object(
            httpx.Client, "request", side_effect=AssertionError("No network in plans")
        ):
            module.main()
        assert capsys.readouterr().out
    smoke = script("smoke_test")
    monkeypatch.setattr("sys.argv", ["smoke_test"])
    with patch.object(smoke.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        smoke.main()
        calls = [call.args[0] for call in run.call_args_list]
    assert calls[0][-2:] == ["--dry-run", "--offline-fixtures"]
    assert "--no-send" in calls[1] and "--offline-fixtures" in calls[1]
    monkeypatch.setattr("sys.argv", ["smoke_test", "--verify-write"])
    with pytest.raises(SystemExit, match="LIVE_SHEET"):
        smoke.main()


def test_local_bootstrap_plan_never_installs():
    path = ROOT / "scripts/bootstrap_local.sh"
    subprocess.run(["bash", "-n", str(path)], check=True)
    run = subprocess.run(
        ["bash", str(path), "--plan"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "uv sync --locked" in run.stdout
