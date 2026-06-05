"""
Simulated critical endpoints (billing / payments / enterprise).

These use emit_critical_metric() so the billing-critical metrics are dual-emitted
to the low-volume critical project where they'll never be downsampled.
"""
import asyncio

import sentry_sdk
from fastapi import APIRouter

from app.metrics_helper import emit_critical_metric
from app.models import PaymentWebhookRequest, SSOConnectRequest, SubscriptionRequest

router = APIRouter()


@router.post("/payment/webhook")
async def payment_webhook(req: PaymentWebhookRequest):
    sentry_sdk.set_tag("endpoint", "/api/v1/payment/webhook")
    sentry_sdk.set_user({"id": req.customer_id, "email": "billing@example.com"})
    await asyncio.sleep(0.01)

    # Critical metric — dual-emitted
    emit_critical_metric(
        "payment_webhook_failure",
        1,
        attributes={"error_type": "timeout", "plan": "enterprise"},
    )
    # Standard metric — primary only
    sentry_sdk.metrics.count(
        "api_request_total",
        1,
        attributes={"endpoint": "/api/v1/payment/webhook", "status_code": 500},
    )
    return {"status": "failed", "event_type": req.event_type, "retry": True}


@router.post("/payment/subscription")
async def payment_subscription(req: SubscriptionRequest):
    sentry_sdk.set_tag("endpoint", "/api/v1/payment/subscription")
    sentry_sdk.set_user({"id": req.customer_id, "email": "billing@example.com"})
    await asyncio.sleep(0.01)

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
    return {"status": "failed", "plan": req.plan, "action": req.action}


@router.post("/enterprise/sso/connect")
async def sso_connect(req: SSOConnectRequest):
    sentry_sdk.set_tag("endpoint", "/api/v1/enterprise/sso/connect")
    sentry_sdk.set_user({"id": req.org_id, "email": req.email})
    await asyncio.sleep(0.01)

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
    return {"status": "failed", "org_id": req.org_id, "provider": req.provider}
