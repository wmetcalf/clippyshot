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


@runtime_checkable
class Sandbox(Protocol):
    """A backend that runs argv inside an isolated environment."""

    name: str

    def run(self, request: SandboxRequest) -> SandboxResult: ...

    def smoketest(self) -> SandboxResult:
        """Run `/bin/true` to confirm the backend is functional."""
        ...
