from enum import Enum
import os
import re
import logging
from typing import List, Optional, Any

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
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
    message: str = Field(..., example="Show me revenue KPI for Q1")
    session_id: Optional[str] = Field(None, example="31cf649c-...")


class ChatResponse(BaseModel):
    type: ResponseType
    value: Optional[float] = None
    currency: Optional[str] = "BnFCFA"
    dashboard_url: Optional[str] = None
    data: Optional[List[dict]] = None


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


@app.exception_handler(Exception)
async def internal_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "bi-chat-api"}


def _heuristic_response(message: str) -> ChatResponse:
    msg = message.lower()

    # KPI detection
    if "kpi" in msg or re.search(r"revenue|sales|profit|margin|growth", msg):
        # simple numeric extractor
        nums = re.findall(r"[-+]?[0-9]*\.?[0-9]+", message.replace(',', ''))
        value = float(nums[0]) if nums else 123.45
        return ChatResponse(
            type=ResponseType.kpi,
            value=value,
            currency="BnFCFA",
            dashboard_url="https://example.com/dashboard/kpi",
        )

    # Table detection
    if "table" in msg or "list" in msg or "show me" in msg:
        return ChatResponse(
            type=ResponseType.table,
            data=[
                {"country": "Cameroon", "sales": 1245000},
                {"country": "Senegal", "sales": 845000},
            ],
            dashboard_url="https://example.com/dashboard/table",
        )

    # Default text response
    return ChatResponse(
        type=ResponseType.text,
        value=None,
        data=None,
    )


@app.post(
    "/chat",
    response_model=ChatResponse,
    summary="Chat with the BI assistant",
    tags=["chat"],
)
async def chat_endpoint(
    req: ChatRequest, authorized: Any = Depends(verify_api_key)
):
    try:
        logger.info("chat request received; session=%s", req.session_id)
        # NOTE: Replace this heuristic with real model/service integration.
        resp = _heuristic_response(req.message)
        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error handling /chat: %s", e)
        raise HTTPException(status_code=500, detail="Failed to process chat request")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
