from pathlib import Path

from PIL import Image

from clippyshot.hasher import hash_png_bytes
from clippyshot.types import PageHashes

CANNED = Path(__file__).parent / "canned_pngs"


def _read(name: str) -> bytes:
    return (CANNED / name).read_bytes()


def test_hash_png_bytes_returns_page_hashes_object():
    h = hash_png_bytes(_read("red_8x8.png"))
    assert isinstance(h, PageHashes)
    assert len(h.sha256) == 64
    assert h.phash != ""
    assert h.colorhash != ""


def test_hash_is_deterministic_for_same_bytes():
    a = hash_png_bytes(_read("red_8x8.png"))
    b = hash_png_bytes(_read("red_8x8.png"))
    assert a == b


def test_hash_differs_for_different_images():
    red = hash_png_bytes(_read("red_8x8.png"))
    blue = hash_png_bytes(_read("blue_8x8.png"))
    half_split = hash_png_bytes(_read("half_split_8x8.png"))

    # sha256 catches any byte-level difference.
    assert red.sha256 != blue.sha256
    assert red.sha256 != half_split.sha256

    # colorhash catches color-signature differences.
    assert red.colorhash != blue.colorhash

    # phash catches structural (DCT) differences. Solid-color images of equal
    # size collapse to identical low-frequency responses, so red vs blue won't
    # differ on phash — the half-split fixture (top half black, bottom half
    # white) has a real low-frequency component to exercise phash divergence.
    assert red.phash != half_split.phash


def test_hash_known_values_for_red_fixture():
    h = hash_png_bytes(_read("red_8x8.png"))
    assert h.phash == "8000000000000000"
    assert h.colorhash == "00000000f00000"
    assert h.sha256 == "efd697c3369b86b0b540c0bded797ca813a3984c1a95d7d16644744e252840a2"


def test_solid_white_image_is_marked_blank():
    import io
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (255, 255, 255)).save(buf, "PNG")
    h = hash_png_bytes(buf.getvalue())
    assert h.is_blank is True


def test_solid_red_image_is_not_blank():
    h = hash_png_bytes(_read("red_8x8.png"))
    assert h.is_blank is False


def test_mostly_white_image_with_sparse_content_is_not_blank():
    """A page that is overwhelmingly white but carries any real pixel content
    (a watermark, a page number, grid lines, a few characters) is NOT blank.

    Even 10 stray black pixels on a 200x200 canvas produce pHash structure
    (many bits set) via the DCT, which the detector correctly reads as
    'this image has content'. This behaviour is important for real-world
    inputs like sparse spreadsheet pages where a few rows of numbers on
    an otherwise white page would otherwise get misclassified as blank.
    """
    import io
    img = Image.new("RGB", (200, 200), (255, 255, 255))  # 40000 px
    # Add 10 black pixels.
    for i in range(10):
        img.putpixel((i, 0), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    h = hash_png_bytes(buf.getvalue())
    assert h.is_blank is False
    # Sanity check: the phash should have many bits set (the sparse content
    # produces real DCT structure), not the uniform-image signature.
    assert bin(int(h.phash, 16)).count("1") > 1


def test_50_percent_black_image_is_not_blank():
    h = hash_png_bytes(_read("half_split_8x8.png"))
    assert h.is_blank is False


def test_solid_black_image_is_marked_blank():
    import io
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (0, 0, 0)).save(buf, "PNG")
    h = hash_png_bytes(buf.getvalue())
    assert h.is_blank is True


def test_solid_green_image_is_not_blank():
    """A solid colorful page is uniform but NOT blank — it carries visible color."""
    import io
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (0, 255, 0)).save(buf, "PNG")
    h = hash_png_bytes(buf.getvalue())
    assert h.is_blank is False



def _blank_page(size: tuple[int, int]) -> bytes:
    import io
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_extreme_aspect_page_with_content_is_not_blank():
    """A very tall, very narrow page full of text is content, not a blank page.

    The fixture is the real thing: LibreOffice renders a wide CSV as a single
    enormous page, and the repo's own 20k-row sleeper.csv comes out 13 x 29632 px
    at the converter's 150 DPI, carrying every row of the file. That page was
    reported blank and dropped, so the conversion produced an empty document and
    still reported success.

    The cause was the hashing thumbnail, not the detector: an aspect-preserving
    downscale of that page is 1 pixel wide, and a 1-pixel-wide strip has no DCT
    structure left to find.

    (Rebuild with: pdftoppm -r 150 -png -f 1 -l 1 <document.pdf> on the PDF the
    converter produces for tests/fixtures/malicious/sleeper.csv.)
    """
    h = hash_png_bytes(_read("csv_20k_rows_13x29632.png"))
    assert h.is_blank is False
    assert bin(int(h.phash, 16)).count("1") > 1


def test_extreme_aspect_page_that_is_blank_is_still_blank():
    """The companion: not collapsing the thumbnail must not cost us the real
    blank pages, which are the whole point of the detector."""
    assert hash_png_bytes(_blank_page((13, 29632))).is_blank is True


def test_hash_thumbnail_never_collapses_a_dimension():
    """The invariant behind both tests above, asserted directly.

    phash computes a 32x32 DCT, so a thumbnail thinner than 32 px in either
    direction is upsampled back to 32 from nothing. No page shape may produce
    one -- a dimension may only shrink to 32, or to its own original size if
    it was already smaller.

    The 32 below is written out rather than imported from the module: the DCT
    size is the requirement, and a test that reads the implementation's own
    floor passes for any floor. (Measured -- with `_HASH_MIN_DIM` imported, a
    mutant that lowered the floor to 16 survived this test.)
    """
    from clippyshot.hasher import _downscale_for_hash

    dct_size = 32
    # Beyond 32:1 is all it takes to scale a dimension under the floor, so
    # these stay small: the largest is 1 Mpx. (An earlier draft reached for
    # 2000x200000 to look thorough -- 400 Mpx, ~1.2 GB before Pillow's resize
    # buffers, on a CI runner, to test the same branch as 100x10000.)
    shapes = [
        (13, 29632),        # the real CSV render: below the floor to begin with
        (29632, 13),        # the same page, landscape
        (1, 5000),
        (5000, 1),
        (100, 10000),       # scales to 10px wide from a width that is above 32
        (10000, 100),
    ]
    for size in shapes:
        with Image.new("RGB", size, (255, 255, 255)) as img:
            thumb = _downscale_for_hash(img)
        assert thumb.size[0] >= min(size[0], dct_size), (size, thumb.size)
        assert thumb.size[1] >= min(size[1], dct_size), (size, thumb.size)


def test_ordinary_page_thumbnails_are_unchanged():
    """The floor must not perturb ordinary pages: their recorded hashes are
    stable across this change, because their thumbnails are identical."""
    from clippyshot.hasher import _HASH_MAX_DIM, _downscale_for_hash

    for size, expected in [
        ((1275, 1650), (791, _HASH_MAX_DIM)),      # US Letter at 150 DPI
        ((2480, 3508), (723, _HASH_MAX_DIM)),      # A4 at 300 DPI
        ((800, 600), (800, 600)),                  # already small: returned as-is
    ]:
        with Image.new("RGB", size, (255, 255, 255)) as img:
            assert _downscale_for_hash(img).size == expected, size
