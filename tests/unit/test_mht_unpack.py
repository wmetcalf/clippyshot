from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from clippyshot.libreoffice.mht_unpack import _safe_filename, unpack_mht


def test_safe_filename_strips_traversal_and_empty_names():
    assert _safe_filename("../../etc/passwd", "fallback.bin") == "passwd"
    assert _safe_filename(r"C:\\temp\\evil.png", "fallback.bin") == "evil.png"
    assert _safe_filename("...", "fallback.bin") == "fallback.bin"


def test_unpack_mht_rewrites_cid_and_content_location(tmp_path):
    msg = MIMEMultipart("related")
    html = MIMEText(
        '<html><body><img src="cid:img1"><img src="file:///C:/fake/../../etc/passwd"></body></html>',
        "html",
        "utf-8",
    )
    msg.attach(html)
    img = MIMEApplication(b"image-bytes", _subtype="octet-stream")
    img.add_header("Content-ID", "<img1>")
    img.add_header("Content-Location", "file:///C:/fake/../../etc/passwd")
    msg.attach(img)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mht_path = tmp_path / "input.mht"
    mht_path.write_bytes(msg.as_bytes())

    html_path = unpack_mht(mht_path, out_dir)

    assert html_path is not None
    rendered = html_path.read_text(encoding="utf-8")
    assert "cid:img1" not in rendered
    assert "file:///C:/fake/../../etc/passwd" not in rendered
    assert "passwd" in rendered
    assert (html_path.parent / "passwd").read_bytes() == b"image-bytes"


def test_unpack_mht_does_not_rewrite_plain_text_mentions(tmp_path):
    msg = MIMEMultipart("related")
    html = MIMEText(
        '<html><body><p>image0.png should stay visible as text</p><img src="image0.png"></body></html>',
        "html",
        "utf-8",
    )
    msg.attach(html)
    img = MIMEApplication(b"image-bytes", _subtype="octet-stream")
    img.add_header("Content-Location", "image0.png")
    msg.attach(img)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mht_path = tmp_path / "input.mht"
    mht_path.write_bytes(msg.as_bytes())

    html_path = unpack_mht(mht_path, out_dir)

    assert html_path is not None
    rendered = html_path.read_text(encoding="utf-8")
    assert "image0.png should stay visible as text" in rendered
    assert '<img src="image0.png">' in rendered
