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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bi-chat-api")


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


def detect_intent(message: str) -> tuple[ResponseType, dict[str, Any]]:
    normalized_message = message.lower().strip()

    logger.debug("Detecting intent for message=%r", normalized_message)

    if "kpi" in normalized_message or re.search(r"\b(revenue|sales|profit|margin|growth)\b", normalized_message):
        logger.debug("KPI intent detected")
        numbers = re.findall(r"[-+]?[0-9]*\.?[0-9]+", message.replace(",", ""))
        kpi_value = float(numbers[0]) if numbers else 150000.0
        return ResponseType.kpi, {
            "value": kpi_value,
            "currency": "BnFCFA",
            "dashboard_url": "https://example.com/dashboard/kpi",
            "data": [],
        }

    if re.search(r"\b(table|list|rows|show me|breakdown)\b", normalized_message):
        logger.debug("Table intent detected")
        return ResponseType.table, {
            "value": 0.0,
            "currency": "BnFCFA",
            "dashboard_url": "https://example.com/dashboard/table",
            "data": [
                {"label": "Cameroon", "sales": 1245000},
                {"label": "Senegal", "sales": 845000},
            ],
        }

    logger.debug("Text intent detected")
    return ResponseType.text, {
        "value": 0.0,
        "currency": "BnFCFA",
        "dashboard_url": "",
        "data": [],
    }


async def execute_sql_query(sql_query: str) -> list[dict[str, Any]]:
    logger.debug("Executing SQL query: %s", sql_query)
    return [
        {"metric": "revenue", "value": 150000.0},
    ]


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
        logger.info("chat request received; session_id=%s", req.session_id)

        response_type, payload = detect_intent(req.message)

        if response_type == ResponseType.kpi:
            sql_query = "SELECT SUM(revenue) AS revenue FROM fact_sales"
            sql_rows = await execute_sql_query(sql_query)
            logger.debug("SQL execution returned %d row(s)", len(sql_rows))
            payload["data"] = sql_rows
            payload["value"] = float(sql_rows[0]["value"]) if sql_rows else float(payload["value"])

        logger.info("Returning response type=%s dashboard_url=%s", response_type.value, payload.get("dashboard_url", ""))
        return _build_response_payload(response_type, payload)
    except HTTPException:
        raise
    except ValueError as exc:
        logger.warning("Validation error while building chat response: %s", exc)
        raise HTTPException(status_code=400, detail=f"Invalid input: {exc}") from exc
    except Exception as e:
        logger.exception("Error handling /chat: %s", e)
        raise HTTPException(status_code=500, detail="Failed to process chat request. Please try again.") from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
