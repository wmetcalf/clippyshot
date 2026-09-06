"""Tests for ClippyShotEngine.warmup() — the warm-tier server seam."""
from __future__ import annotations

import logging

import pytest

from clippyshot.engine import ClippyShotEngine


@pytest.fixture(autouse=True)
def _hermetic_warm_profile(tmp_path, monkeypatch):
    # warmup() writes the hardened LO profile to CLIPPYSHOT_WARM_PROFILE_DIR; redirect it to a
    # tmp dir so tests don't write into /tmp/.clippyshot-warm-profile.
    monkeypatch.setenv("CLIPPYSHOT_WARM_PROFILE_DIR", str(tmp_path / "warm-profile"))


@pytest.fixture
def warm_tier(monkeypatch):
    """Pin the sandbox backend to the WARM tier's own value.

    Warm UNO is declined where each conversion is sandboxed (nsjail/bwrap), so a test
    that leaves selection to whatever the DEVELOPER's machine has installed measures the
    host rather than the behaviour under test -- these five failed on a box with nsjail
    present. The warm guests really do run `CLIPPYSHOT_SANDBOX=container`
    (deploy/docker/docker-compose.gvisor.yml), so this is the tier being described.
    """

    class _ContainerTier:
        name = "container"

    monkeypatch.setattr(
        "clippyshot.sandbox.detect.select_sandbox", lambda **kw: _ContainerTier()
    )
    return _ContainerTier

def test_warmup_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_WARM_UNO", raising=False)
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._uno_server is None


def test_warmup_noop_when_env_falsey(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "0")
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._uno_server is None


def test_warmup_starts_server_when_enabled(warm_tier, monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    started = []

    class FakeServer:
        def start(self):
            started.append(True)

        def stop(self):
            pass

    monkeypatch.setattr(
        "clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: FakeServer()
    )
    eng = ClippyShotEngine()
    eng.warmup()
    assert started == [True]
    assert eng._uno_server is not None


def test_warmup_is_nonfatal_on_start_failure(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")

    class FailingServer:
        def start(self):
            raise RuntimeError("no soffice in this env")

    monkeypatch.setattr(
        "clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: FailingServer()
    )
    eng = ClippyShotEngine()
    eng.warmup()  # must NOT raise — falls back to cold
    assert eng._uno_server is None


def test_warmup_writes_and_passes_hardened_profile(warm_tier, monkeypatch, tmp_path):
    # SECURITY (H1): the warm server must boot with the hardened LO profile (macro/Basic/Java
    # lockdown), captured warm in the snapshot — not LibreOffice defaults.
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    prof = tmp_path / "wp"
    monkeypatch.setenv("CLIPPYSHOT_WARM_PROFILE_DIR", str(prof))
    captured = {}

    class FakeServer:
        def start(self):
            pass

        def stop(self):
            pass

        def convert(self, *a):
            pass

    def _factory(*a, **k):
        captured["user_installation"] = k.get("user_installation")
        return FakeServer()

    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", _factory)
    eng = ClippyShotEngine()
    eng.warmup()
    xcu = prof / "user" / "registrymodifications.xcu"
    assert xcu.exists(), "warmup() must write the hardened profile before starting the server"
    body = xcu.read_text()
    assert "DisableMacrosExecution" in body and "MacroSecurityLevel" in body
    # UnoServer (the FC/TCP transport) takes a PLAIN path — unoserver runs its own
    # Path(...).as_uri(); a file:// URL makes it raise "relative path can't be
    # expressed as a file URI". Only the soffice-pipe transport gets a file:// URL.
    assert captured["user_installation"] == str(prof.resolve())


def test_warmup_fails_closed_when_profile_unwritable(monkeypatch):
    # If the hardened profile can't be written, warmup() must NOT start an unhardened server —
    # it falls back to the cold path (which writes its own per-job hardened profile).
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    started = []

    class FakeServer:
        def start(self):
            started.append(True)

        def stop(self):
            pass

    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: FakeServer())

    def _boom(self):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("clippyshot.libreoffice.profile.HardenedProfile.write", _boom)
    eng = ClippyShotEngine()
    eng.warmup()  # must NOT raise
    assert started == [], "must not start an unhardened warm server"
    assert eng._uno_server is None


class _PrimingServer:
    def __init__(self):
        self.primed = []

    def start(self):
        pass

    def stop(self):
        pass

    def convert(self, src, dst, label):
        # Record the priming convert + create the output (mirrors a real convert).
        self.primed.append((src.name, label))
        dst.write_bytes(b"%PDF-1.4 primed\n")


def test_warmup_primes_filters_by_default(warm_tier, monkeypatch):
    # The snapshot must capture a server with its conversion filters warmed, else the
    # first post-restore convert pays a multi-second cold-filter load. warmup() runs a
    # throwaway conversion per prime doc.
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    monkeypatch.delenv("CLIPPYSHOT_WARM_PRIME", raising=False)
    srv = _PrimingServer()
    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: srv)
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._uno_server is srv
    assert srv.primed, "warmup() must run at least one priming conversion"
    # Default corpus warms the Writer/PDF-export path (txt → writer_pdf_Export).
    assert any(label == "txt" for _name, label in srv.primed)


