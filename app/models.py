"""Pydantic models for request/response payloads."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class SigninRequest(BaseModel):
    email: str = "user@example.com"


class LLMExtractRequest(BaseModel):
    prompt: str = "Extract entities from this text."


class PaymentWebhookRequest(BaseModel):
    event_type: str = "invoice.payment_failed"
    customer_id: str = "cus_123"
    amount: int = 4999


class SubscriptionRequest(BaseModel):
    customer_id: str = "cus_123"
    plan: str = "enterprise"
    action: str = "upgrade"


class SSOConnectRequest(BaseModel):
    org_id: str = "org_456"
    email: str = "admin@enterprise.example.com"
    provider: str = "okta"


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str
    data: Optional[Dict[str, Any]] = None


class ValidationResponse(BaseModel):
    summary: str
    checks: List[CheckResult]
