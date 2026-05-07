"""Business Intelligence SQL service.

This service detects KPI intent, extracts filters, discovers the correct SQL Server
fact table and columns from metadata, and executes parameterized queries.
No hardcoded Fact_Sales-style table references are used in the execution path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from config.settings import settings
from data.db_connection import (
    execute_query,
    get_available_tables,
    get_table_columns,
    validate_columns_exist,
    validate_table_exists,
)
from services.bi_assistant import BIAssistant
from services.kpi_config import KPI_TABLES

logger = logging.getLogger("bi-chat-api.service")


class BIQueryError(Exception):
    """Raised when a BI query cannot be parsed, built, or executed."""


@dataclass
class BIQueryResult:
    type: str
    value: float
    currency: str
    dashboard_url: str
    data: list[dict[str, Any]]


@dataclass(frozen=True)
class ResolvedKPIConfig:
    key: str
    label: str
    table: str
    value_column: str
    date_column: str
    company_column: Optional[str]
    use_abs: bool = False


class BIQueryService:
    def __init__(self) -> None:
        self.assistant = BIAssistant()
        self._resolved_configs: dict[str, ResolvedKPIConfig] = {}
        self._available_tables_cache: list[str] = []
        self._available_tables_cache_loaded = False

    def _normalize_question(self, question: str) -> str:
        normalized = re.sub(r"\s+", " ", (question or "")).strip().lower()
        logger.info("Normalized question: original=%r normalized=%r", question, normalized)
        return normalized

    def _detect_kpi_key(self, normalized_question: str) -> Optional[str]:
        logger.info("Detecting KPI key for question=%r", normalized_question)
        for kpi_key, config in KPI_TABLES.items():
            for alias in config["aliases"]:
                pattern = r"\b" + re.escape(alias) + r"\b"
                if re.search(pattern, normalized_question, re.IGNORECASE):
                    logger.info("Detected KPI key=%s via alias=%r", kpi_key, alias)
                    return kpi_key

        logger.info("No KPI alias matched")
        return None

    def _default_companies(self) -> list[str]:
        raw_companies = settings.DEFAULT_BI_COMPANIES or "PEM,SAPEC"
        companies = [company.strip().upper() for company in raw_companies.split(",") if company.strip()]
        return companies or ["PEM", "SAPEC"]

    def _load_available_tables(self) -> list[str]:
        if not self._available_tables_cache_loaded:
            logger.info("Loading available SQL tables for KPI discovery")
            self._available_tables_cache = get_available_tables()
            self._available_tables_cache_loaded = True
        return self._available_tables_cache

    def _pick_table_for_kpi(self, kpi_key: str, config: dict[str, Any]) -> ResolvedKPIConfig:
        if kpi_key in self._resolved_configs:
            logger.info("Using cached KPI resolution for %s", kpi_key)
            return self._resolved_configs[kpi_key]

        available_tables = self._load_available_tables()
        logger.info("Available tables for KPI resolution count=%d", len(available_tables))

        value_candidates = config["value_columns"]
        date_candidates = config["date_columns"]
        company_candidates = config["company_columns"]
        use_abs = bool(config.get("use_abs", False))
        label = str(config.get("label", kpi_key))

        logger.info(
            "Resolving KPI table for key=%s with value_candidates=%s date_candidates=%s company_candidates=%s",
            kpi_key,
            value_candidates,
            date_candidates,
            company_candidates,
        )

        matches: list[tuple[int, str, str, str, Optional[str]]] = []
        for table_name in available_tables:
            if not validate_table_exists(table_name):
                continue

            columns = get_table_columns(table_name)
            column_set = {column.lower() for column in columns}

            date_column = self._find_matching_column(date_candidates, column_set)
            value_column = self._find_matching_column(value_candidates, column_set)
            company_column = self._find_matching_column(company_candidates, column_set)

            if not date_column or not value_column:
                continue

            score = 0
            if company_column:
                score += 1
            if value_column:
                score += 2
            if date_column:
                score += 2

            matches.append((score, table_name, value_column, date_column, company_column))
            logger.info(
                "KPI candidate matched: table=%s value_column=%s date_column=%s company_column=%s score=%s",
                table_name,
                value_column,
                date_column,
                company_column,
                score,
            )

        if not matches:
            raise BIQueryError(
                f"No SQL table found for KPI '{kpi_key}'. Checked tables={available_tables}, value_candidates={value_candidates}, date_candidates={date_candidates}, company_candidates={company_candidates}"
            )

        matches.sort(key=lambda item: (-item[0], item[1]))
        _, table_name, value_column, date_column, company_column = matches[0]
        resolved = ResolvedKPIConfig(
            key=kpi_key,
            label=label,
            table=table_name,
            value_column=value_column,
            date_column=date_column,
            company_column=company_column,
            use_abs=use_abs,
        )
        self._resolved_configs[kpi_key] = resolved
        logger.info("Resolved KPI config: %s", resolved)
        return resolved

    @staticmethod
    def _find_matching_column(candidates: tuple[str, ...], column_set: set[str]) -> Optional[str]:
        for candidate in candidates:
            candidate_clean = candidate.strip().strip("[]").lower()
            if candidate_clean in column_set:
                return candidate.strip().strip("[]")
        return None

    def parse_filters(self, question: str) -> dict[str, Any]:
        parsed = self.assistant.parse_query(question)
        logger.info(
            "Extracted filters: kpi_type=%s year=%s month=%s company=%s filters=%s",
            parsed["kpi_type"].value,
            parsed.get("year"),
            parsed.get("month"),
            parsed.get("company"),
            parsed.get("filters"),
        )
        return parsed

    def build_sql(self, parsed: dict[str, Any], resolved: ResolvedKPIConfig) -> tuple[str, tuple[Any, ...]]:
        value_expr = f"ABS([{resolved.value_column}])" if resolved.use_abs else f"[{resolved.value_column}]"

        logger.info(
            "Building SQL using table=%s value_column=%s date_column=%s company_column=%s use_abs=%s",
            resolved.table,
            resolved.value_column,
            resolved.date_column,
            resolved.company_column,
            resolved.use_abs,
        )

        where_clauses = []
        params: list[Any] = []

        year = parsed.get("year")
        month = parsed.get("month")
        company = parsed.get("company")
        companies = parsed.get("companies") or self._default_companies()

        if year is not None:
            where_clauses.append(f"YEAR([{resolved.date_column}]) = ?")
            params.append(year)

        if month is not None:
            where_clauses.append(f"MONTH([{resolved.date_column}]) = ?")
            params.append(month)

        if resolved.company_column:
            if company:
                where_clauses.append(f"[{resolved.company_column}] = ?")
                params.append(company)
            elif companies:
                placeholders = ", ".join(["?"] * len(companies))
                where_clauses.append(f"[{resolved.company_column}] IN ({placeholders})")
                params.extend(companies)
        else:
            logger.info("No company column found for table=%s; skipping company filter", resolved.table)

        if not where_clauses:
            raise BIQueryError("Unable to build SQL filters from the user question")

        where_clause = " AND ".join(where_clauses)
        sql = (
            f"SELECT ISNULL(SUM({value_expr}), 0) AS total_value "
            f"FROM {resolved.table} "
            f"WHERE {where_clause}"
        )

        logger.info("Generated SQL: %s", sql)
        logger.info("Generated SQL params: %s", tuple(params))
        return sql, tuple(params)

    async def execute_sql(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        logger.info("Executing SQL Server query asynchronously")
        try:
            rows = await asyncio.to_thread(execute_query, sql, params)
            logger.info("SQL execution result row_count=%d", len(rows))
            if rows:
                logger.info("SQL execution first row=%s", rows[0])
            return rows
        except Exception as exc:
            logger.exception("Database execution failed")
            raise BIQueryError(f"Database execution failed: {exc}") from exc

    async def query(self, question: str) -> BIQueryResult:
        normalized_question = self._normalize_question(question)
        kpi_key = self._detect_kpi_key(normalized_question)

        if kpi_key is None:
            logger.info("Fallback detection: no KPI intent matched; returning text response")
            return BIQueryResult(
                type="text",
                value=0.0,
                currency="BnFCFA",
                dashboard_url="",
                data=[],
            )

        config = KPI_TABLES[kpi_key]
        resolved = self._pick_table_for_kpi(kpi_key, config)
        parsed = self.parse_filters(question)
        parsed["kpi_type"] = self.assistant._extract_kpi_type(normalized_question)

        sql, params = self.build_sql(parsed, resolved)
        rows = await self.execute_sql(sql, params)

        total_value = 0.0
        if rows:
            first_row = rows[0]
            total_value = float(first_row.get("total_value", 0) or 0)
        logger.info("Resolved KPI value=%s", total_value)

        dashboard_url = self.assistant.generate_power_bi_link(parsed.get("filters", ""))
        logger.info("Generated Power BI URL=%s", dashboard_url)

        return BIQueryResult(
            type="kpi",
            value=total_value,
            currency="BnFCFA",
            dashboard_url=dashboard_url,
            data=rows,
        )

    def startup_diagnostics(self) -> dict[str, Any]:
        tables = self._load_available_tables()
        diagnostics: dict[str, Any] = {
            "table_count": len(tables),
            "tables": tables[:50],
            "kpi_config_keys": list(KPI_TABLES.keys()),
            "resolved_configs": {},
        }

        for kpi_key, config in KPI_TABLES.items():
            try:
                resolved = self._pick_table_for_kpi(kpi_key, config)
                diagnostics["resolved_configs"][kpi_key] = {
                    "table": resolved.table,
                    "value_column": resolved.value_column,
                    "date_column": resolved.date_column,
                    "company_column": resolved.company_column,
                }
            except Exception as exc:
                diagnostics["resolved_configs"][kpi_key] = {"error": str(exc)}

        return diagnostics


bi_query_service = BIQueryService()
