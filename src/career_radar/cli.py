"""Composable commands. Private payloads never enter scheduled logs."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import typer
from pydantic import ValidationError

from career_radar import __version__
from career_radar.contacts import import_contacts, redact_contact
from career_radar.demo import demo_book, run_demo
from career_radar.domain import AlertDecision, Contact, JobScore, SourceDefinition
from career_radar.fetch import FetchBudget, fetch
from career_radar.normalize import normalize_company
from career_radar.notifications import render_digest, scheduled_digest_window
from career_radar.orchestration.pipeline import (
    json_value,
    load_contacts,
    load_jobs,
    load_profile,
    row_for,
    uid,
)
from career_radar.orchestration.planner import route_schedule, scheduler_ready
from career_radar.runtime import (
    configured_sources,
    live_book,
    live_outbox,
    readiness,
    require_github_wif,
    scan_live,
)
from career_radar.security import SecurityError, canonical_url, private_token
from career_radar.settings import Settings
from career_radar.sheets import SCHEMA, BaseWorkbook, FakeWorkbook
from career_radar.sources.parsers import parse_eml, parse_import
from career_radar.sources.registry import check_policy

app = typer.Typer(
    help="Career Command Center — private, explainable job discovery.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
source_app = typer.Typer(help="Inspect and propose sources. Approval remains a human decision.")
import_app = typer.Typer(
    help="Import user-owned data; stage privately when Sheets is not configured."
)
sheets_app = typer.Typer(help="Verify the private canonical workbook.")
notify_app = typer.Typer(help="Owner-only notifications. No third-party sending exists.")
privacy_app = typer.Typer(help="Redact contacts and retain keyed suppression tokens.")
export_app = typer.Typer(help="Export a private local backup.")
for name, sub in [
    ("source", source_app),
    ("import", import_app),
    ("sheets", sheets_app),
    ("notify", notify_app),
    ("privacy", privacy_app),
    ("export", export_app),
]:
    app.add_typer(sub, name=name)

JSONFlag = Annotated[bool, typer.Option("--json", help="Emit a machine-readable JSON response.")]


def emit(value: Any, json_output: bool = True) -> None:
    # JSON is also the readable default; avoid two subtly different status representations.
    typer.echo(
        json.dumps(value, indent=2 if not os.getenv("GITHUB_ACTIONS") else None, default=str)
    )


def fail(error: Exception) -> None:
    if isinstance(error, ValidationError):
        detail = "VALIDATION_FAILED:" + ",".join(
            ".".join(str(part) for part in item["loc"])
            for item in error.errors(include_input=False)
        )
    else:
        message = str(error).split(":", 1)[0]
        detail = (
            message
            if re.fullmatch(r"[A-Z][A-Z0-9_ .;-]{1,160}", message)
            else type(error).__name__.upper()
        )
    emit({"status": "FAILED", "error": detail})
    raise typer.Exit(1)


@app.command()
def doctor(
    json_output: JSONFlag = False,
    require: str = typer.Option(
        "offline", help="offline, sheet, or scheduled readiness requirement."
    ),
    verify_write: bool = typer.Option(
        False, help="Verify Sheet write access using one reversible system marker."
    ),
) -> None:
    try:
        if require not in {"offline", "sheet", "scheduled"}:
            raise ValueError("INVALID_READINESS_REQUIREMENT")
        result = readiness(Settings(), verify_write=verify_write)
        emit(result, json_output)
        if (
            require == "sheet"
            and result["sheet_read_write"]
            not in {"READ_VERIFIED_WRITE_NOT_PROBED", "READ_WRITE_VERIFIED"}
            or require == "scheduled"
            and result["scheduled_run"] != "READY"
        ):
            raise typer.Exit(1)
    except (ValueError, SecurityError) as exc:
        fail(exc)


@app.command("bootstrap-sheet")
def bootstrap_sheet(offline: bool = False, json_output: JSONFlag = False) -> None:
    try:
        if offline:
            book = demo_book()
            book.save(".demo/workbook.json")
            emit(
                {"status": "CREATED_OFFLINE", "tabs": len(SCHEMA), "path": ".demo/workbook.json"},
                json_output,
            )
            return
        settings = Settings()
        live = live_book(settings, "full")
        # Bootstrap needs additional schema-formatting requests, never collection quota expansion.
        live.max_write_requests = 45
        result = live.bootstrap()
        if not live.read_tab("Profile"):
            profile = settings.load_bootstrap_profile()
            live.upsert(
                "Profile",
                "evidence_id",
                [
                    {
                        "evidence_id": "profile",
                        "kind": "candidate_profile",
                        "value": profile.model_dump_json(),
                    }
                ],
                "bootstrap",
                actor="user",
            )
        if not live.read_tab("Sources"):
            from career_radar.runtime import catalogue_path
            from career_radar.sources.registry import load_catalogue

            rows = [
                row_for("Sources", {**s.model_dump(mode="json"), "model_json": s.model_dump_json()})
                for s in load_catalogue(catalogue_path())
            ]
            live.upsert("Sources", "source_id", rows, "bootstrap", actor="user")
        profile = load_profile(live.read_tab("Profile"))
        if not live.read_tab("Companies"):
            companies: dict[str, dict[str, Any]] = {}
            for source in live.read_tab("Sources"):
                company = str(source.get("company", ""))
                normalized = normalize_company(company)
                if normalized:
                    companies.setdefault(
                        normalized,
                        {
                            "company_uid": uid(normalized),
                            "company": company,
                            "career_url": source.get("url", ""),
                            "company_tier": profile.company_tiers.get(normalized, 0),
                            "company_onboarding_state": "UNVALIDATED",
                            "enabled": False,
                        },
                    )
            live.upsert(
                "Companies", "company_uid", list(companies.values()), "bootstrap", actor="user"
            )
        if not live.read_tab("Config"):
            from career_radar.scoring import ScoringConfig

            live.upsert(
                "Config",
                "key",
                [
                    {
                        "key": "scoring",
                        "value": asdict(ScoringConfig()),
                        "description": "Private Sheet scoring overrides",
                    },
                    {
                        "key": "zero_cost_mode",
                        "value": True,
                        "description": "Paid APIs and paid runtime overage are disabled",
                    },
                    {
                        "key": "target_base_lpa",
                        "value": profile.target_base_lpa,
                        "description": "Optional source-backed priority signal; never a hard filter",
                    },
                ],
                "bootstrap",
                actor="user",
            )
        live.finalize_run("bootstrap", {"mode": "bootstrap"})
        emit(result, json_output)
    except Exception as exc:
        fail(exc)


@app.command()
def scan(
    mode: str = "incremental",
    dry_run: bool = False,
    offline_fixtures: bool = False,
    send_alerts: bool = False,
    source: str = "",
    output: Path | None = None,
    json_output: JSONFlag = False,
) -> None:
    try:
        if mode not in {"incremental", "full"}:
            raise ValueError("INVALID_SCAN_MODE")
        if dry_run and send_alerts or offline_fixtures and send_alerts:
            raise ValueError("DRY_RUN_OR_FIXTURES_CANNOT_SEND")
        result = (
            run_demo(dry_run=True)
            if offline_fixtures
            else scan_live(
                Settings(), mode=mode, dry_run=dry_run, send_alerts=send_alerts, source_id=source
            )
        )
        if output:
            from career_radar.preview import export_preview

            export_preview(result, output, synthetic=offline_fixtures)
        emit(
            {
                "data_mode": "SYNTHETIC" if offline_fixtures else "PRIVATE_SHEET",
                **result.summary.model_dump(mode="json"),
            },
            json_output,
        )
    except Exception as exc:
        fail(exc)


@app.command()
def demo(output: Path = Path(".demo"), json_output: JSONFlag = False) -> None:
    """Run the fixed oracle twice, export a disposable workbook and interactive preview."""
    try:
        from career_radar.preview import export_preview

        book = demo_book()
        first = run_demo(book, dry_run=False, run_uid="demo-first")
        second = run_demo(book, dry_run=False, run_uid="demo-second")
        if second.summary.new_jobs or second.summary.new_drafts or second.summary.alerts:
            raise ValueError("OFFLINE_IDEMPOTENCY_FAILURE")
        export_preview(first, output, synthetic=True)
        book.save(output / "workbook.json")
        emit(
            {
                "status": "PASS",
                "data_mode": "SYNTHETIC",
                "first": first.summary.model_dump(mode="json"),
                "identical_rerun": second.summary.model_dump(mode="json"),
                "preview": str((output / "dashboard.html").resolve()),
            },
            json_output,
        )
    except Exception as exc:
        fail(exc)


def due_now(value: Any, now: datetime) -> bool:
    if not value:
        return False
    text = str(value)
    try:
        when = (
            datetime.strptime(text, "%Y-%m-%d %H:%M IST").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            if text.endswith(" IST")
            else datetime.fromisoformat(text.replace("Z", "+00:00"))
        )
    except ValueError:
        return False
    return when.tzinfo is not None and when <= now


def digest_result(payload: dict[str, Any], body: str, json_output: bool) -> None:
    if not os.getenv("CAREER_SCHEDULED_RUN") and os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        payload["body"] = body
    emit(payload, json_output)


@app.command()
def digest(
    period: str = "morning",
    no_send: bool = False,
    offline_fixtures: bool = False,
    json_output: JSONFlag = False,
) -> None:
    try:
        if period not in {"morning", "evening"}:
            raise ValueError("INVALID_DIGEST_PERIOD")
        settings = Settings()
        now = datetime.now(UTC)
        window = scheduled_digest_window(period, now)
        synthetic = offline_fixtures or no_send and not settings.google_sheet_id.get_secret_value()
        if synthetic:
            result = run_demo()
            body = render_digest(result.jobs, result.scores)
            digest_result(
                {
                    "data_mode": "SYNTHETIC",
                    "period": period,
                    "send_attempted": False,
                    "items": sum(score.action_priority in {"P0", "P1"} for score in result.scores),
                },
                body,
                json_output,
            )
            return
        book = live_book(settings, period)
        with book.snapshot(["Jobs_Master", "_Alerts", "Applications", "Outreach"]):
            state = book.read_tabs(
                ["Jobs_Master", "_Alerts", "_System_State", "Applications", "Outreach"]
            )
            jobs = load_jobs(state["Jobs_Master"])
            scores = {
                row["job_uid"]: JobScore.model_validate(json_value(row["score_json"]))
                for row in state["Jobs_Master"]
            }
            cursor = next(
                (
                    row.get("value")
                    for row in state["_System_State"]
                    if row.get("key") == "digest_cursor"
                ),
                None,
            )
            previous = (
                datetime.fromisoformat(str(cursor)) if cursor else datetime.min.replace(tzinfo=UTC)
            )
            ambiguous_ids = {
                row.get("job_uid")
                for row in state["_Alerts"]
                if row.get("state") in {"AMBIGUOUS", "SENDING"}
            }
            selected = [
                job
                for job in jobs
                if scores[job.job_uid].action_priority in {"P0", "P1"}
                and (job.first_seen_at > previous or job.job_uid in ambiguous_ids)
            ]
            overdue = [
                f"{row['job_uid']}: {row.get('next_step') or row.get('application_stage', '')}"
                for row in state["Applications"]
                if due_now(row.get("next_step_due"), now)
            ]
            followups = [
                f"{row['job_uid']}: review due follow-up"
                for row in state["Outreach"]
                if row.get("outreach_stage") not in {"CLOSED", "DO_NOT_CONTACT"}
                and due_now(row.get("follow_up_due"), now)
            ]
            changes = [
                f"{job.job_uid}: {job.active_state}"
                for job in jobs
                if job.last_changed_at and job.last_changed_at > previous
            ]
            sheet_url = f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id.get_secret_value()}/edit"
            body = render_digest(
                selected,
                list(scores.values()),
                sheet_url=sheet_url,
                overdue=overdue,
                followups=followups,
                changes=changes,
            )
            if no_send:
                digest_result(
                    {
                        "period": period,
                        "send_attempted": False,
                        "items": len(selected),
                        "overdue": len(overdue),
                        "followups": len(followups),
                        "changes": len(changes),
                    },
                    body,
                    json_output,
                )
                return
            outbox = live_outbox(book, settings)
            members = [
                AlertDecision.model_validate(
                    {
                        key: value
                        for key, value in row.items()
                        if key in AlertDecision.model_fields and value != ""
                    }
                )
                for row in state["_Alerts"]
                if row.get("job_uid") in {job.job_uid for job in selected}
                and row.get("alert_type") in {"P0", "UPDATE"}
            ]
            outcome = outbox.digest(
                period,
                window,
                members,
                body,
                settings.github_run_id or str(uuid.uuid4()),
                actionable=bool(selected or overdue or followups or changes),
            )
            emit(
                {
                    "status": outcome.state if outcome else "NO_CONTENT_OR_ALREADY_ATTEMPTED",
                    "items": len(selected),
                },
                json_output,
            )
    except Exception as exc:
        fail(exc)


@source_app.command("check")
def source_check(
    all_sources: bool = typer.Option(False, "--all"),
    source_id: str = typer.Option("", "--id"),
    live: bool = False,
    json_output: JSONFlag = False,
) -> None:
    try:
        sources, origin = configured_sources(Settings())
        if source_id:
            sources = [s for s in sources if s.source_id == source_id]
            if not sources:
                raise ValueError("UNKNOWN_SOURCE_ID")
        rows = []
        for source in sources:
            reason = "READY_FOR_COLLECTION"
            try:
                check_policy(source, source.url, datetime.now(UTC))
            except (ValueError, SecurityError) as exc:
                reason = str(exc)
            if live and reason == "READY_FOR_COLLECTION":
                result, jobs = __import__("career_radar.fetch", fromlist=["collect"]).collect(
                    source, FetchBudget(max_requests=5)
                )
                reason = result.outcome
            rows.append(
                {
                    "source_id": source.source_id,
                    "policy": source.policy_state,
                    "coverage": source.coverage_state,
                    "health": source.health_state,
                    "check": reason,
                }
            )
        emit(
            {
                "origin": origin,
                "sources": len(rows),
                "automated_coverage": sum(
                    s.enabled
                    and s.policy_state == "APPROVED"
                    and s.coverage_state in {"VERIFIED_ACTIVE", "VERIFIED_EMPTY"}
                    for s in sources
                ),
                "policy_counts": dict(Counter(s.policy_state for s in sources)),
                "coverage_counts": dict(Counter(s.coverage_state for s in sources)),
                "results": rows,
            },
            json_output,
        )
    except Exception as exc:
        fail(exc)


@source_app.command("list")
def source_list(health_state: str = "", json_output: JSONFlag = False) -> None:
    try:
        sources, origin = configured_sources(Settings())
        emit(
            {
                "origin": origin,
                "sources": [
                    {
                        "source_id": s.source_id,
                        "company": s.company,
                        "policy_state": s.policy_state,
                        "coverage_state": s.coverage_state,
                        "health_state": s.health_state,
                    }
                    for s in sources
                    if not health_state or s.health_state == health_state
                ],
            },
            json_output,
        )
    except Exception as exc:
        fail(exc)


@source_app.command("add")
def source_add(career_url: str, probe: bool = True, json_output: JSONFlag = False) -> None:
    try:
        url = canonical_url(career_url)
        host = urlsplit(url).hostname or ""
        provider = next((p for p in ["greenhouse", "lever", "ashby"] if p in host), "jsonld")
        entry = SourceDefinition(
            source_id=uid(url)[:16],
            company="USER_REVIEW_REQUIRED",
            provider=provider,
            url=url,
            access_mode="PUBLIC_HTML_APPROVED",
        )
        result = (
            fetch(
                entry, FetchBudget(max_requests=3), purpose="VALIDATION_PROBE", user_initiated=True
            )
            if probe
            else None
        )
        emit(
            {
                "proposal": entry.model_dump(mode="json"),
                "probe": result.outcome if result else "NOT_REQUESTED",
                "approval": "HUMAN_REVIEW_REQUIRED",
                "jobs_extracted": 0,
                "enabled": False,
            },
            json_output,
        )
    except Exception as exc:
        fail(exc)


@source_app.command("disable")
def source_disable(
    source_id: str, reason: str = typer.Option(...), json_output: JSONFlag = False
) -> None:
    try:
        book = live_book(Settings())
        book.upsert(
            "Sources",
            "source_id",
            [
                {
                    "source_id": source_id,
                    "enabled": False,
                    "policy_state": "DISABLED",
                    "notes": reason,
                }
            ],
            str(uuid.uuid4()),
            actor="user",
        )
        emit({"status": "DISABLED", "source_id": source_id}, json_output)
    except Exception as exc:
        fail(exc)


def stage_or_import(tab: str, rows: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    if settings.google_sheet_id.get_secret_value():
        book = live_book(settings)
        book.upsert(
            tab,
            SCHEMA[tab].key,
            [row_for(tab, row) for row in rows],
            str(uuid.uuid4()),
            actor="user",
        )
        return {"status": "IMPORTED_PRIVATE_SHEET", "records": len(rows)}
    path = Path(".private/imports")
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    digest = private_token(
        settings.hmac_key(), "import", json.dumps(rows, sort_keys=True, default=str)
    )
    destination = path / f"{tab}-{digest}.json"
    destination.write_text(json.dumps(rows, indent=2, default=str))
    destination.chmod(0o600)
    return {
        "status": "STAGED_PRIVATE_NOT_AUTHORITATIVE",
        "records": len(rows),
        "staging_file": str(destination),
        "next": "Bootstrap the private Sheet, then repeat this import.",
    }


@import_app.command("contacts")
def contacts_import(
    path: Path, format: str = "linkedin-connections", json_output: JSONFlag = False
) -> None:
    try:
        settings = Settings()
        suppressed: set[str] = set()
        existing: dict[str, dict[str, Any]] = {}
        if settings.google_sheet_id.get_secret_value():
            existing = {row["contact_uid"]: row for row in live_book(settings).read_tab("Contacts")}
            suppressed = {
                str(row["suppression_token"])
                for row in existing.values()
                if row.get("suppression_token")
                and row.get("do_not_contact") in (True, "TRUE", "true")
            }
        contacts = import_contacts(
            path, format, settings.hmac_key(), datetime.now(UTC), suppressed_tokens=suppressed
        )
        for contact in contacts:
            previous = existing.get(contact.contact_uid, {})
            for field in SCHEMA["Contacts"].user_fields:
                if field in previous and previous[field] != "":
                    value = previous[field]
                    if field in {"whatsapp_allowed", "outreach_allowed", "do_not_contact"}:
                        value = value in (True, "TRUE", "true")
                    setattr(contact, field, value)
        emit(
            stage_or_import("Contacts", [c.model_dump(mode="json") for c in contacts], settings),
            json_output,
        )
    except Exception as exc:
        fail(exc)


@import_app.command("jobs")
def jobs_import(path: Path, json_output: JSONFlag = False) -> None:
    try:
        settings = Settings()
        if path.stat().st_size > 5_000_000:
            raise ValueError("IMPORT_FILE_TOO_LARGE")
        rows = parse_import(
            path.read_bytes(), "user-import", datetime.now(UTC), path.suffix.lstrip(".")
        )
        if not settings.google_sheet_id.get_secret_value():
            emit(
                stage_or_import("RawJobs", [r.model_dump(mode="json") for r in rows], settings),
                json_output,
            )
            return
        from career_radar.orchestration import SourceBatch, run_scan

        book = live_book(settings)
        source = SourceDefinition(
            source_id="user-import",
            company="User import",
            provider="manual",
            url="",
            access_mode="USER_IMPORT",
            policy_state="MANUAL_ONLY",
            cost_class="free",
        )
        result = run_scan(
            book,
            [SourceBatch(source, rows)],
            now=datetime.now(UTC),
            run_uid=str(uuid.uuid4()),
            recipient=settings.recipient(),
            hmac_key=settings.hmac_key(),
        )
        emit(result.summary.model_dump(mode="json"), json_output)
    except Exception as exc:
        fail(exc)


@import_app.command("eml")
def eml_import(directory: Path, json_output: JSONFlag = False) -> None:
    try:
        settings = Settings()
        allowed = {
            value.strip().lower()
            for value in settings.gmail_sender_allowlist.split(",")
            if value.strip()
        }
        if not allowed:
            raise ValueError("GMAIL_SENDER_ALLOWLIST_REQUIRED")
        if not directory.is_dir():
            raise ValueError("EML_DIRECTORY_REQUIRED")
        book = live_book(settings) if settings.google_sheet_id.get_secret_value() else None
        existing = {row["message_uid"] for row in book.read_tab("Inbox")} if book else set()
        rows, messages = [], []
        now = datetime.now(UTC)
        for path in sorted(directory.glob("*.eml")):
            if path.stat().st_size > 5_000_000:
                raise ValueError("EMAIL_TOO_LARGE")
            token, jobs = parse_eml(
                path.read_bytes(),
                "eml-import",
                "Unknown company",
                now,
                settings.hmac_key(),
                allowed,
            )
            if token in existing:
                continue
            existing.add(token)
            rows.extend(jobs)
            messages.append(
                {
                    "message_uid": token,
                    "source_id": "eml-import",
                    "message_hash": token,
                    "parsed_at": now.isoformat(),
                    "state": "PROCESSED",
                }
            )
        if book is None:
            emit(
                stage_or_import("RawJobs", [row.model_dump(mode="json") for row in rows], settings),
                json_output,
            )
            return
        if not messages:
            emit({"status": "ALREADY_PROCESSED_OR_EMPTY", "messages": 0, "records": 0}, json_output)
            return
        from career_radar.orchestration import SourceBatch, run_scan

        source = SourceDefinition(
            source_id="eml-import",
            company="User email import",
            provider="email",
            url="",
            access_mode="EMAIL_ALERT",
            policy_state="MANUAL_ONLY",
            cost_class="free",
        )
        run_uid = str(uuid.uuid4())
        result = run_scan(
            book,
            [SourceBatch(source, rows)],
            now=now,
            run_uid=run_uid,
            recipient=settings.recipient(),
            hmac_key=settings.hmac_key(),
        )
        if result.summary.status != "COMMITTED":
            raise ValueError("IMPORT_NOT_COMMITTED")
        book.upsert(
            "Inbox", "message_uid", [{**row, "run_uid": run_uid} for row in messages], run_uid
        )
        book.finalize_run(run_uid, row_for("Run_Log", result.summary.model_dump(mode="json")))
        emit(
            {
                "status": "IMPORTED_PRIVATE_SHEET",
                "messages": len(messages),
                **result.summary.model_dump(mode="json"),
            },
            json_output,
        )
    except Exception as exc:
        fail(exc)


@sheets_app.command("verify")
def sheets_verify(offline: bool = False, json_output: JSONFlag = False) -> None:
    try:
        book = FakeWorkbook.load(".demo/workbook.json") if offline else live_book(Settings())
        errors = book.verify()
        emit(
            {"status": "PASS" if not errors else "FAIL", "errors": errors, "tabs": len(SCHEMA)},
            json_output,
        )
        if errors:
            raise typer.Exit(1)
    except (ValueError, OSError, RuntimeError) as exc:
        fail(exc)


@notify_app.command("test")
def notify_test(recipient: str = typer.Option(...), json_output: JSONFlag = False) -> None:
    try:
        settings = Settings()
        if recipient.strip().lower() != settings.recipient():
            raise ValueError("RECIPIENT_NOT_CONFIGURED_OWNER")
        book = live_book(settings)
        outbox = live_outbox(book, settings)
        outcome = outbox.test(recipient, str(uuid.uuid4()))
        emit(
            {"status": outcome.state if outcome else "ALREADY_ATTEMPTED", "automatic_retry": False},
            json_output,
        )
    except Exception as exc:
        fail(exc)


def redact_related(book: BaseWorkbook, contacts: list[Contact], settings: Settings) -> None:
    updates: dict[str, list[dict[str, Any]]] = {"Contacts": [], "Outreach": [], "Job_Contacts": []}
    original_ids = {contact.contact_uid for contact in contacts}
    for contact in contacts:
        replacement = redact_contact(contact, settings.hmac_key())
        # Keep the old opaque UID so this overwrites the sensitive row instead
        # of appending a tombstone while leaving its original data behind.
        values = {field: "" for field in SCHEMA["Contacts"].columns}
        values.update(replacement.model_dump(mode="json"))
        values["contact_uid"] = contact.contact_uid
        updates["Contacts"].append(values)
    for tab, id_field in (("Outreach", "draft_id"), ("Job_Contacts", "job_contact_uid")):
        for row in book.read_tab(tab):
            if row.get("contact_uid") in original_ids:
                redacted = {field: "" for field in SCHEMA[tab].columns}
                redacted[id_field] = row[id_field]
                redacted["job_uid"] = row.get("job_uid", "")
                redacted["outreach_stage" if tab == "Outreach" else "status"] = (
                    "DO_NOT_CONTACT" if tab == "Outreach" else "REDACTED"
                )
                updates[tab].append(redacted)
    for row in book.read_tab("Jobs_Master"):
        if str(row.get("best_contact", "")) in original_ids or any(
            contact.name and contact.name in str(row.get("best_contact", ""))
            for contact in contacts
        ):
            updates.setdefault("Jobs_Master", []).append(
                {"job_uid": row["job_uid"], "best_contact": ""}
            )
    book.transaction(updates, str(uuid.uuid4()), actor="user")


@privacy_app.command("redact-contact")
def contact_redact(contact_uid: str, json_output: JSONFlag = False) -> None:
    try:
        settings = Settings()
        book = live_book(settings)
        contacts = load_contacts(book.read_tab("Contacts"))
        contact = next((item for item in contacts if item.contact_uid == contact_uid), None)
        if contact is None:
            raise ValueError("CONTACT_NOT_FOUND")
        redact_related(book, [contact], settings)
        emit({"status": "REDACTED", "suppression_retained": True}, json_output)
    except Exception as exc:
        fail(exc)


@privacy_app.command("purge-expired")
def expired_purge(json_output: JSONFlag = False) -> None:
    try:
        settings = Settings()
        book = live_book(settings)
        contacts = load_contacts(book.read_tab("Contacts"))
        now = datetime.now(UTC)
        expired = [
            contact
            for contact in contacts
            if contact.retention_expires_at and contact.retention_expires_at <= now
        ]
        redact_related(book, expired, settings)
        emit({"status": "PURGED_EXPIRED", "records_redacted": len(expired)}, json_output)
    except Exception as exc:
        fail(exc)


@export_app.command("backup")
def export_backup(directory: Path, offline: bool = False, json_output: JSONFlag = False) -> None:
    try:
        book = (
            FakeWorkbook.load(".demo/workbook.json") if offline else live_book(Settings(), "full")
        )
        path = book.export(directory)
        emit({"status": "EXPORTED_PRIVATE", "file": str(path)}, json_output)
    except Exception as exc:
        fail(exc)


@app.command("run-scheduled")
def run_scheduled() -> None:
    """Validated environment-only dispatcher used by the pinned reusable workflow."""
    try:
        settings = Settings()
        mode = os.getenv("INPUT_MODE", "incremental")
        cron = os.getenv("SCHEDULE_CRON", "")
        if cron:
            mode = route_schedule(cron)
        if mode not in {"incremental", "full", "morning", "evening", "maintenance", "doctor"}:
            raise ValueError("INVALID_MODE")
        for key in ("INPUT_DRY_RUN", "INPUT_SEND_ALERTS", "INPUT_VERIFY_WRITE"):
            if os.getenv(key, "false").lower() not in {"true", "false"}:
                raise ValueError("INVALID_BOOLEAN_INPUT")
        if os.getenv("INPUT_LOOKBACK_HOURS", ""):
            raise ValueError("LOOKBACK_OVERRIDE_UNSUPPORTED_USE_FULL_RECONCILIATION")
        source = os.getenv("INPUT_SOURCE", "")
        if source and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", source):
            raise ValueError("INVALID_SOURCE_ID")
        dry = os.getenv("INPUT_DRY_RUN", "false").lower() == "true"
        send = os.getenv("INPUT_SEND_ALERTS", "true").lower() == "true"
        if dry and send:
            raise ValueError("DRY_RUN_CANNOT_SEND")
        manual_dispatch = (
            settings.github_actions and os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
        )
        verify_write = os.getenv("INPUT_VERIFY_WRITE", "false").lower() == "true"
        if mode == "doctor":
            if not manual_dispatch or settings.tracker_enabled:
                raise ValueError("DOCTOR_REQUIRES_DISABLED_MANUAL_GITHUB_RUN")
            if dry or send or source or not verify_write:
                raise ValueError("DOCTOR_REQUIRES_EXPLICIT_WRITE_PROBE_NO_SEND")
            if not settings.zero_cost_mode or settings.budget_state(datetime.now(UTC)) != "READY":
                raise ValueError("DOCTOR_BUDGET_OR_ZERO_COST_BLOCKED")
            require_github_wif(settings)
            checks = readiness(settings, verify_write=True, verify_email=False)
            if checks["sheet_read_write"] != "READ_WRITE_VERIFIED":
                raise ValueError("DOCTOR_SHEET_WRITE_VERIFICATION_FAILED")
            emit(
                {
                    "status": "WIF_SHEET_READ_WRITE_VERIFIED",
                    "source_fetches": 0,
                    "mail_sent": False,
                    "probe_restored": True,
                }
            )
            return
        if verify_write:
            raise ValueError("WRITE_PROBE_REQUIRES_DOCTOR_MODE")
        manual_setup = not settings.tracker_enabled and manual_dispatch and dry and not send
        if not settings.tracker_enabled and not manual_setup:
            if manual_dispatch:
                raise ValueError("TRACKER_DISABLED_REQUIRES_MANUAL_DRY_RUN_NO_SEND")
            emit({"status": "TRACKER_DISABLED"})
            return
        if manual_setup:
            if not settings.zero_cost_mode:
                raise ValueError("MANUAL_DRY_RUN_REQUIRES_ZERO_COST_MODE")
            if settings.budget_state(datetime.now(UTC)) != "READY":
                raise ValueError("MANUAL_DRY_RUN_BUDGET_BLOCKED")
            checks = readiness(settings, verify_write=False, verify_email=False)
            if (
                checks["private_identity"] != "CONFIGURED"
                or checks["sheet_read_write"] != "READ_VERIFIED_WRITE_NOT_PROBED"
            ):
                raise ValueError("MANUAL_DRY_RUN_SHEET_READINESS_BLOCKED")
        else:
            if not scheduler_ready(settings.scheduler, settings.github_actions):
                raise ValueError("SINGLE_SCHEDULER_MISMATCH")
            if readiness(settings, verify_write=True)["scheduled_run"] != "READY":
                raise ValueError("SCHEDULED_READINESS_BLOCKED")
        if mode in {"morning", "evening"}:
            os.environ["CAREER_SCHEDULED_RUN"] = "true"
            try:
                digest(mode, no_send=dry or not send)
            finally:
                os.environ.pop("CAREER_SCHEDULED_RUN", None)
        elif mode == "maintenance":
            source_check(all_sources=True, live=False)
        else:
            scan(mode, dry, False, send, source)
    except typer.Exit:
        raise
    except Exception as exc:
        fail(exc)


@app.command()
def version() -> None:
    emit({"version": __version__})


if __name__ == "__main__":
    app()
