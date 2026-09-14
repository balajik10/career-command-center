# Working in the private Sheet

The Google Sheet is the production source of truth. Local fixture workbooks are disposable demonstrations; a failed live Sheet read never falls back to local defaults. Keep the Sheet private and share it only with the configured automation identity. No workbook or export belongs in the public repository.

Open **Action_Queue**, act on overdue P0 and new P0 opportunities first, then P1 opportunities and due follow-ups. Review any outreach draft yourself. Send any permitted outreach manually. Record the result in a **new Action_Updates row**. The sorted queue and date views are protected so a row reorder cannot attach an edit to another job.

## Recording a change

1. Select an unused row in Action_Updates. Its preallocated UUID is protected.
2. Copy the stable `job_uid` from the queue. Choose application/referral/outreach changes and enter a note. Skipping requires a reason; a correction requires a reason starting `CORRECTION:`.
3. Check `submit` last. A submitted payload is immutable. Never change its job ID or reuse its UUID.
4. The next run records the original payload hash, applies the canonical change, and writes exactly one immutable Application_Events event in the same bounded Sheets request as the completion acknowledgement.
5. `APPLIED` confirms ingestion; `CHANGED_AFTER_SUBMIT`, `INVALID_TRANSITION`, an unknown ID, or ambiguous outreach target requires review. For a correction, use a new row. User payloads are never cleared or reordered.

If a job has several outreach drafts, a job-level outreach update is ambiguous and is rejected. Edit the intended canonical Outreach row manually. Discovery cannot set `SENT_MANUALLY`, `REPLIED`, or channel permissions. An imported connection remains `WARM_CONNECTION`; willingness to refer must be manually confirmed.

## Ownership and presentation

All 28 tabs are defined in `sheets/schema.py`. Pale yellow cells are editable; system columns, headers, immutable histories and all views are protected. Protected preallocated input UUIDs have a separate system registration. The five underscore-prefixed operational tabs are hidden. Contact details and low-level provenance columns are hidden by default. Hiding is a convenience, not access control: anyone with Sheet access can see its private data.

Application state lives in Applications, job facts in Jobs_Master, contact facts in Contacts, and drafts in Outreach. The [data dictionary](data-dictionary.md) identifies every key and user-owned column. Scheduled field-level writes exclude manual fields even if a stale fetched row contains them. An integration test injects a human edit between reading and writing and verifies it survives.

Today, This_Week and This_Month are protected formula views. The spreadsheet locale is `en_IN`, timezone `Asia/Kolkata`; ISO UTC source timestamps are converted by formulas to IST before date boundaries are compared. Weeks start Monday 00:00 IST. Today also contains separate sections for due, overdue, P0/P1 and follow-up actions. A missing publication date is not a newly posted job: the queue displays `POSTED DATE UNKNOWN` and its first-seen proxy.

## Dashboard definitions

The dashboard includes the median hours from first observation to manual application, recorded-stage funnel and current interview stages, freshness distribution, top ten role families, conversion tables by source/role/company tier, source coverage intervals, and three small overview charts. Date-window counts exclude future timestamps. “Last successful scan” includes committed discovery scans only, so a bootstrap, doctor probe or digest does not move that heartbeat.

Median time-to-apply uses all recorded applications with both timestamps, excludes negative/future intervals, and displays `NO RECORDED APPLICATIONS` when no valid samples exist. The funnel counts each canonical job once and retains explicitly recorded interview/offer history; skipped stages are not invented. A job without a recorded applied timestamp is excluded from application-conversion denominators even if its current workflow has advanced.

Source attribution means the first recorded `_Job_Sources` appearance. A later official or duplicate source does not create an additional application in the rates. Role comes from the canonical job; company tier comes from the user-assigned Companies value. Interview conversion is applied jobs with a recorded interview stage divided by applied jobs. Reply conversion is replied drafts with recorded sent timestamps divided by sent drafts. A reply stage or explicit positive/negative/received reply state records a reply; a generated draft alone is never counted as sent. These are descriptive rates, not claims that a source caused an interview or response. Empty denominators are labeled `NO APPLICATIONS` or `NO SENT OUTREACH` instead of displaying a misleading 0%.

