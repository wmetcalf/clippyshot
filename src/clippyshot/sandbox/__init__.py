"""Sandbox abstraction and backends."""
from clippyshot.sandbox.base import Mount, Sandbox, SandboxRequest
from clippyshot.sandbox.container import ContainerSandbox

__all__ = ["ContainerSandbox", "Mount", "Sandbox", "SandboxRequest"]
