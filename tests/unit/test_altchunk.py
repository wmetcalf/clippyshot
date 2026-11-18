import zipfile
from pathlib import Path

import pytest

from clippyshot.libreoffice.altchunk import inspect_altchunks


def _make_docx_with_altchunks(path: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/afchunk1.mht" ContentType="message/rfc822"/>
  <Override PartName="/word/afchunk2.html" ContentType="text/html"/>
  <Override PartName="/word/unused.mht" ContentType="message/rfc822"/>
</Types>
"""
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:altChunk r:id="rId2"/>
    <w:p/>
    <w:altChunk r:id="rId1"/>
  </w:body>
</w:document>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk" Target="afchunk1.mht"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk" Target="afchunk2.html"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>
"""

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
        zf.writestr("word/afchunk1.mht", b"mime payload")
        zf.writestr("word/afchunk2.html", b"<html>payload</html>")
        zf.writestr("word/unused.mht", b"should not be returned")


def test_inspect_altchunks_returns_only_referenced_parts_in_document_order(
    tmp_path: Path,
):
    docx = tmp_path / "input.docx"
    _make_docx_with_altchunks(docx)

    found = inspect_altchunks(docx)

    assert [a.part_name for a in found] == ["/word/afchunk2.html", "/word/afchunk1.mht"]
    assert [a.content_type for a in found] == ["text/html", "message/rfc822"]
    assert found[0].data == b"<html>payload</html>"
    assert found[1].data == b"mime payload"


@pytest.mark.parametrize("codec", ["utf-8", "utf-16"])
def test_inspect_altchunks_refuses_a_document_with_a_dtd(tmp_path: Path, codec: str):
    """An altChunk docx whose document.xml carries a DTD (an unreferenced external entity,
    which bare ElementTree would accept) yields nothing: the guard refused it."""
    src = tmp_path / "ok.docx"
    _make_docx_with_altchunks(src)
    assert inspect_altchunks(src)  # control: the same package without a DTD is read
    with zipfile.ZipFile(src) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    doc = items["word/document.xml"].decode().replace(
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<?xml version="1.0" encoding="{codec.upper()}"?>'
        '<!DOCTYPE w:document [<!ENTITY x SYSTEM "file:///etc/passwd">]>')
    items["word/document.xml"] = doc.encode(codec)
    bad = tmp_path / "dtd.docx"
    with zipfile.ZipFile(bad, "w") as zf:
        for n, b in items.items():
            zf.writestr(n, b)
    assert inspect_altchunks(bad) == []
