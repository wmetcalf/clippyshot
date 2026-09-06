import json
import socket
import threading
from pathlib import Path

import pytest

from clippyshot.converter import ConvertOptions
from clippyshot.limits import Limits
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
def test_ole_link_does_not_read_outside_sandbox(converter, tmp_path: Path):
    src = MALICIOUS / "ole_link.rtf"
    if not src.exists():
        pytest.skip("malicious fixture not built")
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("THIS_SHOULD_NEVER_BE_READ")
    out = tmp_path / "out"
    converter.convert(
        src,
        out,
        ConvertOptions(limits=Limits(timeout_s=60, max_pages=2)),
    )
    rendered = (out / "page-001.png").read_bytes()
    assert b"THIS_SHOULD_NEVER_BE_READ" not in rendered


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


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
def test_macro_document_writes_nothing_to_the_host(converter, tmp_path: Path):
    """Converting a document whose Basic macro writes /tmp/clippyshot-macro-pwned
    must leave nothing on the HOST filesystem.

    Named for what it can actually prove. It used to be called
    test_autoopen_macro_does_not_execute and its failure message blamed
    MacroSecurityLevel / DisableMacrosExecution, neither of which it can
    detect a regression in. Two measurements, both on this LibreOffice
    (24.2.7.2):

      * Removing MacroSecurityLevel, DisableMacrosExecution and OfficeBasic from
        the hardened profile entirely -> this test still passed.
      * Running soffice --headless --convert-to on the same fixture with a
        DEFAULT profile and NO sandbox -> the sentinel is still never written.
        LibreOffice does not fire Document_Open during a headless conversion,
        so macro execution is not reachable on this path at all.

    On top of that the sandbox mounts /tmp as a tmpfs (bwrap --tmpfs /tmp), so a
    sentinel written inside it could not reach the host anyway.

    What remains, and is worth keeping, is the host-isolation claim: whatever
    LibreOffice does with this document, nothing appears at that path outside
    the sandbox. The profile settings themselves are asserted directly, and
    falsifiably, in tests/unit/test_libreoffice_profile.py."""
    src = MALICIOUS / "macro_autoopen.odt"
    if not src.exists():
        pytest.skip("malicious fixture macro_autoopen.odt not built")

    # Sentinel path the macro would create.
    sentinel = Path("/tmp/clippyshot-macro-pwned")
    # Pre-clean any leftover from a prior run.
    if sentinel.exists():
        sentinel.unlink()

    out = tmp_path / "out"
    try:
        converter.convert(
            src,
            out,
            ConvertOptions(limits=Limits(timeout_s=60, max_pages=2)),
        )
    finally:
        # The sandbox gives the guest its own /tmp, so this is the
        # escape check: the sentinel must not exist on the host.
        wrote_to_host = sentinel.exists()
        if wrote_to_host:
            sentinel.unlink()  # cleanup before asserting so the next run is clean

    assert not wrote_to_host, (
        "the conversion wrote /tmp/clippyshot-macro-pwned on the HOST — "
        "something escaped the sandbox, whose /tmp is a tmpfs"
    )


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
