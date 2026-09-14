# Privacy boundary and contact handling

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
