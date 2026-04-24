import io
import json
from pathlib import Path

import pytest

from clippyshot.converter import Converter, ConvertOptions
from clippyshot.errors import DetectionError
from clippyshot.limits import Limits
from clippyshot.types import (
    DetectedType,
    RasterizedPage,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "safe"


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_stub_png() -> bytes:
    """Create a non-blank 1x1 PNG with a colored pixel.

    Uses a non-white, non-black, non-uniform color so the blank detector
    doesn't flag it (phash of a single colored pixel has >1 bit set when
    upscaled to the 32x32 DCT grid, so it's not considered uniform).
    """
    from PIL import Image as _Image

    # A 2x2 checkerboard pattern ensures phash sees real DCT structure.
    img = _Image.new("RGB", (2, 2), (255, 0, 0))
    img.putpixel((1, 0), (0, 0, 255))
    img.putpixel((0, 1), (0, 255, 0))
    img.putpixel((1, 1), (255, 255, 0))
    return _png_bytes(img)


# Tiny non-blank stub PNG used as a page placeholder in converter tests.
_TINY_PNG: bytes = _make_stub_png()


class StubDetector:
    def __init__(self, label: str = "docx"):
        self.label = label

    def detect(self, path, *, max_input_bytes=None):
        return DetectedType(
            label=self.label,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            extension_hint="docx",
            confidence=0.99,
            source="magika",
            agreed_with_extension=True,
        )


class StubRunner:
    def __init__(self, pages: int = 2):
        self.pages = pages
        self.calls = 0

    def convert_to_pdf(self, input_path, output_dir, limits, label="docx"):
        self.calls += 1
        from PIL import Image

        out = Path(output_dir) / "input.pdf"
        imgs = [
            Image.new("RGB", (612, 792), (255, 255, 255)) for _ in range(self.pages)
        ]
        imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=72.0)
        return out


class StubRasterizer:
    name = "stub"

    def __init__(self, pages: int = 2):
        self.pages = pages
        self.last_page_sizes_mm: list[tuple[float, float]] | None = None

    def rasterize(self, pdf_path, out_dir, dpi, max_pages, page_sizes_mm=None):
        self.last_page_sizes_mm = page_sizes_mm
        results = []
        for i in range(1, min(self.pages, max_pages) + 1):
            name = f"page-{i:03d}.png"
            (Path(out_dir) / name).write_bytes(_TINY_PNG)
            results.append(
                RasterizedPage(
                    index=i,
                    path=name,
                    width_px=1,
                    height_px=1,
                    width_mm=215.9,
                    height_mm=279.4,
                )
            )
        return results


def _build_converter(pages: int = 2, runner_pages: int | None = None) -> Converter:
    return Converter(
        detector=StubDetector(),
        runner=StubRunner(pages=runner_pages if runner_pages is not None else pages),
        rasterizer=StubRasterizer(pages=pages),
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )


def test_converter_writes_metadata_and_pngs(tmp_path: Path):
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    conv = _build_converter(pages=3)
    conv.convert(src, out, ConvertOptions(limits=Limits()))

    assert (out / "metadata.json").is_file()
    assert (out / "page-001.png").is_file()
    assert (out / "page-002.png").is_file()
    assert (out / "page-003.png").is_file()

    meta = json.loads((out / "metadata.json").read_text())
    assert meta["render"]["page_count_rendered"] == 3
    assert meta["render"]["truncated"] is False
    assert len(meta["pages"]) == 3
    for p in meta["pages"]:
        assert "phash" in p
        assert "colorhash" in p
        assert "sha256" in p
    # By default, security internals (sandbox backend, AppArmor profiles) are
    # redacted (M-2). Only the conceptual hardening declarations are present.
    assert "sandbox" not in meta["security"]
    assert "apparmor_profile" not in meta["security"]
    assert meta["security"]["macro_security_level"] == 3
    assert meta["security"]["network"] == "denied"
    assert meta["security"]["java"] == "disabled"
    assert meta["input"]["filename"] == "input.docx"
    assert "sha256" in meta["input"]


