"""Derivative crops for rendered page images.

Produces a supplemental cropped version of each page that removes
trailing blank/solid-color space from the bottom. The original image
is NEVER modified — the trimmed version is saved alongside it.

Useful for spreadsheet renders where SinglePageSheets produces pages
with large amounts of empty grid at the bottom.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

_MAX_VECTOR_PIXELS = 100_000_000


def _open_rgb_image(png_path: Path) -> Image.Image | None:
    try:
        raw = Image.open(png_path)
        img = raw.convert("RGB")
        raw.close()
        return img
    except Exception:
        return None


def _sampled_deviation_mask(arr: np.ndarray, bg: np.ndarray, *, axis: str) -> np.ndarray:
    if axis == "rows":
        step = max(1, arr.shape[1] // 64)
        sampled = arr[:, ::step, :].astype(np.int16)
    else:
        step = max(1, arr.shape[0] // 64)
        sampled = arr[::step, :, :].astype(np.int16)
    diff = np.abs(sampled - bg)
    return diff.max(axis=(1, 2)) if axis == "rows" else diff.max(axis=(0, 2))


def trim_bottom_solid(
    png_path: Path,
    *,
    tolerance: int = 10,
    min_content_ratio: float = 0.1,
) -> dict | None:
    """Trim solid-color rows from the bottom of a PNG image.

    Scans from the bottom up, finds where the solid background ends and
    real content begins. Saves a trimmed copy as ``{stem}-trimmed.png``
    in the same directory. Returns metadata about the trim, or None if
    no meaningful trimming was possible.

    Uses numpy for the row scan — ~100× faster than Python-level pixel
    access on large images.

    Args:
        png_path: Path to the original PNG (never modified).
        tolerance: Max per-channel deviation from the bottom-right pixel
            color to still count as "solid background". Default 10.
        min_content_ratio: Minimum fraction of the image height that must
            be content. If trimming would leave less than this, skip
            (the page is mostly blank and trimming would produce a sliver).

    Returns:
        dict with trim metadata, or None if no trim was produced.
    """
    img = _open_rgb_image(png_path)
    if img is None:
        return None

    try:
        w, h = img.size
        if w == 0 or h == 0:
            return None

        # Avoid materializing multi-gigabyte RGB arrays in the main process.
        # Oversized pages are already bounded by converter limits; for trimming,
        # skipping the supplemental variant is safer than risking worker OOM.
        if w * h > _MAX_VECTOR_PIXELS:
            return None

        arr = np.asarray(img)  # (H, W, 3) uint8, zero-copy when possible

        # Background color from bottom-right corner
        bg = arr[-1, -1].astype(np.int16)  # int16 to allow signed subtraction

        # Vectorised row scan: for each row, check if max per-channel deviation
        # from background exceeds tolerance. Sample every 4th column for speed
        # on very wide images (matches old behaviour of sampling ~64 points).
        row_max = _sampled_deviation_mask(arr, bg, axis="rows")

        # Find the last row (from bottom) that has non-background content
        non_solid = np.where(row_max > tolerance)[0]
        if len(non_solid) == 0:
            # Entire image is solid background
            return None

        content_bottom = int(non_solid[-1])

        # Add a small margin below the content (5% of content height or 20px)
        margin = max(20, int((content_bottom + 1) * 0.05))
        crop_y = min(h, content_bottom + 1 + margin)

        # Skip if trimming wouldn't remove much (less than 10% of height)
        removed_ratio = 1.0 - (crop_y / h)
        if removed_ratio < 0.1:
            return None

        # Skip if the remaining content is too thin
        if crop_y < h * min_content_ratio:
            return None

        # Crop and save
        trimmed = img.crop((0, 0, w, crop_y))
        trimmed_path = png_path.parent / (png_path.stem + "-trimmed.png")
        trimmed.save(trimmed_path, "PNG", optimize=True)
        trimmed.close()

        bg_r, bg_g, bg_b = int(bg[0]), int(bg[1]), int(bg[2])
        return {
            "file": trimmed_path.name,
            "width_px": w,
            "height_px": crop_y,
            "original_height_px": h,
            "removed_percent": round(removed_ratio * 100, 1),
            "background_color": f"#{bg_r:02x}{bg_g:02x}{bg_b:02x}",
        }
    finally:
        img.close()


def focus_content_solid_bg(
    png_path: Path,
    *,
    tolerance: int = 10,
    min_removed_ratio: float = 0.08,
) -> dict | None:
    """Create a content-focused derivative by trimming solid margins on all sides.

    Intended for analyst-friendly spreadsheet views. The original render is
    preserved; this emits a separate ``-focused.png`` derivative only when the
    page has obvious solid-color margins around the meaningful content.
    """
    img = _open_rgb_image(png_path)
    if img is None:
        return None

    try:
        w, h = img.size
        if w == 0 or h == 0:
            return None
        if w * h > _MAX_VECTOR_PIXELS:
            return None

        arr = np.asarray(img)
        bg = arr[-1, -1].astype(np.int16)
        row_max = _sampled_deviation_mask(arr, bg, axis="rows")
        col_max = _sampled_deviation_mask(arr, bg, axis="cols")

        non_bg_rows = np.where(row_max > tolerance)[0]
        non_bg_cols = np.where(col_max > tolerance)[0]
        if len(non_bg_rows) == 0 or len(non_bg_cols) == 0:
            return None

        top = int(non_bg_rows[0])
        bottom = int(non_bg_rows[-1])
        left = int(non_bg_cols[0])
        right = int(non_bg_cols[-1])

        content_w = right - left + 1
        content_h = bottom - top + 1
        margin_x = max(12, int(content_w * 0.03))
        margin_y = max(12, int(content_h * 0.03))

        crop_left = max(0, left - margin_x)
        crop_top = max(0, top - margin_y)
        crop_right = min(w, right + 1 + margin_x)
        crop_bottom = min(h, bottom + 1 + margin_y)

        removed_ratio = 1.0 - (((crop_right - crop_left) * (crop_bottom - crop_top)) / (w * h))
        if removed_ratio < min_removed_ratio:
            return None
        if crop_left == 0 and crop_top == 0 and crop_right == w and crop_bottom == h:
            return None

        # Skip if the focused crop is a useless sliver. When a page has
        # stray artifacts or grid lines spanning every row/column, trim
        # only works in one axis — producing a 30-pixel-wide ribbon or
        # a 50-pixel-tall strip that's impossible to read. Require the
        # result to stay within 1:8 aspect in both directions.
        crop_w = crop_right - crop_left
        crop_h = crop_bottom - crop_top
        if crop_w < 100 or crop_h < 100:
            return None
        aspect = max(crop_w / crop_h, crop_h / crop_w)
        if aspect > 8.0:
            return None

        focused = img.crop((crop_left, crop_top, crop_right, crop_bottom))
        focused_path = png_path.parent / (png_path.stem + "-focused.png")
        focused.save(focused_path, "PNG", optimize=True)
        focused.close()

        return {
            "file": focused_path.name,
            "width_px": crop_right - crop_left,
            "height_px": crop_bottom - crop_top,
            "original_width_px": w,
            "original_height_px": h,
            "crop_box": [crop_left, crop_top, crop_right, crop_bottom],
            "removed_percent": round(removed_ratio * 100, 1),
        }
    finally:
        img.close()
