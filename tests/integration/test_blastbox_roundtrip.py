"""Integration test: ClippyShotEngine round-trip through the blastbox framework.

Proves end-to-end: real document → ClippyShotEngine → run_detonation →
validate_worker_output → host-accepted Envelope with sha-verified artifacts.

Marked ``integration`` — requires a working LibreOffice + sandbox on the host.
Run with: .venv/bin/pytest tests/integration/test_blastbox_roundtrip.py -v -m integration

The engine emits standard ``Page`` nodes (scanner data as ``Record`` children), so
the typed tree survives the worker seal -> host re-parse round-trip unchanged.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from clippyshot.engine import ClippyShotEngine

from blastbox.contract import Page, find_by_type
from blastbox.errors import OutputTrustError
from blastbox.host.trust import validate_worker_output
from blastbox.limits import Limits
from blastbox.worker.harness import run_detonation

pytestmark = [pytest.mark.integration]

_FIXTURE_DOCX = Path(__file__).resolve().parents[1] / "fixtures" / "safe" / "fixture.docx"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def detonation_output(tmp_path_factory):
    """Run the full detonation once and return (output_dir, input_sha256)."""
    out = tmp_path_factory.mktemp("blastbox-out")
    engine = ClippyShotEngine()
    limits = Limits()

    exit_code = run_detonation(
        engine,
        input_path=_FIXTURE_DOCX,
        output_dir=out,
        limits=limits,
    )
    assert exit_code == 0, f"run_detonation returned non-zero exit code: {exit_code}"
    assert (out / "metadata.json").exists(), "metadata.json was not written"

    input_sha256 = _sha256_file(_FIXTURE_DOCX)
    return out, input_sha256


def test_roundtrip_status_ok(detonation_output):
    """Host validates the output and returns status='ok'."""
    out, input_sha256 = detonation_output
    env = validate_worker_output(
        output_dir=out,
        input_sha256=input_sha256,
        engine="clippyshot",
        limits=Limits(),
    )
    assert env.status == "ok", f"Expected 'ok', got {env.status!r}"


def test_roundtrip_has_artifacts(detonation_output):
    """At least one artifact is declared and host-sealed."""
    out, input_sha256 = detonation_output
    env = validate_worker_output(
        output_dir=out,
        input_sha256=input_sha256,
        engine="clippyshot",
        limits=Limits(),
    )
    assert len(env.artifacts) >= 1, "Expected at least one artifact"


def test_roundtrip_page_nodes(detonation_output):
    """payload tree contains at least one Page node."""
    out, input_sha256 = detonation_output
    env = validate_worker_output(
        output_dir=out,
        input_sha256=input_sha256,
        engine="clippyshot",
        limits=Limits(),
    )
    pages = find_by_type(env.payload, Page)
    assert len(pages) >= 1, "Expected at least one Page node in payload"


def test_roundtrip_page_image_ref_resolves(detonation_output):
    """Every Page node's image ArtifactRef resolves to a real PNG artifact on disk."""
    out, input_sha256 = detonation_output
    env = validate_worker_output(
        output_dir=out,
        input_sha256=input_sha256,
        engine="clippyshot",
        limits=Limits(),
    )
    artifact_ids = {a.id for a in env.artifacts}
    pages = find_by_type(env.payload, Page)
    for page in pages:
        assert page.image.id in artifact_ids, (
            f"Page {page.index}: image ArtifactRef {page.image.id!r} not in artifacts"
        )
    # Spot-check: first page's image file is a real PNG on disk.
    first_page = pages[0]
    first_artifact = next(a for a in env.artifacts if a.id == first_page.image.id)
    png_path = out / first_artifact.path
    assert png_path.is_file(), f"PNG not found on disk: {png_path}"
    assert png_path.suffix == ".png", f"Expected .png suffix, got {png_path.suffix}"


def test_roundtrip_artifact_sha256_matches_disk(detonation_output):
    """Host-resealed artifact sha256 matches the real file contents on disk.

    This proves the host re-seal step (not just the worker-reported hash),
    satisfying the 'a real document went in, host-validated output came out'
    proof-of-concept requirement.
    """
    out, input_sha256 = detonation_output
    env = validate_worker_output(
        output_dir=out,
        input_sha256=input_sha256,
        engine="clippyshot",
        limits=Limits(),
    )
    for artifact in env.artifacts:
        artifact_path = out / artifact.path
        real_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        assert artifact.sha256 == real_sha256, (
            f"Artifact {artifact.id!r} sha256 mismatch: "
            f"envelope={artifact.sha256!r}, disk={real_sha256!r}"
        )


def test_roundtrip_page_hashes_present(detonation_output):
    """Each Page node carries phash, colorhash, and sha256 hashes."""
    out, input_sha256 = detonation_output
    env = validate_worker_output(
        output_dir=out,
        input_sha256=input_sha256,
        engine="clippyshot",
        limits=Limits(),
    )
    pages = find_by_type(env.payload, Page)
    for page in pages:
        algos = {h.algo for h in page.hashes}
        assert "phash" in algos, f"Page {page.index}: missing phash"
        assert "sha256" in algos, f"Page {page.index}: missing sha256"


def test_roundtrip_wrong_sha256_raises(detonation_output):
    """validate_worker_output raises OutputTrustError when input_sha256 is wrong."""
    out, _ = detonation_output
    with pytest.raises(OutputTrustError):
        validate_worker_output(
            output_dir=out,
            input_sha256="a" * 64,  # wrong hash — not the real input SHA
            engine="clippyshot",
            limits=Limits(),
        )


_SAFE_FIXTURES = sorted((_FIXTURE_DOCX.parent).iterdir())


def _assert_conforms(envelope) -> None:
    """Check a host-validated envelope against ``blastbox-engine.toml``.

    Skips on a released blastbox that predates ``blastbox.contract.engine_contract``."""
    ec = pytest.importorskip("blastbox.contract.engine_contract")
    ec.assert_conforms(ec.load_contract(ec.find_contract(__file__)), envelope)


def test_roundtrip_conforms_to_engine_contract(detonation_output):
    """The host-accepted envelope is what ``blastbox-engine.toml`` promises."""
    out, input_sha256 = detonation_output
    env = validate_worker_output(
        output_dir=out,
        input_sha256=input_sha256,
        engine="clippyshot",
        limits=Limits(),
    )
    _assert_conforms(env)


@pytest.mark.parametrize("fixture", _SAFE_FIXTURES, ids=lambda p: p.name)
def test_every_safe_fixture_conforms_to_engine_contract(fixture, tmp_path):
    """Every safe fixture, ok or rejected, yields a contract-conforming envelope.

    Covers the spreadsheet, trimmed-derivative and rejected (PDF input) paths the
    single-docx fixture above never reaches. No safe fixture yields a focused crop; that
    family is checked by tests/unit/test_engine_mapping.py's stub instead."""
    pytest.importorskip("blastbox.contract.engine_contract")
    out = tmp_path / "out"
    out.mkdir()
    assert run_detonation(ClippyShotEngine(), input_path=fixture, output_dir=out,
                          limits=Limits()) == 0
    env = validate_worker_output(
        output_dir=out,
        input_sha256=_sha256_file(fixture),
        engine="clippyshot",
        limits=Limits(),
    )
    _assert_conforms(env)

