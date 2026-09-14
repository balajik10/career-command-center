# Sources, coverage, and access policy

Career Command Center collects only individually approved sources through documented public interfaces. A working parser is not evidence that a company permits recurring access. The committed neutral catalogue contains **280 companies across eight categories, 35 per category**. It contains **zero enabled automated sources**, **277 pending/unvalidated rows**, and **three explicit manual/native-alert routes**. This is a catalogue, not a claim of 280 live integrations or a private watchlist.

Amazon, Google, and JPMorgan Chase have verified official careers links and explicit manual/native-alert routes. Their public careers pages were opened on 2026-09-14; no job extraction or recurring collection was enabled. The initial build used three of the maximum 50 company URL checks. All other career URLs are deliberately blank until observed from an official source. Never invent an ATS tenant to increase coverage numbers.

## Supported interfaces

| Interface | Status | Fields and behavior | Safe fallback |
|---|---|---|---|
| Greenhouse Job Board public GET API | Implemented; synthetic contract-tested | Metadata-only board listing, bounded details for engineering candidates; ID, location, description, first_published, updated_at, deadline and source pay ranges | Oversized/schema-invalid boards quarantined; manually review source |
| Lever public Postings API, global/EU | Implemented; synthetic contract-tested | Published postings, 100-item offset pages, job/apply URLs, categories, descriptions and salary text; no undocumented createdAt assumption | Full reconciliation; partial pages never count as complete |
| Ashby public Job Posting API | Implemented; synthetic contract-tested | Listed jobs only, canonical job/apply links, last-publication time, description and compensation summary | Missing listed flag is a schema failure |
| RSS / Atom | Implemented; synthetic contract-tested | Entries, title, link, description and source publication date | Unknown/changed feed schema quarantined |
| Sitemap → JSON-LD | Implemented; synthetic contract-tested; named human policy approval required | Bounded same-origin URL-set links, detail-page JobPosting objects and graphs | Sitemap indexes require explicit individual source onboarding |
| JobPosting JSON-LD | Implemented; synthetic contract-tested; named human policy approval required | Title, organization, location, ID, source date, description, salary and original page link | No script execution or reverse-engineered endpoints |
| User-owned CSV / TSV / JSON jobs | Implemented; local import | `id`, `company`, `title`, `url`, optional `apply_url`, `location`, `description`, `posted_at` | Invalid rows fail the import for review |
| User-owned `.eml` alerts | Implemented; synthetic contract-tested | Sender allowlist, local HTML anchor extraction, minimal title/link observations and HMAC ID; token-bearing redirect URLs discarded | Unrecognized templates and plain-text-only alerts require manual CSV/JSON import |
| Live Gmail / IMAP | Not implemented in this release | No mailbox access requested | Export `.eml` locally and import after configuring privacy key |
| LinkedIn, Naukri, Indeed, Glassdoor, Wellfound website collection | Disabled by transport host guard | Native alerts, user exports and local imports only; imported URLs are never visited by the email parser | Manually open links yourself |
| Workable, SmartRecruiters, Hacker News API | Deferred expansion | Documentation researched where listed in references; no adapters claim support | Native alert or manual import |
| Workday CXS / private XHR / browser collection | Unsupported | No endpoint discovery, session reuse, login automation, anti-bot workaround, proxies or headless browser | Official documented feed, approved public JSON-LD or manual route |

All supported adapters have sanitized success, empty, schema-change, malformed/date, size-limit, duplication, and blocked/rate-limited transport contracts. Fixtures are authored examples, not captured authenticated traffic. Invalid date text is retained as untrusted source evidence for the normalization layer to label unknown; it never becomes the fetch time.

Greenhouse resumes bounded detail collection using a fresh complete board inventory on every run. A listing hash includes the documented, timezone-aware `updated_at` value and identity/title/location/URL metadata. Only successfully normalized details whose canonical Sheet rows have committed receive a small per-job checkpoint in `_System_State`; the cache is never embedded in a large Sources cell. Restored checkpoints are trusted only while their canonical job and exact source appearance still exist. Unchanged versions skip detail requests, and current listing IDs still prove existing jobs present. Changed versions are fetched again, including previously tracked jobs whose titles leave the engineering filter. Cached observations retain elapsed age rather than becoming fresh again.

Conflicting duplicate IDs, a supplied `meta.total` that differs from the unique inventory count, missing/invalid update timestamps, and unexpected 304 responses quarantine Greenhouse collection. An update between list and detail requests leaves the snapshot incomplete and is retried. An invalid detail stops that source for review; valid details earlier in the run can still commit. Budgets and failed details cannot advance missing/closure counters or complete the baseline. A crash before checkpoint persistence may cause a safe repeat detail request.

## Independent source states

`access_mode` describes the mechanism. `policy_state` records the permission decision. `coverage_state` records validation evidence. `health_state` records operational behavior. Unknown or unapproved values fail closed.