def test_converter_passes_mediabox_dimensions_to_rasterizer(tmp_path: Path):
    """The converter opens the PDF once and passes the per-page mediabox
    dimensions (in mm) through to the rasterizer, so the rasterizer doesn't
    have to re-open the PDF. This locks in the optimisation."""
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    rasterizer = StubRasterizer(pages=2)
    conv = Converter(
        detector=StubDetector(),
        runner=StubRunner(pages=2),
        rasterizer=rasterizer,
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )
    conv.convert(src, out, ConvertOptions(limits=Limits()))

    # The rasterizer must have received page_sizes_mm (not None).
    assert rasterizer.last_page_sizes_mm is not None
    # StubRunner produces a Pillow-generated PDF at 612x792 pt = Letter, which
    # is 215.9 x 279.4 mm. Confirm the converter computed real values, not
    # placeholders.
    assert len(rasterizer.last_page_sizes_mm) == 2
    for w_mm, h_mm in rasterizer.last_page_sizes_mm:
        assert 215 < w_mm < 217
        assert 279 < h_mm < 280


def test_converter_truncates_at_max_pages(tmp_path: Path):
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    # Stub runner produces 10 pages, but limits cap to 2.
    conv = _build_converter(pages=2, runner_pages=10)
    conv.convert(src, out, ConvertOptions(limits=Limits(max_pages=2)))

    meta = json.loads((out / "metadata.json").read_text())
    assert meta["render"]["page_count_total"] == 10
    assert meta["render"]["page_count_rendered"] == 2
    assert meta["render"]["truncated"] is True


def test_converter_propagates_detection_error(tmp_path: Path):
    src = tmp_path / "input.bin"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()

    class RejectingDetector:
        def detect(self, path, *, max_input_bytes=None):
            raise DetectionError("unsupported_type", "magika=elf")

    conv = Converter(
        detector=RejectingDetector(),
        runner=StubRunner(),
        rasterizer=StubRasterizer(),
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )
    with pytest.raises(DetectionError):
        conv.convert(src, out, ConvertOptions(limits=Limits()))


def test_converter_records_timings_in_metadata(tmp_path: Path):
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    _build_converter(pages=1).convert(src, out, ConvertOptions(limits=Limits()))

    meta = json.loads((out / "metadata.json").read_text())
    timings = meta["render"]["duration_ms"]
    for stage in (
        "detect",
        "soffice",
        "rasterize",
        "hash",
        "hash_original",
        "trim",
        "focus",
        "hash_derivatives",
        "total",
    ):
        assert stage in timings
        assert isinstance(timings[stage], int)
        assert timings[stage] >= 0
    assert timings["hash"] >= timings["hash_original"]
    assert timings["hash"] >= timings["trim"] + timings["focus"]


def test_converter_surfaces_altchunk_warnings_in_metadata(tmp_path: Path):
    class AltChunkRunner(StubRunner):
        def convert_to_pdf(self, input_path, output_dir, limits, label="docx"):
            self.last_altchunks = [
                {
                    "part_name": "/word/afchunk.mht",
                    "content_type": "message/rfc822",
                    "size": 1234,
                }
            ]
            return super().convert_to_pdf(input_path, output_dir, limits, label)

    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    conv = Converter(
        detector=StubDetector(),
        runner=AltChunkRunner(pages=1),
        rasterizer=StubRasterizer(pages=1),
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )

    conv.convert(src, out, ConvertOptions(limits=Limits()))

    meta = json.loads((out / "metadata.json").read_text())
    assert any(
        warning["code"] == "altchunk_present"
        and warning["part_name"] == "/word/afchunk.mht"
        and warning["content_type"] == "message/rfc822"
        and warning["size"] == 1234
        for warning in meta["warnings"]
    )


