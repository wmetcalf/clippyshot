import json
import subprocess
import sys
from pathlib import Path

from tests.conftest import (
    needs_any_sandbox,
    needs_bwrap_userns,
    needs_pdftoppm,
    needs_soffice,
)

REPO = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "clippyshot.cli"]
FIXTURES = REPO / "tests" / "fixtures" / "safe"


def test_cli_version_outputs_string():
    r = subprocess.run(CLI + ["version"], capture_output=True, text=True, check=True)
    assert "clippyshot" in r.stdout.lower()


def test_cli_help_lists_only_pipeline_subcommands():
    # The host (serve/dispatch/worker) moved to blastbox.host; ClippyShot's CLI
    # keeps only the in-process pipeline commands.
    r = subprocess.run(CLI + ["--help"], capture_output=True, text=True, check=True)
    assert "convert" in r.stdout
    assert "selftest" in r.stdout
    assert "version" in r.stdout
    assert "serve" not in r.stdout
    assert "worker" not in r.stdout


def test_cli_serve_and_worker_are_gone():
    # argparse rejects the retired subcommands (exit 2, "invalid choice").
    for cmd in ("serve", "worker"):
        r = subprocess.run(CLI + [cmd], capture_output=True, text=True)
        assert r.returncode == 2
        assert "invalid choice" in r.stderr.lower()


@needs_any_sandbox
def test_cli_convert_unknown_input_returns_exit_2(tmp_path: Path):
    bad = tmp_path / "garbage.bin"
    bad.write_bytes(b"\x00" * 16)
    r = subprocess.run(
        CLI + ["convert", str(bad), "-o", str(tmp_path / "out")],
        capture_output=True, text=True,
    )
    assert r.returncode == 2


def test_cli_convert_missing_input_returns_nonzero(tmp_path: Path):
    r = subprocess.run(
        CLI + ["convert", str(tmp_path / "does_not_exist.docx"),
               "-o", str(tmp_path / "out")],
        capture_output=True, text=True,
    )
    assert r.returncode != 0


@needs_soffice
@needs_pdftoppm
@needs_bwrap_userns
def test_cli_convert_real_docx(tmp_path: Path):
    out = tmp_path / "out"
    r = subprocess.run(
        CLI + ["convert", str(FIXTURES / "tiny.docx"), "-o", str(out), "--json"],
        capture_output=True, text=True, check=True,
    )
    meta = json.loads(r.stdout)
    assert meta["render"]["page_count_rendered"] >= 1
    assert (out / "page-001.png").exists()
