"""Unit tests for the optional inner nono (Landlock) wrapper.

These exercise the deterministic argv transform + the decorator wiring with a
stand-in binary (``/bin/true``) so they run in CI without nono installed. The
real-conversion enforcement test lives in tests/integration.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from clippyshot.sandbox.base import Mount, SandboxRequest
from clippyshot.sandbox.detect import select_sandbox
from clippyshot.sandbox.nono_wrap import NonoWrap, NonoWrappedSandbox
from clippyshot.types import SandboxResult

# resolve_bin returns the realpath (merged-/usr hosts: /bin -> usr/bin)
_BIN = os.path.realpath("/bin/true")


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
    # smoketest goes THROUGH the nono layer (it is what selection must prove)
    inner.last = None
    assert isinstance(deco.smoketest(), SandboxResult)
    assert inner.last is not None and inner.last.argv[1] == "wrap"
    assert inner.last.argv[-1] == "/bin/true"


def test_select_sandbox_wraps_only_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.delenv("CLIPPYSHOT_INNER_NONO", raising=False)
    fake = _FakeSandbox()

    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    plain = select_sandbox(_container_factory=lambda: fake)
    assert plain is fake  # default: untouched, zero cost

    wrapped = select_sandbox(
        inner_wrap=NonoWrap(bin=_BIN, state_dir=Path("/tmp/cs-nono-test")),
        _container_factory=lambda: _FakeSandbox(),
    )
    assert isinstance(wrapped, NonoWrappedSandbox)


def test_select_sandbox_env_trigger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO", "1")
    monkeypatch.setenv("CLIPPYSHOT_NONO_BIN", _BIN)
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    wrapped = select_sandbox(_container_factory=lambda: _FakeSandbox())
    assert isinstance(wrapped, NonoWrappedSandbox)


def test_select_sandbox_inner_nono_profile_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO", "1")
    profile = tmp_path / "soffice.nono.json"
    profile.write_text("{}")
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO_PROFILE", str(profile))
    monkeypatch.setenv("CLIPPYSHOT_NONO_BIN", _BIN)
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    wrapped = select_sandbox(_container_factory=lambda: _FakeSandbox())
    assert isinstance(wrapped, NonoWrappedSandbox)
    assert wrapped.wrap.profile == profile


def test_select_sandbox_fails_fast_without_landlock(monkeypatch: pytest.MonkeyPatch) -> None:
    from clippyshot.errors import SandboxUnavailable

    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    # gVisor Sentry case: inner-nono requested but Landlock returns ENOSYS.
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: False)
    with pytest.raises(SandboxUnavailable, match="Landlock is unavailable"):
        select_sandbox(inner_wrap=NonoWrap(bin=_BIN), _container_factory=lambda: _FakeSandbox())


# --------------------------------------------------------------------------
# select_sandbox must not hand back a nono wrapper whose binary is missing: that
# "succeeds" at selection time and only fails mid-conversion (every job), or, worse,
# a caller that only inspects the selection believes the Landlock layer is present.
# Every other backend is smoketested before it is accepted; nono must fail closed too.
# --------------------------------------------------------------------------


def test_select_sandbox_fails_closed_when_nono_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clippyshot.errors import SandboxUnavailable

    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    with pytest.raises(SandboxUnavailable, match="nono"):
        select_sandbox(
            inner_wrap=NonoWrap(bin="/nonexistent/nono"),
            _container_factory=lambda: _FakeSandbox(),
        )


def test_select_sandbox_env_trigger_fails_closed_without_nono_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clippyshot.errors import SandboxUnavailable

    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO", "1")
    monkeypatch.delenv("CLIPPYSHOT_NONO_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir: no `nono` anywhere
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    built: list[str] = []

    def _factory() -> _FakeSandbox:
        built.append("x")
        return _FakeSandbox()

    with pytest.raises(SandboxUnavailable, match="nono"):
        select_sandbox(
            _nsjail_factory=_factory, _bwrap_factory=_factory, _container_factory=_factory
        )
    assert built == []  # refused before any backend is constructed or smoketested


def test_select_sandbox_rejects_non_executable_nono(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clippyshot.errors import SandboxUnavailable

    fake = tmp_path / "nono"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o644)  # exists, but cannot be exec'd
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    with pytest.raises(SandboxUnavailable, match="nono"):
        select_sandbox(
            inner_wrap=NonoWrap(bin=str(fake)), _container_factory=lambda: _FakeSandbox()
        )


def test_relative_nono_bin_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every request runs with workdir "/", so a CWD-relative nono passes a bare
    existence check and then fails every conversion."""
    from clippyshot.errors import InnerSandboxLayerUnavailable

    (tmp_path / "bin").mkdir()
    nono = tmp_path / "bin" / "nono"
    nono.write_text("#!/bin/sh\n")
    nono.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    assert NonoWrap(bin="bin/nono").resolve_bin() is None
    monkeypatch.setenv("PATH", "bin")  # relative PATH entry: which() returns bin/nono
    assert NonoWrap().resolve_bin() is None
    with pytest.raises(InnerSandboxLayerUnavailable):
        select_sandbox(inner_wrap=NonoWrap(bin="bin/nono"),
                       _container_factory=lambda: _FakeSandbox())


