"""
Simulated high-volume endpoints.

These emit only standard metrics (primary project only). They exist to simulate
the noise that causes aggressive downsampling in the real service.
"""
import asyncio

import sentry_sdk
from fastapi import APIRouter

from app.models import LLMExtractRequest, SigninRequest

router = APIRouter()


@router.post("/email/signin")
async def email_signin(req: SigninRequest):
    await asyncio.sleep(0.01)
    sentry_sdk.metrics.count(
        "api_request_total",
        1,
        attributes={"endpoint": "/api/email/signin", "status_code": 200},
    )
    return {"status": "ok", "email": req.email}


@router.post("/llm/extract")
async def llm_extract(req: LLMExtractRequest):
    await asyncio.sleep(0.01)
    sentry_sdk.metrics.count(
        "api_request_total",
        1,
        attributes={"endpoint": "/api/llm/extract", "status_code": 200},
    )
    sentry_sdk.metrics.distribution(
        "llm_latency",
        12.5,
        attributes={"endpoint": "/api/llm/extract", "model": "gpt-4o"},
    )
    return {"status": "ok", "entities": ["acme", "invoice"]}


@router.get("/warmup")
async def warmup():
    await asyncio.sleep(0.01)
    sentry_sdk.metrics.count(
        "api_request_total",
        1,
        attributes={"endpoint": "/api/warmup", "status_code": 200},
    )
    return {"status": "warm"}
