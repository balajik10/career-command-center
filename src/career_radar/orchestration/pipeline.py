from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from career_radar.contacts import rank_contacts
from career_radar.dedupe import upsert_job
from career_radar.domain import (
    AlertDecision,
    CandidateProfile,
    Contact,
    JobScore,
    NormalizedJob,
    OutreachDraft,
    RawJob,
    RunSummary,
    SourceDefinition,
)
from career_radar.normalize import freshness_band, normalize_job
from career_radar.notifications import Outbox, plan_messages, render_job, select_immediate
from career_radar.orchestration.planner import BUDGETS, finish_source
from career_radar.outreach import generate_drafts
from career_radar.scoring import ScoringConfig, score_job
from career_radar.security import redact
from career_radar.sheets import SCHEMA, BaseWorkbook, FakeWorkbook, process_action_updates
from career_radar.sheets.views import refresh_action_queue
from career_radar.strategy import build_brief


def uid(*values: str) -> str:
    return hashlib.sha256("\x1f".join(values).encode()).hexdigest()


def row_for(tab: str, values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k in SCHEMA[tab].columns}


def json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def load_profile(rows: list[dict[str, Any]]) -> CandidateProfile:
    for row in rows:
        if row.get("evidence_id") == "profile":
            return CandidateProfile.model_validate(json_value(row["value"]))
    raise ValueError("PRIVATE_SHEET_PROFILE_REQUIRED")


def load_sources(rows: list[dict[str, Any]]) -> list[SourceDefinition]:
    result = []
    for row in rows:
        data = json_value(row.get("model_json") or "{}")
        for key, value in row.items():
            if key in SourceDefinition.model_fields and value not in ("", None):
                if key in {"allowed_hosts", "metadata"}:
                    value = json_value(value)
                data[key] = value
        stop = data.get("metadata", {}).get("policy_stop")
        if stop in {"POLICY_BLOCKED", "LOGIN_REQUIRED"}:
            data["policy_state"] = stop
        result.append(SourceDefinition.model_validate(data))
    return result


def load_jobs(rows: list[dict[str, Any]]) -> list[NormalizedJob]:
    result = []
    for row in rows:
        job = NormalizedJob.model_validate(json_value(row["model_json"]))
        job.do_not_merge = row.get("do_not_merge") in (True, "TRUE", "true")
        result.append(job)
    return result


def hydrate_source_checkpoints(
    sources: list[SourceDefinition],
    rows: list[dict[str, Any]],
    *,
    jobs: list[dict[str, Any]],
    appearances: list[dict[str, Any]],
) -> None:
    """Load bounded per-job committed checkpoints into an ephemeral fetch cache."""
    by_source = {source.source_id: source for source in sources if source.provider == "greenhouse"}
    canonical_ids = {job.get("job_uid") for job in jobs}
    committed = {
        (row.get("source_id"), row.get("external_job_id"), row.get("job_uid"))
        for row in appearances
        if row.get("job_uid") in canonical_ids
    }
    for cached_source in by_source.values():
        cached_source.metadata["_greenhouse_seen_versions"] = {}
    for row in rows:
        if not str(row.get("key", "")).startswith("greenhouse_checkpoint:"):
            continue
        try:
            value = json_value(row.get("value", {}))
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        source = by_source.get(str(value.get("source_id", "")))
        external_id, version = value.get("external_job_id"), value.get("version")
        if (
            source is not None
            and isinstance(external_id, str)
            and external_id
            and isinstance(version, str)
            and len(version) == 64
            and all(char in "0123456789abcdef" for char in version)
            and isinstance(value.get("job_uid"), str)
            and bool(value["job_uid"])
            and (source.source_id, external_id, value.get("job_uid")) in committed
            and row["key"] == "greenhouse_checkpoint:" + uid(source.source_id, external_id)
        ):
            source.metadata["_greenhouse_seen_versions"][external_id] = version


