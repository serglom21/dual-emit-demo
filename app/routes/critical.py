"""
Simulated critical endpoints (billing / payments / enterprise).

Each route emits a critical metric via emit_critical_metric() — which routes
exclusively to the critical project — *and* a routine api_request_total via
plain sentry_sdk.metrics.count(), which lands only on the primary client.
One call site, two projects, no duplication.

The response includes the request's active trace_id so the validator can
confirm the trace_id captured on the critical envelope matches what the
FastAPI integration attached to the scope at emit time.
"""
import asyncio

import sentry_sdk
from fastapi import APIRouter

from app.metrics_helper import emit_critical_metric
from app.models import PaymentWebhookRequest, SSOConnectRequest, SubscriptionRequest

router = APIRouter()


def _current_trace_id():
    """Return the active span's trace_id (whatever the FastAPI integration set up)."""
    span = sentry_sdk.get_current_span()
    return span.trace_id if span is not None else None


@router.post("/payment/webhook")
async def payment_webhook(req: PaymentWebhookRequest):
    sentry_sdk.set_tag("endpoint", "/api/v1/payment/webhook")
    sentry_sdk.set_user({"id": req.customer_id, "email": "billing@example.com"})
    await asyncio.sleep(0.01)

    trace_id = _current_trace_id()
    emit_critical_metric(
        "payment_webhook_failure",
        1,
        attributes={"error_type": "timeout", "plan": "enterprise"},
    )
    sentry_sdk.metrics.count(
        "api_request_total",
        1,
        attributes={"endpoint": "/api/v1/payment/webhook", "status_code": 500},
    )
    return {
        "status": "failed",
        "event_type": req.event_type,
        "retry": True,
        "trace_id": trace_id,
    }


@router.post("/payment/subscription")
async def payment_subscription(req: SubscriptionRequest):
    sentry_sdk.set_tag("endpoint", "/api/v1/payment/subscription")
    sentry_sdk.set_user({"id": req.customer_id, "email": "billing@example.com"})
    await asyncio.sleep(0.01)

    trace_id = _current_trace_id()
    emit_critical_metric(
        "subscription_change_failure",
        1,
        attributes={"error_type": "card_declined", "plan": req.plan},
    )
    sentry_sdk.metrics.count(
        "api_request_total",
        1,
        attributes={"endpoint": "/api/v1/payment/subscription", "status_code": 402},
    )
    return {
        "status": "failed",
        "plan": req.plan,
        "action": req.action,
        "trace_id": trace_id,
    }


@router.post("/enterprise/sso/connect")
async def sso_connect(req: SSOConnectRequest):
    sentry_sdk.set_tag("endpoint", "/api/v1/enterprise/sso/connect")
    sentry_sdk.set_user({"id": req.org_id, "email": req.email})
    await asyncio.sleep(0.01)

    trace_id = _current_trace_id()
    emit_critical_metric(
        "sso_connect_failure",
        1,
        attributes={"error_type": "saml_assertion_invalid", "provider": req.provider},
    )
    sentry_sdk.metrics.count(
        "api_request_total",
        1,
        attributes={"endpoint": "/api/v1/enterprise/sso/connect", "status_code": 403},
    )
    return {
        "status": "failed",
        "org_id": req.org_id,
        "provider": req.provider,
        "trace_id": trace_id,
    }
