"""Daily digest contracts and full watchlist accounting, independent of renderers."""

from dataclasses import dataclass
from datetime import date, timedelta
from math import isfinite
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from quant_system.domain.clocks import NyseSessionClock
from quant_system.domain.models import StrictModel
from quant_system.universe.config import (
    DailyDigestConfig,
    load_raw_candidate_pool_config,
    load_watchlist_config,
)
from quant_system.universe.lifecycle import LifecycleResolver


class DigestValue(StrictModel):
    status: Literal["AVAILABLE", "STALE", "UNAVAILABLE"] = "UNAVAILABLE"
    value: float | None = Field(default=None, allow_inf_nan=False)
    reason: str = "not_observed"
    units: str = "points"
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_state(self):
        if self.status == "UNAVAILABLE" and (self.value is not None or not self.reason):
            raise ValueError("unavailable value requires a reason and null value")
        if self.status in {"AVAILABLE", "STALE"} and self.value is None:
            raise ValueError("available/stale value must be numeric")
        return self


class CoverageRow(StrictModel):
    symbol: str
    resolved_symbol: str
    company_name: str
    source_lists: tuple[str, ...]
    themes: tuple[str, ...]
    status: Literal["IN_SCOPE", "INACTIVE", "PENDING_VERIFICATION"]
    reason: str


class DigestStock(StrictModel):
    symbol: str
    status: Literal["ASSESSED", "UNAVAILABLE", "INACTIVE", "PENDING_VERIFICATION"]
    reason: str
    technical_score: DigestValue = Field(default_factory=DigestValue)
    strategy_score: float | None = Field(default=None, allow_inf_nan=False)
    company_name: str = ""
    resolved_symbol: str = ""
    entry_status: str = "UNAVAILABLE"
    score_details: dict[str, Any] = Field(default_factory=dict)
    rule_checks: dict[str, bool] = Field(default_factory=dict)
    news_risk: str = "UNKNOWN"
    model_context: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    gate: dict[str, Any] = Field(default_factory=dict)


class DigestSector(StrictModel):
    symbol: str
    name: str
    group: Literal["sector", "theme"]
    technical_score: DigestValue = Field(default_factory=DigestValue)
    score_details: dict[str, Any] = Field(default_factory=dict)
    proxy_kind: str = "ETF"
    members: tuple[str, ...] = ()
    valid_count: int = 0
    total_count: int = 1


class DigestMarket(StrictModel):
    cnn_fear_greed: DigestValue = Field(default_factory=DigestValue)
    vix: DigestValue = Field(default_factory=DigestValue)
    technical_score: DigestValue = Field(default_factory=DigestValue)
    regime: str = "UNKNOWN"
    benchmark_scores: dict[str, DigestValue] = Field(default_factory=dict)
    benchmark_details: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DailyDigest(StrictModel):
    schema_version: str = "1"
    phase: str = "FOUNDATION_NOT_ENTRY_CLASSIFICATION"
    metadata: dict[str, Any]
    market: DigestMarket = Field(default_factory=DigestMarket)
    sectors: tuple[DigestSector, ...] = ()
    coverage: tuple[CoverageRow, ...]
    stocks: tuple[DigestStock, ...] = ()
    entry_candidates: tuple[DigestStock, ...] = ()
    pending_candidates: tuple[DigestStock, ...] = ()
    data_health: dict[str, Any] = Field(default_factory=dict)
    research: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    status: str = "DEGRADED"
    blocked_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DigestUniverse:
    universe_paths: tuple[Path, ...]
    scan_symbols: tuple[str, ...]
    context_symbols: tuple[str, ...]
    coverage: tuple[CoverageRow, ...]