def load_contacts(rows: list[dict[str, Any]]) -> list[Contact]:
    result = []
    for row in rows:
        values = {
            key: value for key, value in row.items() if key in Contact.model_fields and value != ""
        }
        if "field_provenance" in values:
            values["field_provenance"] = json_value(values["field_provenance"])
        result.append(Contact.model_validate(values))
    return result


@dataclass
class SourceBatch:
    source: SourceDefinition
    jobs: list[RawJob]
    outcome: str = "SUCCESS_COMPLETE"
    complete: bool = True
    requests: int = 0
    byte_count: int = 0
    error: str = ""
    backoff_until: datetime | None = None
    checkpoint: dict[str, str] = field(default_factory=dict)
    listed_ids: list[str] = field(default_factory=list)
    listing_complete: bool = False


@dataclass
class ScanResult:
    summary: RunSummary
    jobs: list[NormalizedJob]
    scores: list[JobScore]
    briefs: list[dict[str, Any]]
    drafts: list[OutreachDraft]
    workbook: BaseWorkbook
    email_preview: str


@dataclass(frozen=True)
class YieldPolicy:
    drop_threshold: float = 0.7
    min_completed_samples: int = 3
    min_baseline_jobs: int = 10

    def __post_init__(self) -> None:
        if (
            not 0 < self.drop_threshold < 1
            or self.min_completed_samples < 1
            or self.min_baseline_jobs < 1
        ):
            raise ValueError("INVALID_YIELD_POLICY")


def _yield_anomaly(
    batch: SourceBatch, history: list[dict[str, Any]], now: datetime, policy: YieldPolicy
) -> dict[str, Any]:
    if (
        not batch.complete
        or batch.outcome not in {"SUCCESS_COMPLETE", "SUCCESS_EMPTY"}
        or batch.source.access_mode not in {"OFFICIAL_API", "PUBLIC_HTML_APPROVED", "RSS_ATOM"}
    ):
        return {}
    counts: list[int] = []
    for row in history:
        if (
            row.get("source_id") != batch.source.source_id
            or row.get("attempt_outcome") not in {"SUCCESS_COMPLETE", "SUCCESS_EMPTY"}
            or row.get("snapshot_complete") not in (True, "TRUE", "true")
        ):
            continue
        try:
            instant = datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
            count = int(row["jobs"])
        except (ValueError, TypeError, KeyError):
            continue
        if instant.tzinfo is not None and now - timedelta(days=14) <= instant <= now and count >= 0:
            counts.append(count)
    if len(counts) < policy.min_completed_samples:
        return {}
    baseline = float(median(counts))
    observed = (
        len(set(batch.listed_ids))
        if batch.listing_complete
        else len({(raw.provider, raw.tenant, raw.provider_job_id or raw.url) for raw in batch.jobs})
    )
    if (
        baseline < policy.min_baseline_jobs
        or (baseline - observed) / baseline <= policy.drop_threshold
    ):
        return {}
    return {
        "trailing_14_day_median": baseline,
        "observed": observed,
        "drop_fraction": 1 - observed / baseline,
        "samples": len(counts),
        "observed_at": now.isoformat(),
    }


def commit_rows(book: BaseWorkbook, updates: dict[str, list[dict[str, Any]]], run_uid: str) -> None:
    """Checkpoint each bounded request; multi-request runs are explicitly not atomic."""
    pending: dict[str, list[dict[str, Any]]] = {}
    size = 0
    chunk = 0
    for tab, rows in updates.items():
        for row in rows:
            if size + len(row) > 7500:
                _commit_chunk(book, pending, run_uid, chunk)
                chunk += 1
                pending, size = {}, 0
            pending.setdefault(tab, []).append(row)
            size += len(row)
    if pending:
        _commit_chunk(book, pending, run_uid, chunk)


def _commit_chunk(
    book: BaseWorkbook, rows: dict[str, list[dict[str, Any]]], run_uid: str, chunk: int
) -> None:
    rows.setdefault("Run_Log", []).append(
        {"run_uid": run_uid, "status": "IN_PROGRESS", "chunks": chunk + 1}
    )
    rows.setdefault("_System_State", []).append(
        {"key": f"chunk:{run_uid}:{chunk}", "value": "APPLIED", "run_uid": run_uid}
    )
    book.transaction(rows, run_uid, actor="machine")


