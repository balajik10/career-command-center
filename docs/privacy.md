# Career Command Center privacy policy

Updated September 14, 2026.

Career Command Center is a personal, self-hosted application for tracking job opportunities and applications in a private Google Sheet. This policy describes its Google integrations and handling of the owner's career records.

Google access is requested for the feature the owner chooses:

- `openid` and `email` verify that the Google account's verified email matches the privately configured owner account.
- `gmail.send` sends job and operational alerts from the authorized account to the configured owner. It does not read existing messages, labels, or attachments. Live mailbox ingestion is not implemented; a mailbox read grant is unnecessary for these features.
- Optional `drive.file` access supports a one-time tracker setup: creating a private folder, converting an owner-provided workbook into a Google Sheet, checking ownership and permissions, and granting the designated runtime service account editor access. This scope covers files created or opened with the app; the setup operates on its tracker files. Scheduled Sheet updates use the service account, separately from the owner's one-time Drive OAuth grant.

Google processes account verification, stores the tracker and sent notifications, and provides the associated APIs. When the owner enables GitHub scheduling, GitHub Actions processes tracker data to run the application; runtime credentials are stored as secrets in the owner's private runtime repository. Credentials may also be stored in access-restricted, ignored local files. The one-time owner Drive OAuth grant stays local and is not installed in GitHub. Private records and exports are not committed to the public code repository or uploaded as workflow artifacts. Google and GitHub also handle service data under their own privacy policies.

Google user data is used only for the account verification, tracker, and owner notification features described here. The application does not sell this data, use it for advertising, or send it to AI services for model training. Career Command Center's use and transfer of information received from Google APIs will adhere to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.

The owner controls the Sheet, private files, backups, and runtime repository. Tracker and application records remain until the owner deletes them; public-derived contact records additionally follow their recorded retention expiry and the purge controls below. Credentials remain until removed or revoked. To stop access, disable scheduling, revoke the application's grant in [Google Account connections](https://myaccount.google.com/connections), remove the service account from the Sheet's sharing settings, and delete unneeded local grants and GitHub secrets. Revocation stops future authorized access; it does not delete existing Sheet records, backups, or sent email. The owner can edit or delete those copies in their respective storage services and remove the tracker entirely through Google Drive.

Policy changes are recorded on this page with an updated date. For non-private support or policy questions, use the [public project issue tracker](https://github.com/balajik10/career-command-center/issues); never include credentials, private records, or account identifiers in a public issue.

The public repository contains code, neutral company metadata, synthetic fixtures and documentation. Real resumes, verbatim evidence, identity, contact details, compensation, Sheet IDs, OAuth credentials, private preferences, application records, drafts and generated workbooks belong in the private Sheet or ignored local/private secret storage. The private runtime repository contains schedule configuration and named secrets only; it is not a second database or a place to commit personal exports.

The owner address is resolved exclusively from private `ALERT_RECIPIENT_EMAIL`. Notifications are addressed only to that owner. Source text cannot choose a recipient. Outreach is draft-only; the application does not send to recruiters or contacts, automate applications, or request referrals. A draft's existence is not permission to send it.

Valid contact origins are user-supplied exports/rows or a separately approved official recruiting channel. Every observed name, title, company, email, LinkedIn URL and phone needs field-level provenance. Unknown values remain null. Never derive an email from a name/domain, inspect Git commit emails, scrape people-search snippets, enrich identities across sources, or reuse a public personal phone number for WhatsApp.

A current-company relationship supplied by the user creates a `WARM_CONNECTION`. Only the user's recorded agreement makes that contact a `CONFIRMED_REFERRER`. A public stranger is not a referral candidate. A confirmed hiring manager requires explicit approved-source evidence linking the person to the relevant role/team. Generic recruiting addresses are role-based channels, not invented people.

Public-derived recruiting records require a stated contactability basis, evidence URL/time, retention expiry, and `outreach_allowed=false` until reviewed. WhatsApp drafts require a manually set `whatsapp_allowed=true` or an explicit recruiting invitation to that channel. Do-not-contact state always wins.

Email, phone, recipient, message and suppression identifiers use domain-separated HMAC-SHA-256 under the private high-entropy key. Canonical emails are lowercase and phone tokens require E.164. Unkeyed hashes do not provide an adequate suppression/privacy boundary. Key version accompanies retained tokens; do not discard a prior key needed to honor opt-outs without an explicit migration plan.

```bash
uv run career-radar privacy redact-contact CONTACT_UID
uv run career-radar privacy purge-expired
uv run career-radar export backup .private/backups
```

Redaction removes identifying fields and related drafts while retaining the minimum privacy-preserving suppression token. Expiry purge targets expired public-derived recruiting data; it does not authorize wholesale deletion of user-entered contacts or application history. Backups remain private and gitignored. Review the command's returned counts and private Sheet state after a deletion/redaction operation.

All jobs, pages, email bodies and imported cells are untrusted data. They cannot change program instructions, policy, credentials or notification recipients. HTML becomes plain text, XML entities/includes are blocked, token-bearing links are rejected, and spreadsheet writes protect against formula injection. Logs contain sanitized reason codes and aggregate counts rather than descriptions, addresses, drafts or private URLs. Production workflows upload no artifacts.

The repository verifier rejects tracked personal/generated files; the security workflow scans Git history for secrets. Gitignore alone is insufficient: scan before staging and before publishing. If a secret is exposed, disable scheduling, revoke/rotate the credential at its issuer, investigate access, and repair Git history only through an explicit reviewed procedure. Removing a file from the current checkout does not revoke the exposed secret.
