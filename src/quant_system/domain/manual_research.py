"""Strict research-only web evidence, separate from provider coverage."""

import hashlib
import json
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

from quant_system.domain.models import IngestionMetadata, NonEmptyString, StrictModel, Symbol

PROMPT_VERSION = "manual-news-prompt-v2"
RESULT_VERSION = "manual-research-result-v1"
PACKAGE_VERSION = "manual-research-package-v1"
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class UtcResearchModel(StrictModel):
    @field_validator("*", mode="after")
    @classmethod
    def utc_times(cls, value):
        if isinstance(value, datetime) and value.utcoffset().total_seconds() != 0:
            raise ValueError("research timestamps must use UTC")
        return value


class ResearchEvidence(UtcResearchModel):
    evidence_id: NonEmptyString
    symbol: Symbol
    source_name: NonEmptyString
    url: HttpUrl
    title: NonEmptyString
    published_at_utc: AwareDatetime | None
    retrieved_at_utc: AwareDatetime
    summary: NonEmptyString
    severity: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    background_only: bool = False

    @model_validator(mode="after")
    def publication_before_retrieval(self):
        if self.published_at_utc and self.published_at_utc > self.retrieved_at_utc:
            raise ValueError("publication cannot follow retrieval")
        return self


class SymbolResearchReview(UtcResearchModel):
    symbol: Symbol
    coverage: Literal["SEARCHED", "PARTIAL", "UNAVAILABLE", "NOT_SEARCHED"]
    observed_risk: Literal["HIGH", "MEDIUM", "UNKNOWN"]
    research_priority: Literal["FOCUS", "WATCH", "SKIP"]
    quant_interpretation: NonEmptyString
    catalyst_assessment: NonEmptyString
    risk_assessment: NonEmptyString
    integrated_view: NonEmptyString
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]


class ManualResearchResult(UtcResearchModel):
    schema_version: Literal["manual-research-result-v1"] = RESULT_VERSION
    package_id: UUID
    package_sha256: Sha256
    run_id: UUID
    signal_session: date
    price_cutoff_utc: AwareDatetime
    prompt_version: Literal["manual-news-prompt-v1", "manual-news-prompt-v2"] = PROMPT_VERSION
    analyst_system: NonEmptyString
    model_version: NonEmptyString
    generated_at_utc: AwareDatetime
    news_cutoff_utc: AwareDatetime
    overview: NonEmptyString
    evidence: tuple[ResearchEvidence, ...]
    reviews: tuple[SymbolResearchReview, ...]

    @model_validator(mode="after")
    def validate_evidence(self):
        if not self.price_cutoff_utc <= self.news_cutoff_utc <= self.generated_at_utc:
            raise ValueError("research cutoffs must precede result creation")
        by_id = {item.evidence_id: item for item in self.evidence}
        if len(by_id) != len(self.evidence):
            raise ValueError("duplicate evidence IDs")
        if len({row.symbol for row in self.reviews}) != len(self.reviews):
            raise ValueError("duplicate symbol reviews")
        for item in self.evidence:
            if item.retrieved_at_utc > self.news_cutoff_utc:
                raise ValueError("evidence retrieval exceeds news cutoff")
        for row in self.reviews:
            if len(set(row.evidence_ids)) != len(row.evidence_ids):
                raise ValueError("duplicate evidence references")
            if any(key not in by_id or by_id[key].symbol != row.symbol for key in row.evidence_ids):
                raise ValueError("missing or wrong-symbol evidence reference")
            if row.coverage in {"NOT_SEARCHED", "UNAVAILABLE"} and (
                row.evidence_ids or row.observed_risk != "UNKNOWN"
            ):
                raise ValueError("unsearched/unavailable review cannot claim observed risk")
            if row.observed_risk in {"HIGH", "MEDIUM"} and not any(
                by_id[key].severity
                in ({"HIGH"} if row.observed_risk == "HIGH" else {"HIGH", "MEDIUM"})
                for key in row.evidence_ids
            ):
                raise ValueError("observed risk needs matching cited evidence")
        return self


class ManualResearchObservation(IngestionMetadata):
    review_id: UUID
    package_id: UUID
    package_sha256: Sha256
    parent_run_id: UUID
    signal_session: date
    result_sha256: Sha256
    result_json: NonEmptyString
    reviewed_by: NonEmptyString

    @model_validator(mode="after")
    def validate_payload(self):
        result = ManualResearchResult.model_validate_json(self.result_json)
        if (
            result.package_id != self.package_id
            or result.package_sha256 != self.package_sha256
            or result.run_id != self.parent_run_id
            or result.signal_session != self.signal_session
            or content_hash(result.model_dump(mode="json")) != self.result_sha256
            or result.generated_at_utc > self.available_at_utc
        ):
            raise ValueError("manual observation payload/provenance mismatch")
        return self
