"""One-shot worker command for processing a single mounted job directory."""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from clippyshot.api import _parse_bool
from clippyshot.converter import Converter, ConvertOptions
from clippyshot.detector import Detector
from clippyshot.errors import (
    ConversionError,
    DetectionError,
    SandboxError,
    SandboxUnavailable,
)
from clippyshot.libreoffice.runner import LibreOfficeRunner
from clippyshot.limits import Limits
from clippyshot.observability import configure_logging, set_sandbox_backend
from clippyshot.rasterizer.pdftoppm import PdftoppmRasterizer
from clippyshot.sandbox.detect import select_sandbox
from clippyshot.selftest import (
    detect_runtime_apparmor_profile,
    detect_soffice_apparmor_profile,
)


def _build_converter() -> Converter:
    sandbox = select_sandbox()
    set_sandbox_backend(sandbox.name)
    return Converter(
        detector=Detector(),
        runner=LibreOfficeRunner(sandbox=sandbox),
        rasterizer=PdftoppmRasterizer(sandbox=sandbox),
        sandbox_backend=sandbox.name,
        sandbox=sandbox,
        runtime_apparmor_profile=detect_runtime_apparmor_profile(),
        soffice_apparmor_profile=detect_soffice_apparmor_profile(sandbox),
        seccomp=getattr(sandbox, "seccomp_source", "none"),
    )


def run_worker(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    input_path = Path(args.input) if args.input else job_dir / "input"
    output_dir = Path(args.output) if args.output else job_dir / "output"

    if not input_path.exists():
        print(f"input not found: {input_path}", file=sys.stderr)
        return 2
    if input_path.is_dir():
        files = sorted(path for path in input_path.iterdir() if path.is_file())
        if len(files) != 1:
            print(f"expected exactly one staged input file in {input_path}", file=sys.stderr)
            return 2
        input_path = files[0]

    limits = Limits.from_env()
    limits = dataclasses.replace(limits, disclose_security_internals=False)
    options = ConvertOptions(
        limits=limits,
        qr_enabled=_parse_bool(os.environ.get("CLIPPYSHOT_ENABLE_QR"), default=True),
        qr_formats=os.environ.get("CLIPPYSHOT_QR_FORMATS", "qr_code,micro_qr_code,rmqr_code"),
        qr_timeout_s=int(os.environ.get("CLIPPYSHOT_ZXING_TIMEOUT_S", "10")),
        ocr_enabled=_parse_bool(os.environ.get("CLIPPYSHOT_ENABLE_OCR"), default=False),
        ocr_all=_parse_bool(os.environ.get("CLIPPYSHOT_OCR_ALL"), default=False),
        ocr_lang=os.environ.get(
            "CLIPPYSHOT_OCR_LANG",
            "eng+Latin",
        ),
        ocr_psm=int(os.environ.get("CLIPPYSHOT_OCR_PSM", "3")),
        ocr_timeout_s=int(os.environ.get("CLIPPYSHOT_OCR_TIMEOUT_S", "60")),
    )

    try:
        converter = _build_converter()
    except SandboxUnavailable as e:
        print(f"sandbox unavailable: {e}", file=sys.stderr)
        return 3

    try:
        result = converter.convert(input_path, output_dir, options)
    except DetectionError as e:
        print(f"rejected: {e.reason}: {e.detail}", file=sys.stderr)
        return 2
    except (SandboxError, ConversionError) as e:
        print(f"worker failed: {e}", file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print(f"input not found: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {e}", file=sys.stderr)
        return 4

    if not getattr(args, "quiet", False):
        pages = len(result.metadata.get("pages", []))
        job_suffix = f" job={args.job_id}" if getattr(args, "job_id", None) else ""
        print(f"wrote {pages} pages to {output_dir}{job_suffix}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clippyshot worker")
    parser.add_argument("--job-dir", required=True, help="mounted job directory")
    parser.add_argument("--input", help="input file path inside the job directory")
    parser.add_argument("--output", help="output directory path inside the job directory")
    parser.add_argument("--job-id", help="optional job identifier for logging")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging(format_="text")
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_worker(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
