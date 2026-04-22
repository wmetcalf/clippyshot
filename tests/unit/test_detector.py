from pathlib import Path

import pytest

from clippyshot.detector import Detector
from clippyshot.errors import DetectionError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "safe"


@pytest.fixture(scope="module")
def detector() -> Detector:
    return Detector()


def test_detects_real_docx(detector: Detector):
    dt = detector.detect(FIXTURES / "tiny.docx")
    assert dt.label == "docx"
    assert "wordprocessingml" in dt.mime
    assert dt.source == "magika"
    assert dt.agreed_with_extension is True


def test_detects_csv_via_extension_fallback(detector: Detector):
    dt = detector.detect(FIXTURES / "tiny.csv")
    assert dt.label == "csv"


def test_detects_txt(detector: Detector):
    dt = detector.detect(FIXTURES / "tiny.txt")
    assert dt.label == "txt"


def test_spoofed_docx_too_short_accepted_with_warning(detector: Detector):
    """spoofed.docx is 48 bytes — too short for Magika to identify confidently.
    Magika returns 'unknown'. Since the extension .docx IS in the allowlist,
    we accept it with a 'magika_unrecognized_content' warning and let LO try.
    (LO will likely fail to render the PDF bytes as a docx, but that's a
    conversion failure, not a detection rejection.)"""
    dt = detector.detect(FIXTURES / "spoofed.docx")
    assert dt.label == "docx"
    assert dt.source == "extension"
    assert dt.confidence == 0.0
    assert "magika_unrecognized_content" in dt.warnings


def test_oversized_input_is_rejected(detector: Detector, tmp_path: Path):
    big = tmp_path / "huge.txt"
    big.write_bytes(b"x" * 1024)
    with pytest.raises(DetectionError) as ei:
        detector.detect(big, max_input_bytes=512)
    assert ei.value.reason == "input_too_large"


def test_real_pdf_is_rejected_as_unsupported_type(detector: Detector, tmp_path: Path):
    """A real PDF (large enough for Magika to confidently identify) should be
    rejected with reason='unsupported_type', not 'magika_unknown_extension_mismatch'."""
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"%dummy content " * 50 + b"\n%%EOF\n")
    with pytest.raises(DetectionError) as ei:
        detector.detect(pdf)
    assert ei.value.reason == "unsupported_type"
    assert "pdf" in ei.value.detail


def test_docm_extension_is_accepted_with_macro_warning():
    """A .docm file (which Magika labels as 'docx' since the structure is
    identical to a regular .docx) should be accepted with agreed_with_extension
    True. The macro_enabled_format warning is added by the converter, not the
    detector — this test only verifies the detector accepts the file.
    """
    import tempfile

    detector = Detector()
    # Reuse the existing tiny.docx fixture but probe under a .docm name.
    # Copy it to a tmp_path with the .docm extension and detect.
    # (We can't easily craft a real .docm without soffice; the structural
    # identity to .docx means Magika treats them the same.)
    src = FIXTURES / "tiny.docx"
    with tempfile.NamedTemporaryFile(suffix=".docm", delete=False) as f:
        f.write(src.read_bytes())
        path = Path(f.name)
    try:
        dt = detector.detect(path)
        assert dt.label == "docx"  # canonical label
        assert dt.extension_hint == "docm"
        assert dt.agreed_with_extension is True  # via the new allowlist entry
    finally:
        path.unlink()


def test_macro_enabled_extensions_constant_includes_expected():
    from clippyshot.detector import MACRO_ENABLED_EXTENSIONS

    expected = {"docm", "dotm", "xlsm", "xltm", "xlam", "xlsb", "xla", "pptm", "ppsm", "potm", "ppam", "ppa"}
    assert MACRO_ENABLED_EXTENSIONS == expected


# -----------------------------------------------------------------------------
# H-3: zip/xml structural sanity checks
# -----------------------------------------------------------------------------


def test_zip_bomb_with_docx_extension_is_rejected(tmp_path: Path):
    """A highly compressible zip with a .docx extension should fail the
    structural check and be rejected (H-3)."""
    import zipfile

    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 10 MiB of 'A' compresses down to a few KiB.
        zf.writestr("payload.bin", b"A" * (10 * 1024 * 1024))
    detector = Detector()
    with pytest.raises(DetectionError) as ei:
        detector.detect(bomb)
    # The exact reason depends on whether Magika labels this as "zip" (common)
    # or as something more specific. Both the OOXML-check failure and
    # "unsupported_type" are acceptable — the key is that it's rejected.
    assert ei.value.reason in {
        "magika_unknown_extension_mismatch",
        "unsupported_type",
    }


