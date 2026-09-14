"""Typed private configuration. Presence is not proof of live integration readiness."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from career_radar.domain import CandidateProfile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, env_ignore_empty=True
    )
    app_timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"
    zero_cost_mode: bool = True
    tracker_enabled: bool = False
    scheduler: Literal["github", "local", "disabled"] = "disabled"
    google_sheet_id: SecretStr = SecretStr("")
    google_service_account_json_b64: SecretStr = SecretStr("")
    private_profile_yaml_b64: SecretStr = SecretStr("")
    private_profile_path: Path | None = None
    alert_recipient_email: SecretStr = SecretStr("")
    gmail_address: SecretStr = SecretStr("")
    gmail_auth_mode: Literal["oauth", "app_password", "disabled"] = "disabled"
    gmail_app_password: SecretStr = SecretStr("")
    gmail_read_client_id: SecretStr = SecretStr("")
    gmail_read_client_secret: SecretStr = SecretStr("")
    gmail_read_refresh_token: SecretStr = SecretStr("")
    gmail_send_client_id: SecretStr = SecretStr("")
    gmail_send_client_secret: SecretStr = SecretStr("")
    gmail_send_refresh_token: SecretStr = SecretStr("")
    gmail_oauth_in_production: bool = False
    dedicated_mailbox_attested: bool = False
    gmail_label: str = "JobRadar/Incoming"
    gmail_sender_allowlist: str = ""
    privacy_hmac_key: SecretStr = SecretStr("")
    privacy_hmac_key_version: str = "v1"
    github_actions: bool = False
    github_run_id: str = ""
    github_run_attempt: int = 1
    budget_verified_at: datetime | None = None
    included_private_minutes_remaining: int | None = None
    paid_overage_disabled: bool = False
    monthly_plan_minutes: int = 1376
    account_reserve_minutes: int = 100
    max_immediate_alerts_per_run: int = Field(default=5, ge=1, le=5)
    max_alert_emails_per_day: int = Field(default=20, ge=2, le=20)

    @model_validator(mode="after")
    def credential_modes(self) -> Self:
        if self.github_actions and self.gmail_auth_mode == "app_password":
            raise ValueError("APP_PASSWORD_LOCAL_ONLY")
        if self.gmail_auth_mode == "oauth" and self.gmail_app_password.get_secret_value():
            raise ValueError("MIXED_GMAIL_CREDENTIAL_MODES")
        if self.gmail_auth_mode == "app_password":
            if (
                self.gmail_read_refresh_token.get_secret_value()
                or self.gmail_send_refresh_token.get_secret_value()
            ):
                raise ValueError("MIXED_GMAIL_CREDENTIAL_MODES")
            if not self.dedicated_mailbox_attested:
                raise ValueError("DEDICATED_MAILBOX_ATTESTATION_REQUIRED")
        if self.budget_verified_at and self.budget_verified_at.tzinfo is None:
            raise ValueError("BUDGET_TIMESTAMP_MUST_HAVE_TIMEZONE")
        return self

    def hmac_key(self) -> bytes:
        value = self.privacy_hmac_key.get_secret_value().encode()
        if len(value) < 32:
            raise ValueError("PRIVACY_HMAC_KEY_REQUIRES_32_BYTES")
        return value

    def recipient(self) -> str:
        value = self.alert_recipient_email.get_secret_value().strip().lower()
        if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", value):
            raise ValueError("ALERT_RECIPIENT_EMAIL_REQUIRED")
        return value

    def budget_state(self, now: datetime) -> str:
        if (
            not self.budget_verified_at
            or self.budget_verified_at > now
            or now - self.budget_verified_at > timedelta(days=7)
            or self.included_private_minutes_remaining is None
        ):
            return "BLOCKED_BUDGET_UNKNOWN"
        if not self.paid_overage_disabled:
            return "BLOCKED_PAID_OVERAGE"
        if (
            self.included_private_minutes_remaining
            < self.monthly_plan_minutes + self.account_reserve_minutes
        ):
            return "BLOCKED_BUDGET_INSUFFICIENT"
        return "READY"

    def load_bootstrap_profile(self) -> CandidateProfile:
        """Used only for initial bootstrap. Production reads private Sheet Profile."""
        encoded = self.private_profile_yaml_b64.get_secret_value()
        if encoded:
            data = yaml.safe_load(base64.b64decode(encoded, validate=True))
        elif self.private_profile_path:
            data = yaml.safe_load(self.private_profile_path.read_text())
        else:
            raise ValueError("PRIVATE_PROFILE_REQUIRED")
        return CandidateProfile.model_validate(data)
