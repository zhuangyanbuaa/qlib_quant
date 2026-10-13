"""Reviewed company identity additions, with conservative knowledge time."""

from pydantic import HttpUrl, field_validator

from quant_system.domain.models import IngestionMetadata, NonEmptyString, Symbol


class ReviewedCompanyMapping(IngestionMetadata):
    mapping_id: NonEmptyString
    symbol: Symbol
    cik: str
    aliases: tuple[str, ...] = ()
    source_url: str

    @field_validator("cik")
    @classmethod
    def validate_cik(cls, value: str):
        if (
            not value.isascii()
            or not value.isdigit()
            or not 1 <= len(value) <= 10
            or int(value) == 0
        ):
            raise ValueError("CIK must contain 1-10 digits and be nonzero")
        return value.zfill(10)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str):
        return str(HttpUrl(value))
