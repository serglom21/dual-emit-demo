"""
The split-routing helper — the core of what we're validating.

emit_critical_metric / emit_critical_distribution / emit_critical_gauge each
route the metric *only* to the critical project, by forking the active scope
and swapping the client. The primary client never sees critical metrics; the
critical client never sees the routine, high-volume metrics emitted via plain
sentry_sdk.metrics.* calls.

The forked scope natively carries the active span's trace_id, request tags,
and user context — everything the FastAPI integration attached to the current
scope — so the critical metric stays fully trace-correlated.
"""
import sentry_sdk

_critical_client = None


def configure(client):
    global _critical_client
    _critical_client = client


def _emit(metric_fn, name, value, attributes):
    """
    Shared routing primitive: fork the active scope, swap the client to the
    critical client, then call the metric function. scope.client is set via
    direct attribute assignment (not set_client()) to avoid a side effect that
    writes the client to the global scope.
    """
    if _critical_client is None:
        return
    with sentry_sdk.new_scope() as scope:
        scope.client = _critical_client
        metric_fn(name, value, attributes=attributes)


def emit_critical_metric(name, value=1, attributes=None):
    """Counter routed only to the critical project."""
    _emit(sentry_sdk.metrics.count, name, value, attributes)


def emit_critical_distribution(name, value, attributes=None):
    """Distribution routed only to the critical project."""
    _emit(sentry_sdk.metrics.distribution, name, value, attributes)


def emit_critical_gauge(name, value, attributes=None):
    """Gauge routed only to the critical project."""
    _emit(sentry_sdk.metrics.gauge, name, value, attributes)
