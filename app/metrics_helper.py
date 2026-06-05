"""
The dual-emit helper — the core of what we're validating.

emit_critical_metric() sends a metric twice:
  1. To the primary project via the normal path (full scope).
  2. To the critical project via a forked scope with a swapped client.

The forked scope natively carries the active span's trace_id, request tags,
and user context — everything the FastAPI integration attached to the current
scope — so the critical copy stays fully trace-correlated.
"""
import sentry_sdk

_critical_client = None


def configure(client):
    global _critical_client
    _critical_client = client


def emit_critical_metric(name, value=1, attributes=None):
    """
    Dual-emit: primary project (normal path) + critical project
    (forked scope, low volume, full accuracy).

    scope.client is set via direct attribute assignment (not set_client())
    to avoid a side effect that writes to the global scope.
    """
    # 1. Primary — normal path, full scope
    sentry_sdk.metrics.count(name, value, attributes=attributes)

    # 2. Critical — forked scope, swapped client
    if _critical_client is not None:
        with sentry_sdk.new_scope() as scope:
            scope.client = _critical_client
            sentry_sdk.metrics.count(name, value, attributes=attributes)
