"""Resource limits for sandboxed conversions."""
from __future__ import annotations

import os
from collections.abc import Callable
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
    "rasterizer": "RASTERIZER",
}

# Map field name → coerce function for env-var parsing.
_ENV_COERCE: dict[str, Callable[[str], object]] = {
    # default-ON: strip/lower + include "off" so `False`/` no `/`Off` actually disable it.
    "skip_blanks": lambda s: s.strip().lower() not in ("0", "false", "no", "off"),
    # security flag (True = MORE disclosure) -> FAIL-CLOSED: only an explicit truthy token enables it, so a
    # typo / whitespace / "off" can't silently flip on security-internals disclosure.
    "disclose_security_internals": lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
    "rasterizer": lambda s: s.strip().lower(),
}

# PDF-to-PNG rasterizer backends, selectable via CLIPPYSHOT_RASTERIZER.
_RASTERIZERS = ("pdfium", "pdftoppm")


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
    rasterizer: str = "pdfium"

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
        if self.rasterizer not in _RASTERIZERS:
            raise ValueError(
                f"rasterizer must be one of {_RASTERIZERS}, got {self.rasterizer!r}"
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


# ---------------------------------------------------------------------------
# Page-operation parallelism (pipeline-side; host worker-launch sizing moved to
# blastbox.host when ClippyShot adopted blastbox.host).
# ---------------------------------------------------------------------------

# Rough upper-bound for peak RAM of one in-flight page buffer during
# rasterization (pdftoppm/PDFium) or per-page post-processing (PIL loading the
# PNG for hash/trim/focus). A letter page at 150 DPI is ~6 MB; pathological
# spreadsheet renders can hit ~150 MB — estimate 200 MB so the cap errs safe.
_PER_PAGE_PEAK_MB = 200
# Absolute ceiling on parallel page ops even on huge hosts — beyond ~8 the
# wall-clock win flattens and gVisor/kernel contention dominates.
_ABSOLUTE_PAGE_OP_CEILING = 8


def resolve_worker_memory(explicit: str | None = None) -> str:
    """The memory this worker actually has, as a docker-style spec.

    Only ``CLIPPYSHOT_WORKER_MEMORY`` was consulted, so a deployment that sets
    the blastbox-side limit and not this one budgeted for the 4 GB default on a
    smaller worker -- the FC guest runs at ``BLASTBOX_FC_MEM_MIB`` (2048 on the
    stock overlay) and the cold worker at ``BLASTBOX_WORKER_MEMORY`` (3g on the
    fleet). Budgeting a page at twice the memory the worker has is how an
    oversized render OOMs a guest that had a correct limit configured all along.

    In order: this call's own argument, the clippyshot name, the blastbox names,
    then what the OS says -- the cgroup limit (the container's REAL cap; MemTotal
    reports the host's RAM inside a container) and finally MemTotal, which is the
    right answer inside a microVM guest. ``4g`` only when nothing answers.
    """
    for value in (explicit, os.environ.get("CLIPPYSHOT_WORKER_MEMORY"),
                  os.environ.get("BLASTBOX_WORKER_MEMORY")):
        if value and value.strip():
            return value.strip()
    mib = (os.environ.get("BLASTBOX_FC_MEM_MIB") or "").strip()
    if mib.isdigit() and int(mib) > 0:
        return f"{mib}m"
    # Detection may only LOWER the budget. `MemTotal` inside a container with no
    # memory limit reports the HOST's RAM -- on a 64 GB node that would budget a
    # single page against 64 GB and OOM the worker, which is worse than the 4 GB
    # assumption it replaced. Below the default it is exactly the signal we want:
    # an FC guest at 2048 MiB reads its own 2 GB and stops being budgeted as 4.
    for path, unit in ((_CGROUP_MAX, ""), (_MEMINFO, "k")):
        detected = _read_memory_limit(path, unit)
        if detected and 0 < parse_memory_gb(detected) < _DEFAULT_WORKER_GB:
            return detected
    return _DEFAULT_WORKER_MEMORY


_DEFAULT_WORKER_MEMORY = "4g"
_DEFAULT_WORKER_GB = 4.0
_CGROUP_MAX = "/sys/fs/cgroup/memory.max"
_MEMINFO = "/proc/meminfo"


def _read_memory_limit(path: str, unit: str) -> str | None:
    """A memory spec read from the OS, or None when it cannot be read.

    Never raises and never returns a nonsense figure: an unreadable file, the
    cgroup's literal ``max`` (no limit set), or a non-numeric line all mean "ask
    the next source", because a wrong number here silently mis-sizes every page
    budget on the worker.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    if path == _MEMINFO:
        for line in text.splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                return f"{parts[1]}{unit}" if len(parts) > 1 and parts[1].isdigit() else None
        return None
    value = text.strip()
    return f"{value}{unit}" if value.isdigit() and int(value) > 0 else None


def parse_memory_gb(spec: str) -> float:
    """Parse a docker-style memory spec like '4g', '512m', '1024' into GB."""
    if not spec:
        return 0.0
    s = spec.strip().lower()
    try:
        if s.endswith("g"):
            return float(s[:-1])
        if s.endswith("m"):
            return float(s[:-1]) / 1024.0
        if s.endswith("k"):
            return float(s[:-1]) / (1024.0 * 1024.0)
        # Plain number — assume bytes.
        return float(s) / (1024.0 ** 3)
    except ValueError:
        return 0.0


# Per-page pixel budget. Sharding bounds page CONCURRENCY (how many render at
# once) but NOT the size of a single page. A 14400pt SinglePageSheets sheet at
# 150 DPI is ~30000px/side → ~900 MP → one ~3.6 GB RGBA bitmap (+ a comparable
# PNG-encode buffer) that OOM-wedges a memory-capped warm guest — which then makes
# zero progress and burns the whole worker timeout. The rasterizer clamps render
# DPI so the largest page fits this budget: a valid downscaled image, not an OOM.
_MIN_PAGE_PX = 1_000_000            # 1 MP floor — never clamp below a thumbnail.
_MAX_PAGE_PX_CEILING = 200_000_000  # 200 MP hard ceiling regardless of RAM / env.

# Largest page the MAIN-process derivative/scan pipeline (trimmer.py trim/focus,
# scanners) will materialize as a numpy RGB array. A 100 MP page is ~300 MB RGB
# plus transient int16/bool masks (~1.5 GB peak) — the knee for a 4 GB worker.
# The rasterizer's per-page render budget is capped at this so a page we RENDER is
# always one the post-processors can consume — otherwise the giant SinglePageSheets
# spreadsheets that trim/focus exist for would render but never get a derivative.
MAX_POSTPROCESS_PX = 100_000_000


def max_page_px(worker_memory_spec: str | None = None) -> int:
    """Largest single-page pixel area (w*h px) the rasterizer renders before it
    downscales, derived from the worker's memory budget.

    One page costs ~4 B/px RGBA plus a comparable PNG-encode buffer; budget a
    single page at ~1/8 of worker RAM for the RGBA bitmap (leaving the rest for
    the LibreOffice/unoserver resident set, Python, and the encode). The DERIVED
    budget is additionally capped at :data:`MAX_POSTPROCESS_PX` so every rendered
    page stays trim/focus/scan-eligible.

    NOTE: the RAM figure comes from ``CLIPPYSHOT_WORKER_MEMORY`` (same source as
    :func:`max_concurrent_page_ops`), which MUST track the worker's real cgroup
    cap. A deploy that sets only the host-side ``BLASTBOX_WORKER_MEMORY`` and
    leaves ``CLIPPYSHOT_WORKER_MEMORY`` at the 4 GB default on a smaller worker
    would over-budget the render — keep the two in sync (deploy/docker/.env does). Bounded by a 1 MP floor and a 200 MP
    ceiling so a hostile/fat-fingered env can neither disable the guard nor wrap it
    to nonsense. ``CLIPPYSHOT_MAX_PAGE_PX`` overrides the derivation (honored up to
    the 200 MP ceiling — an operator raising it above the post-process budget
    knowingly forfeits derivatives on pages larger than that)."""
    override = os.environ.get("CLIPPYSHOT_MAX_PAGE_PX")
    if override:
        try:
            val = int(override)
        except ValueError:
            val = 0
        if val > 0:
            return min(_MAX_PAGE_PX_CEILING, max(_MIN_PAGE_PX, val))
    mem_gb = parse_memory_gb(resolve_worker_memory(worker_memory_spec))
    if mem_gb <= 0:
        mem_gb = 4.0
    rgba_budget_bytes = mem_gb * (1024.0 ** 3) / 8.0  # 1/8 of RAM for one page's RGBA
    px = int(rgba_budget_bytes / 4.0)                 # 4 bytes/px
    # Cap at the post-process budget so a rendered page can always be trimmed/
    # focused/scanned (the derived value only exceeds it on >=4 GB workers).
    return min(_MAX_PAGE_PX_CEILING, MAX_POSTPROCESS_PX, max(_MIN_PAGE_PX, px))


def max_concurrent_page_ops(
    worker_memory_spec: str | None = None, per_page_peak_mb: float | None = None
) -> int:
    """Bound parallel page-level operations by the worker's memory budget.

    Used by both the rasterizer (shard count) and the converter's per-page
    fan-out (hash/trim/focus/scanners). Both load the full page image into
    memory; running too many at once on a memory-constrained worker risks an
    OOM-kill by the cgroup.

    ``per_page_peak_mb`` lets a caller that knows the ACTUAL largest page (the
    rasterizer reads per-page mediaboxes) push the peak estimate ABOVE the
    ``_PER_PAGE_PEAK_MB`` default — a 14400pt page at 150 DPI is ~2.7 GB, ~13×
    the heuristic — so an oversized page collapses concurrency to 1 instead of
    fanning out N concurrent multi-GB renders. It only ever RAISES the estimate
    (never relaxes the conservative default for normal pages). The cold tier's
    cgroup OOM-kills cleanly either way; this protects the gVisor warm tier,
    which runs without a per-worker memory cgroup.
    """
    mem_gb = parse_memory_gb(resolve_worker_memory(worker_memory_spec))
    # Leave half the worker memory for the Python runtime, LibreOffice, and
    # transient allocations.
    usable_mb = max(1.0, mem_gb * 1024.0 * 0.5)
    peak_mb = max(float(_PER_PAGE_PEAK_MB), per_page_peak_mb or 0.0)
    mem_cap = max(1, int(usable_mb // peak_mb))
    return max(1, min(_ABSOLUTE_PAGE_OP_CEILING, mem_cap))
