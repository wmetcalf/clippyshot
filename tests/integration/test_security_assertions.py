import json
import re
import socket
import threading
from pathlib import Path

import pytest

from clippyshot.converter import ConvertOptions
from clippyshot.limits import Limits
from clippyshot.sandbox.base import SandboxRequest
from tests.conftest import (
    FIXTURES_DIR,
    needs_bwrap_userns,
    needs_nsjail_userns,
    needs_pdftoppm,
    needs_soffice,
)

MALICIOUS = FIXTURES_DIR / "malicious"
SAFE = FIXTURES_DIR / "safe"

pytestmark = [pytest.mark.integration]


class _TrackingListener:
    """Tiny TCP listener that counts connections.

    Used to verify the sandbox blocks network egress: bind a listener and
    assert it receives zero connections during a sandboxed conversion.
    """

    def __init__(self, port: int):
        self.port = port
        self.connections = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)

    def _run(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
                self.connections += 1
                conn.close()
            except (socket.timeout, OSError):
                continue


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
def test_external_image_is_not_fetched(converter, tmp_path: Path):
    src = MALICIOUS / "external_image.docx"
    if not src.exists():
        pytest.skip("malicious fixture not built")
    out = tmp_path / "out"
    with _TrackingListener(65500) as listener:
        converter.convert(
            src,
            out,
            ConvertOptions(limits=Limits(timeout_s=60, max_pages=2)),
        )
    assert listener.connections == 0
    meta = json.loads((out / "metadata.json").read_text())
    assert meta["security"]["network"] == "denied"
    assert meta["security"]["macro_security_level"] == 3


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
def test_an_ole_link_document_still_converts(converter, tmp_path: Path):
    """A document carrying an OLE \\objupdate link converts and renders.

    This was test_ole_link_does_not_read_outside_sandbox: it wrote a canary file
    into tmp_path, converted the fixture, and asserted the canary's bytes were
    absent from page-001.png. It could not fail, for two independent reasons,
    both measured rather than argued.

      * The fixture has no link target. It is
        {\\object\\objupdate\\objemb {\\*\\objclass Excel.Sheet.12}{\\*\\objdata 0102}},
        two bytes of junk object data naming no path -- and the canary lived at
        a random tmp_path the fixture could not have referenced anyway.
      * The detection could not see the canary even if it were rendered. A PNG
        that VISIBLY draws "THIS_SHOULD_NEVER_BE_READ" does not contain those
        bytes -- the raster is compressed, so the assertion holds either way.

    Nor is there a positive control to build one from: LibreOffice does not
    follow an INCLUDETEXT field pointing at a real file during a headless
    conversion even with a DEFAULT profile and no sandbox (measured on 24.2.7.2
    -- the field renders its cached placeholder). What the sandbox does enforce
    is tested where it can fail: test_a_write_to_tmp_inside_the_sandbox_never_
    reaches_the_host, and tests/integration/test_sandbox_escape.py.
    """
    src = MALICIOUS / "ole_link.rtf"
    if not src.exists():
        pytest.skip("malicious fixture not built")
    out = tmp_path / "out"
    converter.convert(
        src,
        out,
        ConvertOptions(limits=Limits(timeout_s=60, max_pages=2)),
    )
    render = json.loads((out / "metadata.json").read_text())["render"]
    assert render["page_count_rendered"] >= 1


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
@pytest.mark.parametrize(
    "max_pages,expect_rendered,expect_truncated",
    [(1, 1, True), (2, 2, False)],
)
def test_max_pages_truncation_is_reported(
    converter, tmp_path: Path, max_pages: int, expect_rendered: int, expect_truncated: bool
):
    """Truncation, on an input whose page count this repo controls.

    The spreadsheet test below cannot pin this down: how LibreOffice paginates a
    CSV varies by host, and where it emits a single page nothing is truncated,
    so a `truncated` regression survives there. An RTF with a hard `\\page`
    break is two pages on any LibreOffice, which makes both sides of the
    boundary reachable everywhere -- including max_pages == the page count,
    where an off-by-one reads as truncation that did not happen.

    (two_page.pdf, the obvious candidate, cannot be used: the converter rejects
    PDF input outright -- `unsupported_type: magika=pdf`.)
    """
    src = tmp_path / "two_pages.rtf"
    src.write_text(
        "{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Helvetica;}}"
        "\\f0\\fs24 Page one.\\page Page two.}"
    )
    out = tmp_path / "out"
    converter.convert(
        src, out, ConvertOptions(limits=Limits(timeout_s=120, max_pages=max_pages))
    )
    render = json.loads((out / "metadata.json").read_text())["render"]
    assert render["page_count_total"] == 2
    assert render["page_count_rendered"] == expect_rendered
    assert render["truncated"] is expect_truncated


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
def test_max_pages_truncation_on_spreadsheet(converter, tmp_path: Path):
    src = MALICIOUS / "sleeper.csv"
    if not src.exists():
        pytest.skip("malicious fixture not built")
    out = tmp_path / "out"
    max_pages = 1
    converter.convert(
        src,
        out,
        ConvertOptions(limits=Limits(timeout_s=120, max_pages=max_pages)),
    )
    render = json.loads((out / "metadata.json").read_text())["render"]

    # How LibreOffice paginates a 20k-row CSV is its business, not ours: on this
    # host the whole file comes out as ONE 13x29632px page, on the Docker image
    # it is many. Asserting `truncated is True` therefore fails where nothing
    # needed truncating -- a fact about the renderer, dressed up as a security
    # regression. What max_pages actually promises is a relationship, so assert
    # that: never more than the limit, everything that existed when it fits, and
    # `truncated` exactly when pages were dropped.
    total = render["page_count_total"]
    assert render["page_count_rendered"] == min(total, max_pages)
    assert render["page_count_rendered"] >= 1, "a page of content must survive"
    assert render["truncated"] is (total > max_pages)


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
def test_timeout_kills_long_running_conversion(converter, tmp_path: Path):
    """A 1s timeout against a 20k-row CSV must result in a ConversionError or
    LibreOfficeError."""
    from clippyshot.errors import ConversionError, LibreOfficeError

    src = MALICIOUS / "sleeper.csv"
    if not src.exists():
        pytest.skip("malicious fixture not built")
    out = tmp_path / "out"
    with pytest.raises((ConversionError, LibreOfficeError)):
        converter.convert(
            src,
            out,
            ConvertOptions(limits=Limits(timeout_s=1, max_pages=1)),
        )


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("nsjail", marks=needs_nsjail_userns),
        pytest.param("bwrap", marks=needs_bwrap_userns),
    ],
)
def test_a_write_to_tmp_inside_the_sandbox_never_reaches_the_host(backend: str):
    """A file written to /tmp INSIDE the sandbox must not appear on the host.

    With a positive control, because that is the entire difficulty here. The
    macro-document test below asserts the same kind of host path is absent after a
    conversion that never attempts the write at all, so it holds whether or not the
    isolation exists. This one makes the guest actually perform the write and proves
    it succeeded (WROTE in stdout) before asking whether the host saw it.

    Each backend is constructed BY NAME rather than through select_sandbox(), which
    returns only the first usable implementation -- nsjail before bwrap -- so probing
    whatever it hands back leaves the other advertised isolation path untested.
    Measured: removing bwrap's `--tmpfs /tmp` left this green, because nsjail ran.

    The cases are gated on the HOST capability marks, not on the backend's own
    smoketest. conftest says why, and it is the reason this test exists at all: a
    regression such as a malformed mount or seccomp argument makes the smoketest
    nonzero, and gating on it would skip the very assertion meant to catch that.
    Construction and execution failures are left to fail.

    ContainerSandbox is not probed: it runs commands directly inside the enclosing
    container and shares this process's /tmp by design.
    """
    from clippyshot.sandbox.bwrap import BwrapSandbox
    from clippyshot.sandbox.nsjail import NsjailSandbox

    sb = {"nsjail": NsjailSandbox, "bwrap": BwrapSandbox}[backend]()
    sentinel = Path(f"/tmp/clippyshot-sandbox-tmp-probe-{backend}")
    sentinel.unlink(missing_ok=True)
    result = sb.run(
        SandboxRequest(
            argv=["/bin/sh", "-c",
                  f"echo pwned > {sentinel} && test -s {sentinel} && echo WROTE"],
            limits=Limits(timeout_s=10, memory_bytes=128 * 1024 * 1024),
        )
    )
    try:
        out = result.stdout.decode(errors="replace")
        assert "WROTE" in out, (
            f"the {backend} probe never wrote inside the guest, so this proves "
            f"nothing: {out!r}"
        )
        assert not sentinel.exists(), (
            f"{sentinel} appeared on the HOST — {backend} is sharing /tmp"
        )
    finally:
        sentinel.unlink(missing_ok=True)


