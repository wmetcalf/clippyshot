from pathlib import Path


from clippyshot.trimmer import trim_bottom_solid


class _HugeImageStub:
    size = (32768, 32768)

    def convert(self, _mode):
        return self

    def close(self):
        return None


def test_trim_large_image_skips_numpy_array_allocation(monkeypatch, tmp_path: Path):
    stub = _HugeImageStub()

    monkeypatch.setattr("clippyshot.trimmer.Image.open", lambda _path: stub)

    def fail_if_called(_img):
        raise AssertionError("numpy allocation should be skipped for huge images")

    monkeypatch.setattr("clippyshot.trimmer.np.asarray", fail_if_called)

    png = tmp_path / "page-001.png"
    png.write_bytes(b"not-a-real-png")

    assert trim_bottom_solid(png) is None


def _page_with_content_ending_at(tmp_path: Path, row: int, height: int = 1000) -> Path:
    """A white page whose last non-background row is `row`."""
    from PIL import Image

    img = Image.new("RGB", (400, height), "white")
    for y in range(row + 1):
        for x in range(0, 400, 7):          # sparse ink, sampled by the row scan
            img.putpixel((x, y), (0, 0, 0))
    png = tmp_path / "page-001.png"
    img.save(png)
    return png


def test_the_trim_margin_never_falls_below_twenty_pixels(tmp_path: Path):
    """Content near the top of a tall page keeps a 20px margin, not 5% of very little.

    `margin = max(20, int((content_bottom + 1) * 0.05))`. For content ending at
    row 99 the percentage is 5px, so only the floor decides -- and nothing tested
    it: dropping `max(20, ...)` left the whole suite green while the crop moved
    from 120px to 105px, shaving 15px off whatever sits just under the detected
    content (a descender, a rule, a faint row the sampled scan missed).

    20 is written out rather than read from the source, so a lowered floor is
    caught rather than tracked.
    """
    png = _page_with_content_ending_at(tmp_path, row=99)
    result = trim_bottom_solid(png)
    assert result is not None
    assert result["height_px"] == 120, result   # 100 rows of content + 20 margin


def test_the_trim_margin_grows_with_the_content(tmp_path: Path):
    """The positive control: the floor is a floor, not a fixed 20px margin.

    Content ending at row 599 makes 5% = 30px, which must win over the floor --
    otherwise a mutant pinning the margin to 20 would pass the test above.
    """
    png = _page_with_content_ending_at(tmp_path, row=599)
    result = trim_bottom_solid(png)
    assert result is not None
    assert result["height_px"] == 630, result   # 600 rows of content + 30 margin
