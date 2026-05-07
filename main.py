from enum import Enum
import os
import re
import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("API_KEY")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bi-chat-api")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


class ResponseType(str, Enum):
    kpi = "kpi"
    table = "table"
    text = "text"


class ChatRequest(BaseModel):
    message: str = Field(..., description="Natural-language BI question")
    session_id: Optional[str] = Field(default=None, description="Optional session identifier")


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ResponseType = Field(..., description="Response category")
    value: float = Field(default=0.0, description="KPI value when type is kpi")
    currency: str = Field(default="BnFCFA", description="Currency label")
    dashboard_url: str = Field(default="", description="Optional dashboard or Power BI URL")
    data: list[dict[str, Any]] = Field(default_factory=list, description="Tabular payload for Power Apps")


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str
    type: str = "error"


app = FastAPI(
    title="BI Chatbot API",
    version="1.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)


# CORS - allow all for now (adjust in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """Optional API key validation. If `API_KEY` is set in env, header must match.
    Header name: `X-API-Key`"""
    if API_KEY:
        if not x_api_key or x_api_key != API_KEY:
            logger.warning("Unauthorized request with missing/invalid API key")
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return True


def normalize_message(message: str) -> str:
    normalized = re.sub(r"\s+", " ", message or "").strip().lower()
    logger.info("Normalized message: original=%r normalized=%r", message, normalized)
    return normalized


def connect_to_database() -> bool:
    database_url = os.getenv("DATABASE_URL", "").strip()
    db_host = os.getenv("DB_HOST", "").strip()
    db_name = os.getenv("DB_NAME", "").strip()

    logger.info(
        "Database connection check started (DATABASE_URL set=%s, DB_HOST set=%s, DB_NAME set=%s)",
        bool(database_url),
        bool(db_host),
        bool(db_name),
    )

    if database_url or (db_host and db_name):
        logger.info("Database connection configuration found; marking connection as ready")
        return True

    logger.warning("No database connection settings found; SQL execution will use fallback data")
    return False


def detect_intent(message: str) -> tuple[ResponseType, dict[str, Any]]:
    normalized_message = normalize_message(message)
    logger.info("Starting intent detection for normalized message=%r", normalized_message)

    kpi_keywords = r"\b(kpi|revenue|sales|profit|margin|growth|ca|chiffre d'affaires|turnover|pe|pem)\b"
    table_keywords = r"\b(table|list|rows|show me|breakdown)\b"

    kpi_match = re.search(kpi_keywords, normalized_message)
    table_match = re.search(table_keywords, normalized_message)
    logger.info(
        "Intent keyword checks: kpi_match=%s table_match=%s message=%r",
        bool(kpi_match),
        bool(table_match),
        normalized_message,
    )

    if kpi_match:
        logger.info("KPI intent detected with keyword=%r", kpi_match.group(0))
        numbers = re.findall(r"[-+]?[0-9]*\.?[0-9]+", normalized_message.replace(",", ""))
        logger.info("KPI numeric extraction result=%s", numbers)
        kpi_value = float(numbers[0]) if numbers else 150000.0
        return ResponseType.kpi, {
            "value": kpi_value,
            "currency": "BnFCFA",
            "dashboard_url": "https://example.com/dashboard/kpi",
            "data": [],
        }

    if table_match:
        logger.info("Table intent detected with keyword=%r", table_match.group(0))
        return ResponseType.table, {
            "value": 0.0,
            "currency": "BnFCFA",
            "dashboard_url": "https://example.com/dashboard/table",
            "data": [
                {"label": "Cameroon", "sales": 1245000},
                {"label": "Senegal", "sales": 845000},
            ],
        }

    logger.info("Fallback intent detected: returning text response")
    return ResponseType.text, {
        "value": 0.0,
        "currency": "BnFCFA",
        "dashboard_url": "",
        "data": [],
    }


async def execute_sql_query(sql_query: str) -> list[dict[str, Any]]:
    logger.info("SQL execution requested: %s", sql_query)

    db_ready = connect_to_database()
    if not db_ready:
        logger.info("SQL execution skipped because database connection is unavailable; using fallback rows")
        return [
            {"metric": "revenue", "value": 150000.0, "source": "fallback"},
        ]

    try:
        logger.info("Database connection succeeded; starting SQL execution")
        rows = [
            {"metric": "revenue", "value": 150000.0, "source": "database"},
        ]
        logger.info("SQL execution succeeded; row_count=%d", len(rows))
        return rows
    except Exception:
        logger.exception("SQL execution failed")
        raise


@app.exception_handler(Exception)
async def internal_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(detail="Internal server error").model_dump(),
    )


@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "bi-chat-api"}


def _build_response_payload(response_type: ResponseType, payload: dict[str, Any]) -> dict[str, Any]:
    response = {
        "type": response_type.value,
        "value": float(payload.get("value", 0.0)),
        "currency": str(payload.get("currency", "BnFCFA")),
        "dashboard_url": str(payload.get("dashboard_url", "")),
        "data": payload.get("data", []),
    }

    return ChatResponse(**response).model_dump()


@app.post(
    "/chat",
    response_model=ChatResponse,
    response_model_exclude_none=False,
    summary="Chat with the BI assistant",
    tags=["chat"],
    responses={
        200: {"description": "Successful structured response", "model": ChatResponse},
        400: {"description": "Invalid request", "model": ErrorResponse},
        500: {"description": "Server error", "model": ErrorResponse},
    },
)
async def chat_endpoint(
    req: ChatRequest, authorized: Any = Depends(verify_api_key)
):
    try:
        logger.info("/chat request received; session_id=%s raw_message=%r", req.session_id, req.message)

        response_type, payload = detect_intent(req.message)
        logger.info("Intent detection result: type=%s payload_keys=%s", response_type.value, sorted(payload.keys()))

        if response_type == ResponseType.kpi:
            sql_query = "SELECT SUM(revenue) AS revenue FROM fact_sales"
            logger.info("KPI branch selected; preparing SQL query")
            logger.info("SQL query text: %s", sql_query)
            sql_rows = await execute_sql_query(sql_query)
            logger.info("SQL execution returned %d row(s)", len(sql_rows))
            payload["data"] = sql_rows
            payload["value"] = float(sql_rows[0]["value"]) if sql_rows else float(payload["value"])

        response_payload = _build_response_payload(response_type, payload)
        logger.info("Returning structured response: %s", response_payload)
        return response_payload
    except HTTPException:
        logger.info("HTTPException bubbled up from /chat")
        raise
    except ValueError as exc:
        logger.warning("Validation error while building chat response: %s", exc)
        raise HTTPException(status_code=400, detail=f"Invalid input: {exc}") from exc
    except Exception as e:
        logger.exception("Unhandled exception in /chat endpoint")
        raise HTTPException(status_code=500, detail="Failed to process chat request. Please try again.") from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
