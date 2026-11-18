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


def test_typed_qr_values_share_a_document_budget(tmp_path):
    """qr_values duplicates every payload already in clippyshot_metadata, and the host refuses
    a sealed envelope over its metadata cap (4 MiB by default). A hostile document full of
    near-capacity QR codes must not turn a conversion that sealed before into one that fails."""
    from clippyshot.engine import _QR_VALUES_BUDGET_BYTES

    pages = [
        {"index": i, "file": f"page-{i:03d}.png", "width_mm": 210.0, "height_mm": 297.0,
         "qr": [{"format": "qr_code", "value": f"{i}-{j}-" + "q" * 2900, "position": []}
                for j in range(16)],
         "ocr": {}}
        for i in range(1, 51)
    ]
    res, _ = _detonate(tmp_path, _meta(pages=pages))
    records = [_scanner(p) for p in res.payload.children]
    kept = sum(len(v) for r in records for v in r["qr_values"])
    assert kept <= _QR_VALUES_BUDGET_BYTES
    assert all(r["qr_count"] == 16 for r in records)
    assert records[0]["qr_values_truncated"] is False
    assert records[-1]["qr_values_truncated"] is True
    assert records[-1]["qr_values"] == []


def test_qr_copies_only_use_the_headroom_the_envelope_leaves(tmp_path):
    """A document that sealed without the typed QR copies must still seal with them: the
    copies get only the room the rest of the envelope leaves under max_metadata_bytes."""
    pages = [
        {"index": i, "file": f"page-{i:03d}.png", "width_mm": 210.0, "height_mm": 297.0,
         "qr": [{"format": "qr_code", "value": f"{i}-{j}-" + "\U0001F600" * 700,
                 "position": []} for j in range(4)],
         "ocr": {}}
        for i in range(1, 21)
    ]
    meta = _meta(pages=pages)
    eng = ClippyShotEngine()
    eng._converter = _StubConverter(meta)
    inp = tmp_path / "in.docx"
    inp.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    cap = 1_000_000
    res = eng.detonate(inp, out, BlastboxLimits(timeout_s=30, max_metadata_bytes=cap))
    sealed = len(res.payload.model_dump_json(by_alias=True).encode()) + sum(
        len(w.model_dump_json().encode()) for w in res.warnings) + 512 * len(res.artifacts)
    assert sealed < cap
    records = [_scanner(p) for p in res.payload.children]
    assert any(r["qr_values_truncated"] for r in records)


def test_qr_value_budget_never_skips_a_middle_value():
    from clippyshot.engine import _budgeted_qr_values

    pages = [{"index": 1, "qr": [{"value": "a" * 900}]},
             {"index": 2, "qr": [{"value": "b" * 2000}, {"value": "c" * 10}]}]
    out = _budgeted_qr_values(pages, 1000)
    assert out[1] == (["a" * 900], False)
    assert out[2] == ([], True)


def test_altchunk_detail_with_non_ascii_content_type_keeps_its_context(tmp_path):
    meta = _meta()
    meta["warnings"][0]["content_type"] = "\U0001F600" * 64
    res, _ = _detonate(tmp_path, meta)
    (alt,) = [w for w in res.warnings if w.code == "altchunk_present"]
    assert alt.context is not None and len(alt.context) <= 255
    detail = json.loads(alt.context)
    assert detail["size"] == 1234 and detail["part_name"] == "/word/afchunk.mht"


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


def test_non_ascii_altchunk_part_name_is_shortened_not_erased(tmp_path):
    meta = _meta()
    meta["warnings"][0]["part_name"] = "/word/" + "\u00e9" * 300
    res, _ = _detonate(tmp_path, meta)
    (alt,) = [w for w in res.warnings if w.code == "altchunk_present"]
    detail = json.loads(alt.context)
    assert len(alt.context) <= 255
    assert detail["part_name"].startswith("/word/\u00e9\u00e9")
    assert len(detail["part_name"]) > 100


def test_repeated_qr_value_does_not_spend_the_finding_cap(tmp_path):
    meta = _meta()
    meta["pages"][0]["qr"] = [{"format": "qr_code", "value": "https://benign.example/",
                               "position": []}] * 300
    meta["pages"][1]["qr"] = [{"format": "qr_code", "value": "https://evil.example/login",
                               "position": []}]
    res, _ = _detonate(tmp_path, meta)
    qr = [(w.message, w.context) for w in res.warnings if w.code == "qr_decoded"]
    assert qr == [("https://benign.example/", "page 1"), ("https://evil.example/login", "page 2")]
    assert not [w for w in res.warnings if w.code == "qr_decoded_truncated"]


