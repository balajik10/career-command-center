# Troubleshooting

Start with `career-radar doctor` and the private Sheet's heartbeat, source attempt history and sanitized reason codes. Credential presence, a successful HTTP response or an empty source is not by itself evidence of a healthy live integration.

| Symptom / code | Meaning | Next action |
|---|---|---|
| Offline install cannot resolve packages | Package-index/network access unavailable | Restore ordinary package-index access, rerun `uv sync --locked`; do not alter the lock merely to hide the failure |
| `SOURCE_NOT_APPROVED`, missing policy evidence | Source has not passed named human review | Review terms/robots/current evidence and source scope; retain manual mode while unresolved |
| 401/403, `ACCESS_DENIED`, `CAPTCHA_STOP`, `LOGIN_STOP` | Access restriction or login requirement | Stop collection and select a permitted/native-alert/manual route; do not retry through another identity |
| 429 / `BACKOFF` | Provider rate limit | Respect the stored backoff; no retry unless Retry-After and a full request fit the deadline |
| `PARSER_CONTRACT`, `CONTENT_TYPE`, schema quarantine | Provider shape changed or wrong endpoint/content | Save only a sanitized minimal fixture, repair parser and contract tests; do not mark prior jobs closed |
| `RESPONSE_TOO_LARGE`, XML limits | Source exceeds documented safety caps | Keep degraded/quarantined; use a permitted narrower source if documented, otherwise manual |
| `DEFERRED_BUDGET`, `PARTIAL_BUDGET` | Run could not finish its bounded source work | Check due-state aging and next-run fairness; partial snapshots must not advance closure or watermarks |
| `GOOGLE_SHEET_ID` missing or Sheet unavailable | Credential/configuration/sharing incomplete | Follow setup guide; share only the intended Sheet with the correct Sheets identity |
| Unknown non-empty Sheet refused | Bootstrap cannot safely identify ownership/schema | Use a new blank Sheet or an explicit adoption/backup procedure; never overwrite the unknown workbook |
| Protected range / user edit conflict | Canonical ownership or submitted edit changed | Inspect Action_Updates status; correct user-owned input and resubmit instead of editing canonical rows |
| Google quota error | Request budget or provider quota exceeded | Wait for quota recovery, preserve pending state, inspect batching; never enable paid quota as a workaround |
| `OAUTH_SCOPE_MISMATCH` | Token grants extra or missing scopes | Use the separate purpose-specific Desktop client and authorize exactly the documented scopes |
| `OAUTH_ACCOUNT_MISMATCH` | Verified OpenID identity differs from configured mailbox | Repeat consent with the intended account; never substitute an inferred recipient |
| `OAUTH_STATE_MISMATCH`, callback rejected | Local callback failed state/path validation | Restart the bounded OAuth setup and open its new URL; do not paste a callback from another session |
| `OAUTH_CALLBACK_TIMEOUT` | User consent did not return within the listener window | Re-run setup deliberately; no token or email operation was completed |
| Refresh `invalid_grant` / Testing-mode token expiry | Revoked, expired or demo-only grant | Verify consent publication state and reauthorize; do not share cookies or account sessions |
| Live Gmail ingestion disabled | Read token verification is available, collector is deferred | Import owned `.eml` files or manual job rows; do not claim OAuth scope alone enables collection |
| SMTP/IMAP app password in GitHub config | Local-only credential mode was supplied to cloud | Remove it from upload input; use separate OAuth grants for cloud, or the local SMTP mode only |
| `BLOCKED_BUDGET_UNKNOWN` / insufficient minutes | Account-wide included usage not recently verified | Review current GitHub allowance and hard $0-overage stop, then record private attestations |
| Delayed/missing scheduled run | GitHub timing, queue, disabled tracking or local sleep | Inspect caller status, heartbeat and due times; use one bounded manual dry run and let overlap/reconciliation recover |
| `AMBIGUOUS` notification | Transport acceptance could not be proven or disproven | Inspect the owner's mailbox/provider evidence manually before any resend; never blind-retry |

`setup_google.py --verify` and `setup_gmail.py --verify` perform read-only schema/token checks; they do not send mail. `smoke_test.py` defaults to offline synthetic work. Its explicit `--live-sheet --verify-write` checks a small reversible Sheet marker through doctor and may still report unrelated schedule prerequisites as blocked.

If a recovery action needs a schema change, first export a private backup and inspect the workbook marker/version. Never delete user application/contact history to repair a collector failure. Preserve the run ledger and rerun the idempotent repair path.
## Managed macOS editable-install paths

If an environment marks the editable install's `.pth` file hidden and `uv run` reports `ModuleNotFoundError: career_radar`, install the ordinary wheel from the same lockfile with `uv sync --locked --no-editable`. Run commands with `.venv/bin/career-radar` or `uv run --no-sync career-radar`. Re-run the non-editable sync after changing source files. The final local release was also verified this way in a clean temporary environment with socket networking denied; this does not weaken any runtime security checks.