Target-band opportunity counts require a configured positive private LPA target, an INR annual base minimum meeting that target, source evidence, an observed/exact confidence, and an opening that is neither closed nor excluded. No currency or salary is guessed. Target cadence and observed age since the last successful source check are separate metrics; configured cadence is not a guarantee of discovery latency.

The tables spill from canonical ranges through row 6000. Review/archive before exceeding that capacity. Empty distribution charts contain no fabricated data point. Bootstrap replaces only dashboard charts whose titles begin `Career Command Center — ` and preserves unrelated charts. The `dashboard_reference` function provides the same numerical denominator rules for audits of private offline snapshots; actual Google formula calculation remains a separate live smoke-test check when credentials are available. Chart request shapes follow Google's [official chart recipes](https://developers.google.com/workspace/sheets/api/samples/charts), verified 2026-09-14.

## Bootstrap and recovery

Run `career-radar bootstrap-sheet` after credential setup. Bootstrap creates or verifies the project/schema markers, headers, field ownership, protections, validations, formats, hidden columns and tabs, input UUID reserve, dashboard and formula views. A nonempty unrecognized workbook is rejected instead of adopted. Repeating bootstrap preserves existing user data and input UUIDs. Replenishment adds new rows before the reserve is exhausted; IDs are never recycled.

Only one automation writer may run at once. Use the private runtime workflow's shared concurrency group or the local process lock. Google Sheets does not provide row compare-and-swap. Its owner can override a protection; such edits are detected by input identity/payload checks and surfaced for manual review. There is no claim of atomicity across multiple HTTP requests.

One `values.batchUpdate` request is atomic. Larger writes are split on complete-row boundaries, tagged with `run_uid`, and accompanied by system chunk markers. Run_Log remains `IN_PROGRESS` until post-write validation and `COMMITTED` finalization. Replaying an interrupted run compares stable keys and updates only missing/changed machine cells. Immutable event keys cannot be reused for different content. A crash after an email claim means possible provider acceptance: reconcile `SENDING` to `AMBIGUOUS` and inspect it manually; never automatically resend it.

Use `career-radar export backup <private-directory>` for a private JSON snapshot. The export is owner-readable and contains sensitive data. Restore through a reviewed migration/import, or use Google Sheets version history; do not paste a whole old workbook over current user workflow. This schema version performs additive bootstrap only; changing existing column meaning/order requires an explicit backup and migration. Never remove canonical user data as an automated migration.

## Notification controls

The outbox stores HMAC recipient tokens, never the owner address. Each alert ID has at most one automatic transport attempt. Exactly five qualifying jobs can produce five messages; more than five produce four individual messages and one ranked bundle. Reduced daily capacity reserves one bundle slot first. The routine daily limit reserves two of twenty slots for digests, permitting at most eighteen immediate messages per IST date. Deferred members remain pending. A definitive provider acceptance produces SENT; a definitive rejection produces FAILED; response loss produces AMBIGUOUS. Gmail records its returned message ID; SMTP records deterministic headers and acceptance code with no invented provider ID.

Digest windows share a cursor. Successful delivery advances it; ambiguous delivery does not. An empty window creates a separate NO_CONTENT record and advances the cursor without mail. Repeating the same scheduled window is idempotent. The only setup send is the explicit `notify test` command, to the normalized configured owner address, with one attempt across reruns.

## API budgets

The implementation batches reads and caches indexes only within a serialized scan; Action_Updates is always reread when checking for interleaved edits. Field-level mutations update the cached machine index. The real REST adapter enforces per-run read/write limits, accounts for retries, limits requests to 45 per minute, and retries a quota response at most twice with bounded backoff. It returns a failure if authority is unavailable.

Google documents 60 reads and 60 writes per minute per user per project, separate project quotas, and request-level atomicity. Verified 2026-09-14: [Google Sheets API usage limits](https://developers.google.com/workspace/sheets/api/limits). Bootstrap is a setup operation with a larger request budget than a rapid scan. A sequence of setup batches is resumable, not atomic.