def _run_scan(
    book: BaseWorkbook,
    batches: list[SourceBatch],
    *,
    now: datetime,
    run_uid: str,
    send_alerts: bool = False,
    recipient: str = "candidate@example.com",
    hmac_key: bytes = b"synthetic-demo-key-for-local-fixtures-only",
    outbox: Outbox | None = None,
    mode: str = "incremental",
) -> ScanResult:
    book.require_schema()
    process_action_updates(book, run_uid, now=now)
    state = book.read_tabs(
        [
            "Jobs_Master",
            "Profile",
            "Config",
            "Applications",
            "Contacts",
            "Job_Contacts",
            "Outreach",
            "_Alerts",
            "_Job_Sources",
            "Job_History",
            "_System_State",
            "_Source_Attempts",
        ]
    )
    profile = load_profile(state["Profile"])
    config_rows = {row["key"]: row.get("value") for row in state["Config"]}
    cfg = (
        ScoringConfig(**json_value(config_rows["scoring"]))
        if config_rows.get("scoring")
        else ScoringConfig()
    )
    jobs = load_jobs(state["Jobs_Master"])
    yield_policy = (
        YieldPolicy(**json_value(config_rows["data_quality"]))
        if config_rows.get("data_quality")
        else YieldPolicy()
    )
    original_jobs = {job.job_uid: job.model_copy(deep=True) for job in jobs}
    overrides = {
        row["job_uid"]: dict(json_value(row.get("manual_override_flags") or "{}"))
        for row in state["Jobs_Master"]
    }
    old_scores = {
        row["job_uid"]: JobScore.model_validate(json_value(row["score_json"]))
        for row in state["Jobs_Master"]
    }
    applications = {row["job_uid"]: row for row in state["Applications"]}
    contacts = load_contacts(state["Contacts"])
    old_source_rows = {row["job_source_uid"]: row for row in state["_Job_Sources"]}
    old_events = {row["job_event_uid"] for row in state["Job_History"]}
    old_attempts = {row["attempt_uid"] for row in state["_Source_Attempts"]}
    missing_counts = {
        row["key"].removeprefix("source_missing:"): dict(json_value(row["value"]))
        for row in state["_System_State"]
        if row.get("key", "").startswith("source_missing:")
    }
    existing_drafts = {row["draft_id"] for row in state["Outreach"]}
    checkpoint_rows: list[dict[str, Any]] = []
    new_ids, changed_ids = set(), set()
    updates: dict[str, list[dict[str, Any]]] = {
        "_Job_Sources": [],
        "Job_History": [],
        "_Source_Attempts": [],
        "Sources": [],
    }
    summary = RunSummary(run_uid=run_uid, started_at=now, mode=mode)
    for original_batch in batches:
        batch = SourceBatch(
            original_batch.source,
            original_batch.jobs,
            original_batch.outcome,
            original_batch.complete,
            original_batch.requests,
            original_batch.byte_count,
            original_batch.error,
            original_batch.backoff_until,
            original_batch.checkpoint,
            original_batch.listed_ids,
            original_batch.listing_complete,
        )
        if not batch.complete and batch.outcome in {
            "SUCCESS_COMPLETE",
            "SUCCESS_EMPTY",
            "NO_CHANGE",
        }:
            batch.outcome = "PARTIAL_BUDGET"
        source = batch.source.model_copy(deep=True)
        # Per-job checkpoints have their own rows; never embed an unbounded cache
        # in Sources metadata or its model_json cell.
        source.metadata.pop("_greenhouse_seen_versions", None)
        anomaly = _yield_anomaly(batch, state["_Source_Attempts"], now, yield_policy)
        if anomaly:
            batch.complete, batch.outcome = False, "QUARANTINED_SCHEMA"
            batch.error = "PARSE_YIELD_DROP"
            source = source.model_copy(deep=True)
            source.metadata["yield_anomaly"] = anomaly
            updates.setdefault("Quarantine", []).append(
                {
                    "quarantine_uid": uid(source.source_id, run_uid, "PARSE_YIELD_DROP"),
                    "source_id": source.source_id,
                    "reason": "PARSE_YIELD_DROP",
                    "excerpt": json.dumps(anomaly, sort_keys=True),
                    "state": "NEEDS_REVIEW",
                    "created_at": now.isoformat(),
                    "run_uid": run_uid,
                }
            )
        seen: set[str] = set()
        valid_jobs = []
        for raw in batch.jobs:
            summary.observations += 1
            try:
                if raw.source_id != source.source_id:
                    raise ValueError("SOURCE_ID_MISMATCH")
                incoming = normalize_job(raw, now, baseline=source.baseline_state != "COMPLETED")
            except (ValueError, TypeError):
                updates.setdefault("Quarantine", []).append(
                    {
                        "quarantine_uid": uid(
                            source.source_id, raw.provider_job_id, "NORMALIZATION"
                        ),
                        "source_id": source.source_id,
                        "reason": "NORMALIZATION_INVALID",
                        "excerpt": "",
                        "state": "NEEDS_REVIEW",
                        "created_at": now.isoformat(),
                        "run_uid": run_uid,
                    }
                )
                batch.complete = False
                batch.outcome = "QUARANTINED_SCHEMA"
                continue
            result = upsert_job(jobs, incoming)
            jobs = result.jobs
            job = result.job
            job.source_health = source.health_state
            seen.add(job.job_uid)
            valid_jobs.append(job)
            version = batch.checkpoint.get(raw.provider_job_id)
            if (
                source.provider == "greenhouse"
                and version
                and version == raw.metadata.get("greenhouse_listing_version")
            ):
                checkpoint_rows.append(
                    {
                        "key": "greenhouse_checkpoint:"
                        + uid(source.source_id, raw.provider_job_id),
                        "value": {
                            "source_id": source.source_id,
                            "external_job_id": raw.provider_job_id,
                            "version": version,
                            "job_uid": job.job_uid,
                        },
                        "run_uid": run_uid,
                    }
                )
            if result.new:
                new_ids.add(job.job_uid)
            elif result.changed:
                changed_ids.add(job.job_uid)
            for event in result.audit:
                event_type = str(event["event"])
                event_id = uid(
                    job.job_uid,
                    event_type,
                    job.description_hash,
                    str(job.material_change_sequence),
                    source.source_id if event_type == "MERGED" else "",
                )
                if event_id not in old_events:
                    updates["Job_History"].append(
                        row_for(
                            "Job_History",
                            {
                                **event,
                                "changed_fields": list(event["diff"]),
                                "after_hash": job.description_hash,
                                "source_id": source.source_id,
                                "job_event_uid": event_id,
                                "job_uid": job.job_uid,
                                "event_type": event_type,
                                "occurred_at": now.isoformat(),
                                "run_uid": run_uid,
                            },
                        )
                    )
                    old_events.add(event_id)
            observation_uid = uid(source.source_id, raw.provider_job_id or raw.url, job.job_uid)
            previous = old_source_rows.get(observation_uid, {})
            old_source_rows[observation_uid] = {
                "job_source_uid": observation_uid,
                "job_uid": job.job_uid,
                "source_id": source.source_id,
                "external_job_id": raw.provider_job_id,
                "listing_url": incoming.canonical_url,
                "apply_url": incoming.apply_url,
                "first_seen_at": previous.get("first_seen_at", now.isoformat()),
                "last_seen_at": now.isoformat(),
                "content_hash": incoming.description_hash,
                "active": True,
                "run_uid": run_uid,
            }
        reopened = _apply_listing_presence(batch, seen, old_source_rows, jobs, now, run_uid)
        changed_ids.update(reopened)
        for reopened_job in jobs:
            if reopened_job.job_uid in reopened:
                updates["Job_History"].append(
                    {
                        "job_event_uid": uid(
                            reopened_job.job_uid,
                            "REOPENED",
                            str(reopened_job.material_change_sequence),
                        ),
                        "job_uid": reopened_job.job_uid,
                        "event_type": "REOPENED",
                        "occurred_at": now.isoformat(),
                        "run_uid": run_uid,
                    }
                )
        _update_missing(batch, seen, old_source_rows, missing_counts, now)
        finished = finish_source(
            source,
            now,
            batch.outcome,
            complete=batch.complete,
            backoff_until=batch.backoff_until,
            anomaly=bool(anomaly),
        )
        for observed_job in jobs:
            if observed_job.job_uid in seen:
                observed_job.source_health = finished.health_state
        updates["Sources"].append(
            row_for(
                "Sources",
                {
                    **finished.model_dump(mode="json"),
                    "model_json": finished.model_dump_json(),
                    "last_outcome": batch.outcome,
                },
            )
        )
        attempt = {
            "attempt_uid": uid(run_uid, source.source_id),
            "run_uid": run_uid,
            "source_id": source.source_id,
            "snapshot_kind": "INITIAL_BASELINE"
            if source.baseline_state != "COMPLETED"
            else ("FULL_RECONCILIATION" if mode == "full" else "INCREMENTAL"),
            "attempt_outcome": batch.outcome,
            "started_at": now.isoformat(),
            "completed_at": now.isoformat(),
            "due_at": source.next_due_at.isoformat() if source.next_due_at else "",
            "requests": batch.requests,
            "bytes": batch.byte_count,
            "jobs": len(set(batch.listed_ids)) if batch.listing_complete else len(valid_jobs),
            "snapshot_complete": batch.complete,
            "watermark_advanced": finished.last_success_at != source.last_success_at,
            "missing_counter_advanced": batch.complete
            and batch.outcome in {"SUCCESS_COMPLETE", "SUCCESS_EMPTY"},
            "health_before": source.health_state,
            "health_after": finished.health_state,
            "error": redact(batch.error)[:500],
        }
        if attempt["attempt_uid"] not in old_attempts:
            updates["_Source_Attempts"].append(attempt)
        summary.source_counts[batch.outcome] = summary.source_counts.get(batch.outcome, 0) + 1
        if batch.error:
            summary.errors.append(redact(f"{source.source_id}:{batch.error}")[:500])
    change_events: dict[str, str] = {}
    for job in jobs:
        appearances = [row for row in old_source_rows.values() if row["job_uid"] == job.job_uid]
        job.missing_snapshots = min(
            (
                int(missing_counts.get(row["job_source_uid"], {}).get("count", 0))
                for row in appearances
            ),
            default=0,
        )
        if job.missing_snapshots >= 2:
            before = job.active_state
            job.active_state = "CLOSED" if job.missing_snapshots >= 3 else "POSSIBLY_CLOSED"
            job.last_missing_at = datetime.fromisoformat(
                max(missing_counts[row["job_source_uid"]]["last_missing_at"] for row in appearances)
            )
            event_id = uid(
                job.job_uid,
                job.active_state,
                str(job.missing_snapshots),
                str(job.material_change_sequence),
            )
            if before != job.active_state and event_id not in old_events:
                updates["Job_History"].append(
                    {
                        "job_event_uid": event_id,
                        "job_uid": job.job_uid,
                        "event_type": job.active_state,
                        "occurred_at": now.isoformat(),
                        "run_uid": run_uid,
                    }
                )
        event_name = _change_event(original_jobs.get(job.job_uid), job)
        if event_name:
            change_events[job.job_uid] = event_name
    updates["_Job_Sources"] = list(old_source_rows.values())
    updates["_System_State"] = [
        {"key": "source_missing:" + key, "value": value, "run_uid": run_uid}
        for key, value in missing_counts.items()
    ]
    scores, briefs, drafts = [], [], []
    updates["Jobs_Master"], updates["Job_Briefs"], updates["Outreach"], updates["Job_Contacts"] = (
        [],
        [],
        [],
        [],
    )
    for job in jobs:
        matches = rank_contacts(job, contacts, now)
        readiness = matches[0].candidate_type if matches else "NONE"
        override = overrides.get(job.job_uid, {})
        permitted_override = bool(override.get("reason"))
        app = applications.get(job.job_uid, {})
        score = score_job(
            job,
            profile,
            now,
            contact_readiness=readiness,
            application_stage=str(app.get("application_stage", "")),
            config=cfg,
            manual_include=bool(permitted_override and override.get("include")),
            manual_priority=float(override["priority"])
            if permitted_override and "priority" in override
            else None,
        )
        old_score = old_scores.get(job.job_uid)
        if old_score and old_score.action_by_ist and score.action_by_ist:
            score.action_by_ist = min(score.action_by_ist, old_score.action_by_ist)
        scores.append(score)
        elapsed = max(0.0, (now - job.last_seen_at).total_seconds() / 3600)
        current_age = None if job.age_hours_max is None else job.age_hours_max + elapsed
        job.freshness_band = freshness_band(current_age, job.baseline)
        # Keep only a capped factual excerpt; descriptions are never archived as full pages.
        persisted = job.model_copy(deep=True)
        persisted.description = persisted.description[:2000]
        values = {
            **persisted.model_dump(mode="json"),
            **score.model_dump(mode="json"),
            "application_stage": app.get("application_stage", "NOT_REVIEWED"),
            "referral_stage": app.get("referral_stage", "NOT_STARTED"),
            "model_json": persisted.model_dump_json(),
            "score_json": score.model_dump_json(),
            "schema_version": "1",
        }
        values.pop("do_not_merge", None)
        updates["Jobs_Master"].append(row_for("Jobs_Master", values))
        for relation in matches[:3]:
            updates["Job_Contacts"].append(
                row_for(
                    "Job_Contacts",
                    {
                        "job_contact_uid": uid(job.job_uid, relation.contact_uid),
                        **relation.model_dump(mode="json"),
                    },
                )
            )
        if score.action_priority in {"P0", "P1"}:
            brief = build_brief(job, score, profile, now)
            briefs.append(brief)
            updates["Job_Briefs"].append(
                {
                    "brief_id": brief["brief_uid"],
                    "job_uid": job.job_uid,
                    "body_json": json.dumps(brief, sort_keys=True),
                    "generated_at": now.isoformat(),
                }
            )
            contact = next(
                (c for c in contacts if matches and c.contact_uid == matches[0].contact_uid), None
            )
            try:
                generated = generate_drafts(
                    job, score, profile, now, contact=contact, existing_ids=existing_drafts
                )
            except ValueError:
                generated = []
                updates.setdefault("Quarantine", []).append(
                    {
                        "quarantine_uid": uid(job.job_uid, "DRAFT_VALIDATION"),
                        "source_id": job.provenance[0].source_id,
                        "reason": "DRAFT_VALIDATION_FAILED",
                        "excerpt": "",
                        "state": "NEEDS_REVIEW",
                        "created_at": now.isoformat(),
                        "run_uid": run_uid,
                    }
                )
            drafts.extend(generated)
            existing_drafts.update(d.draft_id for d in generated)
            updates["Outreach"].extend(
                row_for("Outreach", d.model_dump(mode="json")) for d in generated
            )
    commit_rows(book, updates, run_uid)
    # A source-observed prohibition may revoke permission, never grant it. Only
    # this cell changes; routine discovery still excludes every user-owned field.
    stops = [
        {"source_id": row["source_id"], "policy_state": row["policy_state"]}
        for row in updates["Sources"]
        if row.get("policy_state") in {"POLICY_BLOCKED", "LOGIN_REQUIRED"}
    ]
    if stops:
        book.transaction({"Sources": stops}, run_uid, actor="system")
    alerts = select_immediate(
        jobs,
        scores,
        state["_Alerts"],
        now=now,
        hmac_key=hmac_key,
        recipient=recipient,
        change_events=change_events,
    )
    active_outbox = outbox or Outbox(book, recipient, hmac_key)
    previews = [
        render_job(job, score)
        for job, score in zip(jobs, scores, strict=True)
        if score.action_priority in {"P0", "P1"}
    ]
    summary.canonical_jobs = len(jobs)
    summary.new_jobs = len(new_ids)
    summary.changed_jobs = len(changed_ids - new_ids)
    summary.new_drafts = len(drafts)
    summary.alerts = len(alerts)
    summary.priority_counts = dict(Counter(s.action_priority for s in scores))
    summary.completed_at = now
    refresh_action_queue(book, run_uid, now=now)
    # Validate and commit canonical data before any outbox claim or transport call.
    book.finalize_run(run_uid, row_for("Run_Log", summary.model_dump(mode="json")))
    # Only details whose canonical rows committed may be skipped by a later run.
    # A crash before this point costs a refetch; it cannot permanently lose a job.
    if checkpoint_rows:
        commit_rows(book, {"_System_State": checkpoint_rows}, run_uid)
    active_outbox.enqueue(alerts, run_uid)
    if send_alerts:
        by_job = {job.job_uid: (job, score) for job, score in zip(jobs, scores, strict=True)}
        pending = [
            AlertDecision.model_validate(
                {
                    field: value
                    for field, value in row.items()
                    if field in AlertDecision.model_fields
                }
            )
            for row in state["_Alerts"]
            if row.get("state") == "PENDING"
            and row.get("alert_type") in {"P0", "UPDATE"}
            and row.get("job_uid") in by_job
            and by_job[row["job_uid"]][1].action_priority in {"P0", "P1"}
        ]
        for plan in plan_messages([*pending, *alerts], state["_Alerts"], now=now):
            entries = [by_job[decision.job_uid] for decision in plan.members]
            first_job = entries[0][0]
            label = (
                "JOB UPDATE"
                if any(member.alert_type == "UPDATE" for member in plan.members)
                else "P0 APPLY NOW"
            )
            subject = (
                f"[{label}] {first_job.company} - {first_job.title}"
                if len(entries) == 1
                else f"[{label}] {len(entries)} opportunities"
            )
            active_outbox.deliver(
                plan, subject, "\n\n".join(render_job(j, s) for j, s in entries), run_uid, now=now
            )
    # Enqueue itself checkpoints a write; finish its bookkeeping after delivery.
    book.finalize_run(run_uid, row_for("Run_Log", summary.model_dump(mode="json")))
    return ScanResult(summary, jobs, scores, briefs, drafts, book, "\n\n".join(previews))


