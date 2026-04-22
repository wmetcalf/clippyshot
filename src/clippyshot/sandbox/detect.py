"""Sandbox backend auto-selection."""
from __future__ import annotations

import logging
import os
from typing import Callable

from clippyshot.errors import SandboxUnavailable
from clippyshot.sandbox.base import Sandbox
from clippyshot.sandbox.bwrap import BwrapSandbox
from clippyshot.sandbox.container import ContainerSandbox
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
    """
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
        return sb

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
        return sb

    raise SandboxUnavailable(
        f"no sandbox backend available; last error: {last_error}"
    )
