"""`clippyshot setup-sandbox` and Ubuntu's stock `bwrap-userns-restrict` profile.

Every host interaction is faked: the stock-profile state and the attach probe are
scripted, and host commands are recorded instead of run. No test touches real AppArmor.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import clippyshot.setup_sandbox as ss
from clippyshot import cli
from clippyshot.sandbox import host_apparmor as ha

LOADED = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=True)
ENABLED_UNLOADED = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=False)
DISABLED_LOADED = ha.StockBwrapProfileState(installed=True, disabled=True, loaded=True)
FIXED = ha.StockBwrapProfileState(installed=True, disabled=True, loaded=False)
ABSENT = ha.StockBwrapProfileState(installed=False, disabled=False, loaded=False)
UNMANAGED = ha.StockBwrapProfileState(installed=False, disabled=False, loaded=True)
UNKNOWN = ha.StockBwrapProfileState(installed=True, disabled=False, loaded=None)

PROFILE_DIR = Path(ss.__file__).resolve().parents[2] / "deploy" / "apparmor"  # real, exists
STOCK = "/etc/apparmor.d/bwrap-userns-restrict"


def _probe(stock, ok: bool, cause: str = "ok", stderr: str = "") -> ha.AaExecProbe:
    return ha.AaExecProbe(ok=ok, cause=cause, profile="clippyshot-soffice", argv=("bwrap",),
                          exit_code=0 if ok else 1, stderr=stderr, stock=stock)


class Host:
    """Scripted host: each stock-state read pops the next state (last one sticks)."""

    def __init__(self, monkeypatch, states, *, probe_ok_when_fixed: bool = True):
        self.states = list(states)
        self.commands: list[list[str]] = []
        self.fail_on: str | None = None
        monkeypatch.setattr(ss, "_restrict_active", lambda: True)
        monkeypatch.setattr(ss.shutil, "which", lambda b: f"/usr/bin/{b}")
        monkeypatch.setattr(ss, "_userns_ok", lambda b, p: True)
        monkeypatch.setattr(ss, "default_profile_dir", lambda: PROFILE_DIR)

        def state():
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]

        def probe(bwrap, aa, profile, *, stock_state=None, **kw):
            st = stock_state or state()
            if st.conflict:
                return _probe(st, False, "stock_profile_conflict",
                              "insufficient permissions to change to the profile")
            return _probe(st, probe_ok_when_fixed)

        def run(argv):
            self.commands.append(list(argv))
            return 1 if self.fail_on and self.fail_on in " ".join(argv) else 0

        monkeypatch.setattr(ha, "stock_bwrap_profile_state", state)
        monkeypatch.setattr(ha, "probe_aa_exec", probe)
        monkeypatch.setattr(ss, "_run_host_command", run)


def _four_profile_cmds() -> list[list[str]]:
    out = []
    for p in ("clippyshot-bwrap", "clippyshot-nsjail", "clippyshot-soffice", "clippyshot-runtime"):
        out.append(["sudo", "cp", f"{PROFILE_DIR}/{p}", "/etc/apparmor.d/"])
        out.append(["sudo", "apparmor_parser", "-r", "-W", f"/etc/apparmor.d/{p}"])
    return out


# --- diagnose ----------------------------------------------------------------------------


def test_diagnose_reports_the_stock_profile(monkeypatch):
    Host(monkeypatch, [LOADED])
    rep = ss.diagnose()
    assert rep.stock == LOADED
    assert rep.stock_conflict
    assert any("bwrap-userns-restrict" in n for n in rep.notes)


def test_diagnose_no_conflict_without_bwrap(monkeypatch):
    Host(monkeypatch, [LOADED])
    monkeypatch.setattr(ss.shutil, "which", lambda b: None)
    rep = ss.diagnose()
    assert rep.stock is None and not rep.stock_conflict


def test_diagnose_adds_the_soffice_profile_when_aa_exec_says_it_is_missing(monkeypatch):
    Host(monkeypatch, [ABSENT])
    monkeypatch.setattr(ha, "probe_aa_exec", lambda *a, stock_state=None, **k: _probe(
        ABSENT, False, "profile_not_loaded", "profile 'clippyshot-soffice' does not exist"))
    rep = ss.diagnose()
    assert [a.profile_name for a in rep.actions] == ["clippyshot-soffice"]


# --- the plan ----------------------------------------------------------------------------


def test_plan_for_a_loaded_enabled_profile():
    plan = ss.stock_fix_plan(LOADED, PROFILE_DIR)
    assert [c.argv for c in plan] == [
        ["sudo", "ln", "-sf", STOCK, "/etc/apparmor.d/disable/bwrap-userns-restrict"],
        ["sudo", "apparmor_parser", "-R", STOCK],
        *_four_profile_cmds(),
    ]
    assert not any(c.allow_failure for c in plan)


def test_plan_skips_the_unload_when_not_loaded():
    plan = ss.stock_fix_plan(ENABLED_UNLOADED, PROFILE_DIR)
    assert ["sudo", "apparmor_parser", "-R", STOCK] not in [c.argv for c in plan]
    assert plan[0].argv[:3] == ["sudo", "ln", "-sf"]


def test_plan_skips_the_symlink_when_already_disabled():
    plan = ss.stock_fix_plan(DISABLED_LOADED, PROFILE_DIR)
    assert plan[0].argv == ["sudo", "apparmor_parser", "-R", STOCK]
    assert not any(c.argv[1] == "ln" for c in plan)


def test_plan_tolerates_unloading_when_load_state_is_unknown():
    plan = ss.stock_fix_plan(UNKNOWN, PROFILE_DIR)
    unload = [c for c in plan if c.argv[:3] == ["sudo", "apparmor_parser", "-R"]]
    assert len(unload) == 1 and unload[0].allow_failure


def test_plan_refuses_a_loaded_profile_with_no_file_to_unload():
    with pytest.raises(ss.StockProfileUnmanaged, match="unpriv_bwrap"):
        ss.stock_fix_plan(UNMANAGED, PROFILE_DIR)


# --- the CLI -----------------------------------------------------------------------------


def test_without_the_flag_it_names_the_conflict_and_exits_nonzero(monkeypatch, capsys):
    host = Host(monkeypatch, [LOADED])
    rc = cli.main(["setup-sandbox"])
    out = capsys.readouterr().out
    assert rc != 0
    assert host.commands == []
    assert "bwrap-userns-restrict" in out and "unpriv_bwrap" in out
    assert "clippyshot setup-sandbox --disable-stock-bwrap-profile" in out
    # The exact commands it would run, so an operator can do it by hand.
    assert f"sudo apparmor_parser -R {STOCK}" in out
    assert "sudo ln -sf" in out


def test_apply_alone_does_not_touch_a_conflicted_host(monkeypatch, capsys):
    """Loading clippyshot-bwrap next to the stock profile breaks bwrap's uid map."""
    host = Host(monkeypatch, [LOADED])
    assert cli.main(["setup-sandbox", "--apply"]) != 0
    assert host.commands == []


