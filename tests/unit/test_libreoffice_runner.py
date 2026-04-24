from pathlib import Path

import pytest

from clippyshot.libreoffice.altchunk import AltChunk
from clippyshot.libreoffice.runner import LibreOfficeRunner
from clippyshot.limits import Limits
from clippyshot.sandbox.base import SandboxRequest
from clippyshot.types import SandboxResult


class FakeSandbox:
    name = "fake"

    def __init__(self) -> None:
        self.last_request: SandboxRequest | None = None

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.last_request = request
        # Pretend soffice produced a PDF at the expected output path.
        out_dir_host = next(
            m.host_path
            for m in request.rw_mounts
            if m.sandbox_path == Path("/sandbox/out")
        )
        # Use the input filename's stem to derive the output PDF name, matching
        # how soffice --convert-to pdf names its outputs.
        stage_in = next(
            m.host_path
            for m in request.ro_mounts
            if m.sandbox_path == Path("/sandbox/in")
        )
        # The staged input has the same name as the original.
        in_files = [p for p in stage_in.iterdir() if p.is_file()]
        assert in_files, "FakeSandbox expected one staged input file"
        stem = in_files[0].stem
        (out_dir_host / f"{stem}.pdf").write_bytes(b"%PDF-1.4\nfake\n%%EOF\n")
        return SandboxResult(
            exit_code=0, stdout=b"", stderr=b"", duration_ms=10, killed=False
        )

    def smoketest(self) -> SandboxResult:
        return SandboxResult(0, b"", b"", 1, False)


class RewriteFallbackSandbox:
    name = "fake-rewrite-fallback"

    def __init__(self) -> None:
        self.requests: list[SandboxRequest] = []

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.requests.append(request)
        stage_in = next(
            m.host_path
            for m in request.ro_mounts
            if m.sandbox_path == Path("/sandbox/in")
        )
        requested_name = Path(request.argv[-1]).name
        staged = stage_in / requested_name
        assert staged.is_file()
        if staged.suffix == ".rtf":
            return SandboxResult(
                exit_code=1,
                stdout=b"",
                stderr=b"Error: source file could not be loaded\n",
                duration_ms=5,
                killed=False,
            )
        out_dir_host = next(
            m.host_path
            for m in request.rw_mounts
            if m.sandbox_path == Path("/sandbox/out")
        )
        (out_dir_host / f"{staged.stem}.pdf").write_bytes(b"%PDF-1.4\nfake\n%%EOF\n")
        return SandboxResult(
            exit_code=0, stdout=b"", stderr=b"", duration_ms=10, killed=False
        )

    def smoketest(self) -> SandboxResult:
        return SandboxResult(0, b"", b"", 1, False)


class HtmlTranslationFallbackSandbox:
    name = "fake-html-translation-fallback"

    def __init__(self) -> None:
        self.requests: list[SandboxRequest] = []

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.requests.append(request)
        requested_name = Path(request.argv[-1]).name
        if requested_name.endswith(".html"):
            return SandboxResult(
                exit_code=1,
                stdout=b"",
                stderr=b"translated html failed\n",
                duration_ms=5,
                killed=False,
            )
        return SandboxResult(
            exit_code=0,
            stdout=b"",
            stderr=b"",
            duration_ms=5,
            killed=False,
        )

    def smoketest(self) -> SandboxResult:
        return SandboxResult(0, b"", b"", 1, False)


class FailingSandbox:
    name = "fake-fail"

    def __init__(self, killed: bool = False, exit_code: int = 1):
        self._killed = killed
        self._exit_code = exit_code

    def run(self, request: SandboxRequest) -> SandboxResult:
        return SandboxResult(
            exit_code=self._exit_code,
            stdout=b"",
            stderr=b"soffice exploded",
            duration_ms=5,
            killed=self._killed,
        )

    def smoketest(self) -> SandboxResult:
        return SandboxResult(0, b"", b"", 1, False)


def test_runner_invokes_sandbox_with_hardened_flags(tmp_path: Path):
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    sb = FakeSandbox()
    runner = LibreOfficeRunner(sandbox=sb)

    pdf = runner.convert_to_pdf(src, out, Limits(), "docx")

    assert pdf == out / "input.pdf"
    req = sb.last_request
    assert req is not None
    assert "--headless" in req.argv
    assert "--safe-mode" in req.argv
    assert "--norestore" in req.argv
    assert "--nofirststartwizard" in req.argv
    assert "--nocrashreport" in req.argv
    assert "--nodefault" in req.argv
    assert "--nologo" in req.argv
    assert any(a.startswith("-env:UserInstallation=file://") for a in req.argv)
    assert "--convert-to" in req.argv
    cti = req.argv.index("--convert-to")
    assert req.argv[cti + 1] == "pdf:writer_pdf_Export"
    assert "--outdir" in req.argv


def test_runner_passes_input_readonly_and_output_writable(tmp_path: Path):
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    sb = FakeSandbox()
    LibreOfficeRunner(sandbox=sb).convert_to_pdf(src, out, Limits(), "docx")

    req = sb.last_request
    ro_paths = {m.sandbox_path for m in req.ro_mounts}
    rw_paths = {m.sandbox_path for m in req.rw_mounts}
    assert Path("/sandbox/in") in ro_paths
    assert Path("/sandbox/out") in rw_paths


