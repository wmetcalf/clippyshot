import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

from clippyshot.converter import ConvertOptions
from clippyshot.limits import Limits
from tests.conftest import (
    FIXTURES_DIR,
    needs_bwrap_userns,
    needs_pdftoppm,
    needs_soffice,
)

SAFE = FIXTURES_DIR / "safe"


# Each tuple is (filename, expected_label_options).
# We allow some flexibility in expected labels because Magika may classify some
# text-family fixtures interchangeably (e.g., a CSV may come back as "csv" or
# "txt" depending on content). Office formats should land on their canonical type.
CASES = [
    ("fixture.docx", {"docx"}),
    ("fixture.xlsx", {"xlsx"}),
    ("fixture.pptx", {"pptx"}),
    ("fixture.doc", {"doc"}),
    ("fixture.xls", {"xls"}),
    ("fixture.ppt", {"ppt"}),
    ("fixture.odt", {"odt"}),
    ("fixture.ods", {"ods"}),
    ("fixture.odp", {"odp"}),
    ("fixture.odg", {"odg"}),
    ("fixture.rtf", {"rtf"}),
    ("fixture.txt", {"txt"}),
    ("fixture.csv", {"csv", "txt"}),
    ("fixture.md", {"md", "txt"}),
    ("fixture.xps", {"xps"}),
]


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
@pytest.mark.parametrize("filename,expected_labels", CASES)
def test_round_trip(converter, tmp_path: Path, filename: str, expected_labels: set[str]):
    src = SAFE / filename
    if not src.exists():
        pytest.skip(f"fixture {filename} not built (run build_safe_fixtures.py inside the Docker image)")
    out = tmp_path / "out"
    converter.convert(
        src, out, ConvertOptions(limits=Limits(timeout_s=120, max_pages=10))
    )

    meta = json.loads((out / "metadata.json").read_text())
    assert meta["render"]["page_count_rendered"] >= 1
    assert (out / "page-001.png").exists()
    # First page should have non-zero mm dimensions.
    assert meta["pages"][0]["width_mm"] > 0
    assert meta["pages"][0]["height_mm"] > 0
    # Detection should land on one of the expected labels.
    assert meta["input"]["detected"]["label"] in expected_labels
