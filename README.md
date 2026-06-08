# Split-Routed Critical Metrics — Sentry Reference Implementation

A small FastAPI service that demonstrates and validates a **split-routing**
metrics pattern for Sentry: critical metrics live exclusively in a low-volume
"critical" project; routine, high-volume metrics live exclusively in the
primary project. One call site emits both — different projects receive them.

## The problem

A single high-volume project that sends a large amount of trace metric items
to Sentry triggers aggressive downsampling. Low-volume but business-critical
metrics (billing, payments, enterprise webhooks) become statistically
unreliable because they get downsampled alongside everything else.

## The solution

Keep a single `sentry_sdk.init()` for the primary project, and add a **second,
bare `Client()`** pointed at a separate low-volume project. Then route each
metric to exactly one project:

- **Primary client** → high-volume project — all errors, traces, spans, logs,
  and the routine metric firehose. May be downsampled.
- **Critical client** → low-volume project — receives *only* the metrics
  routed through `emit_critical_metric()`. Never downsampled.

Critical metrics never touch the primary project, and routine metrics never
touch the critical project. The split happens at the SDK call site, not in
Relay or via routing rules.

The routing happens in `app/metrics_helper.py`:

```python
def emit_critical_metric(name, value=1, attributes=None):
    if _critical_client is None:
        return
    with sentry_sdk.new_scope() as scope:
        scope.client = _critical_client
        sentry_sdk.metrics.count(name, value, attributes=attributes)
```

`new_scope()` forks the *current* scope, so the metric the critical client
sends still natively carries the active span's `trace_id`, request tags, and
user context. Assigning `scope.client` directly (instead of
`scope.set_client()`) avoids a side effect that writes the client to the
global scope — so the routine `sentry_sdk.metrics.count()` call right
afterwards still goes to the primary client as intended.

Because trace_id is preserved on the critical-side metric, you can pivot
from a critical metric back to the full trace in the primary project.

## Configuration

DSNs are read from environment variables. **Never commit real DSNs to the
repo.** Copy `.env.example` to `.env` and fill in your project DSNs:

```bash
cp .env.example .env
# edit .env and set SENTRY_PRIMARY_DSN / SENTRY_CRITICAL_DSN
```

The provided `run.py` loads `.env` automatically if `python-dotenv` is
available. If either DSN is unset, that side falls back to a syntactically
valid fake DSN and only the in-memory `CaptureTransport` runs — local
validation still passes, but nothing is shipped to Sentry.

## How to run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

The server listens on `http://localhost:8000`.

## How to validate

One HTTP call validates everything — no manual curl sequence, no separate
test runner:

```bash
curl -X POST http://localhost:8000/validate/trigger
```

`/validate/trigger` resets the in-memory transports, drives the critical and
high-volume endpoints over an in-process ASGI transport, flushes both metric
batchers, then runs the assertion suite and returns JSON.

A successful run returns:

```json
{ "summary": "6/6 PASSED", "checks": [ ... ] }
```

### The six checks

| Check | What it proves |
|-------|----------------|
| `SPLIT_ROUTING` | Primary received only non-critical metrics; critical received only critical metrics. No duplication. |
| `PRIMARY_ISOLATION` | No critical metric (`payment_webhook_failure`, etc.) ever reached the primary project. |
| `CRITICAL_ISOLATION` | No non-critical metric (`api_request_total`, etc.) ever reached the critical project. |
| `TRACE_ID_PRESERVATION` | The trace_id on each critical envelope equals the active transaction's trace_id at emit time — so you can still join back to the trace in the primary project. |
| `ATTRIBUTES_PRESERVED` | The explicit attributes passed to `emit_critical_metric()` survive intact. |
| `SCOPE_ISOLATION` | After every emit, `sentry_sdk.get_client()` is still the primary client — the critical client never leaked onto the global scope. |

Other endpoints:

- `GET /validate` — run the assertions against whatever is currently captured (skips the trace_id check, which needs the trigger's bookkeeping).
- `POST /validate/reset` — clear both transports for a fresh run.
- `POST /validate/stress` — exercise the edges (error attribution, PII, gauges, concurrency, broken DSN, SDK contract). See `STRESS_TEST.md` for the full breakdown of what it covers and what requires manual UI verification.

## How it captures metrics for inspection

`app/sentry_config.py` defines a thread-safe `CaptureTransport` that stores
envelopes in memory for the validation endpoint to inspect. When real DSNs
are configured via env vars, `CaptureTransport` *also* forwards each envelope
to a real `HttpTransport`, so envelopes simultaneously capture locally and
ship to Sentry.

## Adapting this for your real service

1. Set `SENTRY_PRIMARY_DSN` and `SENTRY_CRITICAL_DSN` in your environment.
2. Import `emit_critical_metric` from `app.metrics_helper` and call it from
   your real billing/payment/enterprise paths instead of plain
   `sentry_sdk.metrics.count()` for those specific metrics.
3. In production you can drop `CaptureTransport` entirely and let the SDK use
   its default `HttpTransport` — `CaptureTransport` exists for the demo's
   validation endpoint and isn't load-bearing for the routing pattern.
4. The `metrics_helper.emit_critical_metric()` body is the part that carries
   over verbatim.

## Files

```
.
├── app/
│   ├── main.py              FastAPI app + Sentry init in lifespan
│   ├── sentry_config.py     CaptureTransport + client setup, env-driven DSNs
│   ├── metrics_helper.py    emit_critical_metric() — routes to critical client only
│   ├── models.py            Pydantic request/response models
│   └── routes/
│       ├── high_volume.py   POST /api/email/signin, /api/llm/extract, GET /api/warmup
│       ├── critical.py      POST /api/v1/payment/{webhook,subscription}, /api/v1/enterprise/sso/connect
│       └── validation.py    GET /validate, POST /validate/{reset,trigger}
├── run.py                   uvicorn entry point (loads .env if present)
├── requirements.txt
├── .env.example             template for SENTRY_PRIMARY_DSN / SENTRY_CRITICAL_DSN
└── .gitignore
```
