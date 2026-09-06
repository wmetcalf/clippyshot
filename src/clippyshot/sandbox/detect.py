"""Sandbox backend auto-selection."""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Callable

from clippyshot.errors import SandboxUnavailable
from clippyshot.sandbox.base import Sandbox
from clippyshot.sandbox.bwrap import BwrapSandbox
from clippyshot.sandbox.container import ContainerSandbox
from clippyshot.sandbox.nono_wrap import (
    NonoWrap,
    NonoWrappedSandbox,
    landlock_available,
)
from clippyshot.sandbox.nsjail import NsjailSandbox


_log = logging.getLogger("clippyshot.sandbox")


def _env_truthy(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw not in ("", "0", "false", "no")


def _security_state(sb: Sandbox) -> tuple[bool, list[str]]:
    secure = bool(getattr(sb, "secure", False))
    reasons = list(getattr(sb, "insecurity_reasons", []))
    return secure, reasons


def _keep_existing_error(current: Exception | None) -> bool:
    return current is not None and "insecure" in str(current)


def select_sandbox(
    *,
    inner_wrap: NonoWrap | None = None,
    _nsjail_factory: Callable[[], Sandbox] = NsjailSandbox,
    _bwrap_factory: Callable[[], Sandbox] = BwrapSandbox,
    _container_factory: Callable[[], Sandbox] = ContainerSandbox,
) -> Sandbox:
    """Select the best available sandbox backend.

    Order: nsjail → bwrap → container. Each candidate is constructed AND
    smoketest-verified before being accepted. A backend that constructs
    successfully but fails /bin/true is skipped in favour of the next.

    CLIPPYSHOT_SANDBOX env var (nsjail|bwrap|container) forces a specific
    backend; no silent fallback if forced.

    ``inner_wrap`` (a :class:`~clippyshot.sandbox.nono_wrap.NonoWrap`) is an OPTIONAL
    Landlock layer nested *inside* the selected backend — opt-in via this arg or the
    ``CLIPPYSHOT_INNER_NONO`` env. Default ``None`` leaves the selected backend exactly
    as-is (zero cost). It decorates whichever backend is chosen, so it composes with
    nsjail/bwrap/container alike.
    """
    if inner_wrap is None and _env_truthy("CLIPPYSHOT_INNER_NONO"):
        profile = os.environ.get("CLIPPYSHOT_INNER_NONO_PROFILE", "").strip()
        inner_wrap = NonoWrap(profile=Path(profile) if profile else None)

    # Fail fast on a tier that can't enforce Landlock (e.g. the gVisor Sentry returns
    # ENOSYS) rather than letting nono error mid-conversion. runc + the FC guest support
    # Landlock; gVisor C/R does not — don't enable the inner layer there.
    if inner_wrap is not None and not landlock_available():
        raise SandboxUnavailable(
            "inner nono layer requested (CLIPPYSHOT_INNER_NONO / inner_wrap) but Landlock "
            "is unavailable on this kernel/runtime (the gVisor Sentry does not implement it); "
            "disable inner-nono on this tier — runc and the Firecracker guest support it"
        )

    def _wrap(sb: Sandbox) -> Sandbox:
        return NonoWrappedSandbox(sb, inner_wrap) if inner_wrap is not None else sb

    factories: dict[str, Callable[[], Sandbox]] = {
        "nsjail": _nsjail_factory,
        "bwrap": _bwrap_factory,
        "container": _container_factory,
    }
    forced = os.environ.get("CLIPPYSHOT_SANDBOX", "").strip().lower()
    allow_insecure = _env_truthy("CLIPPYSHOT_WARN_ON_INSECURE")
    if forced:
        if forced not in factories:
            raise SandboxUnavailable(
                f"CLIPPYSHOT_SANDBOX={forced!r} is not a valid backend; "
                f"valid values are: {sorted(factories)}"
            )
        sb = factories[forced]()
        smoke = sb.smoketest()
        if smoke.exit_code != 0 or smoke.killed:
            raise SandboxUnavailable(
                f"forced backend {forced!r} failed smoketest "
                f"(exit={smoke.exit_code}, killed={smoke.killed})"
            )
        secure, reasons = _security_state(sb)
        if not secure:
            detail = ", ".join(reasons) or "unspecified"
            if not allow_insecure:
                raise SandboxUnavailable(
                    f"forced backend {forced!r} is insecure: {detail}"
                )
            _log.warning(
                "sandbox backend selected in insecure mode",
                extra={"backend": forced, "reasons": reasons},
            )
        _log.info("sandbox backend selected (forced)", extra={"backend": forced})
        return _wrap(sb)

    last_error: Exception | None = None
    for name in ("nsjail", "bwrap", "container"):
        factory = factories[name]
        try:
            sb = factory()
        except SandboxUnavailable as e:
            if not _keep_existing_error(last_error):
                last_error = e
            _log.debug(
                "sandbox backend unavailable",
                extra={"backend": name, "error": str(e)},
            )
            continue
        try:
            smoke = sb.smoketest()
        except Exception as e:  # noqa: BLE001
            if not _keep_existing_error(last_error):
                last_error = e
            _log.debug(
                "sandbox backend smoketest raised",
                extra={"backend": name, "error": str(e)},
            )
            continue
        if smoke.exit_code != 0 or smoke.killed:
            last_error = SandboxUnavailable(
                f"{name} smoketest exit={smoke.exit_code} killed={smoke.killed}"
            )
            _log.debug(
                "sandbox backend smoketest non-zero",
                extra={
                    "backend": name,
                    "exit_code": smoke.exit_code,
                    "killed": smoke.killed,
                    "stderr": smoke.stderr.decode(errors="replace")[:200],
                },
            )
            continue
        secure, reasons = _security_state(sb)
        if not secure:
            detail = ", ".join(reasons) or "unspecified"
            if not allow_insecure:
                last_error = SandboxUnavailable(f"{name} insecure: {detail}")
                _log.warning(
                    "sandbox backend rejected as insecure",
                    extra={"backend": name, "reasons": reasons},
                )
                continue
            _log.warning(
                "sandbox backend selected in insecure mode",
                extra={"backend": name, "reasons": reasons},
            )
        _log.info("sandbox backend selected", extra={"backend": name})
        return _wrap(sb)

    raise SandboxUnavailable(
        f"no sandbox backend available; last error: {last_error}"
    )


# Layers applied to EVERY command, as opposed to a tier where the boundary is the
# container or guest around the whole worker.
#
# `nono` counts. `NonoWrappedSandbox.run()` is `inner.run(self.wrap.apply(request))` --
# Landlock applied per request -- so `container+nono` confines each conversion even
# though the container is the outer boundary, and a warm server started outside it skips
# that layer exactly as it would skip nsjail's (codex).
_PER_CALL_BACKENDS = frozenset({"nsjail", "bwrap", "nono"})


def sandboxes_each_call(sandbox: object) -> bool:
    """Does this selection wrap every individual command in its own sandbox?

    Asked by the warm tiers before starting a persistent helper: where each cold
    invocation is sandboxed, a long-lived server outside that wrapper would carry the
    untrusted input past a boundary the cold path insists on.

    Decoration-proof by construction. `CLIPPYSHOT_INNER_NONO=1` returns a
    `NonoWrappedSandbox` whose name is `nsjail+nono`, and an exact-name check silently
    stopped matching -- so the guard was off for exactly the deployment that had asked
    for MORE confinement (codex). This checks each `+`-separated component AND walks the
    `inner` chain, so a decorator that renames entirely is still seen through.
    """
    seen = 0
    while sandbox is not None and seen < 8:   # bounded: a cycle must not hang warmup
        name = str(getattr(sandbox, "name", "") or "")
        if any(part in _PER_CALL_BACKENDS for part in name.split("+")):
            return True
        sandbox = getattr(sandbox, "inner", None)
        seen += 1
    return False


def per_call_sandbox_possible() -> bool:
    """Could a per-call sandbox be selected on this host, even though selection just failed?

    Asked when `select_sandbox()` RAISES during warmup. The exception type cannot answer
    it: `select_sandbox` turns a flaky smoketest into `SandboxUnavailable` too, so that
    error means "none usable right now", not "none installed" (codex). Starting a warm
    parser on it would be starting one on a transient failure, and `_build_converter()`
    retries later -- possibly succeeding with nsjail, by which time the parser is already
    outside it.

    So this asks the host instead, which is stable across a flake: is a per-call backend
    even POSSIBLE here? A warm FC/gVisor guest has no nsjail and no bwrap, so the answer
    is no and the warm tier keeps working; a host that has them declines until selection
    is answerable again.
    """
    # nono FIRST: it decorates whatever is selected, so it makes even a forced
    # `container` per-call. Checking the forced backend first returned False for
    # `CLIPPYSHOT_SANDBOX=container` + `CLIPPYSHOT_INNER_NONO=1`, which is precisely the
    # combination the previous round was about.
    if _env_truthy("CLIPPYSHOT_INNER_NONO"):
        return True
    forced = os.environ.get("CLIPPYSHOT_SANDBOX", "").strip().lower()
    if forced:
        return any(part in _PER_CALL_BACKENDS for part in forced.split("+"))
    return bool(shutil.which("nsjail") or shutil.which("bwrap"))
