"""Sandbox protocol and shared types."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from clippyshot.limits import Limits
from clippyshot.types import SandboxResult


@dataclass(frozen=True)
class Mount:
    host_path: Path
    sandbox_path: Path
    read_only: bool = True


@dataclass(frozen=True)
class SandboxRequest:
    argv: list[str]
    ro_mounts: list[Mount] = field(default_factory=list)
    rw_mounts: list[Mount] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    env: dict[str, str] = field(default_factory=dict)
    workdir: Path = Path("/")
    # Attach the soffice-specific AppArmor profile (bwrap aa-exec / nsjail --proc_apparmor)?
    # Default True. A stage that runs a *different* binary the soffice profile doesn't cover
    # (e.g. the pdfium rasterizer, which bind-mounts its own sys.prefix/venv) sets this False
    # so it isn't denied by a profile written for soffice — it stays confined by the
    # namespace + (nsjail) seccomp.
    attach_apparmor: bool = True


@runtime_checkable
class Sandbox(Protocol):
    """A backend that runs argv inside an isolated environment."""

    name: str

    def run(self, request: SandboxRequest) -> SandboxResult: ...

    def smoketest(self) -> SandboxResult:
        """Run `/bin/true` to confirm the backend is functional."""
        ...
