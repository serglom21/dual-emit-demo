"""
Sentry transport and client setup for the dual-emit pattern.

CaptureTransport stores envelopes in memory so the validation endpoint can
inspect exactly what would be sent, AND (optionally) forwards them to a real
HttpTransport so the same envelopes reach a live Sentry project.

DSNs are read from environment variables:
  SENTRY_PRIMARY_DSN   — your high-volume project DSN
  SENTRY_CRITICAL_DSN  — your low-volume "critical metrics" project DSN

If either env var is unset, that side falls back to a syntactically valid
*fake* DSN and only the in-memory CaptureTransport runs — local validation
still works (5/5 PASSED), but nothing is shipped to Sentry. This keeps the
repo safe to publish: no real DSNs are committed.
"""
import os
from threading import Lock

import sentry_sdk
from sentry_sdk import Client
from sentry_sdk.consts import DEFAULT_OPTIONS
from sentry_sdk.transport import Transport, make_transport


# Fake DSNs — syntactically valid, never resolve. Used when the env vars are
# unset so the SDK can still initialize for local validation runs.
_FAKE_PRIMARY_DSN = (
    "https://primarykey0000000000000000000000@o1.ingest.sentry.io/1111"
)
_FAKE_CRITICAL_DSN = (
    "https://criticalkey000000000000000000000@o1.ingest.sentry.io/2222"
)

PRIMARY_DSN = os.environ.get("SENTRY_PRIMARY_DSN") or _FAKE_PRIMARY_DSN
CRITICAL_DSN = os.environ.get("SENTRY_CRITICAL_DSN") or _FAKE_CRITICAL_DSN

# Only wire the real HttpTransport when a real DSN was provided. With fake
# DSNs we still want CaptureTransport to work, but we must not try to ship
# envelopes to a host that doesn't exist.
_PRIMARY_IS_REAL = "SENTRY_PRIMARY_DSN" in os.environ
_CRITICAL_IS_REAL = "SENTRY_CRITICAL_DSN" in os.environ


class CaptureTransport(Transport):
    """
    Captures envelopes in memory for inspection. If `inner` is set, also
    forwards each envelope to that real transport so it ships to Sentry.
    Thread-safe — FastAPI runs requests concurrently.
    """

    def __init__(self, options=None, inner=None):
        super().__init__(options)
        self._envelopes = []
        self._lock = Lock()
        self._inner = inner

    def set_inner(self, inner):
        self._inner = inner

    def capture_envelope(self, envelope):
        with self._lock:
            self._envelopes.append(envelope)
        if self._inner is not None:
            self._inner.capture_envelope(envelope)

    def get_captured_envelopes(self):
        with self._lock:
            return list(self._envelopes)

    def clear(self):
        with self._lock:
            self._envelopes.clear()

    def flush(self, timeout, callback=None):
        if self._inner is not None:
            self._inner.flush(timeout, callback)

    def kill(self):
        if self._inner is not None:
            self._inner.kill()


# Module-level transport instances. Pass these *as instances* (not via a
# lambda factory) — passing a callable routes through the SDK's deprecated
# _FunctionTransport which drops trace_metric envelopes.
primary_transport = CaptureTransport()
critical_transport = CaptureTransport()

_critical_client = None


def _build_real_transport(dsn):
    """Build a real HttpTransport for the given DSN using SDK defaults."""
    opts = dict(DEFAULT_OPTIONS)
    opts["dsn"] = dsn
    return make_transport(opts)


def init_sentry():
    """
    Initialize the primary client (full integrations, high volume) and create
    a bare critical client (no integrations, low volume, never downsampled).

    Each CaptureTransport wraps a real HttpTransport so envelopes are both
    captured locally (for validation) and shipped to the real Sentry project.
    """
    global _critical_client

    if _PRIMARY_IS_REAL:
        primary_transport.set_inner(_build_real_transport(PRIMARY_DSN))
    if _CRITICAL_IS_REAL:
        critical_transport.set_inner(_build_real_transport(CRITICAL_DSN))

    sentry_sdk.init(
        dsn=PRIMARY_DSN,
        traces_sample_rate=1.0,
        transport=primary_transport,
        enable_tracing=True,
    )

    critical_client = Client(
        dsn=CRITICAL_DSN,
        integrations=[],
        default_integrations=False,
        transport=critical_transport,
    )

    _critical_client = critical_client
    return critical_client


def get_critical_client():
    return _critical_client