def test_flag_disables_persistently_then_loads_and_verifies(monkeypatch, capsys):
    host = Host(monkeypatch, [LOADED, FIXED])
    rc = cli.main(["setup-sandbox", "--disable-stock-bwrap-profile"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert host.commands == [c.argv for c in ss.stock_fix_plan(LOADED, PROFILE_DIR)]
    assert "attach probe: OK" in out


def test_flag_is_idempotent(monkeypatch, capsys):
    host = Host(monkeypatch, [FIXED])
    assert cli.main(["setup-sandbox", "--disable-stock-bwrap-profile"]) == 0
    assert host.commands == []
    assert "nothing to disable" in capsys.readouterr().out


def test_flag_on_a_host_without_the_stock_profile_is_a_noop(monkeypatch, capsys):
    host = Host(monkeypatch, [ABSENT])
    assert cli.main(["setup-sandbox", "--disable-stock-bwrap-profile"]) == 0
    assert host.commands == []


def test_a_failed_step_stops_and_returns_its_code(monkeypatch, capsys):
    host = Host(monkeypatch, [LOADED])
    host.fail_on = "apparmor_parser -R"
    assert cli.main(["setup-sandbox", "--disable-stock-bwrap-profile"]) != 0
    assert host.commands[-1] == ["sudo", "apparmor_parser", "-R", STOCK]


def test_unknown_unload_failure_is_tolerated(monkeypatch, capsys):
    host = Host(monkeypatch, [UNKNOWN, FIXED])
    host.fail_on = "apparmor_parser -R"
    assert cli.main(["setup-sandbox", "--disable-stock-bwrap-profile"]) == 0
    assert host.commands[-1] == ["sudo", "apparmor_parser", "-r", "-W",
                                 "/etc/apparmor.d/clippyshot-runtime"]


def test_verification_failure_after_apply_is_nonzero(monkeypatch, capsys):
    host = Host(monkeypatch, [LOADED, FIXED], probe_ok_when_fixed=False)
    rc = cli.main(["setup-sandbox", "--disable-stock-bwrap-profile"])
    assert rc != 0
    assert host.commands  # it did apply; the probe afterwards is what failed


def test_unmanaged_loaded_profile_is_refused_with_instructions(monkeypatch, capsys):
    host = Host(monkeypatch, [UNMANAGED])
    rc = cli.main(["setup-sandbox", "--disable-stock-bwrap-profile"])
    assert rc != 0
    assert host.commands == []
    assert "unpriv_bwrap" in capsys.readouterr().err


def test_it_states_the_host_wide_cost_before_anyone_opts_in(monkeypatch, capsys):
    """Disabling the stock profile lets any bwrap user get a capability-bearing userns."""
    Host(monkeypatch, [LOADED])
    cli.main(["setup-sandbox"])
    out = capsys.readouterr().out
    assert "host-wide" in out
    assert "dedicated" in out


def test_apply_on_a_conflicted_host_still_loads_the_nsjail_profile(monkeypatch, capsys):
    """The stock profile only attaches to /usr/bin/bwrap: an nsjail host keeps the distro's
    bwrap hardening and still gets clippyshot-nsjail, without touching bwrap policy."""
    host = Host(monkeypatch, [LOADED])
    monkeypatch.setattr(ss, "_userns_ok", lambda b, p: b != "nsjail")
    applied: list = []
    monkeypatch.setattr(ss, "apply", lambda actions, d: applied.extend(actions) or 0)
    rc = cli.main(["setup-sandbox", "--apply"])
    assert rc != 0  # bwrap is still blocked, and it says so
    assert [a.profile_name for a in applied] == ["clippyshot-nsjail"]
    assert host.commands == []  # no stock-profile / bwrap policy change


def test_missing_profile_files_stop_before_any_host_change(monkeypatch, capsys, tmp_path):
    """Disabling the stock profile and then failing to load clippyshot-bwrap would leave the
    host with neither; every profile file is checked before the first sudo step."""
    host = Host(monkeypatch, [LOADED, FIXED])
    (tmp_path / "clippyshot-bwrap").write_text("x")
    (tmp_path / "clippyshot-nsjail").write_text("x")
    rc = cli.main(["setup-sandbox", "--disable-stock-bwrap-profile",
                   "--profile-dir", str(tmp_path)])
    assert rc == 2 and host.commands == []
    assert "clippyshot-soffice" in capsys.readouterr().err


def test_fresh_host_schedules_the_soffice_profile_on_the_first_pass(monkeypatch):
    """bwrap's own userns fails before aa-exec runs, so the probe cannot see a missing
    clippyshot-soffice; the kernel's profile list is used instead."""
    monkeypatch.setattr(ss, "_restrict_active", lambda: True)
    monkeypatch.setattr(ss.shutil, "which", lambda b: None if b == "nsjail" else f"/usr/bin/{b}")
    monkeypatch.setattr(ss, "_userns_ok", lambda b, p: False)
    monkeypatch.setattr(ha, "stock_bwrap_profile_state", lambda: ABSENT)
    monkeypatch.setattr(ha, "loaded_profile_names", lambda *a, **k: frozenset({"docker-default"}))
    monkeypatch.setattr(ha, "probe_aa_exec", lambda *a, stock_state=None, **k: _probe(
        ABSENT, False, "failed", "bwrap: setting up uid map: Permission denied"))
    names = [a.profile_name for a in ss.diagnose().actions]
    assert "clippyshot-bwrap" in names and "clippyshot-soffice" in names
