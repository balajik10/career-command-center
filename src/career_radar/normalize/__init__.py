"""Conservative, provenance-preserving normalization without network access."""

import calendar
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from career_radar.domain import JobIdentity, NormalizedJob, Provenance, RawJob
from career_radar.security import canonical_url, plain_text

SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "Java": ("java",),
    "Python": ("python",),
    "SQL": ("sql",),
    "Bash": ("bash",),
    "Spring Boot": ("spring boot", "springboot", "spring-boot"),
    "Spring Framework": ("spring framework",),
    "REST APIs": ("rest api", "restful", "rest apis"),
    "microservices": ("microservice", "microservices"),
    "distributed systems": ("distributed systems",),
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "Redis": ("redis",),
    "Kafka": ("kafka",),
    "Kubernetes": ("kubernetes", "k8s"),
    "Docker": ("docker",),
    "AWS": ("aws", "amazon web services"),
    "GCP": ("gcp", "google cloud"),
    "Azure": ("azure",),
    "Grafana": ("grafana",),
    "Prometheus": ("prometheus",),
    "FastAPI": ("fastapi", "fast api"),
    "Aerospike": ("aerospike",),
    "Elasticsearch": ("elasticsearch",),
    "HBase": ("hbase",),
    "JPA": ("jpa",),
    "Hibernate": ("hibernate",),
    "Kotlin": ("kotlin",),
    "Go": ("golang", "go"),
    "C++": ("c++",),
    "JavaScript": ("javascript",),
    "React": ("react", "reactjs"),
    "GitHub Actions": ("github actions",),
    "Jenkins": ("jenkins",),
    "Maven": ("maven",),
    "PySpark": ("pyspark",),
    "Databricks": ("databricks",),
    "New Relic": ("new relic",),
    "Kibana": ("kibana",),
    "Zenduty": ("zenduty",),
    "SonarQube": ("sonarqube",),
    "multithreading": ("multithreading", "multi-threading"),
    "system design": ("system design",),
}


def normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def normalize_company(value: str) -> str:
    """Only legal suffixes; parent/brand matches need explicit approved aliases."""
    return re.sub(
        r"\s+(?:inc|limited|ltd|llc|pvt ltd|private limited)$", "", normalized_text(value)
    )


def detect_skills(text: str) -> list[str]:
    return sorted(
        name
        for name, aliases in SKILL_ALIASES.items()
        if any(
            re.search(r"(?<![\w+])" + re.escape(alias) + r"(?![\w+])", text, re.I)
            for alias in aliases
        )
    )


def classify_role(
    title: str, description: str = "", minimum: float | None = None
) -> tuple[str, str]:
    text = normalized_text(title)
    if re.search(
        r"\b(senior|sr|staff|lead|principal|architect|manager|director|head)\b", text
    ) or re.search(
        r"(?:manage|lead) (?:a |the )?(?:team of|engineering team)|staff.level scope",
        description,
        re.I,
    ):
        return "EXCLUDED", "SENIOR"
    if re.search(
        r"frontend|front end|mobile|android|ios|manual qa|tech support|sales|product manager", text
    ):
        return "EXCLUDED", "OUT_OF_SCOPE"
    level_two = bool(re.search(r"\b(ii|2)\b", text))
    if level_two and (minimum is None or minimum > 3):
        return "EXCLUDED", "LEVEL_II_UNCLEAR"
    families = (
        ("BACKEND", r"backend|back end|java|spring boot|api engineer"),
        ("PLATFORM", r"platform|infrastructure|cloud engineer|developer infrastructure"),
        ("RELIABILITY", r"site reliability|production engineer|\bsre\b|reliability"),
        ("FULL_STACK", r"full stack|fullstack"),
        (
            "DISTRIBUTED_SYSTEMS",
            r"distributed systems|search|ranking|recommendation|personalization",
        ),
        (
            "SOFTWARE",
            r"software|\bsde\b|\bswe\b|\bmts\b|member of technical staff|application developer",
        ),
    )
    for family, pattern in families:
        if re.search(pattern, text):
            if family in {"RELIABILITY", "PLATFORM", "FULL_STACK"} and not re.search(
                r"develop|software|programming|code|backend|back.end|java|python|build.*(?:api|service)",
                description,
                re.I,
            ):
                return family, "REVIEW_SCOPE"
            return family, "STRETCH" if level_two else "EARLY_CAREER"
    return "UNKNOWN", "UNKNOWN"