def test_converter_records_extension_mismatch_warning(tmp_path: Path):
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()

    class DisagreeingDetector:
        def detect(self, path, *, max_input_bytes=None):
            return DetectedType(
                label="docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                extension_hint="pdf",
                confidence=0.99,
                source="magika",
                agreed_with_extension=False,
            )

    conv = Converter(
        detector=DisagreeingDetector(),
        runner=StubRunner(pages=1),
        rasterizer=StubRasterizer(pages=1),
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )
    conv.convert(src, out, ConvertOptions(limits=Limits()))

    meta = json.loads((out / "metadata.json").read_text())
    assert any(w.get("code") == "extension_mismatch" for w in meta["warnings"])


def test_converter_skips_blank_pages_when_skip_blanks_true(tmp_path: Path):
    """If the rasterizer produces some blank PNGs, the converter should drop them
    from the metadata pages list (but report the count and indices in render)."""
    from PIL import Image

    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()

    class BlanksMixedRasterizer:
        name = "stub-blanks"

        def rasterize(self, pdf_path, out_dir, dpi, max_pages, page_sizes_mm=None):
            results = []
            # Page 1: white (blank)
            white = Image.new("RGB", (100, 100), (255, 255, 255))
            (Path(out_dir) / "page-001.png").write_bytes(_png_bytes(white))
            # Page 2: red (not blank)
            red = Image.new("RGB", (100, 100), (255, 0, 0))
            (Path(out_dir) / "page-002.png").write_bytes(_png_bytes(red))
            # Page 3: white (blank)
            (Path(out_dir) / "page-003.png").write_bytes(_png_bytes(white))
            for i in (1, 2, 3):
                results.append(
                    RasterizedPage(
                        index=i,
                        path=f"page-{i:03d}.png",
                        width_px=100,
                        height_px=100,
                        width_mm=10,
                        height_mm=10,
                    )
                )
            return results

    conv = Converter(
        detector=StubDetector(),
        runner=StubRunner(pages=3),
        rasterizer=BlanksMixedRasterizer(),
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )
    conv.convert(src, out, ConvertOptions(limits=Limits(skip_blanks=True)))
    meta = json.loads((out / "metadata.json").read_text())
    assert meta["render"]["blank_pages_skipped"] == 2
    assert meta["render"]["blank_pages"] == [1, 3]
    assert meta["render"]["page_count_rendered"] == 1
    # The non-blank page (page 2) is the only one in the records, with original index.
    assert len(meta["pages"]) == 1
    assert meta["pages"][0]["index"] == 2
    # The blank PNGs should be deleted from disk.
    assert not (out / "page-001.png").exists()
    assert (out / "page-002.png").exists()
    assert not (out / "page-003.png").exists()


def test_converter_keeps_blank_pages_when_skip_blanks_false(tmp_path: Path):
    """With skip_blanks=False, blank pages stay in the output but are still flagged."""
    from PIL import Image

    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()

    class OneBlankRasterizer:
        name = "stub-oneblank"

        def rasterize(self, pdf_path, out_dir, dpi, max_pages, page_sizes_mm=None):
            white = Image.new("RGB", (100, 100), (255, 255, 255))
            (Path(out_dir) / "page-001.png").write_bytes(_png_bytes(white))
            return [
                RasterizedPage(
                    index=1,
                    path="page-001.png",
                    width_px=100,
                    height_px=100,
                    width_mm=10,
                    height_mm=10,
                )
            ]

    conv = Converter(
        detector=StubDetector(),
        runner=StubRunner(pages=1),
        rasterizer=OneBlankRasterizer(),
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )
    conv.convert(src, out, ConvertOptions(limits=Limits(skip_blanks=False)))
    meta = json.loads((out / "metadata.json").read_text())
    assert meta["render"]["blank_pages_skipped"] == 0
    assert meta["render"]["blank_pages"] == [1]  # still reported, just not removed
    assert meta["render"]["page_count_rendered"] == 1
    assert (out / "page-001.png").exists()
    assert meta["pages"][0]["is_blank"] is True


