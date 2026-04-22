import pytest

from clippyshot.errors import SandboxUnavailable
from clippyshot.sandbox.detect import select_sandbox


class _FakeSandbox:
    def __init__(
        self,
        name: str,
        exit_code: int = 0,
        *,
        secure: bool = True,
        insecurity_reasons: list[str] | None = None,
    ):
        self.name = name
        self._exit_code = exit_code
        self.secure = secure
        self.insecurity_reasons = insecurity_reasons or []

    def run(self, request):
        from clippyshot.types import SandboxResult
        return SandboxResult(
            exit_code=self._exit_code, stdout=b"", stderr=b"",
            duration_ms=1, killed=False,
        )

    def smoketest(self):
        return self.run(None)


def _bad():
    raise SandboxUnavailable("no")


def test_explicit_env_forces_backend(monkeypatch):
    called = []

    def fake_nsjail():
        called.append("nsjail")
        return _FakeSandbox("nsjail")

    def fake_bwrap():
        called.append("bwrap")
        return _FakeSandbox("bwrap")

    def fake_container():
        called.append("container")
        return _FakeSandbox("container")

    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "bwrap")
    sb = select_sandbox(
        _nsjail_factory=fake_nsjail,
        _bwrap_factory=fake_bwrap,
        _container_factory=fake_container,
    )
    assert sb.name == "bwrap"
    assert called == ["bwrap"]


def test_explicit_env_forces_nsjail(monkeypatch):
    called = []

    def fake_nsjail():
        called.append("nsjail")
        return _FakeSandbox("nsjail")

    def fake_bwrap():
        called.append("bwrap")
        return _FakeSandbox("bwrap")

    def fake_container():
        called.append("container")
        return _FakeSandbox("container")

    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "nsjail")
    sb = select_sandbox(
        _nsjail_factory=fake_nsjail,
        _bwrap_factory=fake_bwrap,
        _container_factory=fake_container,
    )
    assert sb.name == "nsjail"
    assert called == ["nsjail"]


def test_falls_back_to_bwrap_when_nsjail_unavailable(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)

    def bad_nsjail():
        raise SandboxUnavailable("nope")

    def good_bwrap():
        return _FakeSandbox("bwrap")

    sb = select_sandbox(
        _nsjail_factory=bad_nsjail,
        _bwrap_factory=good_bwrap,
        _container_factory=_bad,
    )
    assert sb.name == "bwrap"


def test_prefers_nsjail_when_both_available(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)

    def good_nsjail():
        return _FakeSandbox("nsjail")

    def good_bwrap():
        return _FakeSandbox("bwrap")

    sb = select_sandbox(
        _nsjail_factory=good_nsjail,
        _bwrap_factory=good_bwrap,
        _container_factory=_bad,
    )
    assert sb.name == "nsjail"


def test_raises_when_no_backend_available(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)

    with pytest.raises(SandboxUnavailable):
        select_sandbox(
            _nsjail_factory=_bad,
            _bwrap_factory=_bad,
            _container_factory=_bad,
        )


def test_raises_when_forced_backend_unavailable(monkeypatch):
    """If CLIPPYSHOT_SANDBOX forces a backend that's not available, do not silently
    fall back to the other — fail loudly."""
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "nsjail")

    def bad_nsjail():
        raise SandboxUnavailable("nsjail not installed")

    def good_bwrap():
        return _FakeSandbox("bwrap")

    with pytest.raises(SandboxUnavailable):
        select_sandbox(
            _nsjail_factory=bad_nsjail,
            _bwrap_factory=good_bwrap,
            _container_factory=_bad,
        )


def test_container_fallback_when_both_fail(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)

    def bad_nsjail():
        raise SandboxUnavailable("no")

    def bad_bwrap():
        raise SandboxUnavailable("no")

    def good_container():
        return _FakeSandbox("container")

    sb = select_sandbox(
        _nsjail_factory=bad_nsjail,
        _bwrap_factory=bad_bwrap,
        _container_factory=good_container,
    )
    assert sb.name == "container"


def test_smoketest_failure_falls_through(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)

    def broken_nsjail():
        return _FakeSandbox("nsjail", exit_code=1)  # smoketest will return 1

    def good_bwrap():
        return _FakeSandbox("bwrap")

    def good_container():
        return _FakeSandbox("container")

    sb = select_sandbox(
        _nsjail_factory=broken_nsjail,
        _bwrap_factory=good_bwrap,
        _container_factory=good_container,
    )
    assert sb.name == "bwrap"  # nsjail's broken smoketest was skipped


def test_forced_backend_fails_loudly_on_bad_smoketest(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "nsjail")

    def broken_nsjail():
        return _FakeSandbox("nsjail", exit_code=1)

    def good_bwrap():
        return _FakeSandbox("bwrap")

    def good_container():
        return _FakeSandbox("container")

    with pytest.raises(SandboxUnavailable, match="smoketest"):
        select_sandbox(
            _nsjail_factory=broken_nsjail,
            _bwrap_factory=good_bwrap,
            _container_factory=good_container,
        )


def test_forced_invalid_backend_name_raises(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "bogus")

    with pytest.raises(SandboxUnavailable, match="not a valid backend"):
        select_sandbox(
            _nsjail_factory=_bad,
            _bwrap_factory=_bad,
            _container_factory=_bad,
        )


def test_explicit_env_forces_container(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")

    def good_container():
        return _FakeSandbox("container")

    sb = select_sandbox(
        _nsjail_factory=_bad,
        _bwrap_factory=_bad,
        _container_factory=good_container,
    )
    assert sb.name == "container"


def test_insecure_backend_rejected_by_default(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)
    monkeypatch.delenv("CLIPPYSHOT_WARN_ON_INSECURE", raising=False)

    def insecure_bwrap():
        return _FakeSandbox(
            "bwrap",
            secure=False,
            insecurity_reasons=["seccomp_missing"],
        )

    with pytest.raises(SandboxUnavailable, match="insecure"):
        select_sandbox(
            _nsjail_factory=_bad,
            _bwrap_factory=insecure_bwrap,
            _container_factory=_bad,
        )


def test_insecure_backend_allowed_when_warn_flag_set(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)
    monkeypatch.setenv("CLIPPYSHOT_WARN_ON_INSECURE", "1")

    def insecure_bwrap():
        return _FakeSandbox(
            "bwrap",
            secure=False,
            insecurity_reasons=["seccomp_missing"],
        )

    sb = select_sandbox(
        _nsjail_factory=_bad,
        _bwrap_factory=insecure_bwrap,
        _container_factory=_bad,
    )
    assert sb.name == "bwrap"
    assert sb.secure is False
    assert sb.insecurity_reasons == ["seccomp_missing"]