def test_qr_finding_messages_share_a_character_budget(tmp_path):
    from clippyshot.engine import _QR_WARNING_BUDGET_BYTES

    meta = _meta()
    meta["pages"][0]["qr"] = [{"format": "qr_code", "value": f"{i:04d}" + "z" * 2500,
                               "position": []} for i in range(200)]
    res, _ = _detonate(tmp_path, meta)
    qr = [w for w in res.warnings if w.code == "qr_decoded"]
    assert sum(len(w.message) for w in qr) <= _QR_WARNING_BUDGET_BYTES
    assert [w.code for w in res.warnings].count("qr_decoded_truncated") == 1


def test_derivative_pages_conform_to_contract(tmp_path):
    """No safe fixture produces a p*-focused crop (trimmed ones come from most real renders);
    drive both families through the stub so a drifted id or path fails here."""
    ec = pytest.importorskip("blastbox.contract.engine_contract")
    meta = _meta()
    meta["pages"][0]["trimmed"] = {"file": "page-001-trimmed.png", "removed_percent": 10}
    meta["pages"][0]["focused"] = {"file": "page-001-focused.png", "removed_percent": 20}
    stub = _StubConverter(meta)
    orig = stub.convert

    def convert(input_path, output_dir, options):
        r = orig(input_path, output_dir, options)
        (output_dir / "page-001-trimmed.png").write_bytes(_PNG)
        (output_dir / "page-001-focused.png").write_bytes(_PNG)
        return r

    stub.convert = convert
    eng = ClippyShotEngine()
    eng._converter = stub
    inp = tmp_path / "in.xlsx"
    inp.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    res = eng.detonate(inp, out, BlastboxLimits(timeout_s=30))
    ids = {a.id for a in res.artifacts}
    assert {"p1-trimmed", "p1-focused"} <= ids
    contract = ec.load_contract(ec.find_contract(__file__))
    ec.assert_conforms(contract, res, out)


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


def test_no_qr_warning_room_means_no_qr_warnings_at_all():
    from clippyshot.engine import _qr_warnings

    pages = [{"index": 1, "qr": [{"value": "https://x.example/"}]}]
    assert _qr_warnings(pages, 0) == []
    many = [{"index": 1, "qr": [{"value": f"v{i}" * 50} for i in range(50)]}]
    out = _qr_warnings(many, 2000)
    sealed = sum(len(w.model_dump_json().encode()) for w in out)
    assert out[-1].code == "qr_decoded_truncated" and sealed <= 2000


# --------------------------------------------------------------------------
# QR content inside clippyshot_metadata is budgeted too (was gap
# host-metadata-cap-qr-flood): the host refuses a sealed metadata.json over
# Limits.max_metadata_bytes, so an unbounded QR flood there cost the whole result.
# --------------------------------------------------------------------------

def _sealed_size(res, outdir) -> int:
    """Bytes of the metadata.json the worker harness would write: the real seal."""
    from blastbox.contract.envelope import seal_envelope

    env = seal_envelope(engine="clippyshot", outdir=outdir, input_sha256="0" * 64,
                        detected=res.detected, declared=res.artifacts,
                        warnings=res.warnings, payload=res.payload, status=res.status)
    return len(env.model_dump_json(by_alias=True).encode("utf-8"))


def _detonate_capped(tmp_path, meta, cap):
    eng = ClippyShotEngine()
    eng._converter = _StubConverter(meta)
    inp = tmp_path / "in.docx"
    inp.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    res = eng.detonate(inp, out, BlastboxLimits(timeout_s=30, max_metadata_bytes=cap))
    return res, out


