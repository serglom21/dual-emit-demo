"""
Stress endpoints — exercise the edges of the split-routing pattern so the
validation suite can assert their behavior.

Each one corresponds to a category in STRESS_TEST.md:

  POST /stress/raise_after_emit          — error attribution (cat #1a)
  POST /stress/capture_in_critical_scope — scope-redirect contract (cat #1b)
  POST /stress/pii_attr                  — PII reaches critical unscrubbed (cat #5)
  POST /stress/gauge                     — gauge type routes to critical (cat #12a)
  POST /stress/distribution              — distribution routes to critical (cat #12b)
  POST /stress/concurrent                — N concurrent emits, no loss (cat #9)
  POST /stress/bad_inner_dsn             — primary unaffected if critical inner DSN bad (cat #8)
"""
import asyncio

import sentry_sdk
from fastapi import APIRouter
from pydantic import BaseModel

from app.metrics_helper import (
    emit_critical_distribution,
    emit_critical_gauge,
    emit_critical_metric,
)
from app.sentry_config import critical_transport, get_critical_client

router = APIRouter()


class ConcurrentRequest(BaseModel):
    count: int = 50


@router.post("/stress/raise_after_emit")
async def raise_after_emit():
    """
    Emit a critical metric, then raise unhandled. The exception should be
    captured by the FastAPI integration (primary client), not by the critical
    client — because we're outside the new_scope() block by the time the
    exception flies.
    """
    sentry_sdk.set_tag("stress_case", "raise_after_emit")
    emit_critical_metric(
        "stress_raise_metric", 1, attributes={"case": "raise_after_emit"}
    )
    raise RuntimeError("stress: raise_after_emit (expected — goes to primary)")


@router.post("/stress/capture_in_critical_scope")
async def capture_in_critical_scope():
    """
    Confirms the scope-redirect contract: inside a manually opened
    `with new_scope() as scope: scope.client = critical_client` block,
    every SDK call (including capture_exception) uses the redirected
    client. This is the documented, expected behavior of the scope/client
    mechanism — the route exists so the validation suite can assert the
    SDK still honors it.

    The `emit_critical_metric` helper as shipped does NOT call
    capture_exception inside its scope block, so following the helper
    does not surface this behavior. If you write your own scope-swap
    code and call capture_exception inside, the error goes to the
    redirected client (by design).
    """
    sentry_sdk.set_tag("stress_case", "capture_in_critical_scope")
    critical_client = get_critical_client()
    if critical_client is None:
        return {"status": "skipped", "reason": "no critical client"}

    with sentry_sdk.new_scope() as scope:
        scope.client = critical_client
        try:
            raise RuntimeError(
                "stress: captured inside critical scope (expected scope-redirect)"
            )
        except RuntimeError:
            sentry_sdk.capture_exception()
    return {"status": "captured", "note": "error routed to critical (scope redirect, as designed)"}


@router.post("/stress/pii_attr")
async def pii_attr():
    """
    Emit a critical metric with PII-shaped attribute values. The critical
    client has no PII scrubbing integration, so whatever we pass arrives raw.
    The validation suite asserts this round-trips exactly, documenting the
    asymmetric-scrubbing limitation.
    """
    sentry_sdk.set_tag("stress_case", "pii_attr")
    emit_critical_metric(
        "stress_pii_metric",
        1,
        attributes={
            "email": "leak-test@example.com",
            "ssn_like": "123-45-6789",
            "ip": "203.0.113.42",
        },
    )
    return {"status": "emitted"}


@router.post("/stress/gauge")
async def gauge():
    sentry_sdk.set_tag("stress_case", "gauge")
    emit_critical_gauge(
        "stress_queue_depth", 42.0, attributes={"queue": "billing-retry"}
    )
    return {"status": "emitted"}


@router.post("/stress/distribution")
async def distribution():
    sentry_sdk.set_tag("stress_case", "distribution")
    emit_critical_distribution(
        "stress_webhook_latency", 87.3, attributes={"endpoint": "payment_webhook"}
    )
    return {"status": "emitted"}


@router.post("/stress/concurrent")
async def concurrent(req: ConcurrentRequest):
    """
    Fire `count` critical emits in parallel via asyncio.gather. The validation
    suite asserts every one of them is captured on the critical side with no
    loss and no attribution scrambling.
    """
    sentry_sdk.set_tag("stress_case", "concurrent")
    sentry_sdk.set_tag("concurrent_count", str(req.count))

    async def one(i):
        emit_critical_metric(
            "stress_concurrent_metric",
            1,
            attributes={"i": str(i)},
        )

    await asyncio.gather(*(one(i) for i in range(req.count)))
    return {"status": "emitted", "count": req.count}


@router.post("/stress/bad_inner_dsn")
async def bad_inner_dsn():
    """
    Simulate a broken critical destination, then assert primary keeps working.

    Carefully ordered:
      1. Flush both batchers FIRST so any prior critical metrics ship cleanly
         (otherwise step 3's flush would drop them along with the orphan).
      2. Swap critical CaptureTransport._inner to None — broken-DSN simulation.
      3. Emit orphan + primary, then flush both batchers. Orphan envelope's
         forward-to-inner is a no-op, so it never reaches Sentry. The primary
         metric ships through primary's CaptureTransport (its inner is intact).
      4. Restore the saved inner.
    """
    sentry_sdk.set_tag("stress_case", "bad_inner_dsn")
    critical_client = get_critical_client()
    primary_client = sentry_sdk.get_client()

    # Step 1: drain prior pending metrics so they ship before we break things.
    if critical_client is not None and getattr(critical_client, "metrics_batcher", None):
        critical_client.metrics_batcher.flush()
    if getattr(primary_client, "metrics_batcher", None):
        primary_client.metrics_batcher.flush()

    saved = critical_transport._inner
    try:
        critical_transport.set_inner(None)
        emit_critical_metric(
            "stress_orphan_metric", 1, attributes={"reason": "critical_offline"}
        )
        sentry_sdk.metrics.count(
            "stress_primary_still_works", 1, attributes={"during": "critical_offline"}
        )
        # Flush *while* the critical inner is None so the orphan envelope's
        # forward-to-inner is a no-op (broken DSN), but primary ships normally.
        if critical_client is not None and getattr(critical_client, "metrics_batcher", None):
            critical_client.metrics_batcher.flush()
        if getattr(primary_client, "metrics_batcher", None):
            primary_client.metrics_batcher.flush()
    finally:
        critical_transport.set_inner(saved)
    return {"status": "emitted"}
