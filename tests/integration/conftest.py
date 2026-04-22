"""Shared fixtures for the integration test suite."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from clippyshot.converter import Converter
from clippyshot.detector import Detector
from clippyshot.libreoffice.runner import LibreOfficeRunner
from clippyshot.rasterizer.pdftoppm import PdftoppmRasterizer
from clippyshot.sandbox.detect import select_sandbox
from clippyshot.selftest import detect_apparmor_profile

pytestmark = [pytest.mark.integration]

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def pytest_configure(config) -> None:  # noqa: ANN001
    """Build any missing malicious fixtures at collection time.

    This runs before any tests are collected so that 'if not src.exists():
    pytest.skip(...)' guards in the test bodies never fire due to a missing
    fixture when the builder is available and has no external deps.
    """
    builder_path = _FIXTURES_DIR / "build_malicious_fixtures.py"
    malicious_dir = _FIXTURES_DIR / "malicious"
    odt_fixture = malicious_dir / "macro_autoopen.odt"
    if not odt_fixture.exists() and builder_path.exists():
        # Import and call just the one builder to avoid regenerating all fixtures.
        spec_dir = str(_FIXTURES_DIR)
        if spec_dir not in sys.path:
            sys.path.insert(0, str(_FIXTURES_DIR.parent.parent))
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "build_malicious_fixtures", builder_path
            )
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            mod.build_macro_autoopen_odt()
        except Exception:  # pragma: no cover - best-effort at collection time
            pass


@pytest.fixture(scope="module")
def converter() -> Converter:
    sandbox = select_sandbox()
    return Converter(
        detector=Detector(),
        runner=LibreOfficeRunner(sandbox=sandbox),
        rasterizer=PdftoppmRasterizer(sandbox=sandbox),
        sandbox_backend=sandbox.name,
        apparmor_profile=detect_apparmor_profile(),
    )


@pytest.fixture(scope="session")
def escape_probe(tmp_path_factory):
    """Build the C escape probe binary once per test session."""
    src = Path(__file__).parent / "escape_probe.c"
    if shutil.which("cc") is None:
        pytest.skip("no C compiler available to build escape probe")
    out = tmp_path_factory.mktemp("probe") / "escape_probe"
    subprocess.run(["cc", "-O2", "-o", str(out), str(src)], check=True)
    return out


@pytest.fixture(scope="session")
def qr_fixture_png(tmp_path_factory):
    """300x300 PNG containing the QR encoding of 'Hello QR'."""
    import qrcode
    img = qrcode.make("Hello QR", box_size=10, border=4)
    path = tmp_path_factory.mktemp("qr") / "hello.png"
    img.save(path)
    return path


@pytest.fixture(scope="session")
def text_fixture_png(tmp_path_factory):
    """PNG of 'Hello OCR' rendered in a large bold font."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (400, 100), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
    except (OSError, IOError):
        font = ImageFont.load_default()
    d.text((20, 20), "Hello OCR", fill="black", font=font)
    path = tmp_path_factory.mktemp("ocr") / "hello.png"
    img.save(path)
    return path