def resolve_digest_universe(
    config: DailyDigestConfig,
    *,
    as_of: date,
    resolver: LifecycleResolver | None = None,
    universe_paths: tuple[Path, ...] | None = None,
) -> DigestUniverse:
    """Account for every identifier without admitting review-only raw candidates."""
    paths = config.universe_paths if universe_paths is None else universe_paths
    if not paths:
        raise ValueError("at least one production universe is required")
    resolver = resolver or LifecycleResolver(())
    cutoff = NyseSessionClock().available_at_utc(as_of)
    inventory: dict[str, dict[str, Any]] = {}
    priorities = {"IN_SCOPE": 3, "INACTIVE": 2, "PENDING_VERIFICATION": 1}

    def add(member, path, status, reason, resolved=None):
        symbol = member.symbol
        theme = getattr(member, "theme", getattr(member, "candidate_bucket", ""))
        row = inventory.setdefault(
            symbol,
            {
                "symbol": symbol,
                "resolved_symbol": resolved or symbol,
                "company_name": member.company_name,
                "source_lists": set(),
                "themes": set(),
                "status": status,
                "reason": reason,
            },
        )
        row["source_lists"].add(str(path))
        if theme:
            row["themes"].add(theme)
        if priorities[status] > priorities[row["status"]]:
            row.update(status=status, reason=reason, resolved_symbol=resolved or symbol)

    for path in paths:
        watchlist = load_watchlist_config(path)
        if watchlist.universe_type == "RAW_REVIEW":
            raise ValueError("review-only watchlist cannot enter production scans")
        for member in watchlist.symbols:
            if member.start_date > as_of:
                add(member, path, "INACTIVE", "membership_not_started")
            elif member.end_date is not None and member.end_date < as_of:
                add(member, path, "INACTIVE", "membership_ended")
            else:
                security = resolver.resolve(member.symbol, as_of=as_of, cutoff_utc=cutoff)
                add(
                    member,
                    path,
                    "IN_SCOPE" if security.active else "INACTIVE",
                    "selected_for_canonical_scan" if security.active else security.reason,
                    security.symbol,
                )
    # Explicit --universe overrides are isolated from the default user's raw pool.
    for path in config.review_paths if universe_paths is None else ():
        raw_type = yaml.safe_load(path.read_text())["universe_type"]
        pool = (
            load_raw_candidate_pool_config(path)
            if raw_type == "RAW_RESEARCH_CANDIDATES"
            else load_watchlist_config(path)
        )
        for member in pool.symbols:
            add(member, path, "PENDING_VERIFICATION", "identifier_listing_or_data_review_required")
    coverage = tuple(
        CoverageRow.model_validate(
            row
            | {
                "source_lists": tuple(sorted(row["source_lists"])),
                "themes": tuple(sorted(row["themes"])),
            }
        )
        for _, row in sorted(inventory.items())
    )
    return DigestUniverse(
        universe_paths=tuple(paths),
        scan_symbols=tuple(sorted({r.resolved_symbol for r in coverage if r.status == "IN_SCOPE"})),
        context_symbols=tuple(sorted({"^VIX", *(p.symbol for p in config.proxies)})),
        coverage=coverage,
    )


def load_digest_states(*, repository, database_path, config, scope, session):
    """Read-only diagnostics use the canonical hierarchy implementation and price gate."""
    import hashlib
    import json

    import pandas as pd

    from quant_system.backtest.workflow import load_price_snapshot
    from quant_system.decision.hierarchy import HierarchyProxy, compute_symbol_states
    from quant_system.quality.prices import price_quality_mask

    symbols = tuple(sorted(set(scope.scan_symbols) | {p.symbol for p in config.proxies}))
    prices = load_price_snapshot(
        repository=repository,
        database_path=database_path,
        symbols=symbols,
        start=session - timedelta(days=320),
        end=session,
    )
    proxies = tuple(
        HierarchyProxy(symbol=s, layer="stock", role="digest_only", proxy_for=s, tradable=False)
        for s in symbols
    )
    states = compute_symbol_states(prices=prices, proxies=proxies, as_of=session)
    for symbol, state in states.items():
        rows = prices.loc[prices["symbol"] == symbol].sort_values("session_date_ny")
        latest = rows.tail(1)
        valid = (
            not latest.empty
            and pd.Timestamp(latest.iloc[0]["session_date_ny"]).date() == session
            and bool(price_quality_mask(latest).iloc[0])
        )
        state["price_status"] = "AVAILABLE" if valid else "UNAVAILABLE"
        state["price_provenance"] = {}
        if not latest.empty:
            last = latest.iloc[0]
            fields = (
                "source",
                "source_version",
                "fetched_at_utc",
                "available_at_utc",
                "ingestion_run_id",
            )

            def reference(row, fields=fields):
                return {
                    key: pd.Timestamp(row[key]).tz_convert("UTC").isoformat()
                    if key.endswith("_at_utc")
                    else str(row[key])
                    for key in fields
                }

            refs = [
                reference(row) | {key: str(row[key]) for key in ("symbol", "session_date_ny")}
                for row in rows.to_dict(orient="records")
            ]
            state["price_provenance"] = {
                **reference(last),
                "symbol": symbol,
                "session_date_ny": str(pd.Timestamp(last["session_date_ny"]).date()),
                "is_stale": bool(last["is_stale"]),
                "quality_flags": list(last["quality_flags"]),
                "history_sha256": hashlib.sha256(
                    json.dumps(refs, sort_keys=True).encode()
                ).hexdigest(),
                "history_run_ids": sorted(set(rows["ingestion_run_id"].astype(str))),
                "history_rows": len(rows),
                "raw_root": str(repository.daily_prices_root),
            }
    return states


