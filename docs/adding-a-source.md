# Adding a source

Begin with an exact official careers/board URL you observed. Never guess an ATS token, enumerate tenants or reverse-engineer a private endpoint. Prefer a documented Greenhouse, Lever or Ashby public board, then official RSS/Atom, approved sitemap/JSON-LD or a manual route.

```bash
uv run career-radar source add https://careers.example.com/
uv run career-radar source check --all
```

Replace the example with the exact verified official URL. `source add` proposes a pending, disabled row. It does not confer legal permission, enable automation or promise that the site is supported. Read the returned proposal and edit private source configuration or the Sources Sheet tab. Public company metadata can be contributed to the neutral catalogue, but personal watchlist tiers, cadence overrides and notes remain private.

Record source/company/provider, canonical entry URL, observed provider tenant where applicable, access mode, free cost class, and each approved host. API hosts are explicit allowed destinations: a company careers URL does not automatically authorize a different ATS host. Greenhouse uses its documented Job Board API, Lever its published Postings API (global/EU), and Ashby its listed-job board API. Unsupported providers stay manual until a real permitted adapter and contract tests exist.

Review current official documentation and terms. For generic pages/feeds, review `robots.txt` and record `robots_allowed=true` only when the intended path and user agent are allowed. Record the evidence URL/hash, `policy_reviewed_at`, `policy_review_due_at`, and your named `human_reviewer`. Review the precise automation purpose and rate limits rather than treating robots or HTTP 200 as blanket permission.

A deliberate validation probe may inspect the exact submitted URL, its same-origin robots file, and explicitly supplied official documentation/terms URLs. It returns no extracted jobs or contacts, saves no full page, rejects redirects, and cannot approve itself. A redirected URL requires a separately reviewed canonical source URL. Pending rows remain pending after a probe.

Only after human approval and passing connector validation may the private row become `policy_state=APPROVED`, `coverage_state=VERIFIED_ACTIVE` or `VERIFIED_EMPTY`, and `enabled=true`. Keep policy, coverage and health independent. Zero-cost mode rejects paid/unknown cost classification. Missing/overdue evidence, private addresses, restricted websites, login/CAPTCHA controls or active backoff block collection.

For a new adapter, add sanitized success, empty, schema-change, malformed/date, duplicate, blocked/rate-limit and oversized fixtures. Test that an interrupted source cannot advance baseline/closure state. Document endpoint provenance, fields, date meaning, rate budget, failure signals and safe fallback. Run the full quality gates before marking support in the source matrix; an empty adapter interface is not coverage.

Disable a source with the CLI's source-disable command and a clear reason, or set the private source row disabled. Keep historical jobs and user-owned application state. When a provider prohibits access or requires restricted login, select a native alert or manual import route instead of a workaround.