def _change_event(previous: NormalizedJob | None, current: NormalizedJob) -> str:
    if previous is None:
        return ""
    if (
        current.active_state in {"REOPENED", "CLOSED"}
        and current.active_state != previous.active_state
    ):
        return current.active_state
    if previous.apply_url != current.apply_url:
        return "APPLY_URL_REPLACED"
    if current.deadline and (previous.deadline is None or current.deadline < previous.deadline):
        return "EARLIER_DEADLINE"
    if previous.location != current.location:
        return "LOCATION_CHANGED"
    if (previous.min_years, previous.max_years, previous.education_constraint) != (
        current.min_years,
        current.max_years,
        current.education_constraint,
    ):
        return "ELIGIBILITY_CHANGED"
    return ""


def _apply_listing_presence(
    batch: SourceBatch,
    seen: set[str],
    appearances: dict[str, dict[str, Any]],
    jobs: list[NormalizedJob],
    now: datetime,
    run_uid: str,
) -> set[str]:
    """A current inventory proves presence even when an unchanged detail was skipped."""
    if not batch.listing_complete:
        return set()
    listed = set(batch.listed_ids)
    present = set()
    for row in appearances.values():
        if row["source_id"] == batch.source.source_id and row["external_job_id"] in listed:
            present.add(row["job_uid"])
            row.update(last_seen_at=now.isoformat(), active=True, run_uid=run_uid)
    reopened = set()
    for job in jobs:
        if job.job_uid not in present or job.job_uid in seen:
            continue
        # Stored age bounds are measured at last_seen_at. Advancing observation
        # time must also advance these bounds, so cached listings never become fresh again.
        elapsed = max(0.0, (now - job.last_seen_at).total_seconds() / 3600)
        if job.age_hours_min is not None:
            job.age_hours_min += elapsed
        if job.age_hours_max is not None:
            job.age_hours_max += elapsed
        job.last_seen_at = max(job.last_seen_at, now)
        job.missing_snapshots = 0
        job.last_missing_at = None
        if job.active_state in {"CLOSED", "POSSIBLY_CLOSED"}:
            job.active_state = "REOPENED"
            job.last_changed_at = now
            job.material_change_sequence += 1
            reopened.add(job.job_uid)
    seen.update(present)
    return reopened


