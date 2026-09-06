"""The sandbox backends can host a LONG-LIVED process, not just a one-shot run.

A warm helper outlives every request it serves, so the deadlines `run()` imposes --
nsjail's `--time_limit`, bwrap's RLIMIT_CPU -- would kill it part-way through a slot.
Each request is bounded by the client instead, which SIGKILLs a hung helper (issue #35).
"""
from __future__ import annotations

import resource

import pytest

from clippyshot.limits import Limits
from clippyshot.sandbox.base import SandboxRequest, SpawningSandbox

nsjail = pytest.importorskip("clippyshot.sandbox.nsjail")
bwrap = pytest.importorskip("clippyshot.sandbox.bwrap")


def _req() -> SandboxRequest:
    return SandboxRequest(argv=["/bin/true"], limits=Limits(timeout_s=60), attach_apparmor=False)


class TestNsjailPersistentArgv:
    def _sandbox(self, monkeypatch):
        monkeypatch.setattr(nsjail.shutil, "which", lambda _n: "/usr/local/bin/nsjail")
        monkeypatch.setattr(nsjail, "_probe_nsjail_proc_apparmor", lambda _p: False)
        return nsjail.NsjailSandbox()

    def test_a_one_shot_run_keeps_its_deadline(self, monkeypatch):
        argv = self._sandbox(monkeypatch)._build_argv(_req())
        assert "--time_limit" in argv, "a one-shot run must still be bounded"

    def test_a_persistent_helper_has_no_wall_clock_deadline(self, monkeypatch):
        """`Limits.timeout_s` caps at 600s, so reusing the one-shot argv would kill the
        warm helper ten minutes into a slot."""
        argv = self._sandbox(monkeypatch)._build_argv(_req(), persistent=True)
        assert "--time_limit" not in argv, "the warm helper would be killed mid-slot"

    def test_a_persistent_helper_keeps_every_other_restriction(self, monkeypatch):
        argv = self._sandbox(monkeypatch)._build_argv(_req(), persistent=True)
        for flag in ("--user", "--group", "--iface_no_lo", "--rlimit_as", "--rlimit_fsize"):
            assert flag in argv, f"{flag} was dropped along with the deadline"

    def test_it_advertises_the_capability(self, monkeypatch):
        assert isinstance(self._sandbox(monkeypatch), SpawningSandbox)


class TestBwrapPersistentRlimits:
    def _calls(self, **kw):
        seen = []

        def _fake(res, pair):
            seen.append((res, pair))

        original = resource.setrlimit
        resource.setrlimit = _fake
        try:
            bwrap._apply_rlimits(Limits(timeout_s=60), **kw)()
        finally:
            resource.setrlimit = original
        return dict(seen)

    def test_a_one_shot_run_gets_a_cpu_deadline(self):
        assert resource.RLIMIT_CPU in self._calls()

    def test_a_persistent_helper_does_not(self):
        """RLIMIT_CPU is a LIFETIME budget, and OCR is CPU-bound: a request-shaped
        deadline applied to a process serving many requests kills it mid-slot."""
        assert resource.RLIMIT_CPU not in self._calls(cpu_deadline=False)

    def test_the_other_limits_survive(self):
        calls = self._calls(cpu_deadline=False)
        assert resource.RLIMIT_AS in calls, "memory bound dropped with the CPU deadline"
        assert resource.RLIMIT_FSIZE in calls, "file-size bound dropped with the CPU deadline"
        assert calls[resource.RLIMIT_CORE] == (0, 0)
