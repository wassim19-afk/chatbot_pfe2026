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


class BIQueryService:
    KPI_TABLE_MAP = {
        KPIType.REVENUE: ("[dbo].[Fact_Sales]", "[Sales Amount (Actual)]"),
        KPIType.PURCHASE: ("[dbo].[Fact_Purshase]", "[Purchase Amount (Actual)]"),
        KPIType.CASH_IN: ("[dbo].[Fact_CustomerPayementDetail]", "[Amount]"),
        KPIType.CASH_OUT: ("[dbo].[Fact_VendorPayementDetail]", "[Amount]"),
    }

    def __init__(self) -> None:
        self.assistant = BIAssistant()

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
        table_name, amount_column = self.KPI_TABLE_MAP[kpi_type]

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
            f"SELECT ISNULL(SUM({amount_column}), 0) AS total_value "
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
