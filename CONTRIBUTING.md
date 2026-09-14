# Contributing

Start with an offline synthetic reproduction. Never attach a real resume, alert email, contact export, browser session, or application record.

Run `uv sync --locked`, then `make check` and `make build`. Core package branch coverage must remain at least 90% for every required package. Do not weaken oracle counts or exclude meaningful source files to meet coverage.

A connector is supported only after its documented interface, policy boundary, empty/schema/blocked/malformed/oversized/duplicate contracts, and fixtures are reviewed. A parser does not approve a source. Expansion adapters stay disabled until they meet the same contract.

Keep diffs narrow, explain changed behavior and trade-offs, and update docs and fixtures with the same change. Update pinned Actions SHAs by checking their official release references and reviewing the diff. Runtime callers and implementation references must use the same reviewed public SHA.
