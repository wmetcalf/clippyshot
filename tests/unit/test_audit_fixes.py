"""Regression tests for the security-audit hardening fixes."""
import zipfile

import pytest

from clippyshot._argv import assert_positional
from clippyshot.detector import _correct_odf_label_via_mimetype
from clippyshot.errors import sanitize_public_error
from clippyshot.limits import Limits
from clippyshot.ocr import validate_lang
from clippyshot.qr import validate_formats
from clippyshot.runtime.docker_runtime import (
    InsecureRuntimeRefused,
    select_worker_runtime,
)


# --- Limits bounds (#10) ---------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_width_px": 0},          # would disable the bomb dimension guard
        {"max_height_px": 0},
        {"max_input_bytes": 0},
        {"memory_bytes": -1},
        {"tmpfs_bytes": 0},
        {"max_width_px": 10**9},      # absurdly high
    ],
)
def test_limits_reject_out_of_range(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_limits_defaults_valid():
    Limits()  # must not raise


def test_limits_from_env_zero_rejected(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_MAX_WIDTH", "0")
    with pytest.raises(ValueError):
        Limits.from_env()


def test_limits_from_env_nonnumeric_names_var(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_MAX_INPUT", "lots")
    with pytest.raises(ValueError, match="CLIPPYSHOT_MAX_INPUT"):
        Limits.from_env()


# --- Scanner arg validation (#6) -------------------------------------------

@pytest.mark.parametrize("lang", ["eng", "eng+Latin", "chi_sim", "Cyrillic"])
def test_validate_lang_accepts_good(lang):
    assert validate_lang(lang) == lang


@pytest.mark.parametrize(
    "lang",
    ["../evil", "eng/../x", "eng;rm", "-l", "eng Latin", "a" * 101, ""],
)
def test_validate_lang_rejects_bad(lang):
    with pytest.raises(ValueError):
        validate_lang(lang)


def test_validate_formats_normalizes():
    assert validate_formats("qr_code, micro_qr_code|rmqr_code") == (
        "qr_code,micro_qr_code,rmqr_code"
    )


@pytest.mark.parametrize(
    "formats",
    ["../x", "qr_code;rm", "QR CODE", "", "-fast", "a" * 41],
)
def test_validate_formats_rejects_bad(formats):
    with pytest.raises(ValueError):
        validate_formats(formats)


# --- Option-injection guard (#11) ------------------------------------------

def test_assert_positional_rejects_option_like():
    with pytest.raises(ValueError):
        assert_positional("-rf")
    with pytest.raises(ValueError):
        assert_positional("--version")


def test_assert_positional_allows_normal_paths():
    assert_positional("/sandbox/scan/page-001.png")
    assert_positional("page.png")  # relative but not option-like


# --- Fail-closed runtime (#8) ----------------------------------------------

def test_runtime_refuses_insecure_when_required(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_REQUIRE_SECURE_RUNTIME", "1")
    monkeypatch.delenv("CLIPPYSHOT_WORKER_RUNTIME", raising=False)
    with pytest.raises(InsecureRuntimeRefused):
        select_worker_runtime(available_runtimes=["runc"])


def test_runtime_allows_secure_when_required(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_REQUIRE_SECURE_RUNTIME", "1")
    monkeypatch.delenv("CLIPPYSHOT_WORKER_RUNTIME", raising=False)
    sel = select_worker_runtime(available_runtimes=["runsc", "runc"])
    assert sel.runtime == "runsc" and sel.secure


def test_runtime_downgrades_silently_when_not_required(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_REQUIRE_SECURE_RUNTIME", raising=False)
    monkeypatch.delenv("CLIPPYSHOT_WORKER_RUNTIME", raising=False)
    sel = select_worker_runtime(available_runtimes=["runc"])
    assert sel.runtime == "runc" and not sel.secure


# --- Error scrubber (#13) --------------------------------------------------

@pytest.mark.parametrize(
    "msg,leaks",
    [
        ("boom at /etc/passwd here", "/etc/passwd"),
        ("see /proc/self/status", "/proc/self/status"),
        ("/var/lib/clippyshot/jobs/x/input.docx failed", "/var/lib/clippyshot"),
        ("/run/secret.sock denied", "/run/secret.sock"),
    ],
)
def test_scrubber_redacts_paths(msg, leaks):
    out = sanitize_public_error(msg)
    assert leaks not in out
    assert "<path>" in out


def test_scrubber_keeps_non_paths():
    assert sanitize_public_error("use and/or logic") == "use and/or logic"


# --- ODF subtype detection (Magika collapses odg/odp/ods -> odt) -----------

def _odf(tmp_path, mimetype: bytes, name="f.odg"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("mimetype", mimetype)
        zf.writestr("content.xml", b"<x/>")
    return p


@pytest.mark.parametrize(
    "mimetype,expected",
    [
        (b"application/vnd.oasis.opendocument.graphics", "odg"),
        (b"application/vnd.oasis.opendocument.presentation", "odp"),
        (b"application/vnd.oasis.opendocument.spreadsheet", "ods"),
        (b"application/vnd.oasis.opendocument.text", "odt"),
        (b"application/vnd.oasis.opendocument.graphics-template", "odg"),
    ],
)
def test_odf_mimetype_correction(tmp_path, mimetype, expected):
    # Magika guessed "odt"; the package mimetype is authoritative.
    p = _odf(tmp_path, mimetype)
    assert _correct_odf_label_via_mimetype(p, "odt") == expected


def test_odf_missing_mimetype_unchanged(tmp_path):
    p = tmp_path / "f.odg"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("content.xml", b"<x/>")
    assert _correct_odf_label_via_mimetype(p, "odt") == "odt"


def test_odf_mimetype_read_is_bounded(tmp_path):
    # A 5 MiB mimetype member must not be fully materialized; the marker is
    # in the first bytes, so correction still works without reading it whole.
    p = tmp_path / "f.odg"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "mimetype",
            b"application/vnd.oasis.opendocument.graphics" + b" " * (5 * 1024 * 1024),
        )
    assert _correct_odf_label_via_mimetype(p, "odt") == "odg"
