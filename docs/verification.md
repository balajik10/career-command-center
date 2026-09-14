# Release verification

Verification date: **2026-09-14**. Runtime: CPython 3.12.9 on macOS arm64; dependency resolution uses the committed `uv.lock`. Public CI repeats the gates on Ubuntu with Python 3.12. Local results do not claim that GitHub CI or live Google integrations have run.

## Reproducible behavior

The fixed synthetic oracle produces 12 observations, 9 canonical jobs, 2 P0, 2 P1, 3 P2, 1 P3, and 1 excluded job. It creates 4 drafts and 2 notification decisions. The identical second run creates **zero new jobs, drafts, or alerts** and retains the same canonical counts. No email is sent.

`scripts/verify_offline.py` clears environment credentials, changes to an empty temporary directory, denies socket connections and DNS resolution, and exercises seven installed CLI commands: doctor, offline Sheet bootstrap, fixture scan, no-send digest, source check, the two-run demo, and a synthetic contact import. The contact import uses a synthetic local HMAC key and creates private staging only. The script also confirms the imported package location so a release can be checked outside the source tree.

The browser preview was inspected at wide and narrow layouts. Search, priority filtering, job detail drawers, source coverage, evidence-library navigation, and synthetic-data labeling were checked in the actual browser. The published screenshot contains authored synthetic data.

## Local gates

| Gate | Result |
|---|---|
| Complete pytest suite | **448 passed** |
| Actual branch coverage of every required core module | PASS, minimum 90%; enforced by `check_core_coverage.py` |
| Ruff lint and formatting | PASS |
| `mypy --strict src/career_radar` | PASS, 30 source files |
| Bandit source scan | PASS; narrowly documented false-positive annotations only |
| `pip-audit` | No known dependency vulnerabilities found; local unpublished project has no PyPI advisory record |
| Gitleaks 8.30.1 | No leaks in prepared public files; official release checksum verified |
| Public boundary check | No private artifacts, personal email addresses, or detected credentials in the reviewed public file set |
| Distribution build | Wheel and source archive build; both checked for private files and bundled catalogue |
| Fresh lockfile installation | PASS in a new temporary environment with a non-editable package |
| Account-wide scheduling budget model | 1,176 scheduled + 200 reserve = 1,376 private Actions minutes before separate account reserve |

The coverage gate checks report completeness and each Python file under domain, normalize, dedupe, scoring, strategy, contacts, outreach, notifications, sheets, security, and orchestration. Files with no branches are reported as such, not assigned invented branch counts. The final machine-readable coverage report is local and ignored by Git.

Security regressions cover private-address/redirect/DNS-rebinding requests, blocked platforms, XML entities and parser limits, formula injection, untrusted text, recipient isolation, OAuth scope/account mismatch, contact provenance and suppression, private export permissions, and ambiguous email delivery. Integration regressions cover immutable actions, preserving user columns, dry-run isolation, partial failures, parse-yield drops, repeated source misses, crash/replay, mandatory-source starvation, response-time backoff, and Greenhouse checkpoint recovery.

The Sheet dashboard has a reviewed numeric reference calculation and formula-structure tests for denominators, attribution, timing, and chart source ranges. Those are **not** a live Google formula-engine evaluation. A credentialed smoke test must confirm actual rendering and calculations before activation.

## Live state and limitations

- **Automated company sources enabled: 0.** Catalogue: 280 companies, 277 pending/unvalidated rows, 3 verified manual/native-alert routes. Parsers are fixture-tested; catalogue size is not live coverage.
- **Sheet:** not created or connected. Bootstrap targets a user-created empty private spreadsheet. Live formatting, permissions, formulas, quota behavior, and write restoration remain credential-dependent.
- **Email:** no live send attempt, no mailbox ingestion. Owner-send OAuth/SMTP code is tested with fakes. Gmail/IMAP collection, Workable, SmartRecruiters, Hacker News, and headless collection remain deferred or disabled as listed in the support matrix.
- **Schedules:** present but disabled. The model estimates the worst calendar month; it does not verify this account's current shared allowance. Billing/overage attestations are mandatory before GitHub scheduling.
- **GitHub:** local release code is prepared. Repository creation, publication, remote CI, and private dispatch results are reported separately after owner approval. No remote pass is inferred from local tests.
- **Configuration:** scoring weights and data-quality thresholds are Sheet-configurable; role/skill taxonomy and dedupe tuning limitations are described in `scoring.md`.
- **Storage/recovery:** Google Sheets lacks multi-request database transactions. Use one scheduler/writer, bounded batch writes, post-write commit validation, and outbox recovery. Never infer exactly-once external delivery from deterministic notification IDs.

Complete the explicit checks in [SETUP_REQUIRED.md](../SETUP_REQUIRED.md) before enabling live operation.
