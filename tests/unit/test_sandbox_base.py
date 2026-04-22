from pathlib import Path

from clippyshot.limits import Limits
from clippyshot.sandbox.base import Mount, Sandbox, SandboxRequest


def test_mount_dataclass_holds_paths(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    m = Mount(host_path=src, sandbox_path=Path("/in"), read_only=True)
    assert m.host_path == src
    assert m.sandbox_path == Path("/in")
    assert m.read_only is True


def test_sandbox_request_carries_argv_and_mounts(tmp_path: Path):
    req = SandboxRequest(
        argv=["/bin/true"],
        ro_mounts=[Mount(tmp_path, Path("/in"), read_only=True)],
        rw_mounts=[],
        limits=Limits(),
        env={"FOO": "bar"},
    )
    assert req.argv == ["/bin/true"]
    assert req.env["FOO"] == "bar"


def test_sandbox_protocol_has_required_members():
    members = Sandbox.__protocol_attrs__
    assert "name" in members
    assert "run" in members
    assert "smoketest" in members