def test_runner_raises_libreoffice_error_on_nonzero_exit(tmp_path: Path):
    from clippyshot.errors import LibreOfficeError

    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    runner = LibreOfficeRunner(sandbox=FailingSandbox(exit_code=77))
    with pytest.raises(LibreOfficeError) as ei:
        runner.convert_to_pdf(src, out, Limits(), "docx")
    assert "77" in str(ei.value) or "exploded" in str(ei.value)


def test_runner_raises_on_killed(tmp_path: Path):
    from clippyshot.errors import LibreOfficeError

    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    runner = LibreOfficeRunner(sandbox=FailingSandbox(killed=True))
    with pytest.raises(LibreOfficeError) as ei:
        runner.convert_to_pdf(src, out, Limits(), "docx")
    assert "kill" in str(ei.value).lower() or "timeout" in str(ei.value).lower()


def test_runner_raises_when_input_missing(tmp_path: Path):
    from clippyshot.errors import LibreOfficeError

    out = tmp_path / "out"
    out.mkdir()
    runner = LibreOfficeRunner(sandbox=FakeSandbox())
    with pytest.raises(LibreOfficeError):
        runner.convert_to_pdf(tmp_path / "does_not_exist.docx", out, Limits(), "docx")


def test_runner_falls_back_to_original_extension_when_rewrite_cannot_be_loaded(
    tmp_path: Path,
):
    src = tmp_path / "secagodzx.doc"
    src.write_bytes(b"{\\rtf1\n")
    out = tmp_path / "out"
    out.mkdir()
    sb = RewriteFallbackSandbox()

    pdf = LibreOfficeRunner(sandbox=sb).convert_to_pdf(src, out, Limits(), "rtf")

    assert pdf == out / "secagodzx.pdf"
    assert len(sb.requests) == 2
    assert Path(sb.requests[0].argv[-1]).name == "secagodzx.rtf"
    assert Path(sb.requests[1].argv[-1]).name == "secagodzx.doc"


def test_runner_reroutes_single_html_altchunk(tmp_path: Path, monkeypatch):
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    sb = FakeSandbox()
    runner = LibreOfficeRunner(sandbox=sb)

    monkeypatch.setattr(
        "clippyshot.libreoffice.altchunk.inspect_altchunks",
        lambda path: [
            AltChunk(
                part_name="/word/afchunk.html",
                content_type="text/html",
                size=20,
                data=b"<html>payload</html>",
            )
        ],
    )

    pdf = runner.convert_to_pdf(src, out, Limits(), "docx")

    assert pdf == out / "input.pdf"
    assert sb.last_request is not None
    assert Path(sb.last_request.argv[-1]).name == "input.html"
    assert runner.last_altchunks == [
        {"part_name": "/word/afchunk.html", "content_type": "text/html", "size": 20}
    ]


def test_runner_rejects_unsupported_altchunk_payloads(tmp_path: Path, monkeypatch):
    from clippyshot.errors import LibreOfficeError

    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    runner = LibreOfficeRunner(sandbox=FakeSandbox())

    monkeypatch.setattr(
        "clippyshot.libreoffice.altchunk.inspect_altchunks",
        lambda path: [
            AltChunk(
                part_name="/word/afchunk.doc",
                content_type="application/msword",
                size=7,
                data=b"payload",
            )
        ],
    )

    with pytest.raises(LibreOfficeError, match="unsupported Word altChunk payload"):
        runner.convert_to_pdf(src, out, Limits(), "docx")


def test_runner_rejects_multiple_renderable_altchunks(tmp_path: Path, monkeypatch):
    from clippyshot.errors import LibreOfficeError

    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    runner = LibreOfficeRunner(sandbox=FakeSandbox())

    monkeypatch.setattr(
        "clippyshot.libreoffice.altchunk.inspect_altchunks",
        lambda path: [
            AltChunk(
                part_name="/word/afchunk1.html",
                content_type="text/html",
                size=16,
                data=b"<html>one</html>",
            ),
            AltChunk(
                part_name="/word/afchunk2.mht",
                content_type="message/rfc822",
                size=12,
                data=b"mime payload",
            ),
        ],
    )

    with pytest.raises(
        LibreOfficeError, match="multiple renderable Word altChunk payloads"
    ):
        runner.convert_to_pdf(src, out, Limits(), "docx")


def test_runner_does_not_fall_back_to_raw_input_after_html_altchunk_reroute(
    tmp_path: Path, monkeypatch
):
    from clippyshot.errors import LibreOfficeError

    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    sb = HtmlTranslationFallbackSandbox()
    runner = LibreOfficeRunner(sandbox=sb)

    monkeypatch.setattr(
        "clippyshot.libreoffice.altchunk.inspect_altchunks",
        lambda path: [
            AltChunk(
                part_name="/word/afchunk.html",
                content_type="text/html",
                size=20,
                data=b"<html>payload</html>",
            )
        ],
    )

    with pytest.raises(LibreOfficeError, match="translated html failed"):
        runner.convert_to_pdf(src, out, Limits(), "docx")

    assert [Path(req.argv[-1]).name for req in sb.requests] == ["input.html"]