def _quote_heavy_pages(n_pages=20, per_page=16, chars=2900):
    # '"' and '\\' cost 4 sealed bytes each inside the doubly-encoded metadata string.
    return [
        {"index": i, "file": f"page-{i:03d}.png", "width_mm": 210.0, "height_mm": 297.0,
         "qr": [{"format": "qr_code", "value": f"{i}-{j}-" + '"\\' * (chars // 2),
                 "position": [], "raw_bytes_hex": "22" * chars} for j in range(per_page)],
         "ocr": {}}
        for i in range(1, n_pages + 1)
    ]


def test_qr_flood_in_metadata_stays_under_the_host_cap(tmp_path):
    cap = 1_000_000
    meta = _meta(pages=_quote_heavy_pages())
    assert len(json.dumps(json.dumps(meta))) > 3 * cap  # unbounded, this would be refused
    res, out = _detonate_capped(tmp_path, meta, cap)
    assert res.status == "ok"
    assert _sealed_size(res, out) < cap
    inner = json.loads(res.payload.metadata.fields["clippyshot_metadata"])
    trunc = inner["qr_truncated"]
    assert trunc["omitted"] > 0 and trunc["budget_bytes"] > 0
    # qr_count stays exact on the typed record; the metadata page says how many it dropped.
    for page, typed in zip(inner["pages"], res.payload.children):
        assert _scanner(typed)["qr_count"] == 16
        assert len(page["qr"]) + page.get("qr_omitted", 0) == 16
    assert sum(p.get("qr_omitted", 0) for p in inner["pages"]) == trunc["omitted"]
    assert [w.code for w in res.warnings].count("qr_metadata_truncated") == 1
    # the input the converter returned is not mutated
    assert all(len(p["qr"]) == 16 for p in meta["pages"])


def test_metadata_qr_budget_keeps_whole_entries_in_page_order(tmp_path):
    cap = 600_000
    res, _ = _detonate_capped(tmp_path, _meta(pages=_quote_heavy_pages()), cap)
    inner = json.loads(res.payload.metadata.fields["clippyshot_metadata"])
    kept = [(p["index"], q["value"]) for p in inner["pages"] for q in p["qr"]]
    full = [(p["index"], q["value"]) for p in _quote_heavy_pages() for q in p["qr"]]
    assert kept and len(kept) < len(full)
    assert kept == full[: len(kept)]  # a prefix: no middle entry skipped
    assert all(q["raw_bytes_hex"] for p in inner["pages"] for q in p["qr"])


def test_many_tiny_qr_codes_are_bounded_by_count_not_just_content(tmp_path):
    # A flood of short codes costs ~100 bytes each in metadata whatever the value size.
    pages = [
        {"index": i, "file": f"page-{i:03d}.png", "width_mm": 210.0, "height_mm": 297.0,
         "qr": [{"format": "qr_code", "value": "x", "position": [[1, 2], [3, 4]],
                 "error_correction_level": "L", "is_mirrored": False, "raw_bytes_hex": "78"}
                for _ in range(3000)],
         "ocr": {}}
        for i in range(1, 11)
    ]
    cap = 1_000_000
    res, out = _detonate_capped(tmp_path, _meta(pages=pages), cap)
    assert _sealed_size(res, out) < cap
    inner = json.loads(res.payload.metadata.fields["clippyshot_metadata"])
    assert inner["qr_truncated"]["omitted"] > 0


def test_small_qr_payload_is_untouched_in_metadata(tmp_path):
    res, _ = _detonate(tmp_path)
    inner = json.loads(res.payload.metadata.fields["clippyshot_metadata"])
    assert "qr_truncated" not in inner
    assert [q["value"] for q in inner["pages"][0]["qr"]] == ["https://evil.example/a", "x" * 3000]
    assert "qr_omitted" not in inner["pages"][0]
    assert "qr_metadata_truncated" not in [w.code for w in res.warnings]


def test_the_qr_flood_gap_is_closed_in_the_contract():
    text = (Path(__file__).resolve().parents[2] / "blastbox-engine.toml").read_text()
    assert 'id = "host-metadata-cap-qr-flood"' not in text
    assert 'code = "qr_metadata_truncated"' in text
    # The blastbox-side half (cold dispatcher not forwarding the cap) is still open.
    assert 'id = "blastbox-cold-max-metadata-not-forwarded"' in text


def test_a_repeated_benign_code_cannot_push_a_later_distinct_one_out_of_metadata(tmp_path):
    """Loadout reads QR codes only from clippyshot_metadata; a flood of ONE benign code must
    not spend the budget a later distinct payload needs (the qr_decoded findings already
    de-duplicate for the same reason)."""
    junk = "https://benign.example/?" + '"' * 2870
    pages = [
        {"index": i, "file": f"page-{i:03d}.png", "width_mm": 210.0, "height_mm": 297.0,
         "qr": [{"format": "qr_code", "value": junk, "position": [],
                 "raw_bytes_hex": "22" * 2900} for _ in range(12)],
         "ocr": {}}
        for i in range(1, 22)
    ]
    pages.append({"index": 22, "file": "page-022.png", "width_mm": 210.0, "height_mm": 297.0,
                  "qr": [{"format": "qr_code", "value": "https://evil.example/login",
                          "position": [], "raw_bytes_hex": "00"}], "ocr": {}})
    res, out = _detonate_capped(tmp_path, _meta(pages=pages), 4 << 20)
    assert _sealed_size(res, out) < 4 << 20
    inner = json.loads(res.payload.metadata.fields["clippyshot_metadata"])
    assert inner["qr_truncated"]["omitted"] > 0
    assert [q["value"] for q in inner["pages"][-1]["qr"]] == ["https://evil.example/login"]
    assert inner["pages"][0]["qr"][0]["value"] == junk  # the first copy is kept


def test_truncation_never_grows_a_result_with_no_room(tmp_path):
    """With the envelope already at the cap, dropping one small entry would add more
    bookkeeping (qr_omitted, qr_truncated, the warning) than it saves: keep it instead."""
    page = {"index": 1, "file": "page-001.png", "width_mm": 210.0, "height_mm": 297.0,
            "qr": [{"format": "qr_code", "value": "https://e.example/", "position": [],
                    "raw_bytes_hex": "68"}],
            "ocr": {"text": "o" * 150_000, "char_count": 150_000}}
    res, _ = _detonate_capped(tmp_path, _meta(pages=[page]), 161_800)
    inner = json.loads(res.payload.metadata.fields["clippyshot_metadata"])
    assert "qr_truncated" not in inner
    assert [q["value"] for q in inner["pages"][0]["qr"]] == ["https://e.example/"]
    assert "qr_metadata_truncated" not in [w.code for w in res.warnings]


def test_typed_copies_fit_the_headroom_at_a_low_cap(tmp_path):
    """The typed qr_values / qr_decoded copies get only what the metadata leaves: at a cap
    where their full ceilings would overshoot, the sealed result still fits."""
    pages = [
        {"index": i, "file": f"page-{i:03d}.png", "width_mm": 210.0, "height_mm": 297.0,
         "qr": [{"format": "qr_code", "value": f"{i}-{j}-" + "v" * 2000, "position": []}
                for j in range(16)],
         "ocr": {}}
        for i in range(1, 21)
    ]
    cap = 250_000
    res, out = _detonate_capped(tmp_path, _meta(pages=pages), cap)
    assert _sealed_size(res, out) < cap


def test_one_oversized_early_value_does_not_push_later_small_ones_out():
    from clippyshot.engine import _budget_metadata_qr

    pages = [{"index": 1, "qr": [{"value": "big", "raw_bytes_hex": "ab" * 50_000}]},
             {"index": 2, "qr": [{"value": "https://evil.example/login"}]},
             {"index": 3, "qr": [{"value": "https://evil.example/login"},
                                 {"value": "big", "raw_bytes_hex": "ab" * 50_000}]}]
    out, omitted = _budget_metadata_qr({"pages": pages}, 20_000)
    assert omitted == 2
    assert [[q["value"] for q in p["qr"]] for p in out["pages"]] == [
        [], ["https://evil.example/login"], ["https://evil.example/login"]]
    assert [p.get("qr_omitted") for p in out["pages"]] == [1, None, 1]


def test_truncation_bookkeeping_is_measured_not_guessed():
    """Dropping an entry that saves more than the real bookkeeping costs must happen even
    when the saving is below the conservative up-front estimate."""
    from clippyshot.engine import _budget_metadata_qr, _meta_qr_entry_cost

    entry = {"value": "x" * 900}
    cost = _meta_qr_entry_cost(entry)
    out, omitted = _budget_metadata_qr({"pages": [{"index": 1, "qr": [entry]}]}, cost - 1)
    assert omitted == 1 and out["pages"][0]["qr"] == []


def test_a_smaller_later_copy_stands_in_for_an_oversized_first_one():
    from clippyshot.engine import _budget_metadata_qr

    pages = [{"index": 1, "qr": [{"value": "u", "raw_bytes_hex": "ab" * 50_000}]},
             {"index": 2, "qr": [{"value": "u", "raw_bytes_hex": "75"}]}]
    out, omitted = _budget_metadata_qr({"pages": pages}, 20_000)
    assert omitted == 1
    assert out["pages"][1]["qr"] == [{"value": "u", "raw_bytes_hex": "75"}]


def test_byte_distinct_payloads_with_the_same_text_are_distinct():
    from clippyshot.engine import _budget_metadata_qr

    a = {"value": "same", "raw_bytes_hex": "00" * 1000}
    b = {"value": "same", "raw_bytes_hex": "ff" * 1000}
    out, omitted = _budget_metadata_qr({"pages": [{"index": 1, "qr": [a, a, b]}]}, 5000)
    assert omitted == 1
    assert [q["raw_bytes_hex"][:2] for q in out["pages"][0]["qr"]] == ["00", "ff"]


def test_bookkeeping_reserve_is_not_wastefully_large():
    """Two ~300-byte entries plus their real bookkeeping fit 1000 bytes: keep them."""
    from clippyshot.engine import _budget_metadata_qr

    e = [{"value": str(i) + "x" * 280} for i in range(4)]
    out, omitted = _budget_metadata_qr({"pages": [{"index": 1, "qr": e}]}, 1000)
    assert omitted == 2 and len(out["pages"][0]["qr"]) == 2
