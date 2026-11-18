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


def test_ui_renders_worker_tier():
    """The web UI distinguishes the two warm backends (FC vs gVisor) via worker_tier, not just
    the generic worker_runtime='warm' — guards the badge from regressing to the stale
    'warm == gVisor' assumption."""
    assert "worker_tier" in _INDEX
    assert "firecracker" in _INDEX and "gvisor" in _INDEX


# ── engine/host warnings (qr_metadata_truncated lives on the envelope, not in metadata) ──
def _warning_helpers_js() -> str:
    m = re.search(r"// WARNINGS-HELPERS-BEGIN.*?// WARNINGS-HELPERS-END", _INDEX, re.S)
    assert m, "warning helpers block missing from index.html"
    return m.group(0)


def _run_helpers(script: str):
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, "-e", _warning_helpers_js() + "\n" + script],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_ui_surfaces_envelope_warnings_with_the_embedded_metadata():
    """The full-metadata path used to show only clippyshot_metadata.warnings, dropping the
    envelope's engine warnings such as qr_metadata_truncated."""
    code = _strip_js_comments(_INDEX)
    assert "const envWarnings" in code
    assert "collectWarnings(meta, envWarnings)" in code
    got = _run_helpers("""
      const meta = {warnings: [{code: 'resolution_clamped', message: 'm1'}],
                    qr_truncated: {omitted: 3, budget_bytes: 2048},
                    pages: [{index: 1, qr_omitted: 2}, {index: 2}, {index: 4, qr_omitted: 1}]};
      const env = [{code: 'resolution_clamped', message: 'm1', context: 'x'},
                   {code: 'qr_metadata_truncated', message: 'dropped'},
                   {code: 'qr_decoded', message: 'https://a'}];
      console.log(JSON.stringify(collectWarnings(meta, env)));
    """)
    codes = [w["code"] for w in got]
    assert codes == ["resolution_clamped", "qr_metadata_truncated", "qr_decoded", "qr_truncated"]
    qt = got[-1]["message"]
    assert "3 decoded QR entries" in qt and "2048 bytes" in qt and "1 (2), 4 (1)" in qt


def test_ui_merge_keeps_repeats_but_never_doubles():
    got = _run_helpers("""
      const a = {code: 'altchunk_present', message: 'same'};
      console.log(JSON.stringify([
        mergeWarnings([a, a], [a]).length,
        mergeWarnings([a], [a, a]).length,
        mergeWarnings([], [a, {code: 'x', message: 'y'}]).map(w => w.code),
        mergeWarnings(null, undefined, [null, {message: 'no code'}]).length,
        qrTruncatedWarning({}) === null,
      ]));
    """)
    assert got == [2, 2, ["altchunk_present", "x"], 0, True]


def test_ui_marks_pages_with_omitted_qr_entries():
    assert "p.qr_omitted" in _strip_js_comments(_INDEX)
