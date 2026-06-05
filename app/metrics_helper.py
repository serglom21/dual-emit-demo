"""
The split-routing helper — the core of what we're validating.

emit_critical_metric() routes the metric *only* to the critical project, by
forking the active scope and swapping the client. The primary client never
sees critical metrics; the critical client never sees the routine,
high-volume metrics emitted via plain sentry_sdk.metrics.count().

The forked scope natively carries the active span's trace_id, request tags,
and user context — everything the FastAPI integration attached to the current
scope — so the critical metric stays fully trace-correlated and can be
joined back to its primary-side trace via trace_id.
"""
import sentry_sdk

_critical_client = None


def configure(client):
    global _critical_client
    _critical_client = client


def emit_critical_metric(name, value=1, attributes=None):
    """
    Route a metric to the critical project only.

    scope.client is set via direct attribute assignment (not set_client())
    to avoid a side effect that writes to the global scope.
    """
    if _critical_client is None:
        return
    with sentry_sdk.new_scope() as scope:
        scope.client = _critical_client
        sentry_sdk.metrics.count(name, value, attributes=attributes)
