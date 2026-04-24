import zipfile
from pathlib import Path

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
