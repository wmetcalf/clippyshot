import pytest

from clippyshot.limits import Limits


def test_limits_defaults_match_spec():
    limits = Limits()
    assert limits.timeout_s == 60
    assert limits.memory_bytes == 8 * 1024 * 1024 * 1024
    assert limits.tmpfs_bytes == 1024 * 1024 * 1024
    assert limits.max_input_bytes == 100 * 1024 * 1024
    assert limits.max_pages == 50
    assert limits.dpi == 150


def test_limits_override_individual_field():
    limits = Limits(timeout_s=120)
    assert limits.timeout_s == 120
    assert limits.dpi == 150  # other defaults preserved


def test_limits_from_env(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_TIMEOUT", "30")
    monkeypatch.setenv("CLIPPYSHOT_MAX_PAGES", "10")
    limits = Limits.from_env()
    assert limits.timeout_s == 30
    assert limits.max_pages == 10
    assert limits.dpi == 150


def test_dpi_too_high_rejected():
    with pytest.raises(ValueError, match="dpi"):
        Limits(dpi=10000)


def test_dpi_too_low_rejected():
    with pytest.raises(ValueError, match="dpi"):
        Limits(dpi=10)


def test_max_pages_zero_rejected():
    with pytest.raises(ValueError, match="max_pages"):
        Limits(max_pages=0)


def test_timeout_too_long_rejected():
    with pytest.raises(ValueError, match="timeout_s"):
        Limits(timeout_s=10000)


def test_default_limits_pass_validation():
    Limits()  # should not raise