def _update_missing(
    batch: SourceBatch,
    seen: set[str],
    appearances: dict[str, dict[str, Any]],
    counters: dict[str, dict[str, Any]],
    now: datetime,
) -> None:
    for observation_uid, row in appearances.items():
        if row["source_id"] != batch.source.source_id:
            continue
        if row["job_uid"] in seen:
            counters[observation_uid] = {"count": 0, "last_missing_at": ""}
            row["active"] = True
        elif (
            batch.source.access_mode in {"OFFICIAL_API", "PUBLIC_HTML_APPROVED"}
            and batch.source.provider != "google_careers"
            and batch.complete
            and batch.outcome in {"SUCCESS_COMPLETE", "SUCCESS_EMPTY"}
        ):
            previous = counters.get(observation_uid, {"count": 0, "last_missing_at": ""})
            last = (
                datetime.fromisoformat(previous["last_missing_at"])
                if previous["last_missing_at"]
                else None
            )
            if last is None or now - last >= timedelta(hours=12):
                counters[observation_uid] = {
                    "count": int(previous["count"]) + 1,
                    "last_missing_at": now.isoformat(),
                }
                row["active"] = counters[observation_uid]["count"] < 3


def run_scan(
    book: BaseWorkbook,
    batches: list[SourceBatch],
    *,
    now: datetime,
    run_uid: str,
    dry_run: bool = False,
    send_alerts: bool = False,
    recipient: str = "candidate@example.com",
    hmac_key: bytes = b"synthetic-demo-key-for-local-fixtures-only",
    outbox: Outbox | None = None,
    mode: str = "incremental",
) -> ScanResult:
    if now.tzinfo is None or not run_uid:
        raise ValueError("AWARE_TIME_AND_RUN_UID_REQUIRED")
    if dry_run and send_alerts:
        raise ValueError("DRY_RUN_CANNOT_SEND")
    if mode not in BUDGETS:
        raise ValueError("INVALID_SCAN_MODE")
    if sum(batch.requests for batch in batches) > BUDGETS[mode].requests:
        raise ValueError("RUN_REQUEST_BUDGET_EXCEEDED")
    if any(batch.requests < 0 or batch.byte_count < 0 for batch in batches):
        raise ValueError("INVALID_SOURCE_COUNTER")
    if dry_run:
        copied = FakeWorkbook()
        copied.tabs = book.read_tabs(list(SCHEMA))
        book, outbox = copied, None
    names = [
        "Jobs_Master",
        "Profile",
        "Config",
        "Applications",
        "Application_Events",
        "Contacts",
        "Job_Contacts",
        "Outreach",
        "_Alerts",
        "_Job_Sources",
        "Job_History",
        "_Source_Attempts",
        "Sources",
        "Job_Briefs",
        "Quarantine",
        "Action_Queue",
    ]
    with book.snapshot(names):
        return _run_scan(
            book,
            batches,
            now=now,
            run_uid=run_uid,
            send_alerts=send_alerts,
            recipient=recipient,
            hmac_key=hmac_key,
            outbox=outbox,
            mode=mode,
        )