def test_the_macro_fixture_is_actually_wired_to_document_open():
    """The fixture must be ARMED, or the test below measures nothing.

    Twice the interesting question has been whether "the sentinel was not written"
    means the hardening worked or the fixture was inert -- first because the Basic
    module was bound to no event at all, then because the `dom:` prefix was
    undeclared so the QName resolved to nothing (codex, both times). A measurement
    that cannot tell those apart is not evidence.

    This resolves the chain the way LibreOffice must: the listener's event name is
    the XML Events `load` event, and its href names a Basic routine that the
    document actually defines. Structural rather than executed, deliberately --
    invoking the macro for real DOES write the sentinel, but only against an
    already-resident soffice, and a positive control that depends on whether another
    process happens to be running is worse than none. The execution result is
    recorded in test_a_macro_bearing_document_still_converts instead.
    """
    import xml.etree.ElementTree as ET
    import zipfile

    src = MALICIOUS / "macro_autoopen.odt"
    if not src.exists():
        pytest.skip("malicious fixture macro_autoopen.odt not built")

    with zipfile.ZipFile(src) as z:
        content = z.read("content.xml").decode()
        module = z.read("Basic/Standard/Module1.xml").decode()

    root = ET.fromstring(content)
    ns_script = "urn:oasis:names:tc:opendocument:xmlns:script:1.0"
    listeners = root.findall(f".//{{{ns_script}}}event-listener")
    assert listeners, "no script:event-listener: nothing invokes the macro"

    # The prefix must RESOLVE. ElementTree expands QNames in element and attribute
    # NAMES but not in attribute VALUES, which is where the event name lives, so the
    # declaration is checked directly -- that is the exact defect being guarded.
    assert 'xmlns:dom="http://www.w3.org/2001/xml-events"' in content, (
        "the dom: prefix is undeclared, so dom:load names nothing"
    )
    ns_xlink = "http://www.w3.org/1999/xlink"
    hrefs = [el.get(f"{{{ns_xlink}}}href", "") for el in listeners]
    events = [el.get(f"{{{ns_script}}}event-name", "") for el in listeners]
    assert any(e == "dom:load" for e in events), events
    target = next((h for h in hrefs if "Standard.Module1." in h), "")
    assert target, hrefs
    routine = target.split("Standard.Module1.")[1].split("?")[0]
    # \b, not `in`: "Sub Document_Open" is a substring of "Sub Document_Opened", so a
    # plain containment check passes for a routine the listener does NOT call.
    # (Measured -- renaming the Sub to Document_Opened survived that version.)
    assert re.search(rf"^\s*Sub\s+{re.escape(routine)}\b", module, re.MULTILINE), (
        f"the listener calls {routine}, which Module1 does not define: {module!r}"
    )


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
def test_a_macro_bearing_document_still_converts(converter, tmp_path: Path):
    """A document carrying a Document_Open Basic macro converts and renders.

    This was test_autoopen_macro_does_not_execute, which asserted the macro's
    sentinel was absent from the host and blamed MacroSecurityLevel /
    DisableMacrosExecution if it was not. It could not detect a regression in
    either, and measurement is the reason to say so rather than an opinion.
    Both on this LibreOffice (24.2.7.2):

      * Removing MacroSecurityLevel, DisableMacrosExecution and OfficeBasic from
        the hardened profile entirely -> the test still passed.
      * soffice --headless --convert-to on this fixture with NO sandbox and macro
        security wide open -- MacroSecurityLevel=0, DisableMacrosExecution=false,
        OfficeBasic=2 -- and with the macro BOUND to document-open by a properly
        namespaced dom:load listener: the sentinel is still never written.
        LibreOffice does not fire document events during a headless conversion, so
        the macro cannot run on this path at any setting.

      * The same macro, INVOKED directly against the same document and profile,
        DOES write it:

            soffice --headless -env:UserInstallation=file://<loose-profile> \\
              "vnd.sun.star.script:Standard.Module1.Document_Open\
?language=Basic&location=document" macro_autoopen.odt
            -> /tmp/clippyshot-macro-pwned WRITTEN

        which is the positive control: the routine is live and would write the
        sentinel if anything called it. Only the EVENT is never delivered. (That
        invocation is not a test -- it writes the sentinel only against an
        already-resident soffice, and a control that depends on another process
        happening to run is worse than none. The structural check above is what
        keeps the fixture armed.)

        (The binding matters: the fixture carried a bare Sub in Module1 that
        nothing called, so an inert result would have proved nothing about the
        hardening. build_malicious_fixtures.py wires the listener now, and the
        result above is from the rebuilt fixture.)

    Its two possible claims are covered by tests that can fail: the profile
    settings in tests/unit/test_libreoffice_profile.py, and host isolation by
    the probe above, which writes for real. What is left here, and is worth
    keeping, is that a macro-bearing document does not break the pipeline.
    """
    src = MALICIOUS / "macro_autoopen.odt"
    if not src.exists():
        pytest.skip("malicious fixture macro_autoopen.odt not built")

    out = tmp_path / "out"
    converter.convert(src, out, ConvertOptions(limits=Limits(timeout_s=60, max_pages=2)))

    render = json.loads((out / "metadata.json").read_text())["render"]
    assert render["page_count_rendered"] >= 1


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
def test_metadata_records_security_context(converter, tmp_path: Path):
    """Every successful conversion records the security context that applied."""
    src = FIXTURES_DIR / "safe" / "fixture.docx"
    if not src.exists():
        pytest.skip("safe fixture not built")
    out = tmp_path / "out"
    converter.convert(src, out, ConvertOptions(limits=Limits()))
    meta = json.loads((out / "metadata.json").read_text())
    assert meta["security"]["macro_security_level"] == 3
    assert meta["security"]["java"] == "disabled"
    assert meta["security"]["network"] == "denied"