Collection requires an enabled row, automated access mode, approved policy, verified active/empty coverage, current policy evidence, hash and named human reviewer, free cost classification, approved destination host, no active backoff, and appropriate health. RSS and public HTML also require an affirmative robots review. Populate `allowed_hosts` with each approved API or redirect host; an approved company website does not silently authorize any other host.

Policy evidence must include `policy_reviewed_at`, `policy_review_due_at`, `policy_evidence_url`, `policy_evidence_hash`, and `human_reviewer`. A missing/overdue item blocks collection. New sources begin pending and disabled. `source add` can propose source metadata, but human input must approve the policy and enable a passing source. Automated maintenance cannot make that decision.

`VALIDATION_PROBE` is a separate user-initiated purpose: only the exact supplied URL, its same-origin `/robots.txt`, and explicitly supplied official documentation/terms URLs may be fetched. This implementation rejects probe redirects, returns no response body, extracts no jobs/contacts, and never changes approval or enablement. Re-run a probe with a reviewed canonical URL if a provider redirects it. A probe is not a bypass for restricted websites.

Displayed health precedence is `PAUSED > AUTH_REQUIRED > DOWN > STALE > BACKOFF > DEGRADED > HEALTHY > UNKNOWN`. Backoff time is enforced separately even when the displayed state becomes stale/down. Complete successes reset failure counters and complete a first baseline; partial or deferred attempts never advance success time, baseline completion, watermarks or missing/closure counters. Policy blocking changes policy, not operational health.

## Network boundaries

The GET-only transport has a global concurrency ceiling of 12, per-host serialization, and a conservative two-second minimum between requests (at most 30/minute). It validates every redirect and every resolved IP, blocks private/link-local/loopback addresses and restricted platforms, and connects to the validated IP while verifying TLS against the original hostname. This prevents a second DNS resolution from substituting an internal target. Environment proxy settings are not used.

Requests are bounded by a 5-second connect timeout, 20-second read timeout, 30-second total HTTP timeout, five redirects and 5 MB compressed/decompressed response limits. The caller supplies an inner deadline and hard request budget; the transport reserves a full request window before beginning. Conditional requests use stored ETag/Last-Modified metadata except Greenhouse inventories, whose current listing must be fetched to resume safely. Only transient failures are retried, at most three total attempts; 429 is retried only with valid Retry-After and room before the deadline. Forbidden/login/CAPTCHA responses stop collection. A partial source returns completed observations with `snapshot_complete=false` and must not drive closure.

XML parsing rejects DTDs, entities, XInclude, oversized documents, depth above 64, more than 100,000 nodes, and more than 100 attributes per element. HTML becomes plain text, with scripts/styles removed. Spreadsheet cells receive formula-injection protection at the write boundary. All extracted descriptions, links, and emails are data rather than instructions.

## Manual alert workflow

Create native alerts yourself using broad role/location groups, for example `Software Engineer`, `Backend Engineer`, `Java Engineer`, and `Spring Boot` with Bengaluru or India remote. Experience descriptions such as `0-2`, `1+`, and `2+` remain in scope; use company watches as a supplement. Provider alert limits change and were not live-verified for every platform, so this release does not promise a particular number of alerts. Use each platform's current UI limit and combine broad queries if needed.

Route received alerts to a dedicated folder such as `JobRadar/Incoming`. Export only alerts you own. For `.eml` parsing, configure exact trusted sender addresses; never load remote images or click mail links automatically. The parser hashes raw message bytes with a private domain-separated HMAC, retains minimal job metadata, and does not preserve raw bodies. Forwarded mail must have its observed sender added deliberately. The current generic HTML-link parser can require manual correction when providers change templates.

Google Alerts can assist manual discovery with queries such as `"Backend Engineer" "Bengaluru" site:careers.example.com`, `"Java" "0-2 years" "India"`, or `"Software Engineer I" "Spring Boot"`. Replace the example domain with a verified official company domain. New domains discovered through alerts stay pending, and the system never performs an automated LinkedIn/person search.

## Source configuration and operation

`config/companies.yaml` is a neutral starter catalogue. Keep personal watchlist tiers, cadence overrides, notes and compensation in private configuration or the private Sheet. Configure an observed provider tenant only after verifying its official board URL and legal/policy evidence. A Greenhouse token is taken from the actual board URL, a Lever site from its official published board, and an Ashby board name from its hosted board; no enumeration is implemented.

Run `career-radar source check --all` to inspect source readiness. Review pending rows before enabling any collection. When a source fails, inspect its sanitized reason: policy/access conflicts require review or a manual route; parser failures require updating fixtures and adapter code; rate limits require respecting the backoff. Never turn a failed source into a complete empty snapshot.

Adapter API: `sources.parse(source, fetch_result)` performs offline parsing. `fetch.collect(source, FetchBudget(...))` performs synchronous bounded collection and returns `(FetchResult, list[RawJob])`. Parsing functions and the transport are injectable for deterministic offline tests. Run `pytest tests/test_sources.py tests/test_security.py` to check the supported contracts.
