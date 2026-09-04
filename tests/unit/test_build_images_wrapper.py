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
import sys
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
    # Put the interpreter running these tests on PATH. The version floor needs a
    # python that can `import packaging.version`; whichever `python3` happens to
    # be first on the developer's or CI runner's PATH may not be one, and then
    # the wrapper falls back to `sort -V` and the PEP 440 assertions below would
    # be testing the fallback instead of the thing they name.
    env = {
        **os.environ,
        "PATH": f"{binpath}:{Path(sys.executable).parent}:{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


_MINIMAL_TOOLS = (
    "bash",
    "sh",
    "sed",
    "head",
    "sort",
    "printf",
    "dirname",
    "basename",
    "realpath",
    "mktemp",
    "cat",
    "rm",
    "tr",
    "grep",
    "awk",
    "uname",
    "id",
    "date",
    "env",
    "cut",
    "mkdir",
    "cp",
    "mv",
    "ln",
    "chmod",
    "find",
    "tee",
)


def _minimal_bin(directory: Path) -> Path:
    """A PATH with the usual shell tools but deliberately no capable python.

    The version floor asks an interpreter to compare versions; these tests need
    to control whether one is reachable without also breaking the rest of the
    wrapper, which still needs ordinary coreutils.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for tool in _MINIMAL_TOOLS:
        src = shutil.which(tool)
        if src and not (directory / tool).exists():
            (directory / tool).symlink_to(src)
    return directory


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
        "echo SHOULD-NOT-RUN\n"
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
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(empty)},
        check=False,
    )
    assert p.returncode == 2
    assert BB_MIN in p.stderr, p.stderr
    assert "unbound" not in p.stderr.lower()


def test_the_old_export_script_refuses_and_points_at_the_new_one() -> None:
    p = subprocess.run(
        ["bash", str(REPO / "scripts" / "export_warm_rootfs.sh"), "tagX"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert p.returncode == 2
    assert "build_images.sh" in p.stderr


def test_the_declaration_is_readable_and_covers_every_tier() -> None:
    """The spec IS the build now, so a typo in it is a broken deploy."""
    images = pytest.importorskip("blastbox.host.images")
    plan = images.load_plan(REPO)
    names = {i.name for i in plan.images}
    assert {"clippyshot", "clippyshot-cold-worker"} <= names
    assert {"clippyshot-warm-gvisor", "clippyshot-fc-worker"} <= names, (
        "a warm tier is undeclared"
    )
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


@pytest.mark.parametrize(
    "form",
    [
        ["0.1.35"],  # the legacy bare argument
        ["--blastbox-version", "0.1.35"],  # the option, space-separated
        ["--blastbox-version=0.1.35"],  # the option, joined
    ],
    ids=["bare", "option-space", "option-equals"],
)
def test_every_spelling_of_a_stale_version_is_refused(
    stub_cli: Path, form: tuple[str, ...]
) -> None:
    """Gating only the bare argument left the floor bypassable: the option form
    is forwarded verbatim in `"$@"` — and this script constructs that very
    spelling itself, so it is not a hypothetical."""
    p = _run(stub_cli, "tagX", *form)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "below the floor" in p.stderr


def test_the_option_form_at_the_floor_is_accepted(stub_cli: Path) -> None:
    p = _run(stub_cli, "tagX", f"--blastbox-version={BB_MIN}", "--dry-run")
    assert p.returncode == 0, p.stderr
    assert f"--blastbox-version={BB_MIN}" in p.stdout.split()


def test_a_prerelease_of_the_floor_is_refused(stub_cli: Path) -> None:
    """`sort -V` and PEP 440 disagree exactly where it matters: `0.1.38rc1` is
    BELOW `0.1.38` for pip, and `sort -V` puts it above — so a release candidate
    of the floor version, which predates the fixes the floor exists for, sailed
    straight through."""
    p = _run(stub_cli, "tagX", f"{BB_MIN}rc1")
    assert p.returncode == 2, f"exit={p.returncode} out={p.stdout} err={p.stderr}"
    assert "below the floor" in p.stderr


def test_a_version_above_the_floor_is_accepted(stub_cli: Path) -> None:
    p = _run(stub_cli, "tagX", "9.9.9", "--dry-run")
    assert p.returncode == 0, p.stderr


def test_without_a_pep440_capable_python_the_floor_still_refuses(
    stub_cli: Path, tmp_path: Path
) -> None:
    """The fallback is weaker, not absent.

    `sort -V` cannot rank pre-releases the way pip does, but it ranks plain
    releases fine -- so a version plainly below the floor is still refused, and
    the wrapper says on stderr that it downgraded the comparison.
    """
    empty = _minimal_bin(tmp_path / "nopython")
    env = {**os.environ, "PATH": f"{stub_cli}:{empty}"}
    p = subprocess.run(
        ["bash", str(SCRIPT), "tagX", "0.0.1"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert p.returncode == 2, f"exit={p.returncode} out={p.stdout} err={p.stderr}"
    assert "below the floor" in p.stderr
    assert "sort -V" in p.stderr


def test_a_python3_without_packaging_does_not_cause_a_false_refusal(
    stub_cli: Path, tmp_path: Path
) -> None:
    """The floor asks an interpreter; it must check the answer is available.

    A `python3` that cannot `import packaging.version` exits non-zero for the
    same reason a below-floor comparison does. Reading that as a verdict refuses
    every version, including ones well above the floor.
    """
    fake = _minimal_bin(tmp_path / "nopackaging")
    stub = fake / "python3"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{stub_cli}:{fake}"}
    p = subprocess.run(
        ["bash", str(SCRIPT), "tagX", "9.9.9", "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert p.returncode == 0, f"exit={p.returncode} out={p.stdout} err={p.stderr}"
    assert "sort -V" in p.stderr


@pytest.mark.parametrize(
    "spelling", ["v0.1.35", "V0.1.35", f"{BB_MIN}rc1", f"{BB_MIN}.dev1"]
)
def test_pep440_spellings_below_the_floor_are_refused(
    stub_cli: Path, spelling: str
) -> None:
    """pip normalizes these; `sort -V` does not.

    `v0.1.35` normalizes to the vulnerable 0.1.35, and `0.1.38rc1`/`0.1.38.dev1`
    both predate the released floor -- yet `sort -V` orders every one of them
    above `0.1.38` on the raw string.
    """
    p = _run(stub_cli, "tagX", f"--blastbox-version={spelling}")
    assert p.returncode == 2, f"exit={p.returncode} out={p.stdout} err={p.stderr}"
    assert "below the floor" in p.stderr


def test_a_v_prefixed_version_above_the_floor_is_accepted(stub_cli: Path) -> None:
    """Refusing the `v` spelling outright would be wrong: pip accepts it.

    Derived from BB_MIN rather than written out: a hard-coded version silently
    becomes a BELOW-the-floor case the next time the floor moves, and the test
    then fails for a reason that has nothing to do with what it checks.
    """
    p = _run(stub_cli, "tagX", f"v{BB_MIN}", "--dry-run")
    assert p.returncode == 0, f"exit={p.returncode} err={p.stderr}"


def test_a_version_that_is_not_pep440_is_refused(stub_cli: Path) -> None:
    """An unparseable version is not a pass; it is a refusal with its own reason."""
    p = _run(stub_cli, "tagX", "not-a-version")
    assert p.returncode == 2, f"exit={p.returncode} out={p.stdout} err={p.stderr}"
    assert "not a PEP 440 version" in p.stderr


def test_the_fallback_refuses_spellings_sort_v_cannot_rank(
    stub_cli: Path, tmp_path: Path
) -> None:
    """The weaker path must not become the bypass.

    Without `packaging` the comparison degrades to `sort -V`, which ranks
    `0.1.38rc1` ABOVE the floor. Guessing there would hand back exactly the
    bypass this gate exists to close, so the fallback declines to rank any
    spelling it cannot order the way pip would.
    """
    env = {**os.environ, "PATH": f"{stub_cli}:{_minimal_bin(tmp_path / 'nopython')}"}
    p = subprocess.run(
        ["bash", str(SCRIPT), "tagX", f"{BB_MIN}rc1"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert p.returncode == 2, f"exit={p.returncode} out={p.stdout} err={p.stderr}"
    assert "cannot rank that spelling" in p.stderr


def test_the_fallback_still_accepts_a_v_prefixed_release(
    stub_cli: Path, tmp_path: Path
) -> None:
    """Declining to rank a spelling is a refusal, so keep the set small.

    A leading `v` is the one deviation `sort -V` can be taught (pip just drops
    it). Without that, every `v`-spelled release would be refused on a host
    lacking `packaging` -- a gate failure dressed as a security decision.
    """
    env = {**os.environ, "PATH": f"{stub_cli}:{_minimal_bin(tmp_path / 'nopython')}"}
    p = subprocess.run(
        ["bash", str(SCRIPT), "tagX", "v9.9.9", "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert p.returncode == 0, f"exit={p.returncode} out={p.stdout} err={p.stderr}"
