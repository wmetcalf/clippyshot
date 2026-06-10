"""Optional ``nono`` (Landlock) inner wrapper — a composable argv-transform.

Unlike the nsjail/bwrap/container backends, this is **not** a standalone backend
you select *instead of* another. It is an opt-in *decorator* that nests a Landlock
filesystem+network confinement layer **inside** whatever base sandbox was selected
(nsjail / bwrap / container / a bare runc container), by prefixing
``nono wrap … --`` onto the inner argv before the base sandbox runs it.

Why a decorator and not a 4th backend: nono uses **Landlock**, which needs **no user
namespaces**, so — unlike bwrap/nsjail — it nests cleanly inside another sandbox or
container where a second ``CLONE_NEWUSER`` would be blocked or redundant. That makes
it usable in three shapes from one mechanism:

* **primary** wrapper on the cold / bare-metal path (decorate a no-op base),
* **one-shot** "run this argv sandboxed" (``NonoWrap().apply()`` on any request),
* **nested safety-net** inside FC / gVisor / runc (decorate the real base sandbox).

It is **fully optional**: ``select_sandbox()`` only wraps when ``inner_wrap`` is
passed (or ``CLIPPYSHOT_INNER_NONO`` is set); the default path is byte-for-byte
unchanged and pays zero cost.

The argv recipe mirrors blastbox's proven ``NonoSandbox`` (PR #3): grant the RO
system dirs + RW ``/tmp`` ``/dev``, grant the request's mounts, ``--block-net``, and
exec the child through ``/usr/bin/env -i`` so the child never sees nono's relocated
``$HOME`` state root. nono's own state lives **off** every granted path.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from clippyshot.errors import SandboxUnavailable
from clippyshot.sandbox.base import Mount, Sandbox, SandboxRequest
from clippyshot.types import SandboxResult

# Read-only system dirs every command needs (not remapped between host/sandbox).
_RO_SYSTEM_DIRS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc")
# Host procfs/sysfs reads children commonly need (no PID/mount ns under Landlock).
_RO_RUNTIME_DIRS = ("/proc", "/sys")
# Writable surfaces: a writable scratch HOME (/tmp) + device nodes (incl. /dev/shm
# for the multiprocessing rasterizer).
_RW_BASE_DIRS = ("/tmp", "/dev")

_MINIMAL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Env for the nono PROCESS (not the child): relocate state + silence prompts/updates.
_NONO_QUIET_ENV = {"NONO_NO_UPDATE_CHECK": "1", "NONO_NO_SAVE_PROMPT": "1"}
_DEFAULT_STATE_DIR = "/var/lib/clippyshot/nono-state"


def landlock_available() -> bool:
    """Best-effort probe: is Landlock usable on this kernel/runtime?

    Calls ``landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)`` —
    returns the ABI version (>0) where Landlock is present, or ``-1``/ENOSYS where it
    is not (notably **inside the gVisor Sentry**, which does not implement the
    ``landlock_*`` syscalls). Lets the inner-nono layer fail fast on a tier that
    can't enforce it rather than erroring mid-conversion. x86_64 syscall number.
    """
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.syscall(444, None, 0, 1) > 0
    except Exception:  # noqa: BLE001 — any probe failure ⇒ treat as unavailable
        return False


def _resolve_bin(raw: str | None) -> str | None:
    """Resolve a nono spec to an absolute existing file, or ``None``.

    Accepts a path (``/opt/nono`` — must exist) or a bare name (``nono`` — via PATH).
    """
    cand = (raw or "").strip() or "nono"
    if os.sep in cand:
        p = Path(cand)
        return str(p) if p.is_file() else None
    return shutil.which(cand)


@dataclass(frozen=True)
class NonoWrap:
    """Configuration for the optional inner nono layer.

    Either pass ``profile`` (a nono profile JSON, enforced via ``-p``) OR rely on the
    auto-derived grants (system dirs + the request's mounts). ``extra_read`` /
    ``extra_allow`` add directories on top of either.
    """

    profile: Path | None = None
    block_net: bool = True
    bin: str | None = None  # None -> CLIPPYSHOT_NONO_BIN or "nono" on PATH
    state_dir: Path = Path(_DEFAULT_STATE_DIR)
    extra_read: tuple[Path, ...] = ()
    extra_allow: tuple[Path, ...] = ()

    def resolve_bin(self) -> str | None:
        raw = self.bin if self.bin is not None else os.environ.get("CLIPPYSHOT_NONO_BIN")
        return _resolve_bin(raw)

    # ------------------------------------------------------------------
    def _grants(self, request: SandboxRequest) -> list[str]:
        """Build the ``nono`` grant flags. Mount grants use ``sandbox_path`` (what
        nono sees): the container backend path-translates argv sandbox→host, while
        nsjail/bwrap run the argv as-is inside their namespace — sandbox_path is
        correct for both. Every source is a value arg after its flag, so no caller
        value can inject a nono flag."""
        grants: list[str] = []
        if self.profile is not None:
            grants += ["-p", str(self.profile)]
        else:
            for d in (*_RO_SYSTEM_DIRS, *_RO_RUNTIME_DIRS):
                if Path(d).exists():
                    grants += ["-r", d]
            for d in _RW_BASE_DIRS:
                if Path(d).exists():
                    grants += ["-a", d]
        for p in self.extra_read:
            grants += ["-r", str(p)]
        for p in self.extra_allow:
            grants += ["-a", str(p)]
        # Real file-vs-dir decided on host_path; granted path is what nono sees.
        for m in request.ro_mounts:
            grants += (
                ["--read-file", str(m.sandbox_path)]
                if Path(m.host_path).is_file()
                else ["-r", str(m.sandbox_path)]
            )
        for m in request.rw_mounts:
            grants += (
                ["--allow-file", str(m.sandbox_path)]
                if Path(m.host_path).is_file()
                else ["-a", str(m.sandbox_path)]
            )
        if request.workdir and str(request.workdir) != "/":
            grants += ["--allow-cwd", "--workdir", str(request.workdir)]
        return grants

    def build_argv(self, request: SandboxRequest) -> list[str]:
        nono = self.resolve_bin()
        if nono is None:
            raise SandboxUnavailable(
                "nono not found (install nono or set CLIPPYSHOT_NONO_BIN); "
                "the inner nono layer is opt-in and cannot run without it"
            )
        grants = self._grants(request)
        net = ["--block-net"] if self.block_net else []
        # The child gets a clean env via `env -i`: nono passes its own env (incl. the
        # relocated HOME=state) through to the child, so reset it explicitly. The child
        # must NOT see nono's state HOME.
        child_env = {"PATH": _MINIMAL_PATH, "HOME": "/tmp", **request.env}
        env_prefix = ["/usr/bin/env", "-i"] + [f"{k}={v}" for k, v in child_env.items()]
        return [nono, "wrap", "--silent", *grants, *net, "--", *env_prefix, *request.argv]

    def apply(self, request: SandboxRequest) -> SandboxRequest:
        """Return a new request whose argv is nono-wrapped. nono's state dir is
        created off the grants; the nono binary + state dir are added as identity
        mounts so a namespacing base sandbox (nsjail/bwrap) can see them (no-op for
        the container backend, which runs argv directly)."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxUnavailable(
                f"nono state dir {self.state_dir} not writable "
                f"(set a dir off the grants): {exc}"
            ) from exc
        argv = self.build_argv(request)
        nono = self.resolve_bin()
        bin_dir = Path(nono).parent if nono else None
        extra_ro = (
            [Mount(bin_dir, bin_dir, read_only=True)] if bin_dir is not None else []
        )
        extra_rw = [Mount(self.state_dir, self.state_dir, read_only=False)]
        # nono PROCESS env (separate from the child's env above): relocate state HOME
        # off the grants + silence prompts. Merged onto the existing request env.
        nono_env = {
            **request.env,
            "PATH": _MINIMAL_PATH,
            "HOME": str(self.state_dir),
            **_NONO_QUIET_ENV,
        }
        return replace(
            request,
            argv=argv,
            ro_mounts=[*request.ro_mounts, *extra_ro],
            rw_mounts=[*request.rw_mounts, *extra_rw],
            env=nono_env,
        )


@dataclass
class NonoWrappedSandbox:
    """Decorator that nono-wraps every :meth:`run` then delegates to the base sandbox.

    Satisfies the :class:`~clippyshot.sandbox.base.Sandbox` protocol, so it drops in
    anywhere a ``Sandbox`` is taken. ``smoketest`` delegates unwrapped — it probes the
    base backend's liveness, not the (optional) nono layer.
    """

    inner: Sandbox
    wrap: NonoWrap
    name: str = field(init=False)

    def __post_init__(self) -> None:
        # A plain (settable) str attr, not a read-only @property, so this satisfies
        # the Sandbox protocol's ``name: str`` member for static checking.
        self.name = f"{self.inner.name}+nono"

    @property
    def secure(self) -> bool:
        # The base sandbox owns the security verdict; nono is additive defense.
        return bool(getattr(self.inner, "secure", False))

    @property
    def insecurity_reasons(self) -> list[str]:
        return list(getattr(self.inner, "insecurity_reasons", []))

    def run(self, request: SandboxRequest) -> SandboxResult:
        return self.inner.run(self.wrap.apply(request))

    def smoketest(self) -> SandboxResult:
        return self.inner.smoketest()
