import json
import socket
import threading
from pathlib import Path

import pytest

from clippyshot.converter import ConvertOptions
from clippyshot.limits import Limits
from clippyshot.sandbox.base import SandboxRequest
from clippyshot.sandbox.detect import select_sandbox
from tests.conftest import (
    FIXTURES_DIR,
    needs_bwrap_userns,
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


@needs_bwrap_userns
def test_a_write_to_tmp_inside_the_sandbox_never_reaches_the_host():
    """A file written to /tmp INSIDE the sandbox must not appear on the host.

    With a positive control, because that is the entire difficulty here. The
    macro-document test below asserts the same kind of host path is absent after
    a conversion that never attempts the write at all, so it holds whether or not
    the isolation exists. This one makes the guest actually perform the write and
    proves it succeeded (WROTE in stdout) before asking whether the host saw it.
    """
    sb = select_sandbox()
    # Only the backends that give the CALL its own /tmp can be probed this way:
    # nsjail --tmpfsmount and bwrap --tmpfs. ContainerSandbox runs commands directly
    # inside the enclosing container and shares this process's /tmp by design -- its
    # boundary surrounds the whole worker -- and the nono decorator adds Landlock
    # rules, not a private tmpfs. Probing either would report a supported backend,
    # operating correctly, as an escape.
    #
    # Deliberately narrower than sandboxes_each_call(): that answers "is every call
    # wrapped", which container+nono satisfies while still sharing /tmp.
    if not {"nsjail", "bwrap"} & set(str(sb.name).split("+")):
        pytest.skip(f"{sb.name} does not give each call its own /tmp; nothing to probe")
    sentinel = Path("/tmp/clippyshot-sandbox-tmp-probe")
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
            f"the probe never wrote inside the guest, so this proves nothing: {out!r}"
        )
        assert not sentinel.exists(), (
            f"{sentinel} appeared on the HOST — the sandbox is sharing /tmp"
        )
    finally:
        sentinel.unlink(missing_ok=True)


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
        OfficeBasic=2 -- and with the macro BOUND to document-open by a dom:load
        event listener: the sentinel is still never written. LibreOffice does not
        fire document events during a headless conversion, so the macro cannot
        run on this path at any setting, and the assertion had nothing to observe.

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