def _technical_value(state):
    value = state.get("reversal_score")
    if state.get("price_status") != "AVAILABLE":
        return DigestValue(
            reason="current_price_missing_or_unreliable",
            provenance=state.get("price_provenance", {}),
        )
    if value is None or not isfinite(float(value)):
        return DigestValue(
            reason="insufficient_price_history", provenance=state.get("price_provenance", {})
        )
    return DigestValue(
        status="AVAILABLE",
        value=round(float(value) * 10, 1),
        reason="",
        units="technical_points_100",
        provenance=state.get("price_provenance", {}),
    )


def _score_details(state):
    return {
        key: state.get(key)
        for key in (
            "trend_state",
            "reversal_phase",
            "score_components",
            "score_raw",
            "score_clamp_adjustment",
        )
    }


def classify_digest_entry(candidate, assessment, *, premarket, now, gate_reports=()):
    from quant_system.decision.gates import current_gate_decision

    if assessment.get("price_status") != "AVAILABLE":
        return "UNAVAILABLE", "current_price_missing_or_unreliable", {}
    if assessment.get("news_risk") == "HIGH" or (candidate or {}).get("news_risk") == "HIGH":
        return "VETOED", "high_news_risk", {}
    if not candidate or not assessment.get("rule_checks", {}).get("is_candidate"):
        return "NOT_CANDIDATE", "canonical_rules_not_passed", {}
    session = date.fromisoformat(premarket["metadata"]["signal_session"])
    clock = NyseSessionClock()
    order = clock.next_session(session)
    if now >= clock.session_close_utc(order):
        return "EXPIRED", "order_session_expired", {}
    if candidate.get("earliest_order_session") != order.isoformat():
        return "UNAVAILABLE", "plan_order_session_mismatch", {}
    if candidate.get("news_risk") != "LOW":
        return "PENDING_NEWS", "news_review_required", {}
    if (
        not candidate.get("execution_eligible")
        or candidate.get("recommended_action") != "PREPARE_MANUAL_CONDITIONAL_ORDER"
    ):
        return "PENDING_REVIEW", "plan_requires_manual_review", {}
    gate = current_gate_decision(candidate, premarket=premarket, gate_reports=gate_reports, now=now)
    if gate["decision"] == "KEEP":
        return "GATE_PASSED", "current_gate_passed", gate
    if gate["decision"] == "CANCEL":
        return "VETOED", gate["reason"], gate
    return "PENDING_GATE", gate["reason"], gate


