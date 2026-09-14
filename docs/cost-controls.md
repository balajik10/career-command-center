# Cost controls

There is no runtime LLM, paid job API, proxy service, database, scheduler subscription or required paid cloud tier. Source policy enforces `cost_class=free` when `ZERO_COST_MODE=true`; unknown/paid classifications fail closed. Use standard Ubuntu GitHub runners only. Never enable paid Actions overage or add a payment-dependent service to satisfy a missing integration.

`scripts/estimate_actions_budget.py` reads the committed cron entries and runtime timeout expression, validates the pinned caller SHA pair, and enumerates the 400-year Gregorian cycle. It counts actual weekday/weekend/Sunday combinations, avoiding impossible combinations of separate maxima.

| Worst calendar-valid month | Included private minutes |
|---|---:|
| Rapid: (23 weekdays × 6 + 8 weekend days × 3) × 4 | 648 |
| Daily full: 31 × 10 | 310 |
| Digests: 31 × 2 × 3 | 186 |
| Sunday maintenance: 4 × 8 | 32 |
| Scheduled ceiling | **1,176** |
| Manual/recovery reserve | 200 |
| Private project plan | **1,376** |

Public standard-runner CI planning allowance is a separate 300 minutes. Under current GitHub policy, public standard-runner CI and a private account's included allowance are different buckets. Private minutes are **account-wide**, so other private repositories can exhaust the remaining allowance even when this project's own plan fits. CI fails when scheduled cost exceeds 1,200 or the private plan exceeds 1,600 minutes.

Before enabling production, inspect the authenticated account's current included usage and billing budget. Set a hard $0 paid-overage budget/stop in the billing UI, enable included-usage alerts, and record `PAID_OVERAGE_DISABLED=true` only after checking it. Record `BUDGET_VERIFIED_AT` with timezone, `INCLUDED_PRIVATE_MINUTES_REMAINING`, and an additional `ACCOUNT_RESERVE_MINUTES` (default 100) in private runtime variables. Doctor blocks unknown or stale attestations and requires the remaining allowance to cover this project's full monthly plan plus the account reserve. Recheck at least weekly and after other private workloads change. These variables are attestations, not evidence that the software changed the account's billing settings.

Sheets standard usage is governed by the current API quota and pricing policy. This app uses lower limits: at most 45 reads and 45 writes per minute per configured identity; rapid runs 20/20, full 30/30, digests 10/10. Prefer batched operations, never request quota increases or a billing-dependent replacement. Verify provider free-tier policies periodically; a past documentation check cannot promise unchanged vendor pricing.

The private runtime calls a single bounded Ubuntu job per invocation. Workflow/action setup time shares its outer timeout. No source-per-job matrix, artifact retention for private data, automatic workflow retry loop or paid runner is configured. A local scheduler is available when included Actions usage is insufficient, with the GitHub schedule disabled first.
