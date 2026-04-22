"""Command-line interface."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from clippyshot._version import __version__
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
    run_selftest,
)
from clippyshot.worker import run_worker


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


def _convert_cmd(args: argparse.Namespace) -> int:
    limits = Limits.from_env(
        timeout_s=args.timeout,
        max_pages=args.max_pages,
        dpi=args.dpi,
    )
    # Apply CLI overrides that from_env doesn't accept directly.
    limits = dataclasses.replace(
        limits,
        skip_blanks=args.skip_blanks,
        disclose_security_internals=args.disclose_security_internals,
    )
    options = ConvertOptions(limits=limits)
    out_dir = Path(args.outdir)
    try:
        converter = _build_converter()
    except SandboxUnavailable as e:
        print(f"sandbox unavailable: {e}", file=sys.stderr)
        return 3
    try:
        result = converter.convert(Path(args.input), out_dir, options)
    except DetectionError as e:
        print(f"rejected: {e.reason}: {e.detail}", file=sys.stderr)
        return 2
    except (SandboxError, ConversionError) as e:
        print(f"conversion failed: {e}", file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print(f"input not found: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {e}", file=sys.stderr)
        return 4
    if args.json:
        print(json.dumps(result.metadata, indent=2))
    elif not args.quiet:
        print(f"wrote {len(result.metadata['pages'])} pages to {out_dir}")
    return 0


def _selftest_cmd(_: argparse.Namespace) -> int:
    return run_selftest()


def _serve_cmd(args: argparse.Namespace) -> int:
    import uvicorn

    from clippyshot.api import build_app

    app = build_app(
        job_store_kind=args.job_store,
        redis_url=args.redis_url,
        database_url=args.database_url,
    )
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
    return 0


def _worker_cmd(args: argparse.Namespace) -> int:
    return run_worker(args)


def _version_cmd(_: argparse.Namespace) -> int:
    print(f"clippyshot {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clippyshot")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("convert", help="convert a single document")
    pc.add_argument("input")
    pc.add_argument("-o", "--outdir", default="out")
    pc.add_argument("--dpi", type=int, default=150)
    pc.add_argument("--max-pages", type=int, default=50)
    pc.add_argument("--timeout", type=int, default=60)
    pc.add_argument("--json", action="store_true", help="emit metadata.json on stdout")
    pc.add_argument("--quiet", action="store_true")
    pc.add_argument("--skip-blanks", dest="skip_blanks", action="store_true",
                    default=False, help="drop blank pages from the output")
    pc.add_argument(
        "--disclose-security-internals",
        dest="disclose_security_internals",
        action="store_true",
        default=False,
        help=(
            "include sandbox backend name and AppArmor profile names in "
            "metadata.json security block (default: redacted)"
        ),
    )
    pc.set_defaults(func=_convert_cmd)

    ps = sub.add_parser("selftest", help="run a deployment health check")
    ps.set_defaults(func=_selftest_cmd)

    psv = sub.add_parser("serve", help="run the HTTP API")
    psv.add_argument("--host", default="127.0.0.1")
    psv.add_argument("--port", type=int, default=8000)
    psv.add_argument("--job-store", choices=["memory", "redis", "sql"], default="sql")
    psv.add_argument("--redis-url", default="redis://localhost:6379/0")
    psv.add_argument(
        "--database-url",
        default=os.environ.get("CLIPPYSHOT_DATABASE_URL", "sqlite:///./clippyshot-jobs.db"),
    )
    psv.set_defaults(func=_serve_cmd)

    pw = sub.add_parser("worker", help="process one mounted job directory")
    pw.add_argument("--job-dir", required=True, help="mounted job directory")
    pw.add_argument("--input", help="input file path inside the job directory")
    pw.add_argument("--output", help="output directory path inside the job directory")
    pw.add_argument("--job-id", help="optional job identifier for logging")
    pw.add_argument("--quiet", action="store_true")
    pw.set_defaults(func=_worker_cmd)

    pv = sub.add_parser("version")
    pv.set_defaults(func=_version_cmd)
    return p


def main(argv: list[str] | None = None) -> int:
    configure_logging(format_="text")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
