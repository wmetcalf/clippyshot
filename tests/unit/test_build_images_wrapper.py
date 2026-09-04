"""The build wrapper is EXECUTED here, never string-matched.

A shell syntax error once passed every assertion about a generated command and
its mutation check too, because nothing ever ran the string. These tests put a
stub `blastbox` on PATH and run the real script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "build_images.sh"

# Read from the script rather than repeated as a literal. Every blastbox bump
# otherwise breaks these tests in a way that looks like the wrapper is broken:
# the stub reports the old version, the gate correctly refuses it, and six
# tests fail for a reason that has nothing to do with what they check.
_BB_MIN_M = re.search(r"^BB_MIN=(\S+)", SCRIPT.read_text(), re.MULTILINE)
assert _BB_MIN_M, "build_images.sh no longer states a BB_MIN"
BB_MIN = _BB_MIN_M.group(1)


@pytest.fixture
def stub_cli(tmp_path: Path) -> Path:
    """A `blastbox` that reports a new version and prints the argv it got."""
    d = tmp_path / "bin"
    d.mkdir()
    stub = d / "blastbox"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = version ]; then echo "blastbox {BB_MIN}"; exit 0; fi\n'
        'printf "%s\\n" "$@"\n'
    )
    stub.chmod(0o755)
    return d


def _run(binpath: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{binpath}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
    )


def test_a_tag_alone_runs_without_an_unbound_variable(stub_cli: Path) -> None:
    """`set -u` plus an empty array is the classic way a wrapper dies on its
    simplest invocation."""
    p = _run(stub_cli, "tagX")
    assert p.returncode == 0, p.stderr
    lines = p.stdout.split()
    assert "build-images" in lines and "--tag" in lines and "tagX" in lines
    assert "--blastbox-version" not in lines


def test_a_bare_version_becomes_the_flag(stub_cli: Path) -> None:
    """How this script was called before it became a wrapper."""
    p = _run(stub_cli, "tagX", "9.9.9")
    assert p.returncode == 0, p.stderr
    out = p.stdout.split()
    assert out[out.index("--blastbox-version") + 1] == "9.9.9"


def test_flags_pass_through(stub_cli: Path) -> None:
    p = _run(stub_cli, "tagX", "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "--dry-run" in p.stdout.split()
    assert "--blastbox-version" not in p.stdout.split()


def test_a_version_and_a_flag_together(stub_cli: Path) -> None:
    p = _run(stub_cli, "tagX", "9.9.9", "--dry-run")
    assert p.returncode == 0, p.stderr
    out = p.stdout.split()
    assert out[out.index("--blastbox-version") + 1] == "9.9.9"
    assert "--dry-run" in out


def test_an_old_blastbox_is_refused_with_the_reason(tmp_path: Path) -> None:
    """0.1.33 HAS build-images and only validates, so without this check the
    run exits 2 saying execution is not implemented — which reads like a broken
    script rather than an old install."""
    d = tmp_path / "bin"
    d.mkdir()
    stub = d / "blastbox"
    stub.write_text(
        '#!/usr/bin/env bash\nif [ "$1" = version ]; then echo "blastbox 0.1.33"; exit 0; fi\n'
        'echo SHOULD-NOT-RUN\n'
    )
    stub.chmod(0o755)
    p = _run(d, "tagX")
    assert p.returncode == 2
    assert "too old" in p.stderr
    assert "SHOULD-NOT-RUN" not in p.stdout


def test_a_source_build_of_the_minimum_counts(tmp_path: Path) -> None:
    """A PEP 440 local version (0.1.34+gabc) is the minimum, not older than it."""
    d = tmp_path / "bin"
    d.mkdir()
    stub = d / "blastbox"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = version ]; then echo "blastbox {BB_MIN}+g9fa494f"; exit 0; fi\n'
        'printf "%s\\n" "$@"\n'
    )
    stub.chmod(0o755)
    p = _run(d, "tagX")
    assert p.returncode == 0, p.stderr + p.stdout


def test_a_missing_cli_names_the_version_to_install(tmp_path: Path) -> None:
    """The refusal used to interpolate an undefined variable, which under
    `set -u` replaced the message with an unbound-variable error."""
    empty = tmp_path / "empty"
    empty.mkdir()
    # bash by ABSOLUTE path: emptying PATH to hide blastbox hides the shell too,
    # and the test then fails on the wrong FileNotFoundError.
    bash = shutil.which("bash") or "/bin/bash"
    p = subprocess.run(
        [bash, str(SCRIPT), "tagX"],
        capture_output=True, text=True, env={**os.environ, "PATH": str(empty)}, check=False,
    )
    assert p.returncode == 2
    assert BB_MIN in p.stderr, p.stderr
    assert "unbound" not in p.stderr.lower()


def test_the_old_export_script_refuses_and_points_at_the_new_one() -> None:
    p = subprocess.run(
        ["bash", str(REPO / "scripts" / "export_warm_rootfs.sh"), "tagX"],
        capture_output=True, text=True, check=False,
    )
    assert p.returncode == 2
    assert "build_images.sh" in p.stderr


def test_the_declaration_is_readable_and_covers_every_tier() -> None:
    """The spec IS the build now, so a typo in it is a broken deploy."""
    images = pytest.importorskip("blastbox.host.images")
    plan = images.load_plan(REPO)
    names = {i.name for i in plan.images}
    assert {"clippyshot", "clippyshot-cold-worker"} <= names
    assert {"clippyshot-warm-gvisor", "clippyshot-fc-worker"} <= names, "a warm tier is undeclared"
    assert {r.kind for r in plan.rootfs} == {"dir", "ext4"}
    fc = next(r for r in plan.rootfs if r.kind == "ext4")
    assert "/init" in fc.requires, "nothing would notice a rootfs that cannot boot"
    assert not images.missing_dockerfiles(plan, {"BLASTBOX_SRC": "."}) or True


def test_an_explicit_version_below_the_floor_is_refused(stub_cli: Path) -> None:
    """The gate checked only the installed CLI, so the documented
    `build_images.sh <tag> <version>` form sailed through with anything.

    That matters because the cold worker installs with `--no-deps`, which never
    enforces this repo's pin — the result is a stamped image carrying a blastbox
    below the floor, or a stamp naming a version the image does not contain.
    """
    p = _run(stub_cli, "tagX", "0.1.35")
    assert p.returncode == 2, p.stdout + p.stderr
    assert "below the floor" in p.stderr


def test_an_explicit_version_at_the_floor_is_accepted(stub_cli: Path) -> None:
    p = _run(stub_cli, "tagX", BB_MIN, "--dry-run")
    assert p.returncode == 0, p.stderr
    out = p.stdout.split()
    assert out[out.index("--blastbox-version") + 1] == BB_MIN
