import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_INDEX = (_ROOT / "src" / "clippyshot" / "static" / "index.html").read_text()


def _strip_js_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)(^|\s)//[^\n]*", r"\1", s)
    return s


# ── host-contract guards (the seams the blastbox migration must not regress) ──
def test_ui_does_not_call_unported_routes():
    code = _strip_js_comments(_INDEX)
    for dead in ("/v1/convert", "/v1/jobs/counts", "/infected-zip"):
        assert dead not in code, f"UI calls unported route {dead}"


def test_upload_uses_multipart_form():
    assert "new FormData()" in _INDEX
    assert "fd.append('file'" in _INDEX
    assert "fd.append('engine'" in _INDEX


def test_version_footer_does_not_render_unported_sandbox_field():
    # /v1/version returns {version, allowed_engines} — no `sandbox` (G3 regression).
    assert "v.sandbox" not in _INDEX


def test_pyproject_packages_static_assets():
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    pkg = data["tool"]["setuptools"]["package-data"]["clippyshot"]
    assert any("static/" in p and ".html" in p for p in pkg)
    assert any("static/assets/" in p for p in pkg)


def test_trimmed_preview_opens_in_lightbox():
    html = Path("src/clippyshot/static/index.html").read_text()
    lines = html.splitlines()

    assert "trimUrl" in html
    assert any("openLightbox(" in line and "trimUrl" in line for line in lines)


def test_ui_includes_focused_preview_and_route():
    html = Path("src/clippyshot/static/index.html").read_text()
    lines = html.splitlines()

    assert "/pages/focused/" in html
    assert "Focused view" in html
    assert any("openLightbox(" in line and "focusUrl" in line for line in lines)


def test_ui_includes_job_search_and_delete_controls():
    html = Path("src/clippyshot/static/index.html").read_text()

    assert 'id="job-search"' in html
    assert "searchJobs(this.value)" in html
    assert "deleteJob(" in html
