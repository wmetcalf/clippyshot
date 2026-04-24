"""Host-aware default worker caps.

The dispatcher launches one worker container per job. Hard-coding memory /
CPU / concurrency defaults regardless of host means we either over-commit
on small machines (OOM / CPU thrash) or under-utilize on large ones.

This module reads ``os.cpu_count()`` and ``/proc/meminfo`` at dispatcher
startup and computes defaults that scale with the host. Operator env
overrides (``CLIPPYSHOT_DISPATCH_CONCURRENCY``, ``CLIPPYSHOT_WORKER_MEMORY``,
``CLIPPYSHOT_WORKER_CPUS``, ``CLIPPYSHOT_WORKER_PIDS_LIMIT``) always win —
we only fill in values the operator hasn't set.

The chosen numbers are logged so an operator troubleshooting resource
issues can see what was picked.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass


_log = logging.getLogger("clippyshot.runtime.host_limits")


@dataclass(frozen=True)
class HostDefaults:
    """Resolved worker caps for this host.

    Numbers are already formatted as the strings docker-cli expects
    (``"4g"``, ``"2.0"``, etc.) so downstream code can drop them into
    ``docker run`` argv without further processing.
    """

    concurrency: int
    worker_memory: str     # e.g. "3g"
    worker_cpus: str       # e.g. "2.0"
    worker_pids_limit: str # e.g. "256"
    # Raw probe results — kept for observability.
    host_cpus: int
    host_mem_gb: float


def _read_mem_available_gb() -> float:
    """Return MemAvailable (GB) from /proc/meminfo, or MemTotal fallback.

    Returns 0.0 if /proc/meminfo isn't readable (e.g. non-Linux dev host);
    callers treat that as "don't auto-size memory, let the hardcoded
    fallback apply."
    """
    try:
        with open("/proc/meminfo") as f:
            fields = {}
            for line in f:
                key, _, rest = line.partition(":")
                rest = rest.strip()
                if rest.endswith(" kB"):
                    fields[key.strip()] = int(rest[:-3])
            kb = fields.get("MemAvailable") or fields.get("MemTotal") or 0
            return kb / (1024 * 1024)  # kB → GB
    except OSError:
        return 0.0


# Hard caps so we never produce pathological values even on huge hosts.
_MAX_CONCURRENCY = 16
_MIN_WORKER_MEMORY_GB = 1.0
_MAX_WORKER_MEMORY_GB = 4.0
_MIN_WORKER_CPUS = 1.0
_MAX_WORKER_CPUS = 4.0
_DEFAULT_PIDS_LIMIT = 256
# Reserve this many GB of host memory for the OS + dispatcher + api +
# postgres so the worker budget doesn't starve the control plane.
_HEADROOM_GB = 1.0

# Rough upper-bound for peak RAM of one in-flight page buffer during
# rasterization (pdftoppm) or per-page post-processing (PIL loading
# the PNG for hash/trim/focus). A normal letter-sized page at 150 DPI
# is ~6MB; pathological spreadsheet renders (e.g. 1786x28319) can hit
# ~150MB. We estimate a middle-ground 200MB so the cap errs toward
# safety on mixed corpora.
_PER_PAGE_PEAK_MB = 200
# Absolute ceiling on parallel page-operation counts even on huge
# hosts — beyond ~8 the wall-clock win flattens out and gVisor /
# kernel contention becomes the bottleneck.
_ABSOLUTE_PAGE_OP_CEILING = 8


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


def max_concurrent_page_ops(worker_memory_spec: str | None = None) -> int:
    """Bound parallel page-level operations by the worker's memory budget.

    Used by both pdftoppm rasterization (shard count) and the per-page
    fan-out in the converter (hash/trim/focus/scanners). Both load the
    full page image into memory; running too many concurrently on a
    memory-constrained worker risks OOM-kill by the cgroup.
    """
    mem_gb = parse_memory_gb(
        worker_memory_spec
        or os.environ.get("CLIPPYSHOT_WORKER_MEMORY")
        or "4g"
    )
    # Leave half the worker memory for Python runtime, LibreOffice,
    # and transient allocations.
    usable_mb = max(1.0, mem_gb * 1024.0 * 0.5)
    mem_cap = max(1, int(usable_mb // _PER_PAGE_PEAK_MB))
    return max(1, min(_ABSOLUTE_PAGE_OP_CEILING, mem_cap))


def compute_host_defaults(env: dict[str, str] | None = None) -> HostDefaults:
    """Compute worker-cap defaults sized to the host.

    ``env`` is the environment to consult for operator overrides (defaults
    to ``os.environ``). Values already set there are preserved verbatim;
    anything else is filled in from the host probe.
    """
    env = env if env is not None else dict(os.environ)
    cpus = os.cpu_count() or 2
    mem_gb = _read_mem_available_gb()

    # Concurrency — half the host CPUs, clamped.
    if env.get("CLIPPYSHOT_DISPATCH_CONCURRENCY"):
        try:
            concurrency = max(1, int(env["CLIPPYSHOT_DISPATCH_CONCURRENCY"]))
        except ValueError:
            concurrency = max(1, min(_MAX_CONCURRENCY, cpus // 2))
    else:
        concurrency = max(1, min(_MAX_CONCURRENCY, cpus // 2))

    # Per-worker CPUs — split total among concurrent workers, clamped.
    if env.get("CLIPPYSHOT_WORKER_CPUS"):
        worker_cpus_str = env["CLIPPYSHOT_WORKER_CPUS"]
    else:
        worker_cpus = max(_MIN_WORKER_CPUS, min(_MAX_WORKER_CPUS, cpus / concurrency))
        # Preserve the ".0" suffix docker expects for non-integer values.
        worker_cpus_str = f"{worker_cpus:.1f}" if worker_cpus != int(worker_cpus) else f"{int(worker_cpus)}.0"

    # Per-worker memory — divide headroom-adjusted host memory by
    # concurrency, clamped. If we couldn't probe memory, fall back to
    # the previous hardcoded 4g.
    if env.get("CLIPPYSHOT_WORKER_MEMORY"):
        worker_mem_str = env["CLIPPYSHOT_WORKER_MEMORY"]
    elif mem_gb <= 0:
        worker_mem_str = "4g"
    else:
        usable = max(0.0, mem_gb - _HEADROOM_GB)
        per = max(_MIN_WORKER_MEMORY_GB, min(_MAX_WORKER_MEMORY_GB, usable / concurrency))
        # Round down to the nearest 256MB so docker-cli output is tidy.
        quantum = 0.25
        per_rounded = max(_MIN_WORKER_MEMORY_GB, int(per / quantum) * quantum)
        # Prefer whole-GB suffix when clean, fractional `m` otherwise.
        if per_rounded == int(per_rounded):
            worker_mem_str = f"{int(per_rounded)}g"
        else:
            worker_mem_str = f"{int(per_rounded * 1024)}m"

    pids_limit = env.get("CLIPPYSHOT_WORKER_PIDS_LIMIT") or str(_DEFAULT_PIDS_LIMIT)

    return HostDefaults(
        concurrency=concurrency,
        worker_memory=worker_mem_str,
        worker_cpus=worker_cpus_str,
        worker_pids_limit=pids_limit,
        host_cpus=cpus,
        host_mem_gb=round(mem_gb, 2),
    )


def apply_host_defaults(env: dict[str, str] | None = None) -> HostDefaults:
    """Resolve defaults and poke them into ``os.environ`` for unset keys.

    Called once from the dispatcher bootstrap. After this, anything that
    reads ``CLIPPYSHOT_WORKER_MEMORY`` / ``_CPUS`` / ``_PIDS_LIMIT`` or
    ``CLIPPYSHOT_DISPATCH_CONCURRENCY`` sees the computed values as if
    the operator had set them explicitly — including the existing
    ``docker_runtime.build_worker_docker_run_argv`` path.

    Returns the resolved defaults so callers can log them.
    """
    defaults = compute_host_defaults(env=env)
    # os.environ.setdefault only treats MISSING keys as unset; compose
    # passes an empty string when the operator didn't override, which
    # would otherwise leave downstream int() parsing to crash on "".
    def _set_if_blank(key: str, value: str) -> None:
        if not os.environ.get(key):
            os.environ[key] = value
    _set_if_blank("CLIPPYSHOT_DISPATCH_CONCURRENCY", str(defaults.concurrency))
    _set_if_blank("CLIPPYSHOT_WORKER_MEMORY", defaults.worker_memory)
    _set_if_blank("CLIPPYSHOT_WORKER_CPUS", defaults.worker_cpus)
    _set_if_blank("CLIPPYSHOT_WORKER_PIDS_LIMIT", defaults.worker_pids_limit)
    _log.info(
        "worker_caps_resolved host_cpus=%d host_mem_gb=%.2f concurrency=%d "
        "worker_memory=%s worker_cpus=%s worker_pids=%s",
        defaults.host_cpus, defaults.host_mem_gb, defaults.concurrency,
        defaults.worker_memory, defaults.worker_cpus, defaults.worker_pids_limit,
    )
    return defaults
