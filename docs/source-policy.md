# Source policy

Automated collection is permitted only for individually enabled sources with an approved automated access mode, current named-human policy evidence, verified coverage, free cost classification, allowed destination hosts and operational readiness. Unknown values fail closed. The neutral 280-company catalogue starts with zero automated approvals; Amazon, Google and JPMorgan Chase have explicit human navigation/native-alert routes, and the remaining rows are pending.

The runtime has no CAPTCHA solver, stealth browser, proxy rotation, account-session import, automated job application or third-party outreach sender. It does not scrape LinkedIn, Naukri, Indeed, Glassdoor or Wellfound. It does not discover undocumented/private endpoints, including Workday CXS. Native alerts, owned exports and manual imports provide the fallback.

`policy_state` is one of `PENDING_REVIEW`, `APPROVED`, `MANUAL_ONLY`, `LOGIN_REQUIRED`, `POLICY_BLOCKED` or `DISABLED`. It is independent from coverage validation and source health. Terms/access prohibitions affect policy; credential repair, rate limiting and transient failures affect their appropriate health/backoff dimensions. An approved source with stale evidence cannot continue collecting simply because its last request succeeded.

A user-initiated `VALIDATION_PROBE` is the only preapproval network exception. It is bounded to the supplied URL, same-origin robots and explicitly identified official documentation/terms URLs. This implementation rejects probe redirects, returns no response body and permits no extraction or self-approval. Recurring discovery always uses `COLLECTION` and the full approval gate.

Requests use transparent identification, verified TLS, IP-pinned public destinations, per-host serialization, conditional metadata and strict time/byte/request limits. 401/403, CAPTCHA and login requirements stop collection. Rate-limit backoff is respected even if a source's displayed health later becomes stale/down. No failed or partial attempt may masquerade as a complete empty board.

Publicly visible personal information is not permission to retain, enrich or contact a person. Recruiting contact records need a permitted origin, explicit contactability basis, field provenance and retention controls. Private exports remain private. Imported text is data, never instructions that can override this policy, change credentials or choose message recipients.

See [sources](sources.md) for the support matrix, [adding a source](adding-a-source.md) for onboarding and [official references](references.md) for documentation verified on 2026-09-14. Policy review is a human decision; automated health/contract checks do not substitute for it.