@dataclass(frozen=True)
class Experience:
    minimum: float | None = None
    maximum: float | None = None
    text: str = ""
    confidence: str = "MISSING"
    internship_allowed: bool = False


def parse_experience(text: str) -> Experience:
    allowed = bool(
        re.search(r"internship(?:s)? (?:count|included|accepted)|including internships", text, re.I)
    )
    candidates: list[tuple[float, float | None, str]] = []
    for sentence in re.split(r"[\n.;]", text):
        if re.search(r"preferred|nice.to.have|desirable|bonus", sentence, re.I):
            continue
        match = re.search(
            r"\b(\d+(?:\.\d+)?)\s*(?:[-–—]|to)\s*(\d+(?:\.\d+)?)\s*years?\b|\b(\d+(?:\.\d+)?)\s*(\+)?\s*years?\b",
            sentence,
            re.I,
        )
        if not match:
            continue
        suffix = sentence[match.end() :].strip()
        # Technology-specific experience is a skill gap, never the overall requirement.
        if (
            re.match(r"(?:of\s+)?(?:experience\s+)?(?:in|with|using)\s+", suffix, re.I)
            and detect_skills(suffix)
            and not re.search(
                r"software|professional|industry|total|work experience", sentence, re.I
            )
        ):
            continue
        low = float(match.group(1) or match.group(3))
        high = float(match.group(2)) if match.group(2) else None
        if high is not None and high < low:
            return Experience(
                text=sentence.strip(), confidence="AMBIGUOUS", internship_allowed=allowed
            )
        candidates.append((low, high, sentence.strip()))
    if not candidates:
        return Experience(internship_allowed=allowed)
    low, high, evidence = max(candidates, key=lambda item: item[0])
    return Experience(low, high, evidence, "PARSED", allowed)


@dataclass(frozen=True)
class PostedDate:
    value: datetime | None = None
    confidence: str = "MISSING"
    age_min: float | None = None
    age_max: float | None = None
    error: str = ""


def parse_posted_date(value: str | None, now: datetime, timezone: str = "UTC") -> PostedDate:
    if now.tzinfo is None:
        raise ValueError("run datetime must be timezone-aware")
    if not value:
        return PostedDate()
    raw = value.strip()
    if raw.casefold() in {"today", "yesterday"}:
        day = now.astimezone(ZoneInfo(timezone)).date()
        raw = (day - timedelta(days=int(raw.casefold() == "yesterday"))).isoformat()
    relative = re.fullmatch(r"(\d+)\s*(hour|day|week)s?\s+ago", raw, re.I)
    confidence = "EXACT"
    uncertainty = 0.0
    try:
        if relative:
            unit = {"hour": 1, "day": 24, "week": 168}[relative.group(2).lower()]
            date = now - timedelta(hours=int(relative.group(1)) * unit)
            confidence, uncertainty = "RELATIVE_PARSED", float(unit)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            date = datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(timezone))
            confidence = "DATE_ONLY"
            uncertainty = (
                -((date + timedelta(days=1)).astimezone(UTC) - date.astimezone(UTC)).total_seconds()
                / 3600
            )
        else:
            try:
                date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                return PostedDate(error="NAIVE_POSTED_DATE")
    except (ValueError, KeyError):
        return PostedDate(error="INVALID_POSTED_DATE")
    date = date.astimezone(UTC)
    age = (now - date).total_seconds() / 3600
    if age < 0:
        return PostedDate(error="FUTURE_POSTED_DATE")
    minimum, maximum = (
        (max(0, age + uncertainty), age) if uncertainty < 0 else (age, age + uncertainty)
    )
    return PostedDate(date, confidence, minimum, maximum)