def test_zip_without_content_types_xml_is_rejected(tmp_path: Path):
    """A zip that doesn't contain [Content_Types].xml is not a valid OOXML
    container (H-3)."""
    import zipfile

    fake = tmp_path / "fake.docx"
    with zipfile.ZipFile(fake, "w") as zf:
        zf.writestr("hello.txt", b"not a docx")
    detector = Detector()
    with pytest.raises(DetectionError):
        detector.detect(fake)


def test_xml_with_many_entities_is_rejected(tmp_path: Path):
    """An XML with > 64 entity declarations should be rejected as a possible
    billion-laughs bomb (H-3)."""
    bomb = tmp_path / "bomb.fodt"  # fodt is in our extension allowlist
    entities = "\n".join(f'<!ENTITY e{i} "x">' for i in range(100))
    bomb.write_text(
        f'<?xml version="1.0"?><!DOCTYPE root [\n{entities}\n]><root/>'
    )
    detector = Detector()
    with pytest.raises(DetectionError):
        detector.detect(bomb)


def test_legitimate_minimal_docx_still_passes_structural_check():
    """The existing tiny.docx fixture must still pass the structural check —
    regression guard for H-3."""
    detector = Detector()
    dt = detector.detect(FIXTURES / "tiny.docx")
    assert dt.label == "docx"


def test_looks_like_ooxml_on_tiny_docx_fixture(capsys):
    """Directly exercise _looks_like_ooxml and surface the actual compression
    ratio so we have a data point for the 100:1 threshold."""
    import zipfile

    from clippyshot.detector import _looks_like_ooxml

    assert _looks_like_ooxml(FIXTURES / "tiny.docx") is True
    with zipfile.ZipFile(FIXTURES / "tiny.docx") as zf:
        entries = zf.namelist()
        c = sum(i.compress_size for i in zf.infolist())
        u = sum(i.file_size for i in zf.infolist())
    ratio = (u / c) if c > 0 else 0.0
    # Print for the test log so the implementer can see the real ratio.
    print(
        f"\n[fixture] tiny.docx entries={len(entries)} compressed={c} "
        f"uncompressed={u} ratio={ratio:.2f}"
    )
    # Real docx ratios are well under 20:1; we want confidence the 100:1
    # ceiling is comfortably above that.
    assert ratio < 20.0


def test_generic_labels_no_longer_includes_txtascii():
    """Regression guard for L-6: 'txtascii' was never a real Magika label."""
    from clippyshot.detector import _GENERIC_LABELS

    assert "txtascii" not in _GENERIC_LABELS
    assert _GENERIC_LABELS == {"zip", "xml"}


def test_looks_like_safe_xml_rejects_many_entities(tmp_path: Path):
    from clippyshot.detector import _looks_like_safe_xml

    safe = tmp_path / "safe.xml"
    safe.write_text('<?xml version="1.0"?><root/>')
    assert _looks_like_safe_xml(safe) is True

    bomb = tmp_path / "bomb.xml"
    entities = "\n".join(f'<!ENTITY e{i} "x">' for i in range(65))
    bomb.write_text(f'<?xml version="1.0"?><!DOCTYPE root [{entities}]><root/>')
    assert _looks_like_safe_xml(bomb) is False


def test_looks_like_safe_xml_rejects_split_entities(tmp_path: Path):
    from clippyshot.detector import _looks_like_safe_xml

    bomb = tmp_path / "split_bomb.xml"

    entities = [f'<!ENTITY e{i} "x">' for i in range(64)]
    content_part1 = f'<?xml version="1.0"?><!DOCTYPE root [{"".join(entities)}'

    padding_needed = 8192 - len(content_part1) - 5
    content_part1 += " " * padding_needed

    bomb.write_bytes(content_part1.encode() + b"<!ENT" + b'ITY e64 "x">]><root/>')

    assert _looks_like_safe_xml(bomb) is False


@pytest.mark.parametrize(
    ("fixture_name", "suffix", "expected_label"),
    [
        ("fixture.xlsx", ".xlam", "xlsx"),
        ("fixture.xls", ".xla", "xls"),
        ("fixture.pptx", ".ppam", "pptx"),
        ("fixture.ppt", ".ppa", "ppt"),
    ],
)
def test_addin_extensions_are_accepted_via_family_fallback(fixture_name: str, suffix: str, expected_label: str):
    import tempfile

    detector = Detector()
    src = FIXTURES / fixture_name
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(src.read_bytes())
        path = Path(f.name)
    try:
        dt = detector.detect(path)
        assert dt.label == expected_label
        assert dt.extension_hint == suffix.lstrip('.')
        assert dt.agreed_with_extension is True
    finally:
        path.unlink()
