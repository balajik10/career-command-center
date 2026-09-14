# Operations and recovery

The private runtime repository owns all unattended schedules. The public repository contains CI and a `workflow_call` implementation only. A caller references a full reviewed public commit SHA twice: in its reusable workflow reference and in `implementation_sha`. The called workflow explicitly checks out that public repository at that SHA and compares HEAD before installing from `uv.lock`.

| Run | Asia/Kolkata schedule (IST) | Equivalent UTC cron | Outer / inner deadline |
|---|---|---|---|
| Weekday rapid | 06:47, 09:47, 12:47, 15:47, 18:47, 21:47, Mon–Fri | `17 1,4,7,10,13,16 * * 1-5` | 4 / 3 minutes |
| Weekend rapid | 09:47, 15:47, 21:47, Sat–Sun | `17 4,10,16 * * 0,6` | 4 / 3 minutes |
| Daily reconciliation | 02:23 daily | `53 20 * * *` (previous UTC day) | 10 / 8 minutes |
| Morning digest | 08:11 daily | `41 2 * * *` | 3 / 2 minutes |
| Evening digest | 19:11 daily | `41 13 * * *` | 3 / 2 minutes |
| Maintenance | 03:43 Sunday | `13 22 * * 6` (Saturday UTC) | 8 / 6 minutes |

Exactly one schedule set is active. The committed caller uses `timezone: Asia/Kolkata`, verified against current GitHub documentation. UTC alternatives are documentation/testing inputs, not a second active schedule. The schedule equivalence tests include Sunday rollover and all weekday/weekend paths.

Rapid runs consider at most 40 sources and 80 HTTP requests; full runs at most 250 sources and 500 requests. Their inner deadlines reserve cleanup time before the outer Actions timeout. Deferred/partial sources retain their due state. Completed source chunks can be finalized without declaring unfinished snapshots complete. A 72-hour overlap and full reconciliation are intended to recover from schedule delays; actual source/provider date availability still limits freshness claims.

Nominal scheduled gaps for sources included in both rapid and full reconciliation can reach 4 hours 36 minutes on weekdays and 7 hours 24 minutes on weekends, plus queue delay, source sharding, rate limits, budget deferrals and GitHub scheduling delays. This system is periodic discovery, not real-time detection. Review observed source cadence and first-seen/source-date evidence on the private dashboard.

## Starting and stopping

Keep `TRACKER_ENABLED=false` and `SCHEDULER=disabled` through setup. Complete doctor, private Sheet bootstrap, offline scan, live dry scan, one owner-only test email if supported and configured, then an idempotent real scan and identical rerun. Only then select `SCHEDULER=github` and enable tracking. Missing live email implementations or source approvals remain explicit setup limitations, not green readiness claims.

Manual Actions dispatch validates mode, source, dry-run and send flags. Defaults are dry-run and no send. A disabled tracker permits an explicit GitHub manual dry run with no send after read-only Sheet, private identity, zero-cost, and account-budget checks; it does not require sender credentials or change the scheduler selection. Other disabled manual modes fail closed. Enabled production runs still require all write, sender, scheduler and budget gates. Inputs are passed through environment variables; no user input is inserted into shell program text. Never combine dry-run with send. To stop production, set `TRACKER_ENABLED=false`; the next scheduled caller skips its only job.

A lookback override is not supported by the v1 connector contract and is not exposed as a workflow input. Use `mode=full` for bounded full reconciliation of current official inventories. Legacy `INPUT_LOOKBACK_HOURS` values are explicitly rejected rather than silently ignored.

Manual `mode=doctor` is a separate setup action with no schedule. It requires tracking disabled, `dry_run=false`, `verify_write=true`, `send_alerts=false`, and an empty source filter. It also requires current budget/zero-cost checks and the workflow's WIF service-account credential file, rejecting local ADC and service-account-key fallback. The probe changes one system marker metadata cell, reads it back, restores its original value, and verifies restoration; it does not add run-log rows, collect sources, or refresh/send Gmail. A failed restoration cannot report success. Keep tracking disabled and inspect the private Sheet if verification fails. This mode shares the existing single-writer concurrency group and bounded four-minute manual timeout.

The private caller's sole `career-command-center-production` concurrency group uses `queue: max`. It does not cancel an active writer, and the reusable workflow does not repeat that group. No runtime job matrix, automatic rerun or production artifact upload is configured. Runtime permission is `contents: read` plus `id-token: write` for Sheets federation. Secret-bearing execution is limited to the expected private-runtime repository owner and main ref for schedule/manual events.

## Local fallback

Use local scheduling only after disabling GitHub production and selecting `SCHEDULER=local` in private configuration. `scripts/install_local_scheduler.sh --plan` prints the exact non-mutating plan. An explicit `--apply` additionally requires `GITHUB_SCHEDULE_DISABLED_ATTESTED=true`; it preserves unrelated crontab entries and adds one minute dispatcher. The dispatcher interprets all due times in Asia/Kolkata, uses an advisory local `fcntl` lock, and stores a bounded non-personal slot ledger under `.private`. It claims a slot before invoking the CLI so an ambiguous interruption is never automatically retried. Credentials stay in private `.env` configuration and never appear in the crontab.

On macOS, launchd may invoke the same `scripts/local_dispatch.py` once per minute instead of cron. Do not install both. Sleep/offline periods cause missed local invocations; the next regular reconciliation supplies recovery overlap. An advisory local lock and Sheet heartbeat are contention detectors, not distributed transactional locks. One configured scheduler is the correctness boundary.

## Investigating failures

Start with `career-radar doctor`, the private Sheet heartbeat, Run_Log and source attempt history. A 401/403, login wall or CAPTCHA requires policy/credential review or a manual route, never a bypass. A 429 requires its backoff; a changed schema requires quarantining the snapshot and updating parser contract fixtures. An empty or failed source must never trigger mass closure.

Budget expiry leaves `DEFERRED_BUDGET` or `PARTIAL_BUDGET` rows for fair replay. Investigate old CLAIMED local slots before manually retrying; the outbox records definitive or ambiguous send status and must suppress blind resend. Google failures should be inspected with credential scopes, Sheet sharing/protections and quota state, using aggregate/sanitized logs.

Export a private backup before any schema migration or repair. Restore from the private Sheet's version history or a verified local export, preserve user-owned application/contact columns, and replay only idempotent unfinished chunks. Never describe a sequence of Sheet batch requests as one atomic transaction.

Rotate credentials in private secret storage, disable scheduling first, and test with dry-run. HMAC key versions needed for retained suppression tokens require an explicit migration plan. Review action updates by resolving a maintained release to its full commit SHA, inspecting its change history, then updating pinned action SHAs and rerunning the full gates.

GitHub schedules may be delayed or dropped during load. A public-repository schedule can also be disabled after inactivity under GitHub's policy; this design places schedules in the private runtime repository. Last-run staleness and source due times remain visible independently of GitHub's UI.
