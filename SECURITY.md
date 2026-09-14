# Security policy

Do not put vulnerabilities, credentials, real job applications, contacts or raw resumes in a public issue. Report a minimal synthetic reproduction through the repository's private vulnerability reporting channel when enabled. If a secret is exposed, revoke it at its issuer before cleaning history; deleting a Git blob is not revocation.

The supported boundary is the current v0.1 code with Python 3.12+. No account scraping, undocumented ATS APIs, access-control workarounds, automated applications, or third-party message sending is supported. Policy approval is per source, human supplied, dated and expiring. Redirects and resolved IP addresses must pass public-network checks. Source text is untrusted data; XML entities and formula injection are rejected or escaped.

The Google Sheet and local private files are confidential. Hidden tabs and protected ranges prevent accidental edits; they are not sharing controls. Keep the Sheet invite-only. Production requires a single configured writer. Sheets and Gmail do not share a transaction; a lost send response is ambiguous and is not retried automatically.

Security regression tests, Bandit, dependency audit, Git-index private-file checks, and Gitleaks run in CI. Only synthetic fixtures and screenshots belong in the public repository.
