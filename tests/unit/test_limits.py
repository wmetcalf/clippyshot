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


def test_skip_blanks_bool_spellings(monkeypatch):
    # default-ON: canonical AND noncanonical falsy spellings (incl. whitespace/"off") disable it.
    for val in ("0", "false", "no", "Off", " NO ", "false "):
        monkeypatch.setenv("CLIPPYSHOT_SKIP_BLANKS", val)
        assert Limits.from_env().skip_blanks is False, val
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("CLIPPYSHOT_SKIP_BLANKS", val)
        assert Limits.from_env().skip_blanks is True, val
    monkeypatch.delenv("CLIPPYSHOT_SKIP_BLANKS")
    assert Limits.from_env().skip_blanks is True   # default on


def test_disclose_security_internals_fail_closed(monkeypatch):
    # a SECURITY flag (True = more disclosure) must FAIL CLOSED: only an explicit truthy token enables it,
    # so a typo / whitespace / "off" can't silently flip on security-internals disclosure.
    for val in ("1", "true", "YES", " on "):
        monkeypatch.setenv("CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS", val)
        assert Limits.from_env().disclose_security_internals is True, val
    for val in ("0", "false", "off", "garbage", "No"):
        monkeypatch.setenv("CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS", val)
        assert Limits.from_env().disclose_security_internals is False, val
    monkeypatch.delenv("CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS")
    assert Limits.from_env().disclose_security_internals is False   # default off


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
