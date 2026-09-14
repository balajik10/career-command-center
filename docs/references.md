# Official references checked during implementation

Verification date: **2026-09-14**. These are primary documentation links, not blanket permission for collecting every tenant/company. Named human source review remains required. Documentation availability was checked through read-only browsing; the build did not accept terms, log in to restricted providers, or submit applications.

| Reference | Implementation consequence |
|---|---|
| [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html) | Public GET endpoints; board list then bounded detail requests with pay transparency. `first_published` and `updated_at` have different meanings. |
| [Lever developer documentation](https://hire.lever.co/developer/documentation) and [official Postings API repository](https://github.com/lever/postings-api) | Use published postings API, global/EU endpoint selection and documented offset/limit pagination; do not substitute the authenticated Data API. |
| [Ashby public Job Posting API](https://developers.ashbyhq.com/docs/public-job-posting-api) | Preserve job/apply links, collect only `isListed=true`, request published compensation, and label `publishedAt` as last-publication time. |
| [Schema.org JobPosting](https://schema.org/JobPosting) | Parse observed job fields without inventing missing dates, salary, contact identity or eligibility. |
| [Sitemaps protocol](https://www.sitemaps.org/protocol.html) | Parse bounded approved sitemap URLs; sitemap availability is not site-wide collection permission. |
| [Atom RFC 4287](https://www.rfc-editor.org/rfc/rfc4287) | Recognize namespaced feeds, publication dates and alternate links. |
| [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement) | Restrictions cover scraping, unauthorized bots, copying and access-control circumvention. Website collection is disabled; use user-owned alerts/exports/manual routes. |
| [Workable API documentation](https://help.workable.com/hc/en-us/articles/115013356548-Workable-API-Documentation) | Account API documentation and its published request limit were checked. No general public-permission inference; adapter deferred. |
| [Hacker News official API](https://github.com/HackerNews/API) | Firebase API documents job stories; monthly Who Is Hiring discovery is separate. Adapter deferred. |
| [GitHub schedule events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) | Scheduled workflows can be delayed; current documentation includes IANA timezone support. Schedules belong only in the private runtime repository. |
| [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency) | Queue serialization can use `queue: max`; do not combine it with cancel-in-progress true. |
| [GitHub reusable workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations) | Pin the private runtime caller to a reviewed public code SHA. |
| [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) | Standard public-repository runners and private account allowances differ; private usage shares the account allowance. |
| [Google Sheets API limits](https://developers.google.com/workspace/sheets/api/limits) | Per-user/per-project read and write quotas require aggressive batching and tighter application budgets. |
| [Gmail OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) | Readonly access is a restricted mailbox scope, not a label-level permission boundary; sending requires its own authorization. |

Official careers URLs checked for the explicit manual routes: [Amazon](https://www.amazon.jobs/en/), [Google](https://www.google.com/about/careers/applications/), and [JPMorgan Chase](https://www.jpmorganchase.com/careers). Their presence establishes an official human navigation route only. It does not establish an approved automated connector or verified current jobs.

Recheck relevant documentation and policy evidence before approving a new source, after a provider changes behavior, and before relying on a provider's current free quota. Do not interpret an HTTP 200, robots allowance, public visibility, or structured job data alone as permission.

Google setup protocol references checked on 2026-09-14: [Desktop OAuth with loopback redirects and PKCE](https://developers.google.com/identity/protocols/oauth2/native-app), [OpenID Connect identity](https://developers.google.com/identity/openid-connect/openid-connect), and [Google OAuth web-server/offline access guidance](https://developers.google.com/identity/protocols/oauth2/web-server). Setup uses a user-opened regular browser, PKCE S256, validated state, an ephemeral IPv4 loopback callback and separate exact read/send grants; it does not automate consent or infer that token verification implements mailbox ingestion.

Also checked on 2026-09-14: [Sheets authorization scopes](https://developers.google.com/workspace/sheets/api/scopes), [Google workload identity federation for deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines), and [uv installation](https://docs.astral.sh/uv/getting-started/installation/). Setup grants only the Sheets scope, narrows GitHub trust to the private runtime/main ref, and installs from the committed dependency lock.
