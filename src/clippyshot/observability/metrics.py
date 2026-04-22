"""Prometheus metrics."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram


CONVERSIONS_TOTAL = Counter(
    "clippyshot_conversions_total",
    "Number of conversions by outcome and format",
    ["outcome", "format"],
)
CONVERSION_DURATION = Histogram(
    "clippyshot_conversion_duration_seconds",
    "Conversion stage duration in seconds",
    ["stage"],
)
SANDBOX_BACKEND = Gauge(
    "clippyshot_sandbox_backend",
    "1 for the active sandbox backend, 0 for others",
    ["backend"],
)
JOBS_IN_FLIGHT = Gauge(
    "clippyshot_jobs_in_flight",
    "Number of conversions currently running",
)
INPUT_BYTES = Histogram(
    "clippyshot_input_bytes",
    "Size of accepted input documents",
    buckets=(1024, 8192, 65536, 524288, 4194304, 33554432, 134217728),
)
REJECTIONS_TOTAL = Counter(
    "clippyshot_rejections_total",
    "Inputs rejected before conversion, by reason",
    ["reason"],
)


def record_conversion(
    *,
    outcome: str,
    format_: str,
    duration_ms: int,
    stage_durations: dict[str, int],
) -> None:
    CONVERSIONS_TOTAL.labels(outcome=outcome, format=format_).inc()
    for stage, ms in stage_durations.items():
        CONVERSION_DURATION.labels(stage=stage).observe(ms / 1000.0)


def record_rejection(reason: str) -> None:
    REJECTIONS_TOTAL.labels(reason=reason).inc()


_KNOWN_BACKENDS = ("nsjail", "bwrap")


def set_sandbox_backend(name: str) -> None:
    for b in _KNOWN_BACKENDS:
        SANDBOX_BACKEND.labels(backend=b).set(1 if b == name else 0)
