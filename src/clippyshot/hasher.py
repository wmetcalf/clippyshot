"""Pure-function image hashing."""
from __future__ import annotations

import hashlib
import io

import imagehash
from PIL import Image

from clippyshot.types import PageHashes

# Raise Pillow's decompression bomb limit. The default (~178M pixels) is too
# low for spreadsheet pages rendered at 150 DPI — a wide sheet with many
# columns easily produces a 30000x5000 pixel image (~150M pixels, fine) but
# some multi-sheet workbooks produce even larger renders. We cap at 500M
# pixels (~2GB of RAM for RGB at 4 bytes/pixel) which accommodates the
# largest realistic spreadsheet renders while still catching actual bombs.
# The converter's max_width_px/max_height_px limits provide the first line
# of defense; this is belt-and-suspenders.
Image.MAX_IMAGE_PIXELS = 500_000_000


_HASH_MAX_DIM = 1024


def _downscale_for_hash(img: Image.Image) -> Image.Image:
    """Return a ≤1024px-wide copy for perceptual hashing.

    Both phash (32x32 DCT) and colorhash (binned histograms) are designed
    to work on small representations, so feeding them a 30000×5000 spread-
    sheet render just wastes time on the resize/iteration steps. Downscaling
    to 1024px wide first makes phash ~900× cheaper with no quality loss.
    """
    w, h = img.size
    if w <= _HASH_MAX_DIM and h <= _HASH_MAX_DIM:
        return img
    ratio = min(_HASH_MAX_DIM / w, _HASH_MAX_DIM / h)
    new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
    return img.resize(new_size, Image.LANCZOS)


def hash_png_bytes(png_bytes: bytes) -> PageHashes:
    """Compute pHash, colorhash, SHA-256, and blank-page flag for a PNG.

    Pure function: same bytes in, same hashes out, no I/O.

    A page is flagged blank when its perceptual-hash signature indicates a
    uniform / monochromatic surface:
      - pHash has at most one bit set (only the DC DCT term survives, meaning
        the 32x32 downsampled image has no spatial structure)
      - colorhash bin 0 OR bin 1 is maxed out ('f'), meaning ≥93.75% of
        pixels are in one achromatic tone (black or gray/white)
      - colorhash bins 2..13 are all zero (no pixels in any saturated colour
        hue bin), meaning the page contains only black/gray content

    The pHash check is essential: colorhash alone cannot distinguish a pure
    white page from a low-density spreadsheet page (sparse grid lines + text
    on a white background) because the colorhash bin quantisation is too
    coarse (one value step ≈ 6.25% of pixels). pHash, however, catches any
    real DCT structure from content — even a handful of thin grid lines
    produces many bits of pHash output.

    Together these catch genuinely blank pages (all-white, all-black, pure
    solid tones) without false-positiving on low-density content. They do
    NOT flag uniformly-colored pages (e.g., a solid red separator slide)
    because such a page has a coloured colorhash bin maxed out, failing
    the bins-2..13-all-zero check.
    """
    sha = hashlib.sha256(png_bytes).hexdigest()
    with Image.open(io.BytesIO(png_bytes)) as img:
        img.load()
        thumb = _downscale_for_hash(img)
        phash = str(imagehash.phash(thumb))
        chash = str(imagehash.colorhash(thumb, binbits=4))
        if thumb is not img:
            thumb.close()
    is_blank = _is_blank_signature(phash, chash)
    return PageHashes(phash=phash, colorhash=chash, sha256=sha, is_blank=is_blank)


def _is_blank_signature(phash_hex: str, colorhash_hex: str) -> bool:
    """Decide blankness from the perceptual-hash bits alone (no pixel pass).

    Three conditions must all hold:

    1. ``popcount(phash) <= 1``
       pHash computes a 32x32 DCT of the grayscale-downsampled image, then
       binarises against the median. A perfectly uniform image produces at
       most one bit set (just the DC term); any real DCT structure from
       sparse content produces many more. This cheaply distinguishes
       "solid tone" from "mostly white with some content" — the latter
       being the false-positive case that tripped earlier versions of this
       detector for low-density spreadsheet pages.

    2. ``colorhash[0] == 'f' or colorhash[1] == 'f'``
       colorhash with binbits=4 is 14 hex chars; bin 0 is the black
       fraction (ITU-R 601 luminance < 32/256) and bin 1 is the gray/
       non-black achromatic fraction (saturation < 85/256, not black).
       At least one of these must be maxed (≥93.75% of pixels) for the
       page to be "one achromatic tone".

    3. ``all(colorhash[2:14]) == '0'``
       None of the 12 hue bins (6 faint + 6 bright) contain meaningful
       coloured content. This excludes solid-coloured pages (e.g. a red
       separator slide) which are uniform by pHash but have real visible
       content.

    Known limitation: pure blue (RGB 0,0,255) has ITU-R 601 luminance ≈ 29,
    which falls below the black-fraction threshold, so colorhash places it
    in bin 0 rather than a colour bin. A solid-blue page will therefore
    pass all three checks and be flagged blank. Acceptable for the
    document blank-page use-case where solid-blue pages don't arise.
    """
    if len(colorhash_hex) < 14:
        return False
    # Condition 1: pHash must have at most one bit set (uniform DCT).
    if bin(int(phash_hex, 16)).count("1") > 1:
        return False
    # Condition 2: one achromatic bin dominant.
    if colorhash_hex[0] != "f" and colorhash_hex[1] != "f":
        return False
    # Condition 3: no coloured hue content.
    return all(c == "0" for c in colorhash_hex[2:])