def test_converter_adds_focused_derivative_for_spreadsheets(tmp_path: Path):
    from PIL import Image, ImageDraw

    src = tmp_path / "input.xlsb"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()

    class SpreadsheetDetector:
        def detect(self, path, *, max_input_bytes=None):
            return DetectedType(
                label="xlsb",
                mime="application/vnd.ms-excel.sheet.binary.macroEnabled.12",
                extension_hint="xlsb",
                confidence=0.99,
                source="magika",
                agreed_with_extension=True,
            )

    class SpreadsheetRasterizer:
        name = "stub-sheet"

        def rasterize(self, pdf_path, out_dir, dpi, max_pages, page_sizes_mm=None):
            img = Image.new("RGB", (240, 240), (255, 255, 255))
            draw = ImageDraw.Draw(img)
            draw.rectangle((80, 70, 170, 150), fill=(0, 64, 160))
            (Path(out_dir) / "page-001.png").write_bytes(_png_bytes(img))
            return [
                RasterizedPage(
                    index=1,
                    path="page-001.png",
                    width_px=240,
                    height_px=240,
                    width_mm=10,
                    height_mm=10,
                )
            ]

    conv = Converter(
        detector=SpreadsheetDetector(),
        runner=StubRunner(pages=1),
        rasterizer=SpreadsheetRasterizer(),
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )
    conv.convert(src, out, ConvertOptions(limits=Limits()))

    meta = json.loads((out / "metadata.json").read_text())
    page = meta["pages"][0]
    assert "focused" in page
    assert page["focused"]["file"] == "page-001-focused.png"
    assert (out / "page-001.png").exists()
    assert (out / "page-001-focused.png").exists()


def test_security_block_redacted_by_default(tmp_path: Path):
    """M-2: default metadata must NOT include sandbox backend or AppArmor names."""
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    _build_converter(pages=1).convert(src, out, ConvertOptions(limits=Limits()))
    meta = json.loads((out / "metadata.json").read_text())
    sec = meta["security"]
    # Fingerprinting fields must be absent.
    assert "sandbox" not in sec
    assert "apparmor_profile" not in sec
    assert "runtime_apparmor_profile" not in sec
    assert "soffice_apparmor_profile" not in sec
    # Conceptual hardening declarations must be present.
    assert sec["network"] == "denied"
    assert sec["java"] == "disabled"
    assert sec["macros"] == "disabled"


def test_security_block_disclosed_when_opt_in(tmp_path: Path):
    """M-2: when disclose_security_internals=True, the full block is written."""
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    conv = _build_converter(pages=1)
    conv.convert(
        src, out, ConvertOptions(limits=Limits(disclose_security_internals=True))
    )
    meta = json.loads((out / "metadata.json").read_text())
    sec = meta["security"]
    # Internal fields must be present when explicitly opted in.
    assert "sandbox" in sec
    assert sec["sandbox"] == "bwrap"
    assert "runtime_apparmor_profile" in sec
    assert "soffice_apparmor_profile" in sec


def test_pdf_intermediate_dir_cleaned_on_rasterize_failure(tmp_path: Path):
    """M-7: if the rasterizer raises, the _pdf intermediate dir must still be cleaned."""
    src = tmp_path / "input.docx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()

    class FailingRasterizer:
        name = "failing"

        def rasterize(self, pdf_path, out_dir, dpi, max_pages, page_sizes_mm=None):
            from clippyshot.errors import RasterizeError

            raise RasterizeError("synthetic failure")

    conv = Converter(
        detector=StubDetector(),
        runner=StubRunner(pages=1),
        rasterizer=FailingRasterizer(),
        sandbox_backend="bwrap",
        apparmor_profile="unconfined",
    )
    from clippyshot.errors import ConversionError

    with pytest.raises(ConversionError):
        conv.convert(src, out, ConvertOptions(limits=Limits()))
    # _pdf dir must be gone regardless of the rasterize failure.
    assert not (out / "_pdf").exists()
