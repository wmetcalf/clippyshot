"""Verify result zip is password-encrypted with the 'infected' password."""
from __future__ import annotations

from pathlib import Path

import pyzipper
import pytest

from clippyshot.api import _zip_dir_to_file


def test_zip_is_password_protected(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.txt").write_text("hello world")
    (src / "sub.bin").write_bytes(b"\x00\x01\x02")

    dest = tmp_path / "out.zip"
    _zip_dir_to_file(src, dest)
    assert dest.is_file() and dest.stat().st_size > 0

    with pyzipper.AESZipFile(dest, "r") as zf:
        names = sorted(zf.namelist())
        assert names == ["hello.txt", "sub.bin"]
        with pytest.raises(RuntimeError):
            zf.read("hello.txt")
        zf.setpassword(b"infected")
        assert zf.read("hello.txt") == b"hello world"
        assert zf.read("sub.bin") == b"\x00\x01\x02"


def test_zip_password_overridable_via_env(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    (src / "hello.txt").write_text("hi")
    dest = tmp_path / "out.zip"

    monkeypatch.setenv("CLIPPYSHOT_ZIP_PASSWORD", "custom-pw")
    _zip_dir_to_file(src, dest)

    with pyzipper.AESZipFile(dest, "r") as zf:
        zf.setpassword(b"custom-pw")
        assert zf.read("hello.txt") == b"hi"


def test_wrong_password_fails(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / "hello.txt").write_text("hi")
    dest = tmp_path / "out.zip"
    _zip_dir_to_file(src, dest)

    with pyzipper.AESZipFile(dest, "r") as zf:
        zf.setpassword(b"wrong")
        with pytest.raises(RuntimeError):
            zf.read("hello.txt")
