from clippyshot.types import (
    DetectedType,
    PageHashes,
    RasterizedPage,
    SandboxResult,
)


def test_detected_type_carries_resolution_source():
    dt = DetectedType(
        label="docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        extension_hint="docx",
        confidence=0.999,
        source="magika",
        agreed_with_extension=True,
    )
    assert dt.label == "docx"
    assert dt.source == "magika"
    assert dt.agreed_with_extension is True


def test_page_hashes_serializes_to_dict():
    h = PageHashes(phash="abc", colorhash="def", sha256="01" * 32)
    d = h.to_dict()
    assert d == {"phash": "abc", "colorhash": "def", "sha256": "01" * 32, "is_blank": False}


def test_rasterized_page_holds_dimensions():
    p = RasterizedPage(
        index=1,
        path="page-001.png",
        width_px=1275,
        height_px=1650,
        width_mm=215.9,
        height_mm=279.4,
    )
    assert p.index == 1
    assert p.width_mm == 215.9


def test_sandbox_result_records_exit():
    r = SandboxResult(exit_code=0, stdout=b"hi", stderr=b"", duration_ms=42, killed=False)
    assert r.success
    r2 = SandboxResult(exit_code=137, stdout=b"", stderr=b"", duration_ms=60000, killed=True)
    assert not r2.success
    assert r2.killed


def test_conversion_error_wires_into_pythons_cause_chain():
    from clippyshot.errors import ConversionError

    inner = ValueError("inner failure")
    err = ConversionError("wrap", cause=inner)
    assert err.cause is inner
    assert err.__cause__ is inner

    bare = ConversionError("no cause")
    assert bare.cause is None
    assert bare.__cause__ is None
