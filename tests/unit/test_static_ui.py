from pathlib import Path


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
