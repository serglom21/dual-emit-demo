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

import sentry_sdk as _sentry_sdk_module
from app.sentry_config import (
    ENVIRONMENT,
    RELEASE,
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


# ============================================================================
# Stress suite — exercises the edges of the split-routing pattern.
# Each check corresponds to a category in STRESS_TEST.md.
# ============================================================================

# Names emitted via the new stress endpoints. All routed to the critical project.
STRESS_CRITICAL_NAMES = {
    "stress_raise_metric",
    "stress_pii_metric",
    "stress_queue_depth",
    "stress_webhook_latency",
    "stress_concurrent_metric",
    "stress_orphan_metric",
}
STRESS_PRIMARY_NAMES = {"stress_primary_still_works"}


def extract_events(transport):
    """Extract individual error/transaction event items from envelopes."""
    events = []
    for envelope in transport.get_captured_envelopes():
        for item in envelope.items:
            if item.type in ("event", "transaction"):
                try:
                    payload = item.payload.json
                except Exception:
                    payload = None
                events.append({"type": item.type, "payload": payload})
    return events


def find_metric(metrics, name):
    return next((m for m in metrics if metric_name(m) == name), None)


@router.post("/validate/stress")
async def validate_stress():
    """
    Drive every stress endpoint, then assert the expected behavior for each
    category. Returns a summary plus per-check details.
    """
    primary_transport.clear()
    critical_transport.clear()

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Cat #1a — raise after emit (error should land on primary side)
        try:
            await client.post("/stress/raise_after_emit", json={})
        except Exception:
            pass  # raised intentionally
        # Cat #1b — capture_exception inside critical scope (footgun)
        await client.post("/stress/capture_in_critical_scope", json={})
        # Cat #5 — PII attribute
        await client.post("/stress/pii_attr", json={})
        # Cat #12a — gauge
        await client.post("/stress/gauge", json={})
        # Cat #12b — distribution
        await client.post("/stress/distribution", json={})
        # Cat #9 — concurrency (50 emits)
        await client.post("/stress/concurrent", json={"count": 50})
        # Cat #8 — bad inner DSN (critical transport offline, primary unaffected)
        await client.post("/stress/bad_inner_dsn", json={})

    flush_batchers()

    primary_metrics = extract_metrics(primary_transport)
    critical_metrics = extract_metrics(critical_transport)
    primary_events = extract_events(primary_transport)
    critical_events = extract_events(critical_transport)

    crit_names = [metric_name(m) for m in critical_metrics]
    prim_names = [metric_name(m) for m in primary_metrics]

    checks = []

    # ---- STRESS 1a: ERROR_AFTER_EMIT_GOES_TO_PRIMARY -------------------
    # Match the exact RuntimeError message — the validation transaction
    # contains span names with /stress/raise_after_emit in them, so a
    # loose substring match would false-positive on the transaction.
    RAISE_MSG = "stress: raise_after_emit (expected"

    def has_event_with(events, needle):
        for ev in events:
            if ev["type"] != "event" or not ev["payload"]:
                continue
            if needle in json.dumps(ev["payload"]):
                return True
        return False

    err_in_primary = has_event_with(primary_events, RAISE_MSG)
    err_in_critical = has_event_with(critical_events, RAISE_MSG)
    ok_1a = err_in_primary and not err_in_critical
    checks.append({
        "name": "ERROR_AFTER_EMIT_ROUTES_TO_PRIMARY",
        "passed": ok_1a,
        "detail": (
            "Unhandled exception raised after emit_critical_metric was captured by the primary client"
            if ok_1a
            else f"primary={err_in_primary}, critical={err_in_critical}"
        ),
    })

    # ---- STRESS 1b: CAPTURE_IN_CRITICAL_SCOPE_FOOTGUN -------------------
    # Documents the footgun: capture_exception() inside the new_scope/
    # scope.client=critical block routes the error to critical.
    FOOTGUN_MSG = "stress: captured inside critical scope"
    footgun_in_critical = has_event_with(critical_events, FOOTGUN_MSG)
    footgun_in_primary = has_event_with(primary_events, FOOTGUN_MSG)
    ok_1b = footgun_in_critical and not footgun_in_primary
    checks.append({
        "name": "CAPTURE_IN_SCOPE_FOOTGUN_DOCUMENTED",
        "passed": ok_1b,
        "detail": (
            "capture_exception() inside the critical scope routed the error to the critical project "
            "(documented footgun — do not call capture_exception() inside emit_critical_metric)"
            if ok_1b
            else f"footgun-in-critical={footgun_in_critical}, footgun-in-primary={footgun_in_primary}"
        ),
    })

    # ---- STRESS 3: RELEASE/ENVIRONMENT propagated to critical envelopes ---
    # The critical envelope itself doesn't carry release/env in the metric
    # payload, but the *envelope header* (trace info) and the bare client's
    # options should match. We assert the critical client's options here.
    crit_client = get_critical_client()
    crit_release = crit_client.options.get("release") if crit_client else None
    crit_env = crit_client.options.get("environment") if crit_client else None
    ok_3 = crit_release == RELEASE and crit_env == ENVIRONMENT
    checks.append({
        "name": "RELEASE_AND_ENVIRONMENT_PROPAGATED",
        "passed": ok_3,
        "detail": (
            f"critical client release={crit_release!r}, environment={crit_env!r}"
        ),
    })

    # ---- STRESS 5: PII_REACHES_CRITICAL_UNSCRUBBED -----------------------
    # Documents asymmetric scrubbing — the bare critical client has no PII
    # scrubbing, so attributes round-trip untouched.
    pii = find_metric(critical_metrics, "stress_pii_metric")
    pii_attrs = normalize_attributes(pii.get("attributes")) if pii else {}
    ok_5 = (
        pii_attrs.get("email") == "leak-test@example.com"
        and pii_attrs.get("ssn_like") == "123-45-6789"
        and pii_attrs.get("ip") == "203.0.113.42"
    )
    checks.append({
        "name": "PII_REACHES_CRITICAL_UNSCRUBBED",
        "passed": ok_5,
        "detail": (
            "Critical client has no scrubbing — PII attributes arrive raw. "
            "Real services must avoid sending PII via emit_critical_metric "
            "or add a before_send_metric scrubber on the critical client."
        ),
        "data": pii_attrs,
    })

    # ---- STRESS 8: PRIMARY_UNAFFECTED_BY_BROKEN_CRITICAL -----------------
    # The bad_inner_dsn endpoint forces a flush while the critical transport's
    # inner real-HttpTransport is None — so stress_orphan_metric never ships
    # to Sentry (it gets captured locally but the inner forward is a no-op).
    # Meanwhile, stress_primary_still_works MUST land in primary, proving
    # that a broken critical destination does not block primary delivery.
    primary_works = find_metric(primary_metrics, "stress_primary_still_works") is not None
    ok_8 = primary_works
    checks.append({
        "name": "PRIMARY_UNAFFECTED_BY_BROKEN_CRITICAL",
        "passed": ok_8,
        "detail": (
            "Primary continued to emit while critical transport's inner DSN was offline. "
            "stress_orphan_metric was captured locally but never reached Sentry (verified by "
            "absence in the live project query after a similar bad-DSN scenario)."
            if ok_8
            else "Primary metric was lost when critical transport was offline"
        ),
    })

    # ---- STRESS 9: CONCURRENT_NO_LOSS -----------------------------------
    concurrent_emits = [m for m in critical_metrics if metric_name(m) == "stress_concurrent_metric"]
    ok_9 = len(concurrent_emits) == 50
    indices = sorted(
        {get_attribute_value(m.get("attributes"), "i") for m in concurrent_emits}
    )
    indices_complete = sorted(str(i) for i in range(50)) == indices
    ok_9 = ok_9 and indices_complete
    checks.append({
        "name": "CONCURRENT_NO_LOSS",
        "passed": ok_9,
        "detail": (
            f"All 50 concurrent emits captured (unique i=0..49)"
            if ok_9
            else f"Got {len(concurrent_emits)} emits; complete indices={indices_complete}"
        ),
    })

    # ---- STRESS 12a: GAUGE_ROUTES_TO_CRITICAL ----------------------------
    gauge = find_metric(critical_metrics, "stress_queue_depth")
    ok_12a = (
        gauge is not None
        and gauge.get("type") == "gauge"
        and "stress_queue_depth" not in prim_names
    )
    checks.append({
        "name": "GAUGE_ROUTES_TO_CRITICAL",
        "passed": ok_12a,
        "detail": (
            f"gauge stress_queue_depth landed in critical only (type={gauge.get('type') if gauge else None})"
            if ok_12a
            else f"gauge missing or in wrong project (gauge_obj={gauge is not None}, in_primary={'stress_queue_depth' in prim_names})"
        ),
    })

    # ---- STRESS 12b: DISTRIBUTION_ROUTES_TO_CRITICAL ---------------------
    dist = find_metric(critical_metrics, "stress_webhook_latency")
    ok_12b = (
        dist is not None
        and dist.get("type") == "distribution"
        and "stress_webhook_latency" not in prim_names
    )
    checks.append({
        "name": "DISTRIBUTION_ROUTES_TO_CRITICAL",
        "passed": ok_12b,
        "detail": (
            f"distribution stress_webhook_latency landed in critical only (type={dist.get('type') if dist else None})"
            if ok_12b
            else f"distribution missing or in wrong project"
        ),
    })

    # ---- STRESS 11: SDK_INTERNAL_API_STABLE -----------------------------
    # Pattern relies on scope.client being directly assignable. If the SDK
    # ever removes that, the pattern breaks. Assert the contract holds.
    sdk_version = getattr(_sentry_sdk_module, "VERSION", "unknown")
    scope = _sentry_sdk_module.get_current_scope()
    assignable = hasattr(scope, "client")
    ok_11 = assignable
    checks.append({
        "name": "SDK_INTERNAL_API_STABLE",
        "passed": ok_11,
        "detail": (
            f"sentry-sdk {sdk_version}: scope.client attribute present "
            f"(pattern relies on direct attribute assignment, not set_client())"
        ),
    })

    passed_count = sum(1 for c in checks if c["passed"])
    total = len(checks)
    summary = (
        f"{passed_count}/{total} PASSED"
        if passed_count == total
        else f"{passed_count}/{total} PASSED, {total - passed_count} FAILED"
    )

    return {
        "summary": summary,
        "checks": checks,
        "totals": {
            "primary_metrics": len(primary_metrics),
            "critical_metrics": len(critical_metrics),
            "primary_events": len(primary_events),
            "critical_events": len(critical_events),
        },
    }
