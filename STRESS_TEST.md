# Stress Test — Edges & Assumptions of the Split-Routing Pattern

The basic split-routing assertion suite (`/validate/trigger` → 6/6 PASSED)
proves the **happy path**: critical metrics land in the critical project,
routine metrics land in the primary project, scope context is preserved.

This document covers the **edges**. Each category below has one of three
statuses, called out honestly:

- **TESTED** — automated check in `/validate/stress` *and* verified live in
  Sentry.
- **DOCUMENTED** — manual verification needed; reproduction steps included.
- **INFERRED** — derived from SDK source; not measured end-to-end. The
  inference is sound but is not the same as a live experiment.

---

## Automated checks — `POST /validate/stress` (9/9 PASSED)

| # | Check | What it proves |
|---|---|---|
| 1a | `ERROR_AFTER_EMIT_ROUTES_TO_PRIMARY` | An unhandled exception raised *after* `emit_critical_metric` (i.e. outside the helper's `new_scope()` block) is captured by the FastAPI integration on the **primary** client. It does NOT leak to the critical project. **TESTED**. |
| 1b | `CAPTURE_IN_SCOPE_GOES_TO_REDIRECTED_CLIENT` | Calling `sentry_sdk.capture_exception()` *inside* a `new_scope()` with `scope.client = critical_client` sends the error to the **critical** project. This is the scope-redirect contract working as intended — not a bug, just the literal consequence of swapping the client. The `emit_critical_metric` helper does not call `capture_exception`, so following the helper does not surface this. **TESTED**. |
| 3 | `RELEASE_AND_ENVIRONMENT_PROPAGATED` | The bare critical `Client()` is initialized with the same `release` / `environment` as primary, so critical-side metrics carry the same deployment context. **TESTED**. |
| 5 | `PII_REACHES_CRITICAL_UNSCRUBBED` | The critical client has no PII scrubbing integration — whatever attributes you pass arrive raw. Do not send PII via `emit_critical_metric` unless you've added scrubbing on the critical side. **TESTED**. |
| 8 | `PRIMARY_UNAFFECTED_BY_BROKEN_CRITICAL` | When the critical transport's inner real-`HttpTransport` is unavailable, primary metrics keep flowing. The two clients have independent HttpTransport workers. **TESTED** (and verified in Sentry: `stress_orphan_metric` did not arrive in the critical project; `stress_primary_still_works` did arrive in primary). |
| 9 | `CONCURRENT_NO_LOSS` | Firing 50 concurrent `emit_critical_metric` calls via `asyncio.gather` lands all 50 metrics on the critical side with unique-per-emit attributes, no attribution scrambling. **TESTED** (verified live: `sum(stress_concurrent_metric) = 50`). |
| 12a | `GAUGE_ROUTES_TO_CRITICAL` | `emit_critical_gauge` correctly routes gauge metrics to the critical project. **TESTED**. |
| 12b | `DISTRIBUTION_ROUTES_TO_CRITICAL` | `emit_critical_distribution` correctly routes distribution metrics. **TESTED**. |
| 11 | `SDK_INTERNAL_API_STABLE` | The pattern relies on `scope.client` being directly assignable. Asserted at runtime so an SDK upgrade that removes this contract is caught before deploy. **TESTED** at sentry-sdk 2.61.1. |

Run it:

```bash
curl -X POST http://localhost:8000/validate/stress
```

---

## Manual / operational checks

### #2 — Cross-project trace correlation — **DOCUMENTED**

A critical metric carries the same `trace_id` as the request's primary
transaction (proven in `TRACE_ID_PRESERVATION`). Sentry's UI does not
auto-pivot between projects on click, so the debugger must copy the
`trace_id` from a critical metric and search for it in the primary
project's Trace Explorer.

To verify yourself:

1. Trigger a run: `curl -X POST http://localhost:8000/validate/trigger`.
2. In Sentry, open the critical project's metrics view, click on a
   `payment_webhook_failure` sample, copy the `trace_id`.
3. In the primary project, open Trace Explorer and paste that `trace_id` —
   you'll land on the full transaction with all spans.

Live verification done in the `snout-and-about` org: trace_ids match
across projects.

### #4 — Sampling interaction — **INFERRED, not measured**

> Honest note: this section is derived from reading the SDK source, not
> from an end-to-end experiment. The reasoning is sound but the live
> behavior at `traces_sample_rate < 1.0` has not been demonstrated by the
> stress suite.

**What the SDK source says:** `sentry_sdk.metrics.count()` (and gauge /
distribution) calls `Scope._capture_metric`, which is gated only by
`has_metrics_enabled(client.options)` — there is no `traces_sample_rate`
check on the metric path. Transactions, by contrast, are gated by
`traces_sample_rate` and unsampled transactions never emit envelopes.

**What that implies:** at `traces_sample_rate < 1.0`, critical metrics
still emit and still carry a `trace_id` (read from the active scope), but
the transaction the `trace_id` points to may not have been shipped to
Sentry. The metric is there; the trace link may be a dead end.

**To verify yourself (run this if you care):**

```bash
SENTRY_TRACES_SAMPLE_RATE=0.0 \
SENTRY_PRIMARY_DSN=... SENTRY_CRITICAL_DSN=... \
python run.py

# in another terminal:
curl -X POST http://localhost:8000/validate/trigger
```

Then in Sentry:
1. Find the new `payment_webhook_failure` in the critical project and
   note its `trace_id`.
2. Search the primary project's Trace Explorer for that `trace_id`.
3. With `traces_sample_rate=0.0` the trace will not be found.

In production, keep `traces_sample_rate` high enough that critical-metric
trace_ids usually resolve — or accept that the cross-project link is
statistical.

### #6 — Alerting — **DOCUMENTED, partially measured**

> Honest note: I confirmed by reading the configuration that the bare
> critical client has no error-capturing integrations (`integrations=[]`,
> `default_integrations=False`). I did *not* actually create a Sentry
> issue alert on the critical project and watch it fail to fire. The
> claim "no application errors will arrive at critical" is grounded in
> the SDK configuration; the claim "an alert on critical will sit dead"
> follows from that but was not exercised against the Sentry alerting
> system.

**Metric alerts on the critical project — works (the win):** create a
metric alert in `critical-metrics-backend` on
`sum(payment_webhook_failure) > N over 5m`. Because the project is
low-volume and never downsampled, the alert is statistically reliable —
which is the reason for splitting in the first place.

**Issue alerts on the critical project — won't fire on application
errors:** the critical client has no integration that captures
application exceptions, so no error events will arrive there from
ordinary route handlers / framework code. An operator who sets up
"alert me when an issue appears in critical-metrics-backend" will be
waiting for an event that never arrives.

The only way an error event reaches the critical project is if user
code deliberately routes one (e.g. `capture_exception()` inside a
`new_scope()` block where `scope.client = critical_client`). The
`emit_critical_metric` helper as shipped does not do that.

**To verify yourself:**

1. Create an issue alert in `critical-metrics-backend`: "When a new
   issue is created, send an email."
2. Run `/validate/trigger` and `/validate/stress` several times.
3. Observe inbox.
4. For contrast, create a metric alert on
   `sum(payment_webhook_failure)`. Trigger once. Observe inbox.

### #7 — Dashboards and visualization — **DOCUMENTED**

A "Billing Health" dashboard needs widgets pointed at *both* projects:
metric widgets pointed at the critical project (`payment_webhook_failure`,
`sso_connect_failure`) and request / latency widgets pointed at the
primary project (`api_request_total`, `llm_latency`). Sentry supports
multi-project dashboards; the dashboard author just needs to scope each
panel correctly.

### #10 — Process lifecycle / fork (gunicorn workers) — **INFERRED**

> Honest note: I read the SDK source (`_batcher.py` registers
> `os.register_at_fork(after_in_child=…)`). I did not actually run the
> stress suite under gunicorn with multiple workers in this session.

To verify yourself:

```bash
SENTRY_PRIMARY_DSN=... SENTRY_CRITICAL_DSN=... \
  .venv/bin/gunicorn -k uvicorn.workers.UvicornWorker -w 4 app.main:app
```

Then hit `/validate/trigger` repeatedly and verify in Sentry that each
worker's metrics arrived.

---

## Quick reference

| Edge | Severity | What to do |
|---|---|---|
| `emit_critical_metric` does what it says (routes one metric) | n/a | Follow the helper. Don't extend it with `capture_exception()`-style side effects unless you want errors routed to critical. |
| PII unscrubbed on critical side | High (compliance) | Don't pass PII as attributes, or add a `before_send_metric` hook on the critical client. |
| Critical metric `trace_id` may not resolve in primary at low sample rate | Medium (debugging UX) | Keep `traces_sample_rate` high enough that the trace_ids usually resolve, or accept statistical correlation. **Not measured end-to-end — run the experiment in #4 if you want concrete numbers.** |
| Issue alerts on critical sit dead (no integration ships errors there) | Medium (operator confusion) | Document for operators; use metric alerts on critical, issue alerts on primary. |
| SDK upgrade removes `scope.client` setter | Low (caught by `SDK_INTERNAL_API_STABLE`) | Pin sentry-sdk; CI runs `/validate/stress`. |
| Forked workers lose batcher state | Low (handled by SDK) | None expected — but **not measured under gunicorn in this session.** |
