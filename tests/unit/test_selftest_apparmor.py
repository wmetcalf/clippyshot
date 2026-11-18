"""selftest exposes the bwrap/aa-exec attach probe and the stock-profile state.

So a host upgrade that brings `bwrap-userns-restrict` back shows in the image
healthcheck (`clippyshot selftest`) output, not just as a refused start.
"""
from __future__ import annotations

import json
import subprocess

import pytest

import clippyshot.selftest as st
from clippyshot.errors import SandboxUnavailable
from clippyshot.sandbox import host_apparmor as ha
from clippyshot.types import SandboxResult

_ABSENT = ha.StockBwrapProfileState(installed=False, disabled=False, loaded=False)
_LOADED = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=True)


class _Bwrap:
    name = "bwrap"
    secure = True
    insecurity_reasons: list[str] = []
    apparmor_profile = "clippyshot-soffice"
    seccomp_source = "clippyshot-bwrap"

    def __init__(self, probe):
        self.apparmor_attach_probe = probe

    def smoketest(self):
        return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=1, killed=False)


class _Wrapped:
    """A decorator like NonoWrappedSandbox: renamed, the bwrap is `.inner`."""

    name = "bwrap+nono"
    secure = True
    insecurity_reasons: list[str] = []

    def __init__(self, inner):
        self.inner = inner

    def smoketest(self):
        return self.inner.smoketest()


def _ok_probe():
    return ha.AaExecProbe(ok=True, cause="ok", profile="clippyshot-soffice", argv=("b",),
                          exit_code=0, stderr="", stock=_ABSENT)


def _events(err: str) -> dict[str, dict]:
    out = {}
    for line in err.splitlines():
        line = line.strip()
        if line.startswith("{"):
            rec = json.loads(line)
            out[rec["event"]] = rec
    return out


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(st, "Detector", lambda: object())
    monkeypatch.setattr(st.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(st.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, "LibreOffice 25.8", ""))

    def _set(sandbox=None, stock=_ABSENT, error=None):
        def select():
            if error is not None:
                raise error
            return sandbox

        monkeypatch.setattr(st, "select_sandbox", select)
        monkeypatch.setattr(st, "stock_bwrap_profile_state", lambda: stock)

    return _set


def test_attach_report_for_bwrap():
    rep = st.apparmor_attach_report(_Bwrap(_ok_probe()), stock=_ABSENT)
    assert rep == {"apparmor_attach": "ok", "stock_bwrap_profile": "absent"}


def test_attach_report_sees_through_a_decorator():
    rep = st.apparmor_attach_report(_Wrapped(_Bwrap(_ok_probe())), stock=_ABSENT)
    assert rep["apparmor_attach"] == "ok"


def test_attach_report_not_applicable_for_other_backends():
    class _C:
        name = "container"

    assert st.apparmor_attach_report(_C(), stock=_ABSENT)["apparmor_attach"] == "not_applicable"


def test_selftest_passed_reports_the_probe(host, capsys):
    host(sandbox=_Bwrap(_ok_probe()))
    assert st.run_selftest() == 0
    ev = _events(capsys.readouterr().err)
    assert ev["selftest_passed"]["apparmor_attach"] == "ok"
    assert ev["selftest_passed"]["stock_bwrap_profile"] == "absent"
    assert "stock_bwrap_profile_conflict" not in ev


def test_selftest_warns_when_the_stock_profile_is_back(host, capsys):
    """nsjail selected, but a host upgrade re-enabled the stock profile: visible, not fatal."""

    class _Nsjail:
        name = "nsjail"
        secure = True
        insecurity_reasons: list[str] = []

        def smoketest(self):
            return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=1,
                                 killed=False)

    host(sandbox=_Nsjail(), stock=_LOADED)
    assert st.run_selftest() == 0
    ev = _events(capsys.readouterr().err)
    warn = ev["stock_bwrap_profile_conflict"]
    assert warn["profile"] == "bwrap-userns-restrict"
    assert warn["fix"] == ha.FIX_COMMAND
    assert ev["selftest_passed"]["stock_bwrap_profile"].startswith("loaded")


def test_selftest_fails_naming_the_conflict(host, capsys):
    probe = ha.AaExecProbe(
        ok=False, cause="stock_profile_conflict", profile="clippyshot-soffice", argv=("b",),
        exit_code=1, stderr="insufficient permissions to change to the profile", stock=_LOADED,
    )
    host(error=ha.AppArmorAttachUnavailable(probe), stock=_LOADED)
    assert st.run_selftest() == 1
    ev = _events(capsys.readouterr().err)
    err = ev["sandbox_unavailable"]
    assert "bwrap-userns-restrict" in err["error"]
    assert err["apparmor_attach"] == "stock_profile_conflict"
    assert err["fix"] == ha.FIX_COMMAND


def test_selftest_generic_unavailable_still_reports_stock_state(host, capsys):
    host(error=SandboxUnavailable("no sandbox backend available"), stock=_LOADED)
    assert st.run_selftest() == 1
    err = _events(capsys.readouterr().err)["sandbox_unavailable"]
    assert err["stock_bwrap_profile"].startswith("loaded")
    assert err["fix"] == ha.FIX_COMMAND


def test_no_conflict_warning_under_the_container_backend(host, capsys):
    """gVisor/FC force `container`: the image's /etc/apparmor.d is not host policy."""

    class _Container:
        name = "container"
        secure = True
        insecurity_reasons: list[str] = []

        def smoketest(self):
            return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=1,
                                 killed=False)

    host(sandbox=_Container(), stock=_LOADED)
    assert st.run_selftest() == 0
    ev = _events(capsys.readouterr().err)
    assert "stock_bwrap_profile_conflict" not in ev
    assert ev["selftest_passed"]["apparmor_attach"] == "not_applicable"


def test_attach_report_names_a_bwrap_without_aa_exec():
    """soffice runs unconfined there; 'not_applicable' would read as 'AppArmor irrelevant'."""
    assert st.apparmor_attach_report(_Bwrap(None), stock=_ABSENT)["apparmor_attach"] == "no_aa_exec"


def test_no_disable_hint_for_a_forced_non_bwrap_backend(host, capsys, monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_SANDBOX", "nsjail")
    host(error=SandboxUnavailable("forced backend 'nsjail' failed smoketest"), stock=_LOADED)
    assert st.run_selftest() == 1
    err = _events(capsys.readouterr().err)["sandbox_unavailable"]
    assert "fix" not in err and err["stock_bwrap_profile"].startswith("loaded")