def test_uncreatable_state_dir_is_refused_at_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clippyshot.errors import InnerSandboxLayerUnavailable

    blocker = tmp_path / "file"
    blocker.write_text("x")  # a FILE where the state dir's parent must be
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    with pytest.raises(InnerSandboxLayerUnavailable, match="state dir"):
        select_sandbox(inner_wrap=NonoWrap(bin=_BIN, state_dir=blocker / "state"),
                       _container_factory=lambda: _FakeSandbox())


def test_env_state_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from clippyshot.sandbox.detect import inner_wrap_from_env

    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO", "1")
    monkeypatch.setenv("CLIPPYSHOT_INNER_NONO_STATE_DIR", str(tmp_path / "s"))
    wrap = inner_wrap_from_env()
    assert wrap is not None and wrap.state_dir == tmp_path / "s"
    monkeypatch.delenv("CLIPPYSHOT_INNER_NONO")
    assert inner_wrap_from_env() is None


def test_selftest_does_not_blame_bwrap_hardening_for_missing_nono(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nono check raises before any backend is tried, so the 'disable the stock bwrap
    profile' remedy cannot fix it and must not be offered."""
    import clippyshot.selftest as st
    from clippyshot.errors import InnerSandboxLayerUnavailable
    from clippyshot.sandbox import host_apparmor as ha

    def boom():
        raise InnerSandboxLayerUnavailable("nono binary was not found")

    logged: list[dict] = []

    class _Log:
        def error(self, event, **kw):
            logged.append({"event": event, **kw})

        def info(self, *a, **k):
            pass

        warning = info

    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)
    monkeypatch.setattr(st, "select_sandbox", boom)
    monkeypatch.setattr(st, "Detector", lambda: object())
    monkeypatch.setattr(st, "stock_bwrap_profile_state",
                        lambda: ha.StockBwrapProfileState(True, False, True))
    monkeypatch.setattr(st.shutil, "which", lambda n, *a, **k: "/usr/bin/" + n)
    monkeypatch.setattr(st.subprocess, "run",
                        lambda *a, **k: st.subprocess.CompletedProcess(a, 0, "LO", ""))
    monkeypatch.setattr(st, "get_logger", lambda *a, **k: _Log(), raising=False)
    assert st.run_selftest() == 1
    (ev,) = [e for e in logged if e["event"] == "sandbox_unavailable"]
    assert "fix" not in ev and ev["inner_layer"] == "nono"


def test_missing_profile_is_refused_at_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clippyshot.errors import InnerSandboxLayerUnavailable

    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    with pytest.raises(InnerSandboxLayerUnavailable, match="profile"):
        select_sandbox(
            inner_wrap=NonoWrap(bin=_BIN, state_dir=tmp_path / "s",
                                profile=tmp_path / "missing.json"),
            _container_factory=lambda: _FakeSandbox())


def test_existing_but_unwritable_state_dir_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """mkdir(exist_ok=True) succeeds on a dir the worker cannot write (e.g. created by root
    during setup); nono still cannot use it."""
    import os

    from clippyshot.errors import InnerSandboxLayerUnavailable

    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o555)
    if os.access(state, os.W_OK):
        pytest.skip("running as root: mode bits do not restrict")
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    try:
        with pytest.raises(InnerSandboxLayerUnavailable, match="state dir"):
            select_sandbox(inner_wrap=NonoWrap(bin=_BIN, state_dir=state),
                           _container_factory=lambda: _FakeSandbox())
    finally:
        state.chmod(0o755)


