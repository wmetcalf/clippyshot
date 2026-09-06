"""`tessdata_dir()` decides whether the warm OCR helper can start at all.

It used to live inside `_serve()`, which is marked `pragma: no cover` because it
runs in the helper subprocess -- so it had no tests, and nothing else could
reuse it. The integration test that compares helper output against a direct
tesserocr call therefore built its own bare `PyTessBaseAPI()`, which fails with
`invalid tessdata path: ./` on any host that does not set TESSDATA_PREFIX: a
failure on the test's control arm, telling us nothing about the helper.
"""
from __future__ import annotations

import pathlib

import pytest

from clippyshot import ocr_warm


def _tessdata(tmp: pathlib.Path, name: str, *, models: bool) -> str:
    d = tmp / name
    d.mkdir(parents=True)
    if models:
        (d / "eng.traineddata").write_bytes(b"not a real model")
    return str(d)


@pytest.fixture
def no_system_dirs(monkeypatch, tmp_path):
    """Point discovery at an empty tree, so a host's real /usr/share cannot
    decide the outcome of these tests."""
    monkeypatch.setattr(ocr_warm, "SYSTEM_TESSDATA_DIRS", (str(tmp_path / "nothing" / "*"),))
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)


def test_env_is_used_when_it_holds_models(monkeypatch, tmp_path, no_system_dirs):
    env = _tessdata(tmp_path, "env", models=True)
    monkeypatch.setenv("TESSDATA_PREFIX", env)
    assert ocr_warm.tessdata_dir() == env


def test_env_holding_no_models_is_ignored(monkeypatch, tmp_path):
    """The `docker export` case: the variable survives, the models do not.

    Trusting it would hand tesserocr a directory with nothing in it, which is
    the same dead end as an unset variable -- so discovery must continue.
    """
    empty = _tessdata(tmp_path, "empty-env", models=False)
    system = _tessdata(tmp_path, "system", models=True)
    monkeypatch.setenv("TESSDATA_PREFIX", empty)
    monkeypatch.setattr(ocr_warm, "SYSTEM_TESSDATA_DIRS", (system,))
    assert ocr_warm.tessdata_dir() == system


def test_system_directory_is_discovered_when_env_is_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    system = _tessdata(tmp_path, "system", models=True)
    monkeypatch.setattr(ocr_warm, "SYSTEM_TESSDATA_DIRS", (str(tmp_path / "*"),))
    assert ocr_warm.tessdata_dir() == system


def test_directories_without_models_are_skipped(monkeypatch, tmp_path):
    """A candidate that exists but holds no models is not an answer."""
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    _tessdata(tmp_path, "aaa-empty", models=False)   # sorts first
    real = _tessdata(tmp_path, "zzz-real", models=True)
    monkeypatch.setattr(ocr_warm, "SYSTEM_TESSDATA_DIRS", (str(tmp_path / "*"),))
    assert ocr_warm.tessdata_dir() == real


def test_none_when_nothing_has_models(no_system_dirs):
    """None means 'let tesserocr use its own default', not 'use ./'."""
    assert ocr_warm.tessdata_dir() is None