def freshness_band(age_max: float | None, baseline: bool) -> str:
    if age_max is None:
        return "BASELINE_DATE_UNKNOWN" if baseline else "OBSERVED_NEW_DATE_UNKNOWN"
    if age_max < 0:
        raise ValueError("negative age is quarantined")
    for boundary, band in (
        (6, "HOT_0_6H"),
        (24, "EARLY_6_24H"),
        (96, "FRESH_1_3D"),
        (192, "OPEN_4_7D"),
        (360, "AGING_8_14D"),
    ):
        if age_max < boundary:
            return band
    return "STALE_15D_PLUS"


def month_bounds(value: str) -> tuple[datetime, datetime]:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("employment dates must have YYYY-MM precision")
    year, month = map(int, value.split("-"))
    first = datetime(year, month, 1, tzinfo=UTC)
    last = first.replace(day=calendar.monthrange(year, month)[1], hour=23, minute=59, second=59)
    return first, last


def parse_salary(raw: RawJob) -> dict[str, Any]:
    """Parse explicitly identified salary components, never infer base from total pay."""
    if not raw.salary_text:
        return {}
    try:
        payload = json.loads(raw.salary_text)
    except (ValueError, TypeError):
        return {}
    divisor = 1.0
    if raw.provider == "greenhouse" and isinstance(payload, list):
        # Multiple regional pay ranges must be reviewed; combining them invents a range.
        if len(payload) != 1:
            return {}
        payload = payload[0]
        divisor = 100.0
    if not isinstance(payload, dict):
        return {}
    salary_kind = str(
        payload.get("compensationType") or payload.get("type") or payload.get("title") or ""
    ).casefold()
    if raw.provider == "jsonld":
        prefix = "salary_base"
        values = payload.get("value", {})
        values = {"value": values} if isinstance(values, int | float) else values
    elif "base" in salary_kind:
        prefix, values = "salary_base", payload
    elif "total" in salary_kind:
        prefix, values = "salary_total", payload
    else:
        return {}
    if not isinstance(values, dict):
        return {}
    minimum = values.get(
        "minValue", values.get("min_cents", values.get("min", values.get("value")))
    )
    maximum = values.get("maxValue", values.get("max_cents", values.get("max", minimum)))
    if (
        not isinstance(minimum, int | float)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int | float)
        or isinstance(maximum, bool)
    ):
        return {}
    if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum < 0 or maximum < minimum:
        return {}
    currency = str(payload.get("currency", "")).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        return {}
    unit = str(
        values.get("unitText") or payload.get("interval") or payload.get("period") or ""
    ).upper()
    period = {
        "YEAR": "ANNUAL",
        "ANNUAL": "ANNUAL",
        "YEARLY": "ANNUAL",
        "MONTH": "MONTHLY",
        "MONTHLY": "MONTHLY",
        "HOUR": "HOURLY",
        "HOURLY": "HOURLY",
        "DAY": "DAILY",
        "WEEK": "WEEKLY",
    }.get(unit, "UNKNOWN")
    return {
        prefix + "_min": minimum / divisor,
        prefix + "_max": maximum / divisor,
        "salary_currency": currency,
        "salary_period": period,
        "salary_confidence": "OBSERVED",
    }


