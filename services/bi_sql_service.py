"""
Business Intelligence SQL service.

This service parses KPI questions, extracts filters, builds parameterized SQL Server
queries, executes them through the shared DB helper, and returns structured JSON
without any fallback or mock values.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from config.settings import settings
from data.db_connection import execute_query
from services.bi_assistant import BIAssistant, KPIType

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
class KPIQueryConfig:
    table_candidates: tuple[str, ...]
    amount_candidates: tuple[str, ...]
    use_abs: bool = False


class BIQueryService:
    KPI_TABLE_MAP: dict[KPIType, KPIQueryConfig] = {
        KPIType.REVENUE: KPIQueryConfig(
            table_candidates=("[dbo].[Fact_Sales]",),
            amount_candidates=("[Sales Amount (Actual)]", "[Sales Amt_ Incl_ VAT (Actual)]"),
            use_abs=True,
        ),
        KPIType.PURCHASE: KPIQueryConfig(
            table_candidates=("[dbo].[Fact_Purshase]", "[dbo].[Fact_PurchaseDetail]"),
            amount_candidates=("[Purchase Amount (Actual)]",),
            use_abs=True,
        ),
        KPIType.CASH_IN: KPIQueryConfig(
            table_candidates=("[dbo].[Fact_CustomerPayementDetail]", "[dbo].[Fact_CashInDetail]"),
            amount_candidates=("[Amount]",),
            use_abs=False,
        ),
        KPIType.CASH_OUT: KPIQueryConfig(
            table_candidates=("[dbo].[Fact_VendorPayementDetail]", "[dbo].[Fact_CashOutDetail]"),
            amount_candidates=("[Amount]",),
            use_abs=False,
        ),
    }

    def __init__(self) -> None:
        self.assistant = BIAssistant()
        self._amount_column_cache: dict[str, str] = {}

    def _normalize_question(self, question: str) -> str:
        normalized = re.sub(r"\s+", " ", (question or "")).strip().lower()
        logger.info("Normalized question: original=%r normalized=%r", question, normalized)
        return normalized

    def _detect_kpi_intent(self, normalized_question: str) -> Optional[KPIType]:
        logger.info("Detecting KPI intent for question=%r", normalized_question)
        for keywords, kpi_type in self.assistant.KPI_KEYWORDS.items():
            for keyword in keywords:
                pattern = r"\b" + re.escape(keyword) + r"\b"
                if re.search(pattern, normalized_question, re.IGNORECASE):
                    logger.info("Detected KPI=%s via keyword=%r", kpi_type.value, keyword)
                    return kpi_type

        logger.info("No KPI keyword matched")
        return None

    def _default_companies(self) -> list[str]:
        raw_companies = settings.DEFAULT_BI_COMPANIES or "PEM,SAPEC"
        companies = [company.strip().upper() for company in raw_companies.split(",") if company.strip()]
        return companies or ["PEM", "SAPEC"]

    def _resolve_table_name(self, table_candidates: tuple[str, ...]) -> str:
        logger.info("Resolving KPI table from candidates=%s", table_candidates)
        for candidate in table_candidates:
            if candidate in self._amount_column_cache:
                logger.info("Using cached table resolution for %s", candidate)
                return candidate

            schema_name, bare_table_name = self._split_table_name(candidate)
            metadata_sql = (
                "SELECT 1 AS [exists] "
                "FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND TABLE_TYPE = 'BASE TABLE'"
            )
            try:
                rows = execute_query(metadata_sql, (schema_name, bare_table_name))
                if rows:
                    logger.info("Resolved KPI table: %s", candidate)
                    self._amount_column_cache[candidate] = candidate
                    return candidate
            except Exception:
                logger.exception("Failed to inspect table candidate=%s", candidate)

        raise BIQueryError(f"No supported KPI table found among candidates={table_candidates}")

    def _resolve_amount_column(self, table_name: str, candidates: tuple[str, ...]) -> str:
        cache_key = f"{table_name}::amount"
        if cache_key in self._amount_column_cache:
            resolved = self._amount_column_cache[cache_key]
            logger.info("Using cached amount column %s for table %s", resolved, table_name)
            return resolved

        schema_name, bare_table_name = self._split_table_name(table_name)
        logger.info(
            "Resolving amount column for table=%s using candidates=%s",
            table_name,
            candidates,
        )
        metadata_sql = (
            "SELECT COLUMN_NAME "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
            "ORDER BY ORDINAL_POSITION"
        )
        try:
            rows = execute_query(metadata_sql, (schema_name, bare_table_name))
            available_columns = {str(row.get("COLUMN_NAME", "")) for row in rows}
            logger.info("Available columns for %s: %s", table_name, sorted(available_columns))

            for candidate in candidates:
                clean_candidate = candidate.strip("[]")
                if clean_candidate in available_columns:
                    resolved = f"[{clean_candidate}]"
                    self._amount_column_cache[cache_key] = resolved
                    logger.info("Resolved amount column for %s: %s", table_name, resolved)
                    return resolved

            raise BIQueryError(
                f"No supported amount column found for {table_name}. Candidates={candidates}, available={sorted(available_columns)}"
            )
        except Exception as exc:
            logger.exception("Failed to resolve amount column for table=%s", table_name)
            if isinstance(exc, BIQueryError):
                raise
            raise BIQueryError(f"Unable to resolve amount column for {table_name}: {exc}") from exc

    @staticmethod
    def _split_table_name(table_name: str) -> tuple[str, str]:
        cleaned = table_name.replace("[", "").replace("]", "")
        parts = cleaned.split(".")
        if len(parts) == 2:
            return parts[0], parts[1]
        return "dbo", cleaned

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

    def build_sql(self, parsed: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
        kpi_type = parsed["kpi_type"]
        config = self.KPI_TABLE_MAP[kpi_type]
        table_name = self._resolve_table_name(config.table_candidates)
        amount_column = self._resolve_amount_column(table_name, config.amount_candidates)
        amount_expression = f"ABS({amount_column})" if config.use_abs else amount_column

        logger.info(
            "Using KPI config: kpi_type=%s table=%s amount_column=%s use_abs=%s",
            kpi_type.value,
            table_name,
            amount_column,
            config.use_abs,
        )

        where_clauses = []
        params: list[Any] = []

        year = parsed.get("year")
        month = parsed.get("month")
        company = parsed.get("company")
        companies = parsed.get("companies") or self._default_companies()

        if year:
            where_clauses.append("YEAR([Posting Date]) = ?")
            params.append(year)

        if month:
            where_clauses.append("MONTH([Posting Date]) = ?")
            params.append(month)

        if company:
            where_clauses.append("[companyName] = ?")
            params.append(company)
        elif companies:
            placeholders = ", ".join(["?"] * len(companies))
            where_clauses.append(f"[companyName] IN ({placeholders})")
            params.extend(companies)

        if not where_clauses:
            raise BIQueryError("Unable to build SQL filters from the user question")

        where_clause = " AND ".join(where_clauses)
        sql = (
            f"SELECT ISNULL(SUM({amount_expression}), 0) AS total_value "
            f"FROM {table_name} "
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
        kpi_intent = self._detect_kpi_intent(normalized_question)

        if kpi_intent is None:
            logger.info("Fallback detection: no KPI intent matched; returning text response")
            return BIQueryResult(
                type="text",
                value=0.0,
                currency="BnFCFA",
                dashboard_url="",
                data=[],
            )

        parsed = self.parse_filters(question)
        parsed["kpi_type"] = kpi_intent

        sql, params = self.build_sql(parsed)
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


bi_query_service = BIQueryService()
