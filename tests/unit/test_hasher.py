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
