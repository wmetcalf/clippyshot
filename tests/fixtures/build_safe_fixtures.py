"""Build a deterministic set of safe fixtures for each supported format family.

Run with: .venv/bin/python tests/fixtures/build_safe_fixtures.py
The output is committed to the repo so tests are hermetic.

This script needs LibreOffice (`soffice`) installed. On the dev host where
soffice is missing, the text-family fixtures (txt, csv, md, rtf) will still
be written, but the office-format fixtures (docx/xlsx/pptx/odt/ods/odp/odg/
doc/xls/ppt/xps) will be skipped with a warning. Run this script inside the
ClippyShot Docker image to build the full fixture set.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent / "safe"
ROOT.mkdir(parents=True, exist_ok=True)


def write_text_family() -> None:
    (ROOT / "fixture.txt").write_text("Hello ClippyShot.\nLine two.\n")
    (ROOT / "fixture.csv").write_text("a,b,c\n1,2,3\n4,5,6\n")
    (ROOT / "fixture.md").write_text("# Hello\n\nThis is a *test* fixture.\n")
    (ROOT / "fixture.rtf").write_text(
        "{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Helvetica;}}"
        "\\f0\\fs24 Hello \\b ClippyShot\\b0 .}"
    )


def soffice_make(input_path: Path, target_ext: str, output_name: str | None = None) -> Path:
    """Use soffice to convert input.X → input.<ext>.

    output_name overrides the stem of the produced file so callers that
    convert from a seed (e.g. _seed.fodp) can still get fixture.pptx.
    """
    out_dir = ROOT / "_tmp"
    out_dir.mkdir(exist_ok=True)
    subprocess.run(
        [
            "soffice", "--headless", "--safe-mode", "--norestore",
            "--convert-to", target_ext,
            "--outdir", str(out_dir),
            str(input_path),
        ],
        check=True, capture_output=True,
    )
    bare_ext = target_ext.split(":")[0]
    produced = next(out_dir.glob(f"{input_path.stem}.{bare_ext}"))
    stem = output_name if output_name is not None else produced.stem
    final = ROOT / f"{stem}.{bare_ext}"
    shutil.move(str(produced), str(final))
    return final


def build_office_fixtures() -> None:
    if not shutil.which("soffice"):
        print("soffice not found — skipping office-format fixtures.", file=sys.stderr)
        print("Run inside the ClippyShot Docker image to build the full set.", file=sys.stderr)
        return

    base = ROOT / "fixture.txt"
    for ext in ("docx", "odt", "doc"):
        soffice_make(base, ext)
    csv = ROOT / "fixture.csv"
    for ext in ("xlsx", "ods", "xls"):
        soffice_make(csv, ext)

    # For pptx/odp/ppt, use a tiny ODF Presentation seed.
    seed = ROOT / "_seed.fodp"
    seed.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'office:mimetype="application/vnd.oasis.opendocument.presentation">'
        '<office:body><office:presentation><draw:page xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'draw:name="Slide1"/></office:presentation></office:body></office:document>'
    )
    for ext in ("pptx", "odp", "ppt"):
        soffice_make(seed, ext, output_name="fixture")

    # odg from a tiny svg seed.
    svg = ROOT / "_seed.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<rect width="100" height="100" fill="lightblue"/></svg>'
    )
    soffice_make(svg, "odg", output_name="fixture")

    # XPS is not supported by LibreOffice 24.x export filters; skip it.

    shutil.rmtree(ROOT / "_tmp", ignore_errors=True)
    seed.unlink(missing_ok=True)
    svg.unlink(missing_ok=True)


def main() -> None:
    write_text_family()
    build_office_fixtures()
    print(f"Wrote fixtures to {ROOT}")


if __name__ == "__main__":
    main()
