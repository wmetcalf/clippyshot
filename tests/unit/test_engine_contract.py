"""The engine contract (``blastbox-engine.toml``) parses and names this engine.

The checker lives in ``blastbox.contract.engine_contract``, which released blastbox does
not ship yet, so these skip there. Real-result conformance is checked in
``tests/integration/test_blastbox_roundtrip.py``; this file covers the contract file
itself and the rejected path, which needs no LibreOffice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _contract():
    ec = pytest.importorskip("blastbox.contract.engine_contract")
    path = ec.find_contract(__file__)
    assert path == ROOT / "blastbox-engine.toml"
    return ec, ec.load_contract(path)


def test_contract_validates_and_names_the_engine():
    from clippyshot.engine import ClippyShotEngine

    ec, contract = _contract()
    assert ec.main(["validate", str(ROOT / "blastbox-engine.toml")]) == 0
    assert contract.engine.name == ClippyShotEngine.name
    assert contract.engine.formats == sorted(ClippyShotEngine.formats)
    assert [c.engine for c in contract.consumers] == ["clippyshot"]


def test_rejected_result_conforms(tmp_path):
    """A detector rejection out of the real ``detonate`` mapping conforms (no artifacts,
    rejected status). Same converter stub as test_engine_warmup's rejected test."""
    ec, contract = _contract()
    from blastbox.limits import Limits as BlastboxLimits

    from clippyshot.engine import ClippyShotEngine
    from clippyshot.errors import DetectionError

    class _RejectingConverter:
        def convert(self, *a, **k):
            raise DetectionError("unsupported_type", "magika=iso")

    eng = ClippyShotEngine()
    eng._converter = _RejectingConverter()
    inp = tmp_path / "in.bin"
    inp.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    res = eng.detonate(inp, out, BlastboxLimits(timeout_s=30))
    assert res.status == "rejected"
    ec.assert_conforms(contract, res, out)