def test_warmup_priming_can_be_disabled(warm_tier, monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    monkeypatch.setenv("CLIPPYSHOT_WARM_PRIME", "0")
    srv = _PrimingServer()
    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: srv)
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._uno_server is srv
    assert srv.primed == []  # priming opt-out honored


def test_warmup_priming_failure_is_nonfatal(warm_tier, monkeypatch):
    # A priming-convert failure must leave the warm server in place (the first real
    # convert just pays the warmup once) — it must NOT disable the warm tier or raise.
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    monkeypatch.delenv("CLIPPYSHOT_WARM_PRIME", raising=False)

    class _PrimeFails(_PrimingServer):
        def convert(self, src, dst, label):
            raise RuntimeError("filter load failed")

    srv = _PrimeFails()
    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: srv)
    eng = ClippyShotEngine()
    eng.warmup()  # must NOT raise
    assert eng._uno_server is srv  # warm tier stays active despite priming failure


def test_warm_ocr_off_by_default(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_WARM_OCR", raising=False)
    monkeypatch.delenv("CLIPPYSHOT_WARM_UNO", raising=False)
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._ocr_server is None


def test_warm_ocr_starts_when_enabled(monkeypatch):
    # OCR warm tier is independent of the UNO warm tier.
    monkeypatch.setenv("CLIPPYSHOT_WARM_OCR", "1")
    monkeypatch.delenv("CLIPPYSHOT_WARM_UNO", raising=False)
    monkeypatch.delenv("CLIPPYSHOT_OCR_ENGINE", raising=False)

    # Pin the backend. Warm OCR is declined where each scanner call is
    # sandboxed (nsjail/bwrap), so leaving this to whatever the DEVELOPER's
    # machine happens to select made the outcome depend on the host rather
    # than on the behaviour under test.
    class _ContainerTier:
        name = "container"

    monkeypatch.setattr(
        "clippyshot.sandbox.detect.select_sandbox", lambda **kw: _ContainerTier()
    )
    started = []

    class FakeWarm:
        def start(self):
            started.append(True)

        def is_ready(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR", lambda *a, **k: FakeWarm())
    eng = ClippyShotEngine()
    eng.warmup()
    assert started == [True]
    assert eng._ocr_server is not None


def test_warm_ocr_nonfatal_on_start_failure(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_OCR", "1")

    class FailWarm:
        def start(self):
            raise RuntimeError("tesserocr not installed")

        def stop(self):
            pass

    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR", lambda *a, **k: FailWarm())
    eng = ClippyShotEngine()
    eng.warmup()  # must NOT raise — falls back to CLI
    assert eng._ocr_server is None


def test_warm_ocr_disabled_by_cli_engine(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_OCR", "1")
    monkeypatch.setenv("CLIPPYSHOT_OCR_ENGINE", "tesseract_cli")

    class FakeWarm:
        def start(self):
            raise AssertionError("must not start when forced to the CLI")

        def stop(self):
            pass

    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR", lambda *a, **k: FakeWarm())
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._ocr_server is None


def _fake_sandbox(name):
    return lambda: type("S", (), {"name": name})()


def test_cold_ocr_helper_starts_on_container_backend(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_OCR_ENGINE", raising=False)
    monkeypatch.setenv("CLIPPYSHOT_OCR", "1")  # cold helper only when OCR is enabled
    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR",
                        lambda *a, **k: type("W", (), {"start": lambda self: None,
                                                       "stop": lambda self: None})())
    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", _fake_sandbox("container"))
    eng = ClippyShotEngine()
    eng._maybe_start_cold_ocr_helper()
    assert eng._ocr_server is not None


def test_cold_ocr_helper_skipped_when_ocr_disabled(monkeypatch):
    # OCR off by default → a non-OCR container job must NOT spawn a helper.
    monkeypatch.delenv("CLIPPYSHOT_OCR", raising=False)
    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start")))
    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", _fake_sandbox("container"))
    eng = ClippyShotEngine()
    eng._maybe_start_cold_ocr_helper()
    assert eng._ocr_server is None


def test_cold_ocr_helper_skipped_on_baremetal(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_OCR", "1")
    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", _fake_sandbox("bwrap"))
    eng = ClippyShotEngine()
    eng._maybe_start_cold_ocr_helper()
    assert eng._ocr_server is None


def test_cold_ocr_helper_nonfatal_when_start_fails(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_OCR", "1")
    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", _fake_sandbox("container"))

    def _boom(self):
        raise RuntimeError("tesserocr missing in this container")

    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR",
                        lambda *a, **k: type("W", (), {"start": _boom, "stop": lambda self: None})())
    eng = ClippyShotEngine()
    eng._maybe_start_cold_ocr_helper()  # must NOT raise
    assert eng._ocr_server is None


def test_detonate_maps_detection_error_to_rejected(tmp_path):
    """A detector rejection (DetectionError out of converter.convert) must surface as
    status='rejected' (the dispatcher keeps such a job DONE), NOT propagate to the harness
    as a generic engine_error/FAILED. Restores the 'input rejected' contract on the server path."""
    from blastbox.limits import Limits as BlastboxLimits

    from clippyshot.engine import ClippyShotEngine
    from clippyshot.errors import DetectionError

    class _RejectingConverter:
        def convert(self, *a, **k):
            raise DetectionError("unsupported_type", "magika=iso")

    eng = ClippyShotEngine()
    eng._converter = _RejectingConverter()  # _get_converter returns this (lazy field)
    inp = tmp_path / "in.bin"
    inp.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()

    res = eng.detonate(inp, out, BlastboxLimits(timeout_s=30))

    assert res.status == "rejected"
    assert res.artifacts == []
    assert any(w.code == "rejected" for w in res.warnings)
    assert "unsupported_type" in res.warnings[0].message


@pytest.mark.parametrize("backend", ["nsjail", "bwrap"])
def test_warm_ocr_declines_when_the_backend_cannot_host_the_helper(monkeypatch, backend):
    """The cold path wraps every `tesseract` call; a warm helper outside it would hand
    the untrusted PNG to an UNSANDBOXED image parser.

    The helper now runs INSIDE the sandbox where the backend can host a persistent
    process (issue #35) -- this is the other half of that rule: a backend that cannot
    still declines, rather than falling back to running the parser unconfined. The fake
    here has no `spawn`, so it is not a `SpawningSandbox`.
    """
    from clippyshot.engine import ClippyShotEngine

    monkeypatch.setenv("CLIPPYSHOT_WARM_OCR", "1")
    monkeypatch.delenv("CLIPPYSHOT_OCR_ENGINE", raising=False)

    class Fake:
        name = backend

    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", lambda **kw: Fake())
    started = []
    monkeypatch.setattr(
        "clippyshot.ocr_warm.WarmOCR", lambda *a, **k: started.append(True)
    )

    engine = ClippyShotEngine()
    engine._warmup_ocr()
    assert started == [], f"{backend} sandboxes each call; the warm helper must not start"
    assert engine._ocr_server is None


def test_warm_ocr_still_starts_where_the_worker_itself_is_the_boundary(monkeypatch):
    """A container/guest tier has no per-call sandbox to bypass.

    Keyed on the two per-call backends rather than on "is it a container", so a
    warm FC/gVisor guest keeps the warm tier this feature exists for.
    """
    from clippyshot.engine import ClippyShotEngine

    monkeypatch.setenv("CLIPPYSHOT_WARM_OCR", "1")
    monkeypatch.delenv("CLIPPYSHOT_OCR_ENGINE", raising=False)

    class Fake:
        name = "container"

    started = []

    class FakeWarm:
        def start(self):
            started.append(True)

        def stop(self):
            pass

    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", lambda **kw: Fake())
    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR", lambda *a, **k: FakeWarm())

    engine = ClippyShotEngine()
    engine._warmup_ocr()
    assert started == [True], "a container tier must keep its warm helper"
    assert engine._ocr_server is not None


def test_warm_ocr_survives_an_unresolvable_sandbox(monkeypatch, caplog):
    """A warm guest need not resolve a backend at all.

    With no nsjail/bwrap and no container marker, selection RAISES -- and that
    is the shape a warm FC/gVisor guest takes, so declining on it would disable
    the warm tier this feature exists for. Left open and logged: the scanners
    fail on their own in that state, so refusing here protects nothing.
    """
    from clippyshot.errors import SandboxUnavailable
    from clippyshot.engine import ClippyShotEngine

    monkeypatch.setenv("CLIPPYSHOT_WARM_OCR", "1")
    monkeypatch.delenv("CLIPPYSHOT_OCR_ENGINE", raising=False)

    def _refuse(**kw):
        raise SandboxUnavailable("no backend on this host")

    started = []

    class FakeWarm:
        def start(self):
            started.append(True)

        def stop(self):
            pass

    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", _refuse)
    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR", lambda *a, **k: FakeWarm())

    engine = ClippyShotEngine()
    with caplog.at_level(logging.WARNING, logger="clippyshot.engine"):
        engine._warmup_ocr()
    assert started == [True], "an unresolvable backend must not disable the warm tier"
    assert any("could not determine the sandbox" in r.message for r in caplog.records), (
        "the ambiguity must be logged, not silent"
    )


@pytest.mark.parametrize("backend", ["nsjail", "bwrap"])
def test_warm_ocr_runs_inside_the_sandbox_when_the_backend_can_host_it(monkeypatch, backend):
    """The point of issue #35: the warm tier is restored under nsjail/bwrap by putting
    the helper INSIDE the boundary, rather than declining it and paying the per-page
    model load on the CLI path.

    Asserts the helper was constructed with the SANDBOX-backed spawner -- starting it
    with the default `subprocess.Popen` is exactly the bypass this replaces.
    """
    from clippyshot.engine import ClippyShotEngine

    monkeypatch.setenv("CLIPPYSHOT_WARM_OCR", "1")
    monkeypatch.delenv("CLIPPYSHOT_OCR_ENGINE", raising=False)

    spawned = []

    class Spawning:
        name = backend

        def spawn(self, request, **kwargs):
            spawned.append(request)
            raise AssertionError("not reached: WarmOCR is faked below")

    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", lambda **kw: Spawning())

    seen = {}

    class FakeWarm:
        def __init__(self, *a, **k):
            seen.update(k)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr("clippyshot.ocr_warm.WarmOCR", FakeWarm)

    engine = ClippyShotEngine()
    engine._warmup_ocr()

    assert engine._ocr_server is not None, f"{backend} can host the helper; it must start"
    assert seen.get("popen") is not None, (
        "the helper was started with the default Popen -- outside the sandbox"
    )


@pytest.mark.parametrize("backend", ["nsjail", "bwrap"])
def test_warm_uno_declines_where_each_conversion_is_sandboxed(monkeypatch, backend):
    """The warm soffice is a persistent process OUTSIDE the per-conversion sandbox.

    nsjail and bwrap wrap every cold soffice invocation -- that is what the
    `clippyshot-soffice` AppArmor profile and the KAFEL policy are for -- and
    `LibreOfficeRunner` prefers the warm server whenever it is ready. Enabling the warm
    tier there silently moved document parsing, the largest attack surface in this
    program, out of the sandbox the cold path insists on. Same asymmetry warm OCR had
    (#35); it matters more here, because a document reaches LibreOffice rather than a
    page image reaching tesseract.
    """
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")

    class Fake:
        name = backend

    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", lambda **kw: Fake())
    started = []
    monkeypatch.setattr(
        "clippyshot.libreoffice.uno.UnoServer",
        lambda *a, **k: started.append(True),
    )
    monkeypatch.setattr(
        "clippyshot.libreoffice.uno_pipe.SofficePipeServer",
        lambda *a, **k: started.append(True),
    )

    eng = ClippyShotEngine()
    eng._warmup_uno()

    assert started == [], f"{backend} sandboxes each conversion; the warm server must not start"
    assert eng._uno_server is None, "a warm server here bypasses the sandbox on every job"


@pytest.mark.parametrize("transport", ["socket", "pipe"])
def test_warm_uno_declines_for_both_transports(monkeypatch, transport):
    """Both transports spawn the same unsandboxed soffice; guarding one would leave the
    bypass reachable by flipping an env var."""
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO_TRANSPORT", transport)

    class Fake:
        name = "nsjail"

    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", lambda **kw: Fake())
    started = []
    monkeypatch.setattr(
        "clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: started.append(True)
    )
    monkeypatch.setattr(
        "clippyshot.libreoffice.uno_pipe.SofficePipeServer",
        lambda *a, **k: started.append(True),
    )

    eng = ClippyShotEngine()
    eng._warmup_uno()
    assert started == [], f"transport={transport} started an unsandboxed soffice"


def test_warm_uno_still_starts_where_the_guest_is_the_boundary(monkeypatch, warm_tier):
    """The FC/gVisor warm tiers must keep their warm server: they run with
    CLIPPYSHOT_SANDBOX=container (deploy/docker/docker-compose.gvisor.yml), where the
    guest is the boundary and no per-call sandbox is in play. A guard that also disabled
    those would be a performance regression dressed as a security fix."""
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    started = []

    class FakeServer:
        def start(self):
            started.append(True)

        def stop(self):
            pass

    monkeypatch.setattr(
        "clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: FakeServer()
    )
    eng = ClippyShotEngine()
    eng._warmup_uno()
    assert started == [True], "the warm tier lost its warm server"
    assert eng._uno_server is not None


@pytest.mark.parametrize("inner", ["nsjail", "bwrap"])
def test_inner_nono_decoration_does_not_disable_either_guard(monkeypatch, inner):
    """`CLIPPYSHOT_INNER_NONO=1` wraps the selection as `<backend>+nono`, which an
    exact-name check misses -- so BOTH warm tiers started outside the per-call sandbox on
    a deployment that had asked for MORE confinement, not less (codex on #41).

    The warm-OCR guard has had this hole since it was written; it is the same predicate
    now, so this covers both.
    """
    from clippyshot.engine import ClippyShotEngine
    from clippyshot.sandbox.nono_wrap import NonoWrap, NonoWrappedSandbox

    class Base:
        name = inner

        def run(self, request):  # pragma: no cover - never called
            raise AssertionError

    wrapped = NonoWrappedSandbox(inner=Base(), wrap=NonoWrap())
    assert wrapped.name == f"{inner}+nono"
    monkeypatch.setattr("clippyshot.sandbox.detect.select_sandbox", lambda **kw: wrapped)

    started = []
    monkeypatch.setattr(
        "clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: started.append("uno")
    )
    monkeypatch.setattr(
        "clippyshot.libreoffice.uno_pipe.SofficePipeServer",
        lambda *a, **k: started.append("uno-pipe"),
    )
    monkeypatch.setattr(
        "clippyshot.ocr_warm.WarmOCR", lambda *a, **k: started.append("ocr")
    )

    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    monkeypatch.setenv("CLIPPYSHOT_WARM_OCR", "1")
    monkeypatch.delenv("CLIPPYSHOT_OCR_ENGINE", raising=False)

    eng = ClippyShotEngine()
    eng._warmup_uno()
    eng._warmup_ocr()

    assert started == [], f"{wrapped.name} still sandboxes each call; nothing warm may start"
    assert eng._uno_server is None and eng._ocr_server is None
