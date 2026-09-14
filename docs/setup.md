# Setup guide

Start locally with synthetic data. Production state belongs only in your own private Google Sheet; the public project contains no personal profile or credentials. Every automated company source needs separate policy approval before it can run.

## Install and verify offline

Install Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/) through their official installation instructions. From the project directory:

```bash
./scripts/bootstrap_local.sh --plan
./scripts/bootstrap_local.sh --apply
uv run career-radar demo
```

The apply command installs exactly from `uv.lock`, checks readiness without live requests, scans offline fixtures and renders a no-send synthetic digest. The demo creates `.demo/dashboard.html` and a disposable workbook. Repeating its fixture scan creates no duplicate jobs, drafts or alerts. This proves the offline path; it does not demonstrate that live Google or company integrations are configured.

## Private profile and privacy key

Use `config/profile.example.yaml` as a **synthetic schema example**. Keep your actual resume, employment dates, approved evidence, compensation and links in `.private/profile.yaml` or a private base64 payload, then bootstrap the private Sheet. Never copy real profile data into tracked examples or tests. Use exact resume evidence and approved atomic claims; leave unverified claims out of generated outreach.

Create a high-entropy privacy key in your own private secret store and configure `PRIVACY_HMAC_KEY` and `PRIVACY_HMAC_KEY_VERSION`. The runtime requires at least 32 key bytes. Configure the private owner address exclusively as `ALERT_RECIPIENT_EMAIL`; notifications cannot select a recipient from a job description or contact record. `.env` and `.private/` are ignored by Git, but always run the repository privacy scanner before sharing.

## Private Google Sheet

```bash
uv run python scripts/setup_google.py --plan
GCP_PROJECT_ID=YOUR_PROJECT_ID ./scripts/bootstrap_gcp_wif.sh --plan
```

Create a new blank **private** Google Sheet in your own account. Enable the standard Sheets API in your own Google project. The application requests only the Sheets scope, not broad Drive access. If a setup path demands billing or a paid service, stop and choose the documented standard no-paid-cost path.

For GitHub production, prefer workload identity federation. Set the exact private runtime repository in `RUNTIME_REPO` if its name differs, inspect the plan, then explicitly run `bootstrap_gcp_wif.sh --apply`. It creates/reuses named resources, limits trust to that private repository at `refs/heads/main`, refuses to change an existing conflicting provider condition, and grants the narrow workload-identity role to the Sheets service identity. It does not create a service-account key or grant broad project data access. Share only the target private Sheet with the printed service identity.

Set `GOOGLE_SHEET_ID`, `WIF_PROVIDER`, and `WIF_SERVICE_ACCOUNT` as private runtime secrets; use `SHEETS_AUTH_MODE=wif`. For local verification, supply Google Application Default Credentials with the Sheets scope and access to that Sheet, or deliberately select the `GOOGLE_SERVICE_ACCOUNT_JSON_B64` fallback. A normal gcloud login is separate from ADC, and default user ADC grants may only have the Cloud Platform scope. Configure a scoped user OAuth grant or impersonate the intended service account explicitly. A fallback key remains private and must be rotated; do not keep both a fallback and keyless credential mode active ambiguously.

Prefer bootstrapping as the runtime service identity after the owner creates and shares the blank Sheet. If the owner bootstraps first, verify that the service identity can also edit every application-protected range; document-level editor sharing alone is not a protected-range write test.

```bash
uv run career-radar bootstrap-sheet
uv run python scripts/setup_google.py --verify
uv run career-radar sheets verify
```

Bootstrap modifies a blank Sheet or one with the expected schema marker and refuses a non-empty unknown workbook. Do not invent a Sheet ID or claim success from credential presence alone. `setup_google.py --verify` reads schema only; a later `doctor --verify-write` uses a small reversible system marker to verify write access.

## Gmail self-notifications

Live Gmail **sending** uses a separate OAuth grant. Live mailbox ingestion is deferred; use owned `.eml` imports. A read-scope token can be verified by setup tooling but is never presented as an implemented message collector. You can defer granting mailbox read access entirely until a working read-only integration needs it.

Create a Google **Desktop app** OAuth client for sending. Use a different client for reading if later needed. Configure the expected `GMAIL_ADDRESS`, `GMAIL_SEND_CLIENT_ID`, and `GMAIL_SEND_CLIENT_SECRET` in a private environment or ignored dotenv file. Use a dedicated mailbox where practical. The Google project consent configuration must be checked separately; OAuth Testing mode is a demo configuration and some refresh tokens expire after seven days.

