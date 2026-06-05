"""
The validation endpoint.

GET  /validate           — runs the full assertion suite, returns JSON results
POST /validate/reset     — clears captured envelopes for a fresh run
POST /validate/trigger   — drives the critical + high-volume endpoints, flushes
                           both batchers, then runs the assertion suite
"""
import json

import httpx
import sentry_sdk
from fastapi import APIRouter

from app.sentry_config import (
    critical_transport,
    get_critical_client,
    primary_transport,
)

router = APIRouter()

# Metric names that legitimately belong in the critical transport (i.e. those
# emitted via emit_critical_metric). Everything else must NOT appear there.
CRITICAL_METRIC_NAMES = {
    "payment_webhook_failure",
    "subscription_change_failure",
    "sso_connect_failure",
}


def extract_metrics(transport):
    """Extract individual trace_metric items from captured envelopes."""
    metrics = []
    for envelope in transport.get_captured_envelopes():
        for item in envelope.items:
            if item.type == "trace_metric":
                payload = json.loads(item.get_bytes())
                for m in payload.get("items", []):
                    metrics.append(m)
    return metrics


def metric_name(m):
    """The metric name lives under 'name' in the trace_metric item."""
    return m.get("name")


def get_attribute_value(attributes, key):
    """
    Pull a single attribute value, tolerating both payload shapes:
      {"key": "value"}                              (flat)
      {"key": {"value": "...", "type": "string"}}   (typed)
    """
    if not attributes or key not in attributes:
        return None
    raw = attributes[key]
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def normalize_attributes(attributes):
    """Flatten an attributes dict to {key: value} regardless of shape."""
    result = {}
    for key in (attributes or {}):
        result[key] = get_attribute_value(attributes, key)
    return result


def flush_batchers():
    """Flush both clients' metric batchers so envelopes are captured."""
    primary_client = sentry_sdk.get_client()
    if getattr(primary_client, "metrics_batcher", None):
        primary_client.metrics_batcher.flush()

    crit = get_critical_client()
    if crit is not None and getattr(crit, "metrics_batcher", None):
        crit.metrics_batcher.flush()


# Explicit attributes each critical metric was emitted with, used by CHECK 4.
EXPECTED_ATTRIBUTES = {
    "payment_webhook_failure": {"error_type": "timeout", "plan": "enterprise"},
    "subscription_change_failure": {"error_type": "card_declined", "plan": "enterprise"},
    "sso_connect_failure": {"error_type": "saml_assertion_invalid", "provider": "okta"},
}


def build_checks():
    primary_metrics = extract_metrics(primary_transport)
    critical_metrics = extract_metrics(critical_transport)

    primary_names = [metric_name(m) for m in primary_metrics]
    critical_names = [metric_name(m) for m in critical_metrics]

    checks = []

    # ---- CHECK 1: DUAL_DELIVERY ----------------------------------------
    critical_seen = sorted(set(critical_names) & CRITICAL_METRIC_NAMES)
    dual_pass = (
        len(primary_metrics) > 0
        and CRITICAL_METRIC_NAMES.issubset(set(primary_names))
        and set(critical_names) == CRITICAL_METRIC_NAMES
    )
    checks.append({
        "name": "DUAL_DELIVERY",
        "passed": dual_pass,
        "detail": (
            f"Primary: {len(primary_metrics)} metrics, "
            f"Critical: {len(critical_metrics)} metrics "
            f"({', '.join(critical_seen)})"
        ),
    })

    # ---- CHECK 2: CRITICAL_ISOLATION -----------------------------------
    non_critical = sorted(set(critical_names) - CRITICAL_METRIC_NAMES)
    isolation_pass = len(non_critical) == 0
    checks.append({
        "name": "CRITICAL_ISOLATION",
        "passed": isolation_pass,
        "detail": (
            "Critical transport has 0 non-critical metrics "
            "(api_request_total not present)"
            if isolation_pass
            else f"Critical transport contains non-critical metrics: {non_critical}"
        ),
    })

    # ---- CHECK 3: TRACE_ID_PRESERVATION --------------------------------
    trace_data = {}
    trace_pass = True
    for name in sorted(CRITICAL_METRIC_NAMES):
        p = next((m for m in primary_metrics if metric_name(m) == name), None)
        c = next((m for m in critical_metrics if metric_name(m) == name), None)
        p_tid = p.get("trace_id") if p else None
        c_tid = c.get("trace_id") if c else None
        match = p_tid is not None and p_tid == c_tid
        if not match:
            trace_pass = False
        trace_data[name] = {
            "primary_trace_id": p_tid,
            "critical_trace_id": c_tid,
            "match": match,
        }
    checks.append({
        "name": "TRACE_ID_PRESERVATION",
        "passed": trace_pass,
        "detail": (
            "All critical metrics carry matching trace_id from their request's active span"
            if trace_pass
            else "One or more critical metrics have a mismatched/missing trace_id"
        ),
        "data": trace_data,
    })

    # ---- CHECK 4: ATTRIBUTES_PRESERVED ---------------------------------
    attr_data = {}
    attr_pass = True
    for name in sorted(CRITICAL_METRIC_NAMES):
        c = next((m for m in critical_metrics if metric_name(m) == name), None)
        flat = normalize_attributes(c.get("attributes")) if c else {}
        expected = EXPECTED_ATTRIBUTES[name]
        ok = all(flat.get(k) == v for k, v in expected.items())
        if not ok:
            attr_pass = False
        attr_data[name] = {"attributes": {k: flat.get(k) for k in expected}}
    checks.append({
        "name": "ATTRIBUTES_PRESERVED",
        "passed": attr_pass,
        "detail": (
            "All critical metrics have their explicit attributes intact"
            if attr_pass
            else "One or more critical metrics are missing expected attributes"
        ),
        "data": attr_data,
    })

    # ---- CHECK 5: SCOPE_ISOLATION --------------------------------------
    current = sentry_sdk.get_client()
    crit = get_critical_client()
    scope_pass = current is not crit
    checks.append({
        "name": "SCOPE_ISOLATION",
        "passed": scope_pass,
        "detail": (
            "sentry_sdk.get_client() is still the primary client after all emits"
            if scope_pass
            else "sentry_sdk.get_client() unexpectedly returned the critical client"
        ),
    })

    passed_count = sum(1 for c in checks if c["passed"])
    total = len(checks)
    if passed_count == total:
        summary = f"{passed_count}/{total} PASSED"
    else:
        summary = f"{passed_count}/{total} PASSED, {total - passed_count} FAILED"

    return {"summary": summary, "checks": checks}


@router.get("/validate")
async def validate():
    return build_checks()


@router.post("/validate/reset")
async def validate_reset():
    primary_transport.clear()
    critical_transport.clear()
    return {"status": "cleared"}


@router.post("/validate/trigger")
async def validate_trigger():
    # Step A: Reset
    primary_transport.clear()
    critical_transport.clear()

    # Step B: Make internal requests via ASGI transport (no real network).
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/api/v1/payment/webhook", json={})
        await client.post("/api/v1/payment/subscription", json={})
        await client.post("/api/v1/enterprise/sso/connect", json={})
        await client.post("/api/email/signin", json={})
        await client.get("/api/warmup")

    # Step C: Flush both batchers
    flush_batchers()

    # Step D + E: Extract and assert
    return build_checks()