def normalize_job(raw: RawJob, now: datetime, *, baseline: bool = True) -> NormalizedJob:
    if now.tzinfo is None:
        raise ValueError("run datetime must be timezone-aware")
    description = plain_text(raw.description)
    experience = parse_experience(description)
    family, seniority = classify_role(raw.title, description, experience.minimum)
    date = parse_posted_date(
        raw.posted_at_raw, now, str(raw.metadata.get("source_timezone", "UTC"))
    )
    updated = parse_posted_date(raw.updated_at_raw, now)
    # Deadlines may be future, unlike posting times.
    deadline: datetime | None = None
    if raw.deadline_raw:
        try:
            parsed = datetime.fromisoformat(raw.deadline_raw.replace("Z", "+00:00"))
            deadline = parsed if parsed.tzinfo is not None else None
        except ValueError:
            pass
    url = canonical_url(raw.url) if raw.url else ""
    apply_url = canonical_url(raw.apply_url) if raw.apply_url else url
    company = normalize_company(raw.company)
    identity_key = (
        f"{raw.provider}|{raw.tenant}|{raw.provider_job_id}"
        if raw.provider_job_id
        else url or f"{company}|{raw.title}|{raw.location}|{description}"
    )
    uid = "job_" + hashlib.sha256(identity_key.encode()).hexdigest()[:24]
    location_text = f"{raw.location} {description}".lower()
    mode = (
        "REMOTE"
        if "remote" in raw.location.lower()
        else "HYBRID"
        if "hybrid" in raw.location.lower()
        else "ONSITE"
        if raw.location
        else "UNKNOWN"
    )
    india = bool(
        re.search(
            r"india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|noida|gurugram|gurgaon|delhi",
            raw.location,
            re.I,
        )
    )
    eligibility: bool | None = True if india else None
    if re.search(
        r"(?:us|usa|united states|europe|uk) (?:only|residents)|must (?:reside|be based) in (?:us|usa|europe|uk)",
        location_text,
    ):
        eligibility = False
    elif not india and re.search(
        r"india.based candidates|relocation (?:provided|supported)|visa sponsorship (?:available|provided)",
        description,
        re.I,
    ):
        eligibility = True
    required: set[str] = set()
    preferred: set[str] = set()
    for sentence in re.split(r"[\n.;]", description):
        target = (
            preferred
            if re.search(r"preferred|nice.to.have|bonus|desirable", sentence, re.I)
            else required
        )
        target.update(detect_skills(sentence))
    digest = hashlib.sha256(description.encode()).hexdigest()
    provenance = Provenance(
        source_id=raw.source_id,
        url=url,
        fetched_at=raw.fetched_at,
        parser_version=raw.parser_version,
        evidence=description[:1000],
    )
    job = NormalizedJob(
        job_uid=uid,
        dedupe_group_id=uid,
        company=plain_text(raw.company),
        normalized_company=company,
        title=plain_text(raw.title),
        normalized_title=normalized_text(raw.title),
        role_family=family,
        seniority=seniority,
        location=plain_text(raw.location),
        work_mode=mode,
        location_eligible=eligibility,
        visa_text=next(
            (
                part.strip()
                for part in re.split(r"[\n.;]", description)
                if re.search(r"visa|sponsor|relocation", part, re.I)
            ),
            "",
        )[:500],
        description=description[:4000],
        description_hash=digest,
        summary=description[:500],
        required_skills=sorted(required),
        preferred_skills=sorted(preferred - required),
        experience_text=experience.text,
        min_years=experience.minimum,
        max_years=experience.maximum,
        experience_confidence=experience.confidence,
        internship_allowed=experience.internship_allowed,
        education_constraint=next(
            (
                part.strip()
                for part in re.split(r"[\n.;]", description)
                if re.search(r"graduat|degree|bachelor", part, re.I)
            ),
            "",
        )[:500],
        employment_type=plain_text(raw.employment_type),
        canonical_url=url,
        apply_url=apply_url,
        official_link_state=raw.official_link_state,
        identities=[
            JobIdentity(
                provider=raw.provider,
                tenant=raw.tenant,
                provider_job_id=raw.provider_job_id,
                canonical_url=url,
                requisition_id=raw.requisition_id,
                requisition_namespace=raw.requisition_namespace,
            )
        ],
        provenance=[provenance],
        posted_at_source=date.value,
        posted_at_confidence=date.confidence,
        age_hours_min=date.age_min,
        age_hours_max=date.age_max,
        updated_at_source=updated.value,
        deadline=deadline,
        first_seen_at=now,
        last_seen_at=now,
        freshness_basis="SOURCE_POSTED_AT" if date.value else "FIRST_SEEN_PROXY",
        freshness_band=freshness_band(date.age_max, baseline),
        baseline=baseline,
        quarantine_reason=date.error,
        exclusion_reason="OUT_OF_SCOPE_ROLE" if family == "EXCLUDED" else "",
        metadata=raw.metadata,
    )
    salary = raw.metadata.get("salary") or parse_salary(raw)
    if salary and isinstance(salary, dict):
        for key in (
            "salary_base_min",
            "salary_base_max",
            "salary_total_min",
            "salary_total_max",
            "salary_currency",
            "salary_period",
            "salary_confidence",
        ):
            if key in salary:
                setattr(job, key, salary[key])
        job.salary_source = url
    return job
