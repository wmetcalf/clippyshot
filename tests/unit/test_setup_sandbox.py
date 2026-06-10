"""setup-sandbox: detection + command generation (probes injected; no host deps)."""
from pathlib import Path

import clippyshot.setup_sandbox as ss


def test_no_actions_when_restriction_inactive(monkeypatch):
    monkeypatch.setattr(ss, "_restrict_active", lambda: False)
    monkeypatch.setattr(ss.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(ss, "_userns_ok", lambda b, p: False)  # failing, but not the restriction
    rep = ss.diagnose()
    assert rep.actions == []                       # nothing to load when restriction is off
    assert any("not the AppArmor restriction" in n for n in rep.notes)


def test_actions_when_blocked_by_restriction(monkeypatch):
    monkeypatch.setattr(ss, "_restrict_active", lambda: True)
    monkeypatch.setattr(ss.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(ss, "_userns_ok", lambda b, p: b == "nsjail")  # bwrap blocked, nsjail ok
    rep = ss.diagnose()
    assert [a.binary for a in rep.actions] == ["bwrap"]
    assert rep.actions[0].profile_name == "clippyshot-bwrap"


def test_no_action_when_userns_already_works(monkeypatch):
    monkeypatch.setattr(ss, "_restrict_active", lambda: True)
    monkeypatch.setattr(ss.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(ss, "_userns_ok", lambda b, p: True)
    assert ss.diagnose().actions == []             # loaded already -> probe passes -> no-op


def test_absent_binary_is_a_note_not_an_action(monkeypatch):
    monkeypatch.setattr(ss, "_restrict_active", lambda: True)
    monkeypatch.setattr(ss.shutil, "which", lambda b: None)
    rep = ss.diagnose()
    assert rep.actions == []
    assert all("absent" in n for n in rep.notes)


def test_commands_for_shape():
    a = ss.ProfileAction("bwrap", "/usr/bin/bwrap", "clippyshot-bwrap")
    cmds = ss.commands_for([a], Path("/repo/deploy/apparmor"))
    assert cmds[0] == ["sudo", "cp", "/repo/deploy/apparmor/clippyshot-bwrap", "/etc/apparmor.d/"]
    assert cmds[1] == ["sudo", "apparmor_parser", "-r", "-W", "/etc/apparmor.d/clippyshot-bwrap"]


def test_default_profile_dir_points_at_repo():
    d = ss.default_profile_dir()
    assert d.name == "apparmor" and d.parent.name == "deploy"
