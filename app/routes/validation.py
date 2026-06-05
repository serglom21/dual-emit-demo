"""
The validation endpoint.

GET  /validate           — runs the full assertion suite, returns JSON results
POST /validate/reset     — clears captured envelopes for a fresh run
POST /validate/trigger   — drives the critical + high-volume endpoints, flushes
                           both batchers, then runs the assertion suite

This validates a *split-routing* setup: critical metrics live exclusively in
the critical project, non-critical metrics live exclusively in the primary
project. No duplication across projects.
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

# Metric names routed exclusively to the critical project.
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
    return {k: get_attribute_value(attributes, k) for k in (attributes or {})}


def flush_batchers():
    primary_client = sentry_sdk.get_client()
    if getattr(primary_client, "metrics_batcher", None):
        primary_client.metrics_batcher.flush()

    crit = get_critical_client()
    if crit is not None and getattr(crit, "metrics_batcher", None):
        crit.metrics_batcher.flush()


EXPECTED_ATTRIBUTES = {
    "payment_webhook_failure": {"error_type": "timeout", "plan": "enterprise"},
    "subscription_change_failure": {"error_type": "card_declined", "plan": "enterprise"},
    "sso_connect_failure": {"error_type": "saml_assertion_invalid", "provider": "okta"},
}

# Critical metric name → endpoint that emits it. The validator hits each
# endpoint, captures its returned trace_id, and later asserts the critical
# envelope's trace_id matches.
CRITICAL_ENDPOINTS = {
    "payment_webhook_failure": "/api/v1/payment/webhook",
    "subscription_change_failure": "/api/v1/payment/subscription",
    "sso_connect_failure": "/api/v1/enterprise/sso/connect",
}


def build_checks(expected_trace_ids=None):
    """
    expected_trace_ids: {metric_name: trace_id} captured from each critical
    endpoint's response. When None, the trace-id check is skipped (used by
    GET /validate against whatever happens to be in the buffers).
    """
    primary_metrics = extract_metrics(primary_transport)
    critical_metrics = extract_metrics(critical_transport)

    primary_names = [metric_name(m) for m in primary_metrics]
    critical_names = [metric_name(m) for m in critical_metrics]
    primary_set = set(primary_names)
    critical_set = set(critical_names)

    checks = []

    # ---- CHECK 1: SPLIT_ROUTING ----------------------------------------
    # Primary received the non-critical metric; critical received the
    # critical metrics — and neither side contains anything from the
    # other.
    leaked_to_primary = sorted(primary_set & CRITICAL_METRIC_NAMES)
    leaked_to_critical = sorted(critical_set - CRITICAL_METRIC_NAMES)
    critical_present = sorted(critical_set & CRITICAL_METRIC_NAMES)
    non_critical_present = sorted(primary_set - CRITICAL_METRIC_NAMES)
    split_pass = (
        len(leaked_to_primary) == 0
        and len(leaked_to_critical) == 0
        and len(critical_present) > 0
        and len(non_critical_present) > 0
    )
    checks.append({
        "name": "SPLIT_ROUTING",
        "passed": split_pass,
        "detail": (
            f"Primary: {len(primary_metrics)} metrics ({', '.join(non_critical_present)}); "
            f"Critical: {len(critical_metrics)} metrics ({', '.join(critical_present)})"
        ),
        "data": {
            "primary_metric_names": non_critical_present,
            "critical_metric_names": critical_present,
        },
    })

    # ---- CHECK 2: PRIMARY_ISOLATION ------------------------------------
    # No critical metric leaked into the primary project.
    primary_iso_pass = len(leaked_to_primary) == 0
    checks.append({
        "name": "PRIMARY_ISOLATION",
        "passed": primary_iso_pass,
        "detail": (
            "Primary transport contains no critical metrics"
            if primary_iso_pass
            else f"Primary transport leaked critical metrics: {leaked_to_primary}"
        ),
    })

    # ---- CHECK 3: CRITICAL_ISOLATION -----------------------------------
    # No non-critical metric leaked into the critical project.
    crit_iso_pass = len(leaked_to_critical) == 0
    checks.append({
        "name": "CRITICAL_ISOLATION",
        "passed": crit_iso_pass,
        "detail": (
            "Critical transport contains no non-critical metrics (api_request_total absent)"
            if crit_iso_pass
            else f"Critical transport leaked non-critical metrics: {leaked_to_critical}"
        ),
    })

    # ---- CHECK 4: TRACE_ID_PRESERVATION --------------------------------
    # Each critical envelope's trace_id equals the trace_id the FastAPI
    # integration attached to the active scope at emit time, as reported
    # by the endpoint's response.
    trace_data = {}
    trace_pass = True
    if expected_trace_ids is None:
        trace_detail = "skipped — call POST /validate/trigger to verify trace_id preservation"
        trace_pass = True
    else:
        for name in sorted(CRITICAL_METRIC_NAMES):
            c = next((m for m in critical_metrics if metric_name(m) == name), None)
            envelope_tid = c.get("trace_id") if c else None
            expected_tid = expected_trace_ids.get(name)
            match = (
                envelope_tid is not None
                and expected_tid is not None
                and envelope_tid == expected_tid
            )
            if not match:
                trace_pass = False
            trace_data[name] = {
                "expected_trace_id": expected_tid,
                "envelope_trace_id": envelope_tid,
                "match": match,
            }
        trace_detail = (
            "Every critical envelope's trace_id matches the active transaction's trace_id at emit time"
            if trace_pass
            else "One or more critical metrics have a mismatched/missing trace_id"
        )
    checks.append({
        "name": "TRACE_ID_PRESERVATION",
        "passed": trace_pass,
        "detail": trace_detail,
        "data": trace_data,
    })

    # ---- CHECK 5: ATTRIBUTES_PRESERVED ---------------------------------
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

    # ---- CHECK 6: SCOPE_ISOLATION --------------------------------------
    scope_pass = sentry_sdk.get_client() is not get_critical_client()
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
    summary = (
        f"{passed_count}/{total} PASSED"
        if passed_count == total
        else f"{passed_count}/{total} PASSED, {total - passed_count} FAILED"
    )
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

    # Step B: Drive endpoints over the ASGI transport (no real network).
    from app.main import app

    expected_trace_ids = {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for metric, path in CRITICAL_ENDPOINTS.items():
            r = await client.post(path, json={})
            expected_trace_ids[metric] = r.json().get("trace_id")
        await client.post("/api/email/signin", json={})
        await client.get("/api/warmup")

    # Step C: Flush both batchers
    flush_batchers()

    # Step D + E: Extract and assert
    return build_checks(expected_trace_ids=expected_trace_ids)
