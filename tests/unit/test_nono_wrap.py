"""Unit tests for the optional inner nono (Landlock) wrapper.

These exercise the deterministic argv transform + the decorator wiring with a
stand-in binary (``/bin/true``) so they run in CI without nono installed. The
real-conversion enforcement test lives in tests/integration.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from clippyshot.sandbox.base import Mount, SandboxRequest
from clippyshot.sandbox.detect import select_sandbox
from clippyshot.sandbox.nono_wrap import NonoWrap, NonoWrappedSandbox
from clippyshot.types import SandboxResult

_BIN = "/bin/true"  # exists -> resolve_bin returns it; argv shape is what we assert


class _FakeSandbox:
    """Records the request it was handed; satisfies the Sandbox protocol."""

    name = "fake"
    secure = True
    insecurity_reasons: list[str] = []

    def __init__(self) -> None:
        self.last: SandboxRequest | None = None

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.last = request
        return SandboxResult(0, b"", b"", 1, False)

    def smoketest(self) -> SandboxResult:
        return SandboxResult(0, b"", b"", 1, False)


def _req(**kw) -> SandboxRequest:
    kw.setdefault("argv", ["/usr/bin/soffice", "--convert-to", "pdf", "x.docx"])
    return SandboxRequest(**kw)


def test_build_argv_wraps_with_nono_and_clean_env() -> None:
    argv = NonoWrap(bin=_BIN).build_argv(_req())
    assert argv[:3] == [_BIN, "wrap", "--silent"]
    assert "--block-net" in argv
    # the child execs through `env -i` and the original argv is the tail
    sep = argv.index("--")
    assert argv[sep + 1 : sep + 3] == ["/usr/bin/env", "-i"]
    assert argv[-4:] == ["/usr/bin/soffice", "--convert-to", "pdf", "x.docx"]
    # system dirs granted read-only by default
    assert "-r" in argv and "/usr" in argv


def test_mount_grants_use_sandbox_path(tmp_path: Path) -> None:
    d = tmp_path / "indir"
    d.mkdir()
    f = tmp_path / "in.docx"
    f.write_bytes(b"x")
    req = _req(
        ro_mounts=[Mount(f, Path("/sb/in.docx")), Mount(d, Path("/sb/dir"))],
        rw_mounts=[Mount(tmp_path / "out", Path("/sb/out"))],
    )
    argv = NonoWrap(bin=_BIN).build_argv(req)
    # a real file -> --read-file <sandbox_path>; a dir -> -r <sandbox_path>
    assert "--read-file" in argv and "/sb/in.docx" in argv
    assert argv[argv.index("/sb/dir") - 1] == "-r"
    assert argv[argv.index("/sb/out") - 1] == "-a"


def test_profile_mode_uses_dash_p_and_skips_dir_baseline() -> None:
    argv = NonoWrap(bin=_BIN, profile=Path("/etc/clippyshot/soffice.nono.json")).build_argv(_req())
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "/etc/clippyshot/soffice.nono.json"
    # with an explicit profile we don't also splatter the system-dir baseline
    assert "/usr" not in argv


def test_block_net_toggle() -> None:
    assert "--block-net" not in NonoWrap(bin=_BIN, block_net=False).build_argv(_req())


def test_apply_relocates_state_and_adds_mounts(tmp_path: Path) -> None:
    state = tmp_path / "nono-state"
    wrap = NonoWrap(bin=_BIN, state_dir=state)
    out = wrap.apply(_req(env={"FOO": "bar"}))
    # nono PROCESS gets HOME=state + quiet env; child env stays clean (env -i in argv)
    assert out.env["HOME"] == str(state)
    assert out.env["NONO_NO_SAVE_PROMPT"] == "1"
    assert out.env["FOO"] == "bar"
    # state dir created off the grants; bin dir + state added as identity mounts
    assert state.is_dir()
    rw = {str(m.host_path) for m in out.rw_mounts}
    ro = {str(m.host_path) for m in out.ro_mounts}
    assert str(state) in rw
    assert str(Path(_BIN).parent) in ro


def test_missing_binary_raises_only_at_build() -> None:
    from clippyshot.errors import SandboxUnavailable

    wrap = NonoWrap(bin="/nonexistent/nono")
    # constructing + decorating must NOT raise (opt-in is cheap); only build/run does
    NonoWrappedSandbox(_FakeSandbox(), wrap)
    with pytest.raises(SandboxUnavailable):
        wrap.build_argv(_req())


def test_decorator_delegates_transformed_request() -> None:
    inner = _FakeSandbox()
    deco = NonoWrappedSandbox(inner, NonoWrap(bin=_BIN, state_dir=Path("/tmp/cs-nono-test")))
    assert deco.name == "fake+nono"
    assert deco.secure is True
    deco.run(_req())
    assert inner.last is not None
    assert inner.last.argv[0] == _BIN and inner.last.argv[1] == "wrap"
    # smoketest probes the BASE only (no nono prefix)
    assert isinstance(deco.smoketest(), SandboxResult)


def test_select_sandbox_wraps_only_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.delenv("CLIPPYSHOT_INNER_NONO", raising=False)
    fake = _FakeSandbox()

    plain = select_sandbox(_container_factory=lambda: fake)
    assert plain is fake  # default: untouched, zero cost

    wrapped = select_sandbox(
        inner_wrap=NonoWrap(bin=_BIN), _container_factory=lambda: _FakeSandbox()
    )
    assert isinstance(wrapped, NonoWrappedSandbox)


def test_select_sandbox_env_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO", "1")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    wrapped = select_sandbox(_container_factory=lambda: _FakeSandbox())
    assert isinstance(wrapped, NonoWrappedSandbox)


def test_select_sandbox_inner_nono_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO", "1")
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO_PROFILE", "/etc/clippyshot/soffice.nono.json")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    wrapped = select_sandbox(_container_factory=lambda: _FakeSandbox())
    assert isinstance(wrapped, NonoWrappedSandbox)
    assert wrapped.wrap.profile == Path("/etc/clippyshot/soffice.nono.json")


def test_select_sandbox_fails_fast_without_landlock(monkeypatch: pytest.MonkeyPatch) -> None:
    from clippyshot.errors import SandboxUnavailable

    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    # gVisor Sentry case: inner-nono requested but Landlock returns ENOSYS.
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: False)
    with pytest.raises(SandboxUnavailable, match="Landlock is unavailable"):
        select_sandbox(inner_wrap=NonoWrap(bin=_BIN), _container_factory=lambda: _FakeSandbox())
