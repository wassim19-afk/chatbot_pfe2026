from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from data.db_connection import test_db_connection
from services.bi_sql_service import BIQueryError, bi_query_service

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bi-chat-api")
logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))

API_KEY = os.getenv("API_KEY")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., description="Natural-language BI question")
    session_id: Optional[str] = Field(default=None, description="Optional session identifier")


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="Response type: kpi, table, or text")
    value: float = Field(default=0.0, description="KPI value")
    currency: str = Field(default="BnFCFA", description="Currency label")
    dashboard_url: str = Field(default="", description="Power BI dashboard link")
    data: list[dict[str, Any]] = Field(default_factory=list, description="Structured result rows")


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "error"
    message: str
    detail: Optional[str] = None


app = FastAPI(
    title="BI Chatbot API",
    version="3.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event() -> None:
    """FastAPI startup: log configuration and test SQL Server connection."""
    logger.info("=" * 80)
    logger.info("FastAPI BI Chatbot API starting up")
    logger.info("=" * 80)
    
    # Log pymssql backend info
    logger.info("Using pymssql backend (no pyodbc / ODBC dependency)")
    logger.info("DB_SERVER: %s", os.getenv("DB_SERVER", "not set"))
    logger.info("DB_DATABASE: %s", os.getenv("DB_DATABASE", "not set"))
    logger.info("DB_USER: %s", "set" if os.getenv("DB_USER") else "not set")
    logger.info("LOG_LEVEL: %s", os.getenv("LOG_LEVEL", "INFO"))
    
    try:
        logger.info("Running BI service diagnostics...")
        diagnostics = bi_query_service.startup_diagnostics()
        logger.info("BI diagnostics table_count=%s", diagnostics.get("table_count"))
        logger.info("BI diagnostics tables=%s", diagnostics.get("tables", [])[:10])
        
        logger.info("Testing SQL Server connection (SELECT 1)...")
        success = await asyncio.to_thread(test_db_connection)
        logger.info("SQL connection test: %s", "✓ SUCCESS" if success else "✗ FAILED")
        
        if success:
            logger.info("=" * 80)
            logger.info("FastAPI startup complete - all systems ready")
            logger.info("=" * 80)
        else:
            logger.error("SQL connection test failed but continuing (queries may fail)")
            
    except Exception as e:
        logger.exception("FastAPI startup diagnostic failed: %s", str(e))
        logger.error("API will start but SQL queries may fail - check logs for details")


def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    if not API_KEY:
        logger.info("API key validation disabled because API_KEY is not configured")
        return True

    if not x_api_key or x_api_key != API_KEY:
        logger.warning("API key validation failed")
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    logger.info("API key validation succeeded")
    return True


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception for path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(message="Internal server error", detail=str(exc)).model_dump(),
    )


@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "bi-chat-api"}


@app.get("/diagnostics", tags=["diagnostics"])
async def diagnostics_endpoint():
    """
    Diagnostics endpoint: check DB connection, backend info, and configuration.
    Useful for testing Render deployment and troubleshooting connectivity.
    """
    logger.info("/diagnostics request")
    
    diagnostics_result = {
        "service": "bi-chat-api",
        "backend": "pymssql",
        "db_server": os.getenv("DB_SERVER", "not set"),
        "db_database": os.getenv("DB_DATABASE", "not set"),
        "db_user_set": bool(os.getenv("DB_USER")),
        "python_version": "3.11.9",
        "using_pymssql": True,
        "connection_test": False,
        "error": None,
    }
    
    try:
        success = await asyncio.to_thread(test_db_connection)
        diagnostics_result["connection_test"] = success
        if success:
            logger.info("/diagnostics: connection_test SUCCESS")
        else:
            diagnostics_result["error"] = "SELECT 1 test returned False"
            logger.warning("/diagnostics: connection_test FAILED")
    except Exception as e:
        diagnostics_result["connection_test"] = False
        diagnostics_result["error"] = str(e)
        logger.exception("/diagnostics: connection_test exception: %s", str(e))
    
    return diagnostics_result


@app.post(
    "/chat",
    response_model=ChatResponse,
    response_model_exclude_none=False,
    summary="Chat with the BI assistant",
    tags=["chat"],
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def chat_endpoint(req: ChatRequest, authorized: Any = Depends(verify_api_key)):
    logger.info("/chat request received: session_id=%s message=%r", req.session_id, req.message)

    try:
        result = await bi_query_service.query(req.message)
        response = ChatResponse(**result.__dict__)
        logger.info("/chat response=%s", response.model_dump())
        return response.model_dump()
    except BIQueryError as exc:
        logger.exception("BI query failed")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(message="SQL execution failed", detail=str(exc)).model_dump(),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error in /chat")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(message="Unexpected server error", detail=str(exc)).model_dump(),
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
