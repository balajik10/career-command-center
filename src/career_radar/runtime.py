"""Live entry points: fail closed when the authoritative private Sheet is unavailable."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from career_radar.domain import SourceDefinition
from career_radar.fetch import FetchBudget, collect, retry_after_seconds
from career_radar.integrations.google import gmail_access_token
from career_radar.notifications import GmailTransport, Outbox, SMTPTransport, owner_address
from career_radar.orchestration import ScanResult, SourceBatch, run_scan
from career_radar.orchestration.pipeline import (
    hydrate_source_checkpoints,
    load_profile,
    load_sources,
)
from career_radar.orchestration.planner import BUDGETS, plan_sources
from career_radar.settings import Settings
from career_radar.sheets import GoogleSheetsWorkbook, google_authenticated_session
from career_radar.sheets.workbook import Session
from career_radar.sources.registry import load_catalogue


def google_session(settings: Settings) -> Session:
    encoded = settings.google_service_account_json_b64.get_secret_value()
    if encoded:
        data = json.loads(base64.b64decode(encoded, validate=True))
        credential_factory: Callable[..., Any] = (
            service_account.Credentials.from_service_account_info
        )
        credentials = credential_factory(
            data, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        factory: Callable[[Any], Session] = AuthorizedSession
        return factory(credentials)
    return google_authenticated_session()


def live_book(settings: Settings, mode: str = "incremental") -> GoogleSheetsWorkbook:
    sheet_id = settings.google_sheet_id.get_secret_value()
    if not sheet_id:
        raise ValueError(
            "SETUP_REQUIRED: GOOGLE_SHEET_ID and Sheet credentials; see SETUP_REQUIRED.md"
        )
    budget = BUDGETS[mode]
    return GoogleSheetsWorkbook(
        sheet_id,
        google_session(settings),
        max_read_requests=budget.sheet_reads,
        max_write_requests=budget.sheet_writes,
    )


def live_outbox(book: GoogleSheetsWorkbook, settings: Settings) -> Outbox:
    if settings.gmail_auth_mode == "app_password":
        import smtplib

        sender = owner_address(settings.gmail_address.get_secret_value())
        password = settings.gmail_app_password.get_secret_value()
        if not password:
            raise ValueError("GMAIL_APP_PASSWORD_REQUIRED")
        connection = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        try:
            connection.login(sender, password)
        except Exception:
            connection.close()
            raise ValueError("SMTP_AUTH_REQUIRED") from None
        return Outbox(
            book, settings.recipient(), settings.hmac_key(), SMTPTransport(connection, sender)
        )
    token, sender = gmail_access_token(settings, "send")
    return Outbox(
        book,
        settings.recipient(),
        settings.hmac_key(),
        GmailTransport(cast(Session, httpx.Client(timeout=30)), token, sender),
    )


def readiness(
    settings: Settings,
    *,
    verify_live: bool = True,
    verify_write: bool = False,
    verify_email: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    result: dict[str, Any] = {
        "offline_demo": "READY",
        "sheet_read_write": "SETUP_REQUIRED",
        "gmail_ingestion": "CONNECTOR_DISABLED",
        "email_send": "SETUP_REQUIRED",
        "scheduled_run": "BLOCKED_CREDENTIALS",
        "budget": settings.budget_state(now),
        "zero_cost_mode": settings.zero_cost_mode,
        "tracker_enabled": settings.tracker_enabled,
    }
    try:
        settings.hmac_key()
        settings.recipient()
        result["private_identity"] = "CONFIGURED"
    except ValueError:
        result["private_identity"] = "SETUP_REQUIRED"
    if settings.google_sheet_id.get_secret_value() and verify_live:
        try:
            book = live_book(settings)
            errors = book.verify()
            result["sheet_read_write"] = (
                "READ_VERIFIED_WRITE_NOT_PROBED" if not errors else "SCHEMA_REPAIR_REQUIRED"
            )
            if not errors and verify_write:
                run_uid = "doctor:" + str(uuid.uuid4())
                book.transaction(
                    {
                        "_System_State": [
                            {
                                "key": "doctor_write_probe",
                                "value": now.isoformat(),
                                "run_uid": run_uid,
                            }
                        ],
                        "Run_Log": [
                            {
                                "run_uid": run_uid,
                                "mode": "doctor",
                                "status": "COMMITTED",
                                "completed_at": now.isoformat(),
                            }
                        ],
                    },
                    run_uid,
                )
                result["sheet_read_write"] = "READ_WRITE_VERIFIED"
        except Exception:
            result["sheet_read_write"] = "AUTH_OR_SHEET_UNAVAILABLE"
    if settings.gmail_auth_mode == "oauth" and verify_live and verify_email:
        purposes: tuple[tuple[Literal["read", "send"], str], ...] = (
            ("read", "gmail_ingestion"),
            ("send", "email_send"),
        )
        for purpose, capability in purposes:
            if purpose == "read" and not settings.gmail_read_refresh_token.get_secret_value():
                continue
            try:
                gmail_access_token(settings, purpose)
                result[capability] = (
                    "TOKEN_VERIFIED_CONNECTOR_DISABLED"
                    if purpose == "read"
                    else "READY"
                    if settings.gmail_oauth_in_production
                    else "DEMO_ONLY_OAUTH_TESTING"
                )
            except Exception:
                result[capability] = "AUTH_OR_SCOPE_OR_IDENTITY_FAILURE"
    elif settings.gmail_auth_mode == "app_password":
        result["email_send"] = "LOCAL_SMTP_AUTH_NOT_PROBED"
    if result["private_identity"] == "CONFIGURED" and result["email_send"] == "READY":
        if result["sheet_read_write"] == "READ_VERIFIED_WRITE_NOT_PROBED":
            result["scheduled_run"] = "BLOCKED_WRITE_VERIFICATION_REQUIRED"
        elif result["sheet_read_write"] == "READ_WRITE_VERIFIED":
            result["scheduled_run"] = result["budget"]
            if settings.scheduler == "disabled":
                result["scheduled_run"] = "BLOCKED_SCHEDULER_NOT_SELECTED"
            elif not settings.zero_cost_mode:
                result["scheduled_run"] = "BLOCKED_ZERO_COST_MODE_DISABLED"
    return result


def catalogue_path() -> Path:
    from importlib.resources import files

    local = Path("config/companies.yaml")
    if local.exists():
        return local
    bundled = Path(str(files("career_radar").joinpath("data/companies.yaml")))
    source_tree = Path(__file__).resolve().parents[2] / "config/companies.yaml"
    return bundled if bundled.exists() else source_tree


def configured_sources(settings: Settings) -> tuple[list[SourceDefinition], str]:
    if settings.google_sheet_id.get_secret_value():
        book = live_book(settings)
        book.require_schema()
        return load_sources(book.read_tab("Sources")), "PRIVATE_SHEET"
    return load_catalogue(catalogue_path()), "BOOTSTRAP_CATALOGUE_READ_ONLY"


def scan_live(
    settings: Settings, *, mode: str, dry_run: bool, send_alerts: bool, source_id: str = ""
) -> ScanResult:
    if dry_run and send_alerts:
        raise ValueError("DRY_RUN_CANNOT_SEND")
    book = live_book(settings, mode)
    book.require_schema()
    current = book.read_tabs(["Sources", "Profile", "_System_State", "Jobs_Master", "_Job_Sources"])
    profile = load_profile(current["Profile"])
    sources = load_sources(current["Sources"])
    hydrate_source_checkpoints(
        sources,
        current["_System_State"],
        jobs=current["Jobs_Master"],
        appearances=current["_Job_Sources"],
    )
    if source_id:
        sources = [source for source in sources if source.source_id == source_id]
        if not sources:
            raise ValueError("UNKNOWN_SOURCE_ID")
    now = datetime.now(UTC)
    planned, deferred = plan_sources(
        sources, now, mode=mode, mandatory=tuple(profile.mandatory_rapid_source_ids)
    )
    budget = BUDGETS[mode]
    fetch_budget = FetchBudget(
        max_requests=budget.requests, deadline=time.monotonic() + budget.seconds - 30
    )

    def fetch_source(source: SourceDefinition) -> SourceBatch:
        try:
            result, jobs = collect(source, fetch_budget)
            retry_after = None
            if result.outcome == "BACKOFF":
                received_at = result.fetched_at
                delay = retry_after_seconds(result.headers.get("retry-after", ""), received_at)
                retry_after = received_at + timedelta(seconds=60 if delay is None else delay)
            return SourceBatch(
                source,
                jobs,
                result.outcome,
                result.snapshot_complete,
                result.request_count,
                result.bytes_received,
                result.error_code,
                backoff_until=retry_after,
                checkpoint=result.checkpoint,
                listed_ids=result.listed_ids,
                listing_complete=result.listing_complete,
            )
        except Exception:
            return SourceBatch(
                source, [], "FAILED_TRANSIENT", False, error="SOURCE_EXCEPTION_REDACTED"
            )

    with ThreadPoolExecutor(max_workers=12) as pool:
        batches = list(pool.map(fetch_source, planned))
    # Publication can legitimately happen while collection is in progress. Score
    # against the completed observation time, never the earlier planning clock.
    observed_at = datetime.now(UTC)
    batches.extend(SourceBatch(source, [], "DEFERRED_BUDGET", False) for source in deferred)
    run_uid = settings.github_run_id or str(uuid.uuid4())
    outbox = live_outbox(book, settings) if send_alerts else None
    if outbox:
        outbox.reconcile_interrupted(run_uid)
    result = run_scan(
        book,
        batches,
        now=observed_at,
        run_uid=run_uid,
        dry_run=dry_run,
        send_alerts=send_alerts,
        recipient=settings.recipient(),
        hmac_key=settings.hmac_key(),
        outbox=outbox,
        mode=mode,
    )
    return result
