"""BwrapSandbox refuses to start, by name, when aa-exec cannot attach clippyshot-soffice.

The probe itself is faked (see test_host_apparmor.py for its classification); these tests
pin the wiring: when it runs, what a failure raises, what select_sandbox and selftest
report, and that success is probed once per process.
"""
from __future__ import annotations

import pytest

import clippyshot.sandbox.bwrap as bwrap_mod
from clippyshot.errors import SandboxUnavailable
from clippyshot.sandbox import host_apparmor as ha
from clippyshot.sandbox.bwrap import BwrapSandbox
from clippyshot.sandbox.detect import select_sandbox
from clippyshot.types import SandboxResult

_ABSENT = ha.StockBwrapProfileState(installed=False, disabled=False, loaded=False)
_LOADED = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=True)


def _probe(ok: bool, cause: str = "ok", stderr: str = "") -> ha.AaExecProbe:
    return ha.AaExecProbe(
        ok=ok, cause=cause, profile="clippyshot-soffice",
        argv=tuple(ha.aa_exec_probe_argv("/usr/bin/bwrap", "/usr/bin/aa-exec",
                                         "clippyshot-soffice")),
        exit_code=0 if ok else 1, stderr=stderr,
        stock=_ABSENT if ok else _LOADED,
    )


_CONFLICT = _probe(
    False, "stock_profile_conflict",
    "aa-exec: ERROR: insufficient permissions to change to the profile",
)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(bwrap_mod, "_ATTACH_PROBE_OK", {})


@pytest.fixture
def make_bwrap(monkeypatch):
    """A BwrapSandbox on a faked host: bwrap + (optionally) aa-exec present, no real exec."""

    def _make(*, aa_exec: bool = True, probe_results=None, run_exit: int = 0):
        monkeypatch.setattr(bwrap_mod.shutil, "which", lambda n: (
            ("/usr/bin/aa-exec" if aa_exec else None) if n == "aa-exec" else f"/usr/bin/{n}"
        ))
        monkeypatch.setattr(bwrap_mod, "_probe_bwrap_cgroup_pids", lambda _p: True)
        calls: list[tuple] = []
        results = list(probe_results or [_probe(True)])

        def fake_probe(bwrap, aa, profile, **kw):
            calls.append((bwrap, aa, profile))
            return results.pop(0) if len(results) > 1 else results[0]

        monkeypatch.setattr(ha, "probe_aa_exec", fake_probe)
        sb = BwrapSandbox(bwrap_path="/usr/bin/bwrap")
        ran: list = []

        def fake_run(req):
            ran.append(req)
            return SandboxResult(exit_code=run_exit, stdout=b"", stderr=b"",
                                 duration_ms=1, killed=False)

        monkeypatch.setattr(sb, "run", fake_run)
        return sb, calls, ran

    return _make


def test_smoketest_runs_the_attach_probe_first(make_bwrap):
    sb, calls, ran = make_bwrap()
    assert sb.smoketest().exit_code == 0
    assert calls == [("/usr/bin/bwrap", "/usr/bin/aa-exec", "clippyshot-soffice")]
    assert len(ran) == 1
    assert sb.apparmor_attach_probe is not None and sb.apparmor_attach_probe.ok


def test_failed_probe_refuses_with_the_stock_profile_named(make_bwrap):
    sb, _calls, ran = make_bwrap(probe_results=[_CONFLICT])
    with pytest.raises(ha.AppArmorAttachUnavailable) as ei:
        sb.smoketest()
    assert isinstance(ei.value, SandboxUnavailable)
    assert "bwrap-userns-restrict" in str(ei.value)
    assert ha.FIX_COMMAND in str(ei.value)
    assert ran == []          # never ran the payload past a failed attach


def test_no_probe_without_aa_exec(make_bwrap):
    sb, calls, ran = make_bwrap(aa_exec=False)
    sb.smoketest()
    assert calls == []
    assert sb.apparmor_attach_probe is None
    assert len(ran) == 1


def test_success_is_probed_once_per_process(make_bwrap):
    sb, calls, _ = make_bwrap()
    sb.smoketest()
    sb.smoketest()
    assert len(calls) == 1


def test_a_second_instance_reuses_the_success(make_bwrap, monkeypatch):
    sb, calls, _ = make_bwrap()
    sb.smoketest()
    sb2 = BwrapSandbox(bwrap_path="/usr/bin/bwrap")
    monkeypatch.setattr(sb2, "run", lambda req: SandboxResult(
        exit_code=0, stdout=b"", stderr=b"", duration_ms=1, killed=False))
    sb2.smoketest()
    assert len(calls) == 1
    assert sb2.apparmor_attach_probe is not None and sb2.apparmor_attach_probe.ok


def test_failure_is_not_cached(make_bwrap):
    """A host fixed while the worker runs is picked up on the next selection."""
    sb, calls, _ = make_bwrap(probe_results=[_CONFLICT, _probe(True)])
    with pytest.raises(ha.AppArmorAttachUnavailable):
        sb.smoketest()
    sb.smoketest()
    assert len(calls) == 2


def test_forced_bwrap_refuses_to_start_naming_the_profile(make_bwrap, monkeypatch):
    sb, _, _ = make_bwrap(probe_results=[_CONFLICT])
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "bwrap")
    with pytest.raises(SandboxUnavailable, match="bwrap-userns-restrict"):
        select_sandbox(_bwrap_factory=lambda: sb)


def test_auto_selection_says_why_bwrap_was_passed_over(make_bwrap, monkeypatch):
    sb, _, _ = make_bwrap(probe_results=[_CONFLICT])
    monkeypatch.delenv("CLIPPYSHOT_SANDBOX", raising=False)

    def none():
        raise SandboxUnavailable("absent")

    with pytest.raises(SandboxUnavailable) as ei:
        select_sandbox(_nsjail_factory=none, _bwrap_factory=lambda: sb, _container_factory=none)
    assert "bwrap-userns-restrict" in str(ei.value)
    assert ha.FIX_COMMAND in str(ei.value)
