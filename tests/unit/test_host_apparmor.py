"""Stock `bwrap-userns-restrict` detection + the bwrap/aa-exec attach probe.

Every host interaction is faked: a tmp_path stands in for /etc/apparmor.d and the
AppArmor securityfs, and a recording runner stands in for subprocess.run. Nothing here
reads or mutates the real host's AppArmor state.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clippyshot.errors import SandboxUnavailable
from clippyshot.sandbox import host_apparmor as ha


# --- fakes -------------------------------------------------------------------------------


def _securityfs(root: Path, names: list[str]) -> Path:
    """Lay out <root>/policy/profiles/<name>.<n>/name like the kernel's apparmorfs."""
    profiles = root / "policy" / "profiles"
    profiles.mkdir(parents=True)
    for i, name in enumerate(names):
        d = profiles / f"{name.replace('/', '.')}.{i}"
        d.mkdir()
        (d / "name").write_text(name + "\n")
    return root


def _apparmor_d(root: Path, *, installed: bool, disabled: bool) -> Path:
    (root / "disable").mkdir(parents=True)
    if installed:
        (root / ha.STOCK_BWRAP_PROFILE).write_text("profile bwrap /usr/bin/bwrap {}\n")
    if disabled:
        (root / "disable" / ha.STOCK_BWRAP_PROFILE).symlink_to(root / ha.STOCK_BWRAP_PROFILE)
    return root


