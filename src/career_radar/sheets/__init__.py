"""Canonical Google Sheet adapters and immutable workflow input."""

from .actions import process_action_updates
from .schema import SCHEMA, SCHEMA_VERSION
from .workbook import (
    BaseWorkbook,
    FakeWorkbook,
    GoogleSheetsWorkbook,
    SheetError,
    Workbook,
    bootstrap_google_sheet,
    google_authenticated_session,
)

__all__ = [
    "BaseWorkbook",
    "FakeWorkbook",
    "GoogleSheetsWorkbook",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SheetError",
    "Workbook",
    "bootstrap_google_sheet",
    "google_authenticated_session",
    "process_action_updates",
]
