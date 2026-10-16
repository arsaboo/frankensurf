"""OpenTelemetry spans for reads, when opentelemetry-api is installed (pip install "frankensurf[otel]").

Each read is a span, ``frankensurf.read``, with one child span per tool tried (``frankensurf.attempt``),
so a read that climbed through five tools shows as five children in Jaeger, Honeycomb, Datadog,
Grafana Tempo or Langfuse. Frankensurf only uses the API: configure a TracerProvider and exporter in
your process (or with opentelemetry-instrument) as usual. Without the package, or without a configured
provider, nothing is recorded and nothing changes.

Attributes: server.address and url.path (never the query string; only the host for signed-in reads),
frankensurf.status, .method, .failure.code, .failure.message, .cost_usd, .trace_id, .attempts; and on
each attempt span frankensurf.provider, .failure.code, .failure.message, .failure.stage, .latency_ms.
Attempt spans are laid end to end from the receipt's latencies; tools raced at once (lightning,
hedges) overlap in reality.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

try:  # optional dependency
    from opentelemetry import trace as _trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover - exercised by the no-otel path
    _trace = None

_TRACER = None


def _tracer():
    global _TRACER
    if _trace is None:
        return None
    if _TRACER is None:
        try:
            from importlib.metadata import version
            release = version("frankensurf")
        except Exception:
            release = "dev"
        _TRACER = _trace.get_tracer("frankensurf", release)
    return _TRACER


def _where(url: str, private: bool) -> dict:
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return {}
    where = {"server.address": parsed.hostname or ""}
    if not private:
        where["url.path"] = parsed.path or "/"
    return where


def _set(span, attributes: dict) -> None:
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


async def traced(operation: str, url: str, work, private: bool = False):
    """Await work (a coroutine returning a result dict with a receipt) inside a span."""
    tracer = _tracer()
    if tracer is None:
        return await work
    with tracer.start_as_current_span(f"frankensurf.{operation}", record_exception=True,
                                      set_status_on_exception=True) as span:
        started = time.time_ns()
        _set(span, _where(url, private))
        result = await work
        receipt = (result or {}).get("receipt") or {} if isinstance(result, dict) else {}
        failure = receipt.get("failure") or {}
        attempts = [a for a in receipt.get("attempts") or () if isinstance(a, dict)]
        _set(span, {"frankensurf.status": receipt.get("status"), "frankensurf.method": receipt.get("method"),
                    "frankensurf.failure.code": failure.get("code"),
                    "frankensurf.failure.message": failure.get("message"),
                    "frankensurf.cost_usd": receipt.get("cost_usd"),
                    "frankensurf.trace_id": receipt.get("trace_id"),
                    "frankensurf.attempts": len(attempts)})
        if receipt.get("status") not in (None, "observed"):
            span.set_status(Status(StatusCode.ERROR, failure.get("code") or "failed"))
        cursor = started
        for attempt in attempts:
            took = int(attempt.get("latency_ms") or 0) * 1_000_000
            child = tracer.start_span("frankensurf.attempt", start_time=cursor)
            _set(child, {"frankensurf.provider": attempt.get("provider"),
                         "frankensurf.status": attempt.get("status"),
                         "frankensurf.failure.code": attempt.get("failure"),
                         "frankensurf.failure.message": attempt.get("message"),
                         "frankensurf.failure.stage": attempt.get("failure_stage"),
                         "frankensurf.latency_ms": attempt.get("latency_ms")})
            if attempt.get("status") == "failed":
                child.set_status(Status(StatusCode.ERROR, attempt.get("failure") or "failed"))
            child.end(end_time=cursor + took)
            cursor += took
        return result
