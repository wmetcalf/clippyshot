import pytest

from clippyshot.observability.logging import configure_logging, get_logger
from clippyshot.observability.metrics import (
    CONVERSIONS_TOTAL,
    CONVERSION_DURATION,
    INPUT_BYTES,
    JOBS_IN_FLIGHT,
    REJECTIONS_TOTAL,
    SANDBOX_BACKEND,
    record_conversion,
    record_rejection,
    set_sandbox_backend,
)


def test_get_logger_returns_a_bound_logger():
    configure_logging(format_="json", level="INFO")
    log = get_logger("test")
    assert hasattr(log, "info")
    assert hasattr(log, "bind")


def test_record_conversion_increments_counter():
    before = CONVERSIONS_TOTAL.labels(outcome="success", format="docx")._value.get()
    record_conversion(
        outcome="success",
        format_="docx",
        duration_ms=1234,
        stage_durations={"detect": 10, "soffice": 1000, "rasterize": 200, "hash": 24, "total": 1234},
    )
    after = CONVERSIONS_TOTAL.labels(outcome="success", format="docx")._value.get()
    assert after == before + 1


def test_record_conversion_observes_each_stage_histogram():
    """Each stage in stage_durations should produce a CONVERSION_DURATION observation."""
    before = CONVERSION_DURATION.labels(stage="detect")._sum.get()
    record_conversion(
        outcome="success",
        format_="docx",
        duration_ms=42,
        stage_durations={"detect": 42, "total": 42},
    )
    after = CONVERSION_DURATION.labels(stage="detect")._sum.get()
    assert after > before


def test_record_rejection_increments_counter():
    before = REJECTIONS_TOTAL.labels(reason="unsupported_type")._value.get()
    record_rejection("unsupported_type")
    after = REJECTIONS_TOTAL.labels(reason="unsupported_type")._value.get()
    assert after == before + 1


def test_set_sandbox_backend_gauge():
    set_sandbox_backend("bwrap")
    assert SANDBOX_BACKEND.labels(backend="bwrap")._value.get() == 1
    assert SANDBOX_BACKEND.labels(backend="nsjail")._value.get() == 0

    set_sandbox_backend("nsjail")
    assert SANDBOX_BACKEND.labels(backend="nsjail")._value.get() == 1
    assert SANDBOX_BACKEND.labels(backend="bwrap")._value.get() == 0


def test_jobs_in_flight_is_a_gauge():
    # Just confirm it's a Gauge with inc/dec.
    assert hasattr(JOBS_IN_FLIGHT, "inc")
    assert hasattr(JOBS_IN_FLIGHT, "dec")


def test_input_bytes_is_a_histogram():
    assert hasattr(INPUT_BYTES, "observe")


def test_configure_logging_text_format():
    """Text format should also work without raising."""
    configure_logging(format_="text", level="DEBUG")
    log = get_logger("test")
    log.info("smoke", key="value")  # should not raise
