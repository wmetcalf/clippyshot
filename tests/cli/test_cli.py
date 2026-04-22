import json
import subprocess
import sys
from pathlib import Path

from clippyshot import worker
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


def test_cli_help_lists_subcommands():
    r = subprocess.run(CLI + ["--help"], capture_output=True, text=True, check=True)
    assert "convert" in r.stdout
    assert "selftest" in r.stdout
    assert "serve" in r.stdout
    assert "worker" in r.stdout
    assert "version" in r.stdout


def test_worker_help_lists_job_arguments():
    r = subprocess.run(CLI + ["worker", "--help"], capture_output=True, text=True, check=True)
    assert "--job-dir" in r.stdout
    assert "--input" in r.stdout
    assert "--output" in r.stdout


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


def test_worker_command_processes_single_job_dir(tmp_path: Path, monkeypatch):
    job_dir = tmp_path / "job"
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True)
    input_file = input_dir / "sample.docx"
    input_file.write_bytes(b"payload")

    class FakeConverter:
        def convert(self, input_path, outdir, options):
            assert input_path == input_file
            assert outdir == output_dir
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "metadata.json").write_text(json.dumps({"pages": []}))
            (outdir / "page-001.png").write_bytes(b"png")

            class Result:
                metadata = {"pages": []}

            return Result()

    monkeypatch.setattr(worker, "_build_converter", lambda: FakeConverter())

    exit_code = worker.main([
        "--job-dir", str(job_dir),
        "--input", str(input_file),
        "--output", str(output_dir),
        "--job-id", "job-123",
    ])

    assert exit_code == 0
    assert (output_dir / "metadata.json").exists()
    assert (output_dir / "page-001.png").exists()


def test_worker_command_uses_single_staged_file_from_input_dir(tmp_path: Path, monkeypatch):
    job_dir = tmp_path / "job"
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True)
    staged_file = input_dir / "sample.docx"
    staged_file.write_bytes(b"payload")

    class FakeConverter:
        def convert(self, input_path, outdir, options):
            assert input_path == staged_file
            assert outdir == output_dir

            class Result:
                metadata = {"pages": []}

            return Result()

    monkeypatch.setattr(worker, "_build_converter", lambda: FakeConverter())

    exit_code = worker.main(["--job-dir", str(job_dir), "--output", str(output_dir), "--quiet"])

    assert exit_code == 0


def test_worker_command_exits_nonzero_on_failure(tmp_path: Path, monkeypatch, capsys):
    job_dir = tmp_path / "job"
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True)
    input_file = input_dir / "sample.docx"
    input_file.write_bytes(b"payload")

    def boom():
        class FakeConverter:
            def convert(self, input_path, outdir, options):
                raise RuntimeError("boom")

        return FakeConverter()

    monkeypatch.setattr(worker, "_build_converter", boom)

    exit_code = worker.main([
        "--job-dir", str(job_dir),
        "--input", str(input_file),
        "--output", str(output_dir),
    ])

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "boom" in captured.err.lower()


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
