"""Build deliberately-spicy fixtures for security assertions.

These files exercise format features (remote refs, OLE links) but do NOT
contain real exploits.

Run with: .venv/bin/python tests/fixtures/build_malicious_fixtures.py
"""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).parent / "malicious"
ROOT.mkdir(parents=True, exist_ok=True)


def build_external_image_docx() -> None:
    """A docx that references a remote image at 127.0.0.1:65500.

    If the sandbox is doing its job, soffice will not attempt to fetch this.
    A TCP listener bound to 65500 in the test verifies zero connections.
    """
    out = ROOT / "external_image.docx"
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        ),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdImg" TargetMode="External" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            'Target="http://127.0.0.1:65500/track.png"/></Relationships>'
        ),
        "word/document.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<w:body><w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            '<wp:extent cx="2000000" cy="2000000"/><wp:docPr id="1" name="img"/>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="img"/><pic:cNvPicPr/></pic:nvPicPr>'
            '<pic:blipFill><a:blip r:link="rIdImg"/></pic:blipFill>'
            '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="2000000" cy="2000000"/></a:xfrm>'
            '<a:prstGeom prst="rect"/></pic:spPr></pic:pic></a:graphicData></a:graphic>'
            '</wp:inline></w:drawing></w:r></w:p></w:body></w:document>'
        ),
    }
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)


def build_ole_link_rtf() -> None:
    """An RTF with an OLE \\object\\objupdate link.

    LibreOffice with link updates disabled must not follow it.
    """
    out = ROOT / "ole_link.rtf"
    out.write_text(
        "{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Helvetica;}}"
        "{\\object\\objupdate\\objemb {\\*\\objclass Excel.Sheet.12}"
        "{\\*\\objdata 0102}{\\result {\\f0 OLE result}}}"
        "Hello world.}"
    )


def build_sleeper_csv() -> None:
    """A CSV with many rows used to test that max_pages truncation works
    on spreadsheet outputs."""
    out = ROOT / "sleeper.csv"
    rows = ["a,b,c"]
    for i in range(20000):
        rows.append(f"{i},{i*2},{i*3}")
    out.write_text("\n".join(rows) + "\n")


def build_macro_autoopen_odt() -> None:
    """A real ODT with an embedded Basic library and a Document_Open handler.

    When loaded by LibreOffice, the macro would write a sentinel file to
    /tmp/clippyshot-macro-pwned.  Under our hardened profile
    (MacroSecurityLevel=4, DisableMacrosExecution=true) the macro must NOT
    execute — the integration test asserts the sentinel file is absent after
    conversion.
    """
    out = ROOT / "macro_autoopen.odt"
    sentinel = "/tmp/clippyshot-macro-pwned"

    parts: dict[str, str] = {
        "mimetype": "application/vnd.oasis.opendocument.text",
        "META-INF/manifest.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">\n'
            '  <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>\n'
            '  <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>\n'
            '  <manifest:file-entry manifest:full-path="Basic/" manifest:media-type="application/binary"/>\n'
            '  <manifest:file-entry manifest:full-path="Basic/Standard/" manifest:media-type="application/binary"/>\n'
            '  <manifest:file-entry manifest:full-path="Basic/Standard/Module1.xml" manifest:media-type="text/xml"/>\n'
            '  <manifest:file-entry manifest:full-path="Basic/Standard/script-lb.xml" manifest:media-type="text/xml"/>\n'
            '  <manifest:file-entry manifest:full-path="Basic/script-lc.xml" manifest:media-type="text/xml"/>\n'
            '</manifest:manifest>\n'
        ),
        "content.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<office:document-content'
            ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
            ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">\n'
            '  <office:body><office:text>'
            '<text:p>Macro fixture body — if you see this rendered, macros did not execute (good).</text:p>'
            '</office:text></office:body>\n'
            '</office:document-content>\n'
        ),
        "Basic/script-lc.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<library:libraries'
            ' xmlns:library="http://openoffice.org/2000/library"'
            ' xmlns:xlink="http://www.w3.org/1999/xlink">\n'
            '  <library:library library:name="Standard" library:link="false"/>\n'
            '</library:libraries>\n'
        ),
        "Basic/Standard/script-lb.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<library:library'
            ' xmlns:library="http://openoffice.org/2000/library"'
            ' library:name="Standard"'
            ' library:readonly="false"'
            ' library:passwordprotected="false">\n'
            '  <library:element library:name="Module1"/>\n'
            '</library:library>\n'
        ),
        "Basic/Standard/Module1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<script:module'
            ' xmlns:script="http://openoffice.org/2000/script"'
            ' script:name="Module1"'
            ' script:language="StarBasic">\n'
            f'Sub Document_Open\n'
            f'    Open "{sentinel}" For Output As #1\n'
            f'    Print #1, "pwned"\n'
            f'    Close #1\n'
            f'End Sub\n'
            '</script:module>\n'
        ),
    }

    with zipfile.ZipFile(out, "w") as zf:
        # mimetype must be first and uncompressed per ODF spec
        zf.writestr("mimetype", parts["mimetype"], compress_type=zipfile.ZIP_STORED)
        for name, data in parts.items():
            if name != "mimetype":
                zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)


def main() -> None:
    build_external_image_docx()
    build_ole_link_rtf()
    build_sleeper_csv()
    build_macro_autoopen_odt()
    print(f"Wrote malicious fixtures to {ROOT}")


if __name__ == "__main__":
    main()
