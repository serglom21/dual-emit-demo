# Stress Test — Limits & Assumptions of the Split-Routing Pattern

The basic split-routing assertion suite (`/validate/trigger` → 6/6 PASSED)
proves the **happy path**: critical metrics land in the critical project,
routine metrics land in the primary project, scope context is preserved.

This document covers the **edges** — what happens at the boundaries, where
the pattern leaks, and what users need to know before adopting it.

Twelve categories. Nine are automated via `POST /validate/stress`; three are
operational and verified by hand in the Sentry UI.

---

## Automated checks — `POST /validate/stress` (9/9 PASSED)

| # | Check | What it proves |
|---|---|---|
| 1a | `ERROR_AFTER_EMIT_ROUTES_TO_PRIMARY` | An unhandled exception raised *after* `emit_critical_metric` is captured by the FastAPI integration on the **primary** client — it does NOT leak to the critical project. |
| 1b | `CAPTURE_IN_SCOPE_FOOTGUN_DOCUMENTED` | Calling `sentry_sdk.capture_exception()` *inside* a manually opened `new_scope()` with `scope.client = critical_client` routes the error to the **critical** project. This is a footgun: do not call `capture_exception()` inside `emit_critical_metric`, or wrap it carefully. |
| 3 | `RELEASE_AND_ENVIRONMENT_PROPAGATED` | The bare critical `Client()` is initialized with the same `release` / `environment` as primary, so critical-side metrics carry the same deployment context. |
| 5 | `PII_REACHES_CRITICAL_UNSCRUBBED` | The critical client has no PII scrubbing integration — whatever attributes you pass arrive raw. Do not send PII via `emit_critical_metric` unless you've added scrubbing on the critical side. |
| 8 | `PRIMARY_UNAFFECTED_BY_BROKEN_CRITICAL` | When the critical transport's inner real-`HttpTransport` is unavailable, primary metrics keep flowing. The two clients have independent HttpTransport workers. |
| 9 | `CONCURRENT_NO_LOSS` | Firing 50 concurrent `emit_critical_metric` calls via `asyncio.gather` lands all 50 metrics on the critical side with unique-per-emit attributes, no attribution scrambling. |
| 12a | `GAUGE_ROUTES_TO_CRITICAL` | `emit_critical_gauge` correctly routes gauge metrics to the critical project. |
| 12b | `DISTRIBUTION_ROUTES_TO_CRITICAL` | `emit_critical_distribution` correctly routes distribution metrics. |
| 11 | `SDK_INTERNAL_API_STABLE` | The pattern relies on `scope.client` being directly assignable. Asserted at runtime so an SDK upgrade that removes this contract is caught before deploy. |

Run it:

```bash
curl -X POST http://localhost:8000/validate/stress
```

---

## Manual / operational checks

### #2 — Cross-project trace correlation

**Status:** works, with one click.

A critical metric carries the **same `trace_id`** as the request's primary
transaction. Verifying:

1. Trigger a run: `curl -X POST http://localhost:8000/validate/trigger`.
2. In Sentry, open the critical project's metrics view, click on a
   `payment_webhook_failure` sample, copy the `trace_id`.
3. In the primary project (flask-backend), open Traces and paste that
   `trace_id` — you'll land on the full transaction with all spans.

**Limitation:** Sentry doesn't auto-pivot between projects. The debugger
must open both projects side-by-side. If you have many critical projects,
that friction adds up.

**Live verification done in the snout-and-about org:** trace_id values match
across projects (see `/validate/trigger` response's `TRACE_ID_PRESERVATION`
data block — same `trace_id` echoed by primary transaction and critical
metric).

### #4 — Sampling interaction

**Status:** the metric path is independent of trace sampling.

`sentry_sdk.metrics.count()` (and gauge/distribution) emits regardless of
the trace sample rate. When `traces_sample_rate < 1.0`, the transaction may
never reach Sentry, but the metric still does — with a `trace_id` pointing
to a trace that doesn't exist on the primary side.

To test:

```bash
SENTRY_TRACES_SAMPLE_RATE=0.0 python run.py
# in another terminal:
curl -X POST http://localhost:8000/validate/trigger
```

Then check the critical project — the metric is there with a trace_id;
flask-backend's Traces view will not find that trace_id. **Expected
behavior**, but worth knowing: trace_id on a critical metric isn't a
guaranteed link.

In production, keep `traces_sample_rate` high enough that critical metrics'
trace_ids resolve in primary — or accept that some won't.

### #6 — Alerting

**Status:** works on the critical project for what matters; doesn't work for
errors there.

**Works (the win):** create a metric alert in `critical-metrics-backend` on
`sum(payment_webhook_failure) > N over 5m`. Because the project is
low-volume and never downsampled, the alert is statistically reliable —
which is the whole reason for splitting.

**Doesn't work:** issue alerts on the critical project will never fire — no
integration on that side captures errors. If someone sets one up expecting
"alert when payment webhook errors," they need to set it on the **primary**
project, not critical.

Manual verification: create one of each alert type in Sentry, trigger an
error or a metric threshold, observe.

### #7 — Dashboards and visualization

**Status:** works, but requires multi-project widgets.

A "Billing Health" dashboard needs metric widgets pointed at the critical
project (`payment_webhook_failure`, `sso_connect_failure`) and request /
latency widgets pointed at the primary project (`api_request_total`,
`llm_latency`). Sentry dashboards support multi-project widgets; you just
need to know to point each panel at the right project.

### #10 — Process lifecycle / fork (gunicorn workers)

**Status:** should work — the SDK registers `os.register_at_fork` callbacks
on the batchers. Verified once manually under gunicorn with 4 workers; not
automated because of the orchestration burden.

To verify yourself:

```bash
SENTRY_PRIMARY_DSN=... SENTRY_CRITICAL_DSN=... \
  .venv/bin/gunicorn -k uvicorn.workers.UvicornWorker -w 4 app.main:app
```

Then hit `/validate/trigger` repeatedly and verify in Sentry that each run's
metrics arrived.

---

## Quick reference

| Risk | Severity | Mitigation |
|---|---|---|
| Error routed to critical via `capture_exception` inside scope | High (bug magnet) | Don't call `capture_exception` inside `emit_critical_metric`. Lint for it. |
| PII unscrubbed on critical side | High (compliance) | Don't pass PII as attributes, or add a `before_send_metric` on critical. |
| Critical metric trace_id has no matching trace in primary | Medium (debugging UX) | Tune `traces_sample_rate` higher than the rate critical metrics fire. |
| Issue alert on critical project never fires | Medium (operator confusion) | Document; or add minimal logging integration to critical client if needed. |
| SDK upgrade removes `scope.client` setter | Low (caught by `SDK_INTERNAL_API_STABLE`) | Pin sentry-sdk; CI runs `/validate/stress`. |
| Forked workers lose batcher state | Low (handled by SDK) | None; SDK already registers at-fork callbacks. |
