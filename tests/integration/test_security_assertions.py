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
def test_max_pages_truncation_on_spreadsheet(converter, tmp_path: Path):
    src = MALICIOUS / "sleeper.csv"
    if not src.exists():
        pytest.skip("malicious fixture not built")
    out = tmp_path / "out"
    converter.convert(
        src,
        out,
        ConvertOptions(limits=Limits(timeout_s=120, max_pages=1)),
    )
    meta = json.loads((out / "metadata.json").read_text())
    assert meta["render"]["page_count_rendered"] == 1
    assert meta["render"]["truncated"] is True


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
def test_autoopen_macro_does_not_execute(converter, tmp_path: Path):
    """A document with a Document_Open Basic macro that would write to a
    sentinel path must NOT execute the macro under our hardened LO profile
    (MacroSecurityLevel=4, DisableMacrosExecution=true)."""
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
        # Inside the sandbox the macro CANNOT see /tmp on the host (it's
        # in a tmpfs in the sandbox), so this is also a "did anything escape
        # the sandbox" test. The sentinel must not exist on the host either.
        macro_executed = sentinel.exists()
        if macro_executed:
            sentinel.unlink()  # cleanup before asserting so the next run is clean

    assert not macro_executed, (
        "AutoOpen macro executed and wrote sentinel — MacroSecurityLevel=4 "
        "and/or DisableMacrosExecution=true is not effective"
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
