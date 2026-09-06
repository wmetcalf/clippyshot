"""Integration: the WarmOCR helper actually OCRs via tesserocr, and its output
matches a direct tesserocr call (protocol introduces no corruption).

Runs in-image (where TESSDATA_PREFIX + eng/Latin tessdata exist). Marked
``integration`` so the default unit suite (which lacks tessdata configuration)
skips it.
"""
import pytest

pytestmark = pytest.mark.integration

WORDS = "the quick brown fox jumps over the lazy dog invoice total amount due"


@pytest.fixture
def text_png(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1100, 200), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    d.text((20, 60), WORDS, fill="black", font=font)
    p = tmp_path / "page-001.png"
    img.save(p)
    return p


def test_warm_helper_reads_known_text(text_png):
    from clippyshot.ocr_warm import WarmOCR

    srv = WarmOCR()
    srv.start()
    try:
        assert srv.is_ready()
        result = srv.ocr(text_png, lang="eng", psm=3, timeout_s=30)
    finally:
        srv.stop()
    low = result.text.lower()
    # Recognition isn't perfect, but the distinctive words should be present.
    assert "quick" in low and "brown" in low and "invoice" in low
    assert result.char_count == len(result.text)


def test_warm_helper_matches_direct_tesserocr(text_png):
    """The helper's pipe protocol must not alter the OCR output: helper text ==
    a direct in-process tesserocr call (same engine, same image)."""
    from tesserocr import PSM, PyTessBaseAPI

    from clippyshot.ocr_warm import WarmOCR, tessdata_dir

    # Resolve tessdata the way the helper does, rather than assuming the caller's
    # environment sets TESSDATA_PREFIX. A bare PyTessBaseAPI() fails with
    # "invalid tessdata path: ./" on any host that does not, which fails this
    # test on its own CONTROL arm and says nothing about the helper.
    api = PyTessBaseAPI(lang="eng", psm=PSM.AUTO, path=tessdata_dir() or "")
    api.SetVariable("user_defined_dpi", "150")
    api.SetImageFile(str(text_png))
    direct = (api.GetUTF8Text() or "").rstrip("\n")
    api.End()

    srv = WarmOCR()
    srv.start()
    try:
        warm = srv.ocr(text_png, lang="eng", psm=3, timeout_s=30).text
    finally:
        srv.stop()
    assert warm == direct
