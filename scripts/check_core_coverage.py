"""Enforce actual branch coverage for every required core package; no omitted files."""

import json
from pathlib import Path

CORE = {
    "domain",
    "normalize",
    "dedupe",
    "scoring",
    "strategy",
    "contacts",
    "outreach",
    "notifications",
    "sheets",
    "security",
    "orchestration",
}


def main() -> None:
    report = json.loads(Path("coverage.json").read_text())
    totals = {name: [0, 0] for name in CORE}
    measured = set()
    failed = []
    for filename, row in report["files"].items():
        parts = Path(filename).parts
        if "career_radar" not in parts:
            continue
        index = parts.index("career_radar")
        if len(parts) <= index + 1 or parts[index + 1] not in CORE:
            continue
        package = parts[index + 1]
        measured.add(Path(filename).resolve())
        totals[package][0] += row["summary"]["covered_branches"]
        totals[package][1] += row["summary"]["num_branches"]
        branches = row["summary"]["num_branches"]
        if branches and row["summary"]["covered_branches"] / branches < 0.9:
            failed.append(filename)
    for package in CORE:
        for source in (Path("src/career_radar") / package).rglob("*.py"):
            if source.resolve() not in measured:
                failed.append(f"{source} (missing from report)")
    for name, (covered, total) in sorted(totals.items()):
        percentage = 100 * covered / total if total else 100.0
        print(f"{name}: {covered}/{total} branches ({percentage:.2f}%)")
    if failed:
        raise SystemExit("Core module coverage missing or below 90%: " + ", ".join(failed))


if __name__ == "__main__":
    main()