class _Runner:
    """Records argv; answers with a canned (returncode, stderr)."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, b"", self.stderr.encode())


_LOADED_STOCK = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=True)
_ABSENT = ha.StockBwrapProfileState(installed=False, disabled=False, loaded=False)


# --- loaded profile names ----------------------------------------------------------------


def test_loaded_names_come_from_the_unprivileged_policy_tree(tmp_path):
    fs = _securityfs(tmp_path, ["bwrap", "unpriv_bwrap", "clippyshot-soffice"])
    assert ha.loaded_profile_names(fs) == {"bwrap", "unpriv_bwrap", "clippyshot-soffice"}


def test_loaded_names_fall_back_to_the_profiles_file(tmp_path):
    (tmp_path / "profiles").write_text(
        "unpriv_bwrap (enforce)\nbwrap (unconfined)\nclippyshot-soffice (enforce)\n"
    )
    assert ha.loaded_profile_names(tmp_path) == {"unpriv_bwrap", "bwrap", "clippyshot-soffice"}


def test_loaded_names_unknown_when_securityfs_is_unreadable(tmp_path):
    assert ha.loaded_profile_names(tmp_path / "missing") is None


# --- stock profile state -----------------------------------------------------------------


def test_absent_stock_profile_is_no_conflict(tmp_path):
    st = ha.stock_bwrap_profile_state(
        apparmor_d=_apparmor_d(tmp_path / "etc", installed=False, disabled=False),
        securityfs=_securityfs(tmp_path / "fs", ["clippyshot-soffice"]),
    )
    assert st == _ABSENT
    assert not st.conflict
    assert st.describe() == "absent"


def test_installed_and_loaded_stock_profile_is_a_conflict(tmp_path):
    st = ha.stock_bwrap_profile_state(
        apparmor_d=_apparmor_d(tmp_path / "etc", installed=True, disabled=False),
        securityfs=_securityfs(tmp_path / "fs", ["bwrap", "unpriv_bwrap"]),
    )
    assert st.conflict and st.loaded is True
    assert "loaded" in st.describe()


def test_unloaded_but_enabled_stock_profile_is_still_a_conflict(tmp_path):
    """What CI does (`apparmor_parser -R` only): fine now, back on the next boot."""
    st = ha.stock_bwrap_profile_state(
        apparmor_d=_apparmor_d(tmp_path / "etc", installed=True, disabled=False),
        securityfs=_securityfs(tmp_path / "fs", []),
    )
    assert st.loaded is False
    assert st.conflict
    assert "reboot" in st.describe()


def test_disabled_and_unloaded_stock_profile_is_resolved(tmp_path):
    st = ha.stock_bwrap_profile_state(
        apparmor_d=_apparmor_d(tmp_path / "etc", installed=True, disabled=True),
        securityfs=_securityfs(tmp_path / "fs", ["clippyshot-bwrap"]),
    )
    assert not st.conflict
    assert st.describe() == "disabled"


def test_disabled_but_still_loaded_is_a_conflict(tmp_path):
    st = ha.stock_bwrap_profile_state(
        apparmor_d=_apparmor_d(tmp_path / "etc", installed=True, disabled=True),
        securityfs=_securityfs(tmp_path / "fs", ["unpriv_bwrap"]),
    )
    assert st.conflict


def test_unknown_load_state_with_disabled_file_is_not_a_conflict(tmp_path):
    st = ha.stock_bwrap_profile_state(
        apparmor_d=_apparmor_d(tmp_path / "etc", installed=True, disabled=True),
        securityfs=tmp_path / "unreadable",
    )
    assert st.loaded is None
    assert not st.conflict


# --- the probe ---------------------------------------------------------------------------


def test_probe_argv_is_ci_probe_plus_a_fresh_proc():
    argv = ha.aa_exec_probe_argv("/usr/bin/bwrap", "/usr/bin/aa-exec", "clippyshot-soffice")
    assert argv == [
        "/usr/bin/bwrap", "--unshare-user", "--die-with-parent", "--ro-bind", "/", "/",
        "--proc", "/proc", "--",
        "/usr/bin/aa-exec", "-p", "clippyshot-soffice", "--", "/bin/true",
    ]


def test_probe_ok():
    run = _Runner(0)
    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice", runner=run, stock_state=_ABSENT)
    assert p.ok and p.cause == "ok"
    assert len(run.calls) == 1


@pytest.mark.parametrize("stderr", [
    "[42] aa-exec: ERROR: insufficient permissions to change to the profile 'clippyshot-soffice'",
    "bwrap: setting up uid map: Permission denied",
])
def test_probe_names_the_stock_profile_on_its_signature_errors(stderr):
    enabled = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=None)
    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice", runner=_Runner(1, stderr),
                         stock_state=enabled)
    assert not p.ok
    assert p.cause == "stock_profile_conflict"
    msg = p.message()
    assert "bwrap-userns-restrict" in msg
    assert ha.FIX_COMMAND in msg
    assert stderr.split(": ", 1)[-1] in msg


def test_probe_blames_a_loaded_stock_profile_even_on_unfamiliar_stderr():
    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice", runner=_Runner(1, "weird"),
                         stock_state=_LOADED_STOCK)
    assert p.cause == "stock_profile_conflict"


def test_probe_reports_an_unloaded_soffice_profile():
    p = ha.probe_aa_exec(
        "/b", "/a", "clippyshot-soffice",
        runner=_Runner(1, "aa-exec: ERROR: profile 'clippyshot-soffice' does not exist"),
        stock_state=_ABSENT,
    )
    assert p.cause == "profile_not_loaded"
    assert "clippyshot setup-sandbox --apply" in p.message()
    assert "bwrap-userns-restrict" not in p.message()


def test_probe_other_failure_carries_stderr():
    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice",
                         runner=_Runner(1, "aa-exec: ERROR: Read-only file system"),
                         stock_state=_ABSENT)
    assert p.cause == "failed"
    assert "Read-only file system" in p.message()


def test_probe_that_cannot_run_is_a_failure_not_a_crash():
    def boom(argv, **kw):
        raise OSError("no such binary")

    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice", runner=boom, stock_state=_ABSENT)
    assert not p.ok and p.cause == "failed"
    assert "no such binary" in p.message()


def test_probe_timeout_is_a_failure():
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 15)

    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice", runner=slow, stock_state=_ABSENT)
    assert not p.ok


def test_attach_unavailable_is_a_sandbox_unavailable():
    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice",
                         runner=_Runner(1, "insufficient permissions to change to the profile"),
                         stock_state=_ABSENT)
    err = ha.AppArmorAttachUnavailable(p)
    assert isinstance(err, SandboxUnavailable)
    assert err.probe is p
    assert str(err) == p.message()


def test_change_profile_refusal_without_a_stock_profile_is_not_blamed_on_it():
    """Inside a container under docker-default, aa-exec gets the same 'insufficient
    permissions' error with no stock profile anywhere; don't send the operator to disable
    host hardening for it."""
    import subprocess as _sp

    from clippyshot.sandbox import host_apparmor as ha

    absent = ha.StockBwrapProfileState(installed=False, disabled=False, loaded=None)

    def runner(argv, capture_output, timeout):
        return _sp.CompletedProcess(argv, 1, b"", b"aa-exec: ERROR: insufficient permissions "
                                    b"to change to the profile 'clippyshot-soffice'")

    p = ha.probe_aa_exec("bwrap", "aa-exec", "clippyshot-soffice", runner=runner,
                         stock_state=absent)
    assert p.cause == "change_profile_denied"
    assert ha.FIX_COMMAND not in p.message() and "docker-default" in p.message()


def test_uid_map_failure_without_a_stock_profile_is_a_plain_failure():
    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice",
                         runner=_Runner(1, "bwrap: setting up uid map: Permission denied"),
                         stock_state=_ABSENT)
    assert p.cause == "failed" and ha.FIX_COMMAND not in p.message()


def test_missing_soffice_with_an_unloaded_stock_profile_names_the_load_fix():
    """The unloaded stock profile cannot cause this; --apply loads clippyshot-soffice even
    on a host where the stock profile is enabled."""
    enabled = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=False)
    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice",
                         runner=_Runner(1, "aa-exec: ERROR: profile 'clippyshot-soffice' does not exist"),
                         stock_state=enabled)
    assert p.cause == "profile_not_loaded" and ha.LOAD_COMMAND in p.message()


def test_unknown_kernel_state_is_not_reported_as_absent():
    st = ha.StockBwrapProfileState(installed=False, disabled=False, loaded=None)
    assert st.describe() != "absent"


def test_unrelated_failure_with_an_unloaded_stock_profile_is_not_blamed_on_it():
    enabled_unloaded = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=False)
    p = ha.probe_aa_exec("/b", "/a", "clippyshot-soffice",
                         runner=_Runner(1, "bwrap: No space left on device"),
                         stock_state=enabled_unloaded)
    assert p.cause == "failed"
    assert "No space left on device" in p.message()
