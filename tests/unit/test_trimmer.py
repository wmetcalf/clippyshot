from pathlib import Path

import pytest

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