def test_unreadable_profile_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    from clippyshot.errors import InnerSandboxLayerUnavailable

    prof = tmp_path / "p.json"
    prof.write_text("{}")
    prof.chmod(0o000)
    if os.access(prof, os.R_OK):
        pytest.skip("running as root")
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    try:
        with pytest.raises(InnerSandboxLayerUnavailable, match="profile"):
            select_sandbox(inner_wrap=NonoWrap(bin=_BIN, state_dir=tmp_path / "s", profile=prof),
                           _container_factory=lambda: _FakeSandbox())
    finally:
        prof.chmod(0o644)


def test_profile_is_mounted_for_namespacing_bases(tmp_path: Path) -> None:
    prof = tmp_path / "p.json"
    prof.write_text("{}")
    out = NonoWrap(bin=_BIN, profile=prof, state_dir=tmp_path / "s").apply(_req())
    assert any(m.host_path == prof and m.sandbox_path == prof and m.read_only
               for m in out.ro_mounts)


def test_selection_fails_closed_when_the_wrapped_smoketest_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A nono that runs but cannot wrap (bad profile, missing grants, ...) only shows up
    when a command goes through it: selection must run one."""
    from clippyshot.errors import InnerSandboxLayerUnavailable

    class _NonoFails(_FakeSandbox):
        def run(self, request):
            self.last = request
            failed = request.argv[1:2] == ["wrap"]
            return SandboxResult(1 if failed else 0, b"", b"nono: bad profile", 1, False)

    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    with pytest.raises(InnerSandboxLayerUnavailable, match="smoketest"):
        select_sandbox(inner_wrap=NonoWrap(bin=_BIN, state_dir=tmp_path / "s"),
                       _container_factory=lambda: _NonoFails())


def test_symlinked_nono_resolves_to_its_target(tmp_path: Path) -> None:
    real_dir = tmp_path / "opt"
    real_dir.mkdir()
    real = real_dir / "nono-real"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "nono").symlink_to(real)
    wrap = NonoWrap(bin=str(link_dir / "nono"), state_dir=tmp_path / "s")
    assert wrap.resolve_bin() == str(real)
    out = wrap.apply(_req())
    assert any(m.host_path == real_dir for m in out.ro_mounts)


def test_untraversable_nono_path_is_none_not_permission_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    nono = locked / "nono"
    nono.write_text("#!/bin/sh\n")
    nono.chmod(0o755)
    locked.chmod(0o000)
    try:
        if os.access(nono, os.F_OK):
            pytest.skip("running as root: mode bits do not restrict")
        with pytest.raises(PermissionError):
            nono.is_file() or nono.stat()  # the call the fix must contain
        assert NonoWrap(bin=str(nono)).resolve_bin() is None
    finally:
        locked.chmod(0o755)


@pytest.mark.parametrize("field", ["state_dir", "profile"])
def test_relative_state_dir_or_profile_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str
) -> None:
    from clippyshot.errors import InnerSandboxLayerUnavailable

    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.json").write_text("{}")
    kw = {"state_dir": tmp_path / "s", "profile": None}
    kw[field] = Path("rel-state") if field == "state_dir" else Path("p.json")
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "container")
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    with pytest.raises(InnerSandboxLayerUnavailable, match="absolute"):
        select_sandbox(inner_wrap=NonoWrap(bin=_BIN, **kw),
                       _container_factory=lambda: _FakeSandbox())


def test_nono_failure_under_the_chosen_base_fails_closed_not_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Enabling the opt-in EXTRA layer must never change which base is chosen: a nono
    failure under nsjail must not fall through to a weaker bwrap/container."""
    from clippyshot.errors import InnerSandboxLayerUnavailable

    class _Base(_FakeSandbox):
        def __init__(self, name: str, nono_ok: bool) -> None:
            super().__init__()
            self.name = name
            self.nono_ok = nono_ok

        def run(self, request):
            wrapped = request.argv[1:2] == ["wrap"]
            return SandboxResult(0 if (self.nono_ok or not wrapped) else 1, b"", b"", 1, False)

    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)
    monkeypatch.setattr("clippyshot.sandbox.detect.landlock_available", lambda: True)
    wrap = NonoWrap(bin=_BIN, state_dir=tmp_path / "s")
    built: list[str] = []

    def bwrap():
        built.append("bwrap")
        return _Base("bwrap", True)

    with pytest.raises(InnerSandboxLayerUnavailable, match="nsjail"):
        select_sandbox(inner_wrap=wrap, _nsjail_factory=lambda: _Base("nsjail", False),
                       _bwrap_factory=bwrap,
                       _container_factory=lambda: _Base("container", True))
    assert built == []
