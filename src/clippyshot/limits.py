"""Resource limits for sandboxed conversions."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


_ENV_PREFIX = "CLIPPYSHOT_"
_ENV_MAP = {
    "timeout_s": "TIMEOUT",
    "memory_bytes": "MEM",
    "tmpfs_bytes": "TMPFS",
    "max_input_bytes": "MAX_INPUT",
    "max_pages": "MAX_PAGES",
    "dpi": "DPI",
    "skip_blanks": "SKIP_BLANKS",
    "disclose_security_internals": "DISCLOSE_SECURITY_INTERNALS",
    "max_width_px": "MAX_WIDTH",
    "max_height_px": "MAX_HEIGHT",
}

# Map field name → coerce function for env-var parsing.
_ENV_COERCE: dict[str, object] = {
    "skip_blanks": lambda s: s.lower() not in ("0", "false", "no"),
    "disclose_security_internals": lambda s: s.lower() not in ("0", "false", "no"),
}


@dataclass(frozen=True)
class Limits:
    """Strict-by-default limits, overridable per call or via env."""

    timeout_s: int = 60
    # Virtual address space limit. LibreOffice uses 4-8GB of VADDR for a
    # complex presentation (mmap'd libraries, thread stacks, file I/O)
    # while only consuming ~500MB RSS. The container's Docker --memory
    # flag is the real RSS cap; RLIMIT_AS just needs to be high enough
    # to not SIGSEGV soffice on legitimate docs.
    memory_bytes: int = 8 * 1024 * 1024 * 1024  # 8GB VADDR
    tmpfs_bytes: int = 1024 * 1024 * 1024
    max_input_bytes: int = 100 * 1024 * 1024
    max_pages: int = 50
    dpi: int = 150
    skip_blanks: bool = True
    disclose_security_internals: bool = False
    max_width_px: int = 32768
    max_height_px: int = 32768

    # Hard ceilings for the byte/pixel caps. These exist so a hostile or
    # fat-fingered env var can't silently *disable* a cap (e.g. MAX_WIDTH=0
    # turning off the decompression-bomb guard) or wrap to a nonsensical
    # value. 64 GiB / 256k px are far above any legitimate document.
    _MAX_BYTES_CEILING = 64 * 1024 * 1024 * 1024
    _MAX_PX_CEILING = 262144

    def __post_init__(self) -> None:
        if not 36 <= self.dpi <= 600:
            raise ValueError(f"dpi must be in [36, 600], got {self.dpi}")
        if not 1 <= self.max_pages <= 1000:
            raise ValueError(f"max_pages must be in [1, 1000], got {self.max_pages}")
        if not 1 <= self.timeout_s <= 600:
            raise ValueError(f"timeout_s must be in [1, 600], got {self.timeout_s}")
        for name in ("memory_bytes", "tmpfs_bytes", "max_input_bytes"):
            val = getattr(self, name)
            if not 1 <= val <= self._MAX_BYTES_CEILING:
                raise ValueError(
                    f"{name} must be in [1, {self._MAX_BYTES_CEILING}], got {val}"
                )
        for name in ("max_width_px", "max_height_px"):
            val = getattr(self, name)
            if not 1 <= val <= self._MAX_PX_CEILING:
                raise ValueError(
                    f"{name} must be in [1, {self._MAX_PX_CEILING}], got {val}"
                )

    @classmethod
    def from_env(cls, **overrides) -> "Limits":
        values: dict = {}
        for f in fields(cls):
            env_key = _ENV_PREFIX + _ENV_MAP[f.name]
            raw = os.environ.get(env_key)
            if raw is not None:
                coerce = _ENV_COERCE.get(f.name, int)
                try:
                    values[f.name] = coerce(raw)
                except (ValueError, TypeError) as e:
                    # Fail loudly with the offending var name rather than
                    # crashing deep in the dataclass with an opaque message.
                    raise ValueError(
                        f"invalid value for {env_key}={raw!r}: {e}"
                    ) from e
        values.update(overrides)
        return cls(**values)
