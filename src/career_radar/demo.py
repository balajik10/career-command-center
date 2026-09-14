"""Fixed, credential-free, network-free release oracle. Never loads a private profile."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from importlib.resources import files
from typing import Any

import yaml

from career_radar.domain import CandidateProfile, RawJob, SourceDefinition
from career_radar.orchestration import ScanResult, SourceBatch, run_scan
from career_radar.orchestration.pipeline import load_sources
from career_radar.sheets import FakeWorkbook


def oracle() -> dict[str, Any]:
    return dict(json.loads(files("career_radar").joinpath("data/oracle.json").read_text()))


def demo_profile() -> CandidateProfile:
    return CandidateProfile.model_validate(
        yaml.safe_load(files("career_radar").joinpath("data/profile.yaml").read_text())
    )


def demo_book() -> FakeWorkbook:
    book = FakeWorkbook()
    book.bootstrap()
    book.upsert(
        "Profile",
        "evidence_id",
        [
            {
                "evidence_id": "profile",
                "kind": "candidate_profile",
                "value": demo_profile().model_dump_json(),
            }
        ],
        "bootstrap-demo",
        actor="user",
    )
    return book


def run_demo(
    book: FakeWorkbook | None = None, *, dry_run: bool = True, run_uid: str = "offline-demo"
) -> ScanResult:
    workbook = book or demo_book()
    data = oracle()
    observed: dict[str, list[RawJob]] = defaultdict(list)
    for row in data["observations"]:
        raw = RawJob.model_validate(row)
        observed[raw.source_id].append(raw)
    saved_sources = {
        source.source_id: source for source in load_sources(workbook.read_tab("Sources"))
    }
    batches = [
        SourceBatch(
            saved_sources.get(key)
            or SourceDefinition(
                source_id=key,
                company="Synthetic source",
                provider=jobs[0].provider,
                url="https://careers.example.com",
                access_mode="USER_IMPORT",
                policy_state="MANUAL_ONLY",
                cost_class="free",
            ),
            jobs,
        )
        for key, jobs in observed.items()
    ]
    return run_scan(
        workbook,
        batches,
        now=datetime.fromisoformat(data["now"]),
        run_uid=run_uid,
        dry_run=dry_run,
        send_alerts=False,
    )
