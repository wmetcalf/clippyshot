"""Command-line interface."""
from __future__ import annotations

import argparse
import dataclasses
import json
import shlex
import shutil
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


def _nsjail_selectable() -> bool:
    """Would select_sandbox accept nsjail? An insecure one (e.g. no seccomp policy) is
    refused unless CLIPPYSHOT_WARN_ON_INSECURE is set."""
    import os

    try:
        from clippyshot.sandbox.nsjail import NsjailSandbox

        sb = NsjailSandbox()
    except Exception as e:  # noqa: BLE001
        print(f"  nsjail backend unavailable: {e}")
        return False
    if sb.secure:
        return True
    allow = os.environ.get("CLIPPYSHOT_WARN_ON_INSECURE", "").strip().lower() not in (
        "", "0", "false", "no")
    print(f"  nsjail is insecure here ({', '.join(sb.insecurity_reasons)})"
          + ("; accepted because CLIPPYSHOT_WARN_ON_INSECURE is set" if allow else
             "; select_sandbox will refuse it"))
    return allow


def _setup_sandbox_cmd(args: argparse.Namespace) -> int:
    from clippyshot import setup_sandbox as ss
    from clippyshot.sandbox import host_apparmor as ha

    rep = ss.diagnose()
    print("Sandbox host check:")
    print(f"  apparmor_restrict_unprivileged_userns = {'1 (ACTIVE)' if rep.restrict_active else '0/absent'}")
    for note in rep.notes:
        print(f"  {note}")
    profile_dir = Path(args.profile_dir) if args.profile_dir else ss.default_profile_dir()
    disable_stock = args.disable_stock_bwrap_profile

    if rep.stock_conflict and rep.stock is not None:
        stock = ha.STOCK_BWRAP_PROFILE
        print(
            f"\nCONFLICT: Ubuntu's stock AppArmor profile {stock} ({rep.stock.describe()}).\n"
            "  It attaches to /usr/bin/bwrap as `bwrap` and confines every child bwrap starts\n"
            "  to `unpriv_bwrap`, which may not change profile, so `aa-exec -p "
            f"{ss.SOFFICE_PROFILE}` fails\n"
            "  (\"insufficient permissions to change to the profile\"). Loading clippyshot-bwrap\n"
            "  next to it breaks bwrap's uid map instead. ClippyShot's bwrap backend refuses\n"
            "  to start (fail closed) until it is disabled.\n"
            f"\n  COST: {stock} is host-wide hardening. Disabling it lets ANY local user or\n"
            "  bwrap consumer get a capability-bearing user namespace through /usr/bin/bwrap\n"
            "  (clippyshot-bwrap is unconfined + userns). Only do this on a host or VM dedicated\n"
            "  to ClippyShot with no untrusted local users; otherwise run ClippyShot in its own VM\n"
            "  or a gVisor/Firecracker tier, which never runs bwrap (deploy/apparmor/README.md)."
        )
        if not disable_stock:
            # The stock profile only conflicts with clippyshot-bwrap (both attach
            # /usr/bin/bwrap). Every other scheduled profile (clippyshot-nsjail,
            # clippyshot-soffice) is still loaded on --apply, so a host can keep the
            # distro's bwrap hardening.
            other = [a for a in rep.actions if a.binary != "bwrap"]
            if args.apply and other:
                if not profile_dir.is_dir():
                    print(f"\nprofile dir not found: {profile_dir} (pass --profile-dir)",
                          file=sys.stderr)
                    return 2
                print(f"\nLoading {len(other)} profile(s) unaffected by the conflict via sudo "
                      "(you'll be prompted for your password):")
                rc = ss.apply(other, profile_dir)
                if rc != 0:
                    return rc
                print(f"\nLoaded {', '.join(a.profile_name for a in other)}; the distro's "
                      "bwrap hardening is untouched.")
            if not other or args.apply:
                # Success is a working backend, not what this run happened to load: re-check
                # nsjail itself (it is unaffected by the stock profile).
                nsjail = shutil.which("nsjail")
                if nsjail and ss._userns_ok("nsjail", nsjail) and _nsjail_selectable():
                    print("\nThe nsjail backend is usable, with the distro's bwrap hardening "
                          "kept. Only the bwrap backend still needs "
                          f"`{ha.FIX_COMMAND}` (dedicated hosts only).")
                    return 0
                if args.apply:
                    print("\nNo backend is usable yet: nsjail is "
                          + ("not working" if nsjail else "not installed")
                          + f"; bwrap still needs `{ha.FIX_COMMAND}` (dedicated hosts only).")
                    return 1
            if other:
                print(f"\nTo load only what does not need this ({', '.join(a.profile_name for a in other)}),"
                      " keeping the distro's bwrap hardening, run:\n\n  "
                      f"{ha.LOAD_COMMAND}")
        try:
            plan = ss.stock_fix_plan(rep.stock, profile_dir)
        except ss.StockProfileUnmanaged as e:
            print(f"\n{e}", file=sys.stderr)
            return 1
        if not disable_stock:
            print(f"\nTo fix on a dedicated host, run:\n\n  {ha.FIX_COMMAND}\n\n"
                  f"which disables {stock} persistently and loads the four ClippyShot "
                  "profiles via sudo:\n")
            for c in plan:
                print("  " + shlex.join(c.argv))
            return 1
        missing = [p for p in ss.ALL_PROFILES if not (profile_dir / p).is_file()]
        if missing:
            # Check before the first sudo step: disabling the stock profile and then failing
            # to load clippyshot-bwrap would leave the host with neither.
            print(f"\nprofile file(s) missing in {profile_dir}: {', '.join(missing)} "
                  "(pass --profile-dir); nothing changed", file=sys.stderr)
            return 2
        if shutil.which("aa-exec") is None:
            # Without aa-exec ClippyShot can't attach clippyshot-soffice anyway: don't trade
            # away the distro hardening for a backend that still won't work.
            print("\naa-exec is not installed (package apparmor); install it first. "
                  "Nothing changed.", file=sys.stderr)
            return 2
        print(f"\nDisabling {stock} and loading the ClippyShot profiles via sudo "
              "(you'll be prompted for your password):")
        rc = ss.apply_plan(plan)
        if rc != 0:
            return rc
        ok, lines = ss.verify_attach()
        print("\nAfter:")
        for line in lines:
            print(f"  {line}")
        return 0 if ok else 1

    if disable_stock:
        print(f"\n{ha.STOCK_BWRAP_PROFILE}: not in conflict; nothing to disable.")
    if not rep.actions:
        probe = rep.aa_probe
        if probe is not None and not probe.ok:
            # Nothing this command can load fixes it (e.g. docker-default confinement).
            print(f"\nNo profile to load, but bwrap cannot attach {ss.SOFFICE_PROFILE}:\n"
                  f"  {probe.message()}", file=sys.stderr)
            return 1
        if shutil.which("bwrap") and not shutil.which("aa-exec"):
            print("\nWARNING: bwrap is installed but aa-exec is not, so the bwrap backend "
                  f"cannot attach {ss.SOFFICE_PROFILE} (it is refused unless "
                  "CLIPPYSHOT_WARN_ON_INSECURE=1). Install the apparmor package.")
        print("\nNothing to do — the namespace backends are usable (or unaffected).")
        return 0
    if not profile_dir.is_dir():
        print(f"\nprofile dir not found: {profile_dir} (pass --profile-dir)", file=sys.stderr)
        return 2
    if args.apply or disable_stock:
        print(f"\nLoading {len(rep.actions)} scoped profile(s) via sudo "
              "(you'll be prompted for your password):")
        rc = ss.apply(rep.actions, profile_dir)
        if rc != 0 or rep.aa_probe is None or rep.aa_probe.ok:
            return rc
        # The attach probe had failed: report whether loading fixed it.
        ok, lines = ss.verify_attach()
        print("\nAfter:")
        for line in lines:
            print(f"  {line}")
        return 0 if ok else 1
    print(f"\n{len(rep.actions)} scoped AppArmor profile(s) to load. Re-run with --apply to "
          "load them (sudo), or run:\n")
    for cmd in ss.commands_for(rep.actions, profile_dir):
        print("  " + shlex.join(cmd))
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

    pss = sub.add_parser(
        "setup-sandbox",
        help="detect (and with --apply, load) the AppArmor profiles bwrap/nsjail need; reports Ubuntu's stock bwrap-userns-restrict conflict",
    )
    pss.add_argument("--apply", action="store_true",
                     help="load the needed profiles via sudo (interactive); default just prints them")
    pss.add_argument(
        "--disable-stock-bwrap-profile", action="store_true",
        help="dedicated hosts only (removes host-wide bwrap hardening): disable Ubuntu's stock "
             "bwrap-userns-restrict AppArmor profile persistently (symlink into "
             "/etc/apparmor.d/disable/ + apparmor_parser -R), then load all four ClippyShot "
             "profiles via sudo and re-probe; implies --apply. Without it, a host where that "
             "profile is active is reported and setup exits non-zero",
    )
    pss.add_argument("--profile-dir", default=None,
                     help="directory holding the clippyshot-{bwrap,nsjail} profiles (default: repo deploy/apparmor)")
    pss.set_defaults(func=_setup_sandbox_cmd)
    return p


def main(argv: list[str] | None = None) -> int:
    configure_logging(format_="text")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