def build_daily_digest(*, config, scope, metadata, premarket, states, now, gate_reports=()):
    if now.utcoffset() is None:
        raise ValueError("digest clock must be aware")
    signals = {r["symbol"]: r for r in premarket.get("candidates", [])}
    assessments = {r["symbol"]: r for r in premarket.get("symbol_assessments", [])}
    stocks = []
    for coverage in scope.coverage:
        symbol = coverage.resolved_symbol
        state = states.get(symbol, {})
        assessment = assessments.get(symbol, {})
        candidate = signals.get(symbol)
        value = _technical_value(state)
        entry, reason, gate = classify_digest_entry(
            candidate, assessment, premarket=premarket, now=now, gate_reports=gate_reports
        )
        if coverage.status != "IN_SCOPE":
            entry, reason = coverage.status, coverage.reason
        elif value.status != "AVAILABLE":
            entry, reason = "UNAVAILABLE", value.reason
        status = (
            ("ASSESSED" if value.status == "AVAILABLE" else "UNAVAILABLE")
            if coverage.status == "IN_SCOPE"
            else coverage.status
        )
        stocks.append(
            DigestStock(
                symbol=coverage.symbol,
                resolved_symbol=symbol,
                company_name=coverage.company_name,
                status=status,
                reason=reason,
                entry_status=entry,
                technical_score=value,
                strategy_score=(candidate or {}).get("score", assessment.get("strategy_score")),
                score_details=_score_details(state),
                rule_checks=assessment.get("rule_checks", {}),
                news_risk=assessment.get("news_risk", "UNKNOWN"),
                plan=candidate or {},
                gate=gate,
                model_context={
                    "status": premarket.get("model_rank_context", {}).get("status", "UNAVAILABLE"),
                    **{k: v for k, v in (candidate or {}).items() if k.startswith("model_")},
                },
            )
        )

    def order_key(row):
        return (
            -(row.technical_score.value if row.technical_score.value is not None else -1),
            -(row.strategy_score if row.strategy_score is not None else float("-inf")),
            row.symbol,
        )

    stocks.sort(key=order_key)
    benchmarks = {symbol: _technical_value(states.get(symbol, {})) for symbol in ("SPY", "QQQ")}
    market_score = DigestValue(reason="market_benchmark_missing")
    if all(v.status == "AVAILABLE" for v in benchmarks.values()):
        market_score = DigestValue(
            status="AVAILABLE",
            value=round(sum(v.value for v in benchmarks.values()) / 2, 1),
            reason="",
            units="technical_points_100",
            provenance={"weights": {"SPY": 0.5, "QQQ": 0.5}},
        )
    sectors = [
        DigestSector(
            symbol=p.symbol,
            name=p.name,
            group=p.group,
            technical_score=_technical_value(states.get(p.symbol, {})),
            score_details=_score_details(states.get(p.symbol, {})),
            valid_count=int(_technical_value(states.get(p.symbol, {})).status == "AVAILABLE"),
        )
        for p in config.proxies
        if p.group != "market"
    ]
    for theme in sorted(
        {theme for r in scope.coverage if r.status == "IN_SCOPE" for theme in r.themes}
    ):
        members = sorted(
            {
                r.resolved_symbol
                for r in scope.coverage
                if r.status == "IN_SCOPE" and theme in r.themes
            }
        )
        values = [_technical_value(states.get(s, {})) for s in members]
        valid = sum(v.status == "AVAILABLE" for v in values)
        value = DigestValue(reason="theme_incomplete_coverage")
        if valid == len(members) and members:
            value = DigestValue(
                status="AVAILABLE",
                value=round(sum(v.value for v in values) / len(values), 1),
                reason="",
                units="technical_points_100",
                provenance={"aggregation": "equal_weight_watchlist", "members": members},
            )
        sectors.append(
            DigestSector(
                symbol=f"theme:{theme}",
                name=theme,
                group="theme",
                technical_score=value,
                proxy_kind="WATCHLIST",
                members=tuple(members),
                valid_count=valid,
                total_count=len(members),
            )
        )
    sectors.sort(
        key=lambda row: (
            row.group,
            -(row.technical_score.value if row.technical_score.value is not None else -1),
            row.symbol,
        )
    )
    return DailyDigest(
        schema_version="2",
        phase="SCORED_DECISION_SUPPORT",
        metadata=metadata
        | {
            "score_version": config.score_version,
            "config_version": config.version,
            "evaluated_at_utc": now.isoformat(),
        },
        market=DigestMarket(
            technical_score=market_score,
            benchmark_scores=benchmarks,
            benchmark_details={s: _score_details(states.get(s, {})) for s in benchmarks},
            regime=premarket.get("portfolio_posture", {}).get("market_regime", "UNKNOWN"),
        ),
        coverage=scope.coverage,
        stocks=tuple(stocks),
        sectors=tuple(sectors),
        entry_candidates=tuple(r for r in stocks if r.entry_status == "GATE_PASSED"),
        pending_candidates=tuple(
            r for r in stocks if r.entry_status.startswith("PENDING_") and r.status == "ASSESSED"
        ),
        data_health=premarket.get("data_health", {}),
    )
