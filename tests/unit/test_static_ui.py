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


def _outside_helpers() -> str:
    return _strip_js_comments(_INDEX.replace(_warning_helpers_js(), ""))


def test_ui_captures_envelope_warnings_before_the_shim_swaps_meta():
    """The envelope's own warnings and typed QR counts must be read before loadResult
    replaces meta with the embedded clippyshot_metadata (which has neither)."""
    code = _outside_helpers()
    shim = code.index("meta = full;")
    for capture in ("const envWarnings = (meta && meta.payload && Array.isArray(meta.warnings))",
                    "const typedQr = typedQrCounts(meta);"):
        assert -1 < code.index(capture) < shim, capture
    assert "collectWarnings(meta, envWarnings, typedQr)" in code


def test_ui_page_cards_render_the_missing_qr_note():
    code = _outside_helpers()
    assert "qrPageNote(p, typedQr)" in code
    assert "esc(qrNote)" in code


def test_ui_surfaces_envelope_warnings_with_the_embedded_metadata():
    """The full-metadata path used to show only clippyshot_metadata.warnings, dropping the
    envelope's engine warnings such as qr_metadata_truncated."""
    got = _run_helpers("""
      const meta = {warnings: [{code: 'resolution_clamped', message: 'm1'}],
                    qr_truncated: {omitted: 3, budget_bytes: 2048},
                    pages: [{index: 1, qr: [], qr_omitted: 2}, {index: 2, qr: []},
                            {index: 4, qr: [{value: 'a'}], qr_omitted: 1}]};
      const env = [{code: 'resolution_clamped', message: 'm1', context: 'x'},
                   {code: 'qr_metadata_truncated', message: 'dropped'},
                   {code: 'qr_decoded', message: 'https://a'}];
      console.log(JSON.stringify(collectWarnings(meta, env, {})));
    """)
    codes = [w["code"] for w in got]
    assert codes == ["resolution_clamped", "qr_metadata_truncated", "qr_decoded", "qr_truncated"]
    qt = got[-1]["message"]
    assert "3 decoded QR entries" in qt and "2048 bytes" in qt and "1 (2), 4 (1)" in qt


def test_ui_flags_qr_dropped_without_any_marker_from_typed_counts():
    """The engine's last resort empties every page's qr list with no qr_truncated /
    qr_omitted marker and no envelope warning; only the typed records keep qr_count."""
    got = _run_helpers("""
      const env = {payload: {children: [
        {_type: 'page', index: 1, children: [{_type: 'record', fields: {qr_count: 2}}]},
        {_type: 'page', index: 2, children: [{_type: 'record', fields: {qr_count: 0}}]},
        {_type: 'page', index: 3, children: [{_type: 'record', fields: {qr_count: 1}}]}]}};
      const typed = typedQrCounts(env);
      const meta = {pages: [{index: 1, qr: []}, {index: 2, qr: []}, {index: 3, qr: [{v: 1}]}]};
      console.log(JSON.stringify([typed, collectWarnings(meta, [], typed),
        qrPageNote(meta.pages[0], typed), qrPageNote(meta.pages[2], typed),
        qrPageNote({index: 1, qr: {count: 2}}, typed)]));
    """)
    typed, warnings, note1, note3, fallback = got
    assert typed == {"1": 2, "2": 0, "3": 1}
    assert [w["code"] for w in warnings] == ["qr_truncated"]
    assert warnings[0]["message"].startswith("2 decoded QR entries not shown")
    assert "pages: 1 (2)" in warnings[0]["message"]
    assert note1 == "2 of 2 decoded QR entries not shown (dropped to fit the metadata budget)"
    assert note3 == ""
    assert fallback == "2 decoded QR entries (values not recorded in this result)"


def test_ui_merge_matches_warnings_the_engine_cut_to_its_limits():
    """engine.py re-seals metadata warnings with message[:2000] / code[:64] (code points)."""
    got = _run_helpers("""
      const long = String.fromCodePoint(0x1F600).repeat(1990) + 'A'.repeat(50);
      const meta = [{code: 'altchunk_present', message: long}];
      const env = [{code: 'altchunk_present', message: Array.from(long).slice(0, 2000).join(''),
                    context: '{}'}];
      console.log(JSON.stringify(mergeWarnings(meta, env).length));
    """)
    assert got == 1
def test_ui_merge_keeps_repeats_but_never_doubles():
    got = _run_helpers("""
      const a = {code: 'altchunk_present', message: 'same'};
      console.log(JSON.stringify([
        mergeWarnings([a, a], [a]).length,
        mergeWarnings([a], [a, a]).length,
        mergeWarnings([], [a, {code: 'x', message: 'y'}]).map(w => w.code),
        mergeWarnings(null, undefined, [null, {message: 'no code'}]).length,
        qrTruncatedWarning({}, {}) === null,
      ]));
    """)
    assert got == [2, 2, ["altchunk_present", "x"], 0, True]


def test_job_list_qr_badge_is_added_once_per_row():
    code = _outside_helpers()
    assert "!cell.querySelector('.badge-qr')" in code
    assert "typedQrCounts(meta)).some(n => n > 0)" in code


def test_ui_merge_keeps_the_envelope_context_on_a_tie():
    got = _run_helpers("""
      const m = mergeWarnings([{code: 'altchunk_present', message: 'same'}],
                              [{code: 'altchunk_present', message: 'same', context: 'part=/a.mht'}]);
      const long = 'x'.repeat(2100);
      const m2 = mergeWarnings([{code: 'a', message: long}],
                               [{code: 'a', message: long.slice(0, 2000), context: 'c'}]);
      console.log(JSON.stringify([m.map(w => w.context || null), m2[0].message.length,
                                  m2[0].context]));
    """)
    assert got == [["part=/a.mht"], 2100, "c"]


def test_ui_thin_envelope_fallback_shows_typed_qr_values():
    code = _outside_helpers()
    assert "sf.qr_values.map(v => ({ value: String(v)" in code
    assert "{ count: sf.qr_count }" not in code
    # No typed qr_values (results sealed before the embed): null, not [] -- an empty list
    # would read as "every value dropped to fit the budget".
    assert "String(v), format: '' })) : null," in code
    got = _run_helpers("""
      const typed = {1: 2};
      console.log(JSON.stringify([qrPageNote({index: 1, qr: null}, typed),
                                  collectWarnings({pages: [{index: 1, qr: null}]}, [], typed)]));
    """)
    assert got == ["2 decoded QR entries (values not recorded in this result)", []]