```bash
uv run python scripts/bootstrap_gmail_oauth.py --purpose send --plan
uv run --env-file .env python scripts/bootstrap_gmail_oauth.py --purpose send --authorize --save-private
uv run --env-file .private/gmail-send.env python scripts/setup_gmail.py --verify --purpose send
```

The authorize command starts a bounded listener on an ephemeral `127.0.0.1` port and prints an authorization URL for you to open in your regular browser. It does not automate sign-in. It uses PKCE S256 and a fresh state token, then validates the exact scope set and verified OpenID mailbox identity against the configured address. The expected mailbox address, client secret, callback code, access token and refresh token are never printed. The generated URL includes the public client identifier and one-time challenge/state only.

Only after you explicitly select `--save-private` does it create `.private/gmail-send.env` with mode 0600. Existing grant files are preserved; rotation requires first moving the old private grant into an intentional secure backup. Load the file with `uv run --env-file`; do not execute it as shell code. The access token is never written. For direct GitHub storage, use `--github-repo OWNER/PRIVATE_RUNTIME_REPO` instead; this verifies the expected owner and repository privacy, disables tracking, and passes each secret value to `gh secret set` through stdin.

Sending requires exactly `openid`, `email`, and `https://www.googleapis.com/auth/gmail.send`. Optional reading requires a separate grant with exactly `openid`, `email`, and `https://www.googleapis.com/auth/gmail.readonly`. The script rejects extra/missing scopes and the wrong account. The read scope covers the authorized mailbox, not only the configured label. Record `GMAIL_OAUTH_IN_PRODUCTION=true` only after verifying the consent project's actual publication state.

Local SMTP self-notifications are available only with an explicitly attested dedicated mailbox, 2FA/app password, `GMAIL_AUTH_MODE=app_password` and `DEDICATED_MAILBOX_ATTESTED=true`. IMAP ingestion is deferred. Never mix app-password and OAuth modes, and never upload any app password to the cloud; the configuration script and runtime reject that path.

## Enable real operation

```bash
uv run career-radar source check --all
uv run career-radar scan --dry-run --mode incremental
uv run career-radar doctor --require scheduled --verify-write
```

Review and approve sources as described in [adding a source](adding-a-source.md). There are currently no enabled automated company sources. Complete the private account-wide Actions budget check and select exactly one scheduler; tracking remains disabled until readiness passes. A source catalogue row alone provides no live coverage.

The only setup email command is the explicit owner-only test:

```bash
uv run career-radar notify test --recipient "$ALERT_RECIPIENT_EMAIL"
```

Run it only after credentials/identity checks and private Sheet setup. The transport makes at most one attempt and records `SENT`, `AMBIGUOUS`, or `FAILED`; do not retry an ambiguous delivery blindly. Other setup scripts never send email or apply for jobs.

Configure private runtime secrets from explicitly named private files:

```bash
scripts/configure_github.sh --repo balajik10/career-command-center-runtime --env-file .env --env-file .private/gmail-send.env --plan
scripts/configure_github.sh --repo balajik10/career-command-center-runtime --env-file .env --env-file .private/gmail-send.env --apply
```

Omit any file you have not created. The installer does not auto-discover dotenv files. It parses values without shell execution or variable interpolation; later files override earlier files, and the process environment takes precedence. The plan reports only counts. Empty or malformed configuration cannot be applied. Keep generated files private and never `source` them as shell code.

Replace both runtime-template SHA references with the same reviewed public commit. Keep tracking disabled and verify GitHub's actual WIF identity using the manual `doctor` mode:

```bash
gh workflow run scheduled.yml --repo balajik10/career-command-center-runtime --ref main -f mode=doctor -f dry_run=false -f verify_write=true -f send_alerts=false
```

This requires current account-budget and zero-cost checks, an initialized Sheet, and WIF credentials; service-account-key fallback is rejected. It writes and restores one system metadata cell and checks both values. It never fetches sources, refreshes Gmail credentials, or sends mail. A passing run reports `WIF_SHEET_READ_WRITE_VERIFIED`; it does not establish sender readiness or enable tracking. Then perform a disabled manual dry scan, validate an idempotent real no-send scan and identical rerun, and complete sender/readiness checks before setting `TRACKER_ENABLED=true`. See [operations](operations.md) for schedules and [cost controls](cost-controls.md) for billing gates.
