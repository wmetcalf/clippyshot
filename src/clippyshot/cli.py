"""Command-line interface."""
from __future__ import annotations

import argparse
import dataclasses
import json
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
from clippyshot.rasterizer import build_rasterizer
from clippyshot.sandbox.detect import select_sandbox
from clippyshot.selftest import (
    detect_runtime_apparmor_profile,
    detect_soffice_apparmor_profile,
    run_selftest,
)


def _build_converter() -> Converter:
    sandbox = select_sandbox()
    set_sandbox_backend(sandbox.name)
    return Converter(
        detector=Detector(),
        runner=LibreOfficeRunner(sandbox=sandbox),
        rasterizer=build_rasterizer(sandbox),
        sandbox_backend=sandbox.name,
        sandbox=sandbox,
        runtime_apparmor_profile=detect_runtime_apparmor_profile(),
        soffice_apparmor_profile=detect_soffice_apparmor_profile(sandbox),
        seccomp=getattr(sandbox, "seccomp_source", "none"),
    )


def _convert_cmd(args: argparse.Namespace) -> int:
    # Splat ONLY the flags the user actually passed (default=None) so an unset flag falls through
    # to the CLIPPYSHOT_* env var via from_env, then the dataclass default (L4). Passing the
    # argparse defaults unconditionally clobbered the env.
    overrides: dict = {}
    if args.timeout is not None:
        overrides["timeout_s"] = args.timeout
    if args.max_pages is not None:
        overrides["max_pages"] = args.max_pages
    if args.dpi is not None:
        overrides["dpi"] = args.dpi
    limits = Limits.from_env(**overrides)
    # Apply CLI-only flags that from_env doesn't accept directly — only when explicitly passed.
    replace_kw: dict = {}
    if args.skip_blanks is not None:
        replace_kw["skip_blanks"] = args.skip_blanks
    if args.disclose_security_internals is not None:
        replace_kw["disclose_security_internals"] = args.disclose_security_internals
    if replace_kw:
        limits = dataclasses.replace(limits, **replace_kw)
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


def _version_cmd(_: argparse.Namespace) -> int:
    print(f"clippyshot {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clippyshot")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("convert", help="convert a single document")
    pc.add_argument("input")
    pc.add_argument("-o", "--outdir", default="out")
    # default=None so an unset flag falls through to CLIPPYSHOT_* env (via Limits.from_env) and
    # then the dataclass default — flag > env > default. With argparse defaults the flag value
    # would always clobber the env (L4). The dataclass defaults are dpi=150/max_pages=50/
    # timeout_s=60/skip_blanks=True.
    pc.add_argument("--dpi", type=int, default=None)
    pc.add_argument("--max-pages", type=int, default=None)
    pc.add_argument("--timeout", type=int, default=None)
    pc.add_argument("--json", action="store_true", help="emit metadata.json on stdout")
    pc.add_argument("--quiet", action="store_true")
    pc.add_argument("--skip-blanks", dest="skip_blanks", action="store_true",
                    default=None, help="drop blank pages from the output")
    pc.add_argument(
        "--disclose-security-internals",
        dest="disclose_security_internals",
        action="store_true",
        default=None,
        help=(
            "include sandbox backend name and AppArmor profile names in "
            "metadata.json security block (default: redacted)"
        ),
    )
    pc.set_defaults(func=_convert_cmd)

    ps = sub.add_parser("selftest", help="run a deployment health check")
    ps.set_defaults(func=_selftest_cmd)

    # The HTTP API + dispatcher + worker now run on blastbox.host:
    #   blastbox serve --allowed-engines clippyshot   (BLASTBOX_INGRESS_EXTENSION=clippyshot.blastbox_ingress:make_extension)
    #   blastbox dispatch                             (BLASTBOX_ENGINES=clippyshot=<cold-worker-image>)
    #   python -m blastbox.worker.cold                (BLASTBOX_ENGINE=clippyshot.engine:ClippyShotEngine)
    # ClippyShot's CLI keeps only the in-process pipeline commands.

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
