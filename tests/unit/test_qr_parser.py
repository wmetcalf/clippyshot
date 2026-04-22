"""Unit tests for the ZXingReader JSON-lines output parser."""
from __future__ import annotations

import pytest

from clippyshot.qr import QRResult, _parse_json_line, parse_zxing_output


class TestParseJsonLine:
    def test_simple_string_fields(self):
        line = '{"Format": "QRCode", "Text": "hello"}'
        assert _parse_json_line(line) == {"Format": "QRCode", "Text": "hello"}

    def test_escaped_quote_in_string(self):
        line = r'{"Text": "he said \"hi\""}'
        assert _parse_json_line(line) == {"Text": 'he said "hi"'}

    def test_newline_escape_in_string(self):
        line = r'{"Text": "line1\nline2"}'
        assert _parse_json_line(line) == {"Text": "line1\nline2"}

    def test_null_value_becomes_none(self):
        line = '{"Format": "QRCode", "ECLevel": null}'
        assert _parse_json_line(line) == {"Format": "QRCode", "ECLevel": None}

    def test_boolean_value_preserved_as_string(self):
        line = '{"IsMirrored": true}'
        assert _parse_json_line(line) == {"IsMirrored": "true"}

    def test_numeric_value_preserved_as_string(self):
        line = '{"Format": "QRCode", "Text": "x"}'
        out = _parse_json_line(line)
        assert out["Format"] == "QRCode"
        assert out["Text"] == "x"

    def test_rejects_malformed_brace(self):
        with pytest.raises(ValueError):
            _parse_json_line("not a json object")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            _parse_json_line("")


class TestParseZxingOutput:
    def test_empty_output_returns_empty_list(self):
        assert parse_zxing_output("") == []
        assert parse_zxing_output("\n\n") == []

    def test_single_result(self):
        out = '{"Format": "QRCode", "Text": "https://example.com", "Position": "10,10 50,10 50,50 10,50", "ECLevel": "L", "IsMirrored": false}'
        results = parse_zxing_output(out)
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, QRResult)
        assert r.format == "qr_code"
        assert r.value == "https://example.com"
        assert r.position == "10,10 50,10 50,50 10,50"
        assert r.error_correction_level == "L"
        assert r.is_mirrored is False

    def test_multi_line_output(self):
        out = (
            '{"Format": "QRCode", "Text": "first"}\n'
            '{"Format": "MicroQRCode", "Text": "second"}\n'
        )
        results = parse_zxing_output(out)
        assert [r.value for r in results] == ["first", "second"]
        assert [r.format for r in results] == ["qr_code", "micro_qr_code"]

    def test_skips_entries_missing_format(self):
        out = '{"Text": "no format"}\n{"Format": "QRCode", "Text": "ok"}'
        results = parse_zxing_output(out)
        assert len(results) == 1
        assert results[0].value == "ok"

    def test_format_normalization(self):
        out = (
            '{"Format": "RMQRCode", "Text": "a"}\n'
            '{"Format": "DataMatrix", "Text": "b"}\n'
        )
        results = parse_zxing_output(out)
        assert [r.format for r in results] == ["rmqr_code", "data_matrix"]
