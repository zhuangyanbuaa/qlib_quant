"""SEC submissions API adapter for point-in-time company events."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from quant_system.ingestion.base import (
    ProviderCompanyEvent,
    ProviderCompanyEventResult,
    ProviderError,
    TransientProviderError,
)

Transport = Callable[[str, dict[str, str], float], dict[str, object]]


class SecCompanyEventAdapter:
    """Fetch recent SEC filings from data.sec.gov submissions JSON."""

    name = "sec_submissions"
    version = "data.sec.gov-submissions-v1"

    def __init__(
        self,
        *,
        cik_by_symbol: dict[str, str],
        user_agent: str,
        timeout_seconds: float,
        forms: tuple[str, ...],
        transport: Transport | None = None,
    ) -> None:
        if not user_agent or "example.com" in user_agent:
            raise ValueError("SEC adapter requires a real contact User-Agent")
        self.cik_by_symbol = {
            symbol.upper(): cik.zfill(10) for symbol, cik in cik_by_symbol.items()
        }
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.forms = frozenset(form.upper() for form in forms)
        self._transport = transport or _json_get

    def fetch_events(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> ProviderCompanyEventResult:
        events: list[ProviderCompanyEvent] = []
        warnings: dict[str, str] = {}
        for symbol in tuple(dict.fromkeys(symbol.upper() for symbol in symbols)):
            cik = self.cik_by_symbol.get(symbol)
            if cik is None:
                warnings[symbol] = "missing_cik"
                continue
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            payload = self._transport(url, {"User-Agent": self.user_agent}, self.timeout_seconds)
            filings = payload.get("filings", {})
            if not isinstance(filings, dict) or not isinstance(filings.get("recent"), dict):
                warnings[symbol] = "missing_recent_filings"
                continue
            events.extend(
                _parse_recent_filings(
                    filings["recent"],
                    symbol=symbol,
                    cik=cik,
                    forms=self.forms,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )
            )
        return ProviderCompanyEventResult(events=tuple(events), warnings=warnings)


def _parse_recent_filings(
    recent: dict[str, object],
    *,
    symbol: str,
    cik: str,
    forms: frozenset[str],
    start_utc: datetime,
    end_utc: datetime,
) -> list[ProviderCompanyEvent]:
    form_values = _as_list(recent.get("form"))
    accession_values = _as_list(recent.get("accessionNumber"))
    filing_dates = _as_list(recent.get("filingDate"))
    accepted_values = _as_list(recent.get("acceptanceDateTime"))
    primary_documents = _as_list(recent.get("primaryDocument"))
    row_count = min(
        len(form_values),
        len(accession_values),
        len(filing_dates),
        len(accepted_values),
        len(primary_documents),
    )
    events: list[ProviderCompanyEvent] = []
    for index in range(row_count):
        form_type = str(form_values[index]).upper()
        if forms and form_type not in forms:
            continue
        accepted_at = _parse_sec_datetime(str(accepted_values[index]))
        if accepted_at < start_utc or accepted_at > end_utc:
            continue
        filed_at = _parse_filing_date(str(filing_dates[index]))
        accession = str(accession_values[index])
        primary_document = str(primary_documents[index])
        events.append(
            ProviderCompanyEvent(
                cik=cik,
                symbol=symbol,
                form_type=form_type,
                accession_number=accession,
                filed_at_utc=filed_at,
                accepted_at_utc=accepted_at,
                filing_url=_filing_url(
                    cik=cik,
                    accession_number=accession,
                    primary_document=primary_document,
                ),
            )
        )
    return events


def _parse_sec_datetime(value: str) -> datetime:
    normalized = value.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"invalid SEC acceptanceDateTime: {value}")


def _parse_filing_date(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time.min, tzinfo=UTC)


def _filing_url(*, cik: str, accession_number: str, primary_document: str) -> str:
    compact_cik = str(int(cik))
    accession_path = accession_number.replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{compact_cik}/{accession_path}/{primary_document}"
    )


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _json_get(url: str, headers: dict[str, str], timeout_seconds: float) -> dict[str, object]:
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as error:
        if 500 <= error.code < 600 or error.code == 429:
            raise TransientProviderError(f"HTTP {error.code}") from error
        raise ProviderError(f"HTTP {error.code}") from error
    except URLError as error:
        raise TransientProviderError(str(error)) from error
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ProviderError("JSON response must be an object")
    return decoded
