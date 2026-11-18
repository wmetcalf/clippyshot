"""``ClippyShotEngine.detonate`` maps a converter result to the blastbox contract.

A stub converter stands in for LibreOffice so the mapping (what reaches the typed tree,
the warnings and the declared artifact set) is checked without a render.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from blastbox.limits import Limits as BlastboxLimits

from clippyshot.converter import ConversionResult
from clippyshot.engine import ClippyShotEngine

_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def _meta(**overrides) -> dict:
    meta = {
        "clippyshot_version": "9.9.9",
        "input": {"detected": {"label": "docx", "mime": "application/msword", "confidence": 1.0}},
        "render": {"page_count_total": 2, "page_count_rendered": 2, "truncated": False},
        "pages": [
            {"index": 1, "file": "page-001.png", "width_mm": 210.0, "height_mm": 297.0,
             "qr": [{"format": "qr_code", "value": "https://evil.example/a", "position": []},
                    {"format": "qr_code", "value": "x" * 3000, "position": []}],
             "ocr": {"text": "hello", "char_count": 5}},
            {"index": 2, "file": "page-002.png", "width_mm": 210.0, "height_mm": 297.0,
             "qr": [], "ocr": {}},
        ],
        "warnings": [
            {"code": "altchunk_present", "message": "input contains a Word altChunk",
             "part_name": "/word/afchunk.mht", "content_type": "message/rfc822", "size": 1234},
            {"code": "resolution_clamped", "message": "1 page(s) rendered below 150 DPI",
             "pages": [{"index": 2, "effective_dpi": 72}]},
        ],
    }
    meta.update(overrides)
    return meta


class _StubConverter:
    """Writes what Converter.convert writes (pages, document.pdf, its own metadata.json)."""

    def __init__(self, meta: dict) -> None:
        self.meta = meta

    def convert(self, input_path: Path, output_dir: Path, options) -> ConversionResult:
        for page in self.meta["pages"]:
            (output_dir / page["file"]).write_bytes(_PNG)
        (output_dir / "document.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (output_dir / "metadata.json").write_text(json.dumps(self.meta))
        return ConversionResult(output_dir=output_dir, metadata=self.meta)


def _detonate(tmp_path: Path, meta: dict | None = None):
    eng = ClippyShotEngine()
    eng._converter = _StubConverter(meta or _meta())
    inp = tmp_path / "in.docx"
    inp.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    return eng.detonate(inp, out, BlastboxLimits(timeout_s=30)), out


def _scanner(page):
    (rec,) = [c for c in page.children if c.type == "record"]
    return rec.fields


def test_every_file_left_in_outdir_is_declared(tmp_path):
    """The converter's own metadata.json is not an output: nothing declares it and the harness
    overwrites it with the sealed envelope. detonate must not leave it behind."""
    res, out = _detonate(tmp_path)
    declared = {a.path for a in res.artifacts}
    on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert on_disk == declared


def test_each_decoded_qr_is_a_finding_and_a_typed_value(tmp_path):
    res, _ = _detonate(tmp_path)
    qr = [w for w in res.warnings if w.code == "qr_decoded"]
    assert [w.message for w in qr] == ["https://evil.example/a", "x" * 2000]
    assert [w.context for w in qr] == ["page 1", "page 1 (value truncated)"]
    p1, p2 = res.payload.children
    assert _scanner(p1)["qr_values"] == ["https://evil.example/a", "x" * 3000]
    assert _scanner(p2)["qr_values"] == []


def test_qr_findings_are_capped_with_a_marker(tmp_path):
    many = [{"format": "qr_code", "value": f"v{i}", "position": []} for i in range(300)]
    meta = _meta()
    meta["pages"][0]["qr"] = many
    res, _ = _detonate(tmp_path, meta)
    codes = [w.code for w in res.warnings]
    assert codes.count("qr_decoded") == 256
    assert codes.count("qr_decoded_truncated") == 1
    # The typed per-page list is not capped by the warning budget.
    assert len(_scanner(res.payload.children[0])["qr_values"]) == 300


def test_warning_detail_survives_in_typed_form(tmp_path):
    res, _ = _detonate(tmp_path)
    (alt,) = [w for w in res.warnings if w.code == "altchunk_present"]
    assert json.loads(alt.context) == {
        "part_name": "/word/afchunk.mht", "content_type": "message/rfc822", "size": 1234}
    p1, p2 = res.payload.children
    assert _scanner(p1)["effective_dpi"] is None
    assert _scanner(p2)["effective_dpi"] == 72


def test_oversized_altchunk_detail_still_fits_the_context_cap(tmp_path):
    meta = _meta()
    meta["warnings"][0]["part_name"] = "/word/" + "a" * 1000
    res, _ = _detonate(tmp_path, meta)
    (alt,) = [w for w in res.warnings if w.code == "altchunk_present"]
    detail = json.loads(alt.context)
    assert len(alt.context) <= 255
    assert detail["size"] == 1234 and detail["content_type"] == "message/rfc822"
    assert detail["part_name"].startswith("/word/aaa")


def test_root_carries_a_metadata_schema_version(tmp_path):
    res, _ = _detonate(tmp_path)
    assert res.payload.metadata.fields["schema_version"] == "1"


def test_no_unused_engine_node_type_is_registered():
    """ClippyShotPage was registered but never emitted; consumers walk plain ``page`` nodes."""
    import clippyshot.engine as engine

    assert not hasattr(engine, "ClippyShotPage")


def test_stub_result_conforms_to_contract(tmp_path):
    ec = pytest.importorskip("blastbox.contract.engine_contract")
    res, out = _detonate(tmp_path)
    contract = ec.load_contract(ec.find_contract(__file__))
    ec.assert_conforms(contract, res, out)
