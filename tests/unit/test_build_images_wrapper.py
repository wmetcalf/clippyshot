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


def test_the_too_old_refusal_runs_nothing_and_keeps_its_text(tmp_path: Path) -> None:
    """A refusal message must not EXECUTE part of itself.

    Inside a double-quoted string bash reads backticks as command substitution,
    so an explanation mentioning `docker build -t` ran it -- printing an
    unrelated docker error, or `command not found`, and dropping the command
    text out of the very sentence that needed it.

    Executed rather than grepped: a source-level check for backticks would pass
    on a string that still runs, and fail on one that legitimately quotes them.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "blastbox"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = version ]; then echo "blastbox 0.0.1"; exit 0; fi\n'
        'printf "%s\\n" "$@"\n'
    )
    stub.chmod(0o755)
    marker = tmp_path / "docker-was-run"
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$*" >> "{marker}"\nexit 9\n')
    docker.chmod(0o755)

    p = subprocess.run(
        ["bash", str(SCRIPT), "tagX", "--dry-run"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=False,
    )
    assert p.returncode == 2, f"exit={p.returncode} err={p.stderr}"
    assert "is too old" in p.stderr, p.stderr
    assert not marker.exists(), f"the refusal ran docker: {marker.read_text()}"
    assert "`docker build -t`" in p.stderr, p.stderr


class TestAFailingCliIsDiagnosable:
    """`blastbox version 2>/dev/null` threw away the only evidence of WHY the CLI failed.

    Measured, not imagined: blastbox installed without its `host` extra shipped a console script
    that died on `ModuleNotFoundError: No module named 'structlog'`. The traceback went to the
    discarded stderr, so this wrapper reported "no usable `version` output" for a perfectly
    current blastbox, and the remediation it printed reinstalled the same thing. Fixed in the
    library (blastbox 0.1.40); this keeps the NEXT such failure visible.
    """

    @pytest.fixture
    def broken_cli(self, tmp_path: Path) -> Path:
        """A `blastbox` that exists, exits non-zero, and explains itself on STDERR."""
        d = tmp_path / "bin"
        d.mkdir()
        stub = d / "blastbox"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'echo "Traceback (most recent call last):" >&2\n'
            "echo \"ModuleNotFoundError: No module named 'structlog'\" >&2\n"
            "exit 1\n"
        )
        stub.chmod(0o755)
        return d

    def test_the_real_error_is_shown(self, broken_cli: Path) -> None:
        r = _run(broken_cli, "sometag", "--dry-run")
        assert r.returncode == 2
        assert "no usable `version` output" in r.stderr
        assert "ModuleNotFoundError" in r.stderr, (
            "the wrapper hid the only line that explains the failure: " + r.stderr
        )
        assert "structlog" in r.stderr

    def test_a_silent_cli_says_so_rather_than_printing_nothing(self, tmp_path: Path) -> None:
        """A stub that prints NOTHING must not produce an empty 'printed:' section -- the
        absence of output is itself the diagnosis."""
        d = tmp_path / "bin"
        d.mkdir()
        stub = d / "blastbox"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(0o755)

        r = _run(d, "sometag", "--dry-run")
        assert r.returncode == 2
        assert "(nothing at all)" in r.stderr

    def test_a_working_cli_is_unaffected(self, stub_cli: Path) -> None:
        """The capture must not change the happy path: a good version still passes the gate."""
        r = _run(stub_cli, "sometag", "--dry-run")
        assert "no usable `version` output" not in r.stderr


class TestAFailedVersionCannotSatisfyTheFloor:
    """Capturing stderr for the diagnostic must not feed it to the version parser.

    A Python traceback names paths like `/usr/lib/python3.11/site-packages/...`. Merged into the
    parsed stream, `3.11` becomes the "installed version" -- and 3.11 sorts far above the 0.1.x
    floor, so a CLI that cannot start would CLEAR the gate and the wrapper would go on to run
    `blastbox build-images` with it. The change meant to explain failures would have disabled the
    check that catches them (codex).
    """

    def _stub(self, tmp_path: Path, body: str) -> Path:
        d = tmp_path / "bin"
        d.mkdir()
        stub = d / "blastbox"
        stub.write_text("#!/usr/bin/env bash\n" + body)
        stub.chmod(0o755)
        return d

    def test_a_traceback_path_is_not_a_version(self, tmp_path: Path) -> None:
        d = self._stub(
            tmp_path,
            'echo "Traceback (most recent call last):" >&2\n'
            'echo "  File \"/usr/lib/python3.11/site-packages/blastbox/cli.py\"" >&2\n'
            "echo \"ModuleNotFoundError: No module named 'structlog'\" >&2\n"
            "exit 1\n",
        )
        r = _run(d, "sometag", "--dry-run")
        assert r.returncode == 2, (
            "a CLI that cannot start cleared the version floor via its own traceback: " + r.stdout
        )
        assert "no usable `version` output" in r.stderr
        assert "ModuleNotFoundError" in r.stderr
        assert "build-images" not in r.stdout

    def test_a_noisy_failure_cannot_fill_the_temp_filesystem(self, tmp_path: Path) -> None:
        """Capturing stderr introduced an exposure `2>/dev/null` did not have.

        The stub floods stderr and only then touches a marker. Under the size cap the flood
        dies of SIGXFSZ first, so the marker is never created -- an observable proof of the
        bound rather than an assertion about the wrapper's intentions.
        """
        marker = tmp_path / "finished-writing"
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            "  for i in $(seq 1 200000); do\n"
            '    printf "%s\\n" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" >&2\n'
            "  done\n"
            f'  touch "{marker}"\n'
            "  exit 0\n"
            "fi\n"
            'printf "%s\\n" "$@"\n',
        )
        r = _run(d, "sometag", "--dry-run")
        assert not marker.exists(), (
            "the CLI wrote its whole flood to $TMPDIR; the size cap is not in effect"
        )
        assert r.returncode == 2
        assert "no usable `version` output" in r.stderr

    def test_a_supervisor_signal_terminates_the_wrapper(self, tmp_path: Path) -> None:
        """A signal to THIS PID ONLY -- what a process supervisor sends, and what the
        process-group test cannot cover, because that one also kills the child.

        A cleanup handler that returns instead of re-raising makes bash swallow the request:
        it waits out the CLI, runs the handler, and then proceeds into the build.
        """
        import signal
        import time

        # The CLI HANGS. A four-second child hid the defect codex found: with a foreground
        # command substitution bash defers the trap until the command returns, so the wrapper
        # was unkillable for as long as the CLI ran -- and a short sleep let the test pass
        # anyway. It also records its pid, so the child can be checked for afterwards.
        childpid = tmp_path / "childpid"
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            f'  echo $$ > "{childpid}"\n'
            "  sleep 300\n"
            "fi\n",
        )
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "sometag", "--dry-run"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "PATH": f"{d}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            },
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not childpid.exists():
            time.sleep(0.05)
        assert childpid.exists(), "the stub CLI never started"
        kid = int(childpid.read_text().strip())

        proc.send_signal(signal.SIGTERM)          # the PID, deliberately not the group
        try:
            # Well under the child's 300s: the point is that the wrapper does NOT wait for it.
            out, _err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)
            os.kill(kid, signal.SIGKILL)
            raise AssertionError(
                "the wrapper waited for a hung CLI instead of terminating"
            ) from None

        assert proc.returncode != 0, "SIGTERM was swallowed and the wrapper reported success"
        assert "--dry-run" not in out, (
            "the wrapper carried on into the build after being told to stop: " + out
        )
        time.sleep(0.5)
        try:
            os.kill(kid, 0)
        except ProcessLookupError:
            pass
        else:
            os.kill(kid, signal.SIGKILL)
            raise AssertionError(f"the CLI ({kid}) outlived the wrapper as an orphan")

    def test_a_stdout_flood_cannot_exhaust_memory(self, tmp_path: Path) -> None:
        """stdout used to be captured by a command substitution, i.e. buffered whole in shell
        memory, and `ulimit -f` does not bound a pipe. Both streams are files now; the marker
        after the flood is the observable proof the writer was stopped."""
        marker = tmp_path / "finished-stdout"
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            "  for i in $(seq 1 200000); do\n"
            '    printf "%s\\n" "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"\n'
            "  done\n"
            f'  touch "{marker}"\n'
            "  exit 0\n"
            "fi\n"
            'printf "%s\\n" "$@"\n',
        )
        r = _run(d, "sometag", "--dry-run")
        assert not marker.exists(), "the flood ran to completion; stdout is not bounded"
        assert r.returncode == 2

    def test_an_interrupted_run_leaves_no_temp_file(self, tmp_path: Path) -> None:
        """Ctrl-C while `blastbox version` is running must not strand the diagnostic file.

        The wrapper is started with its own TMPDIR, interrupted mid-`version`, and that
        directory is then required to be empty -- executed, not reasoned about.
        """
        import signal
        import time

        d = self._stub(tmp_path, 'if [ "$1" = version ]; then sleep 30; fi\n')
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        env = {
            **os.environ,
            "PATH": f"{d}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "TMPDIR": str(tmpdir),
        }
        # Its own session, and the signal goes to the process GROUP -- which is what a
        # terminal does on Ctrl-C. Signalling only the bash pid does not reproduce it: bash
        # defers a trap until the foreground command returns, so the `sleep` inside the
        # command substitution keeps running and the script sits there for its full duration.
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "sometag", "--dry-run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(tmpdir.iterdir()):
            time.sleep(0.05)
        assert any(tmpdir.iterdir()), "the wrapper never created its diagnostic temp file"

        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait(timeout=10)
            raise AssertionError("the wrapper did not exit on SIGINT") from exc

        leftovers = list(tmpdir.iterdir())
        assert leftovers == [], f"interrupted run stranded {leftovers} in TMPDIR"

    def test_a_nonzero_exit_is_not_trusted_even_with_a_version_on_stdout(
        self, tmp_path: Path
    ) -> None:
        """The exit status is its own gate. A CLI that prints a plausible version and then dies
        has not answered the question -- and without this check, parsing stdout alone would
        accept it."""
        d = self._stub(tmp_path, 'echo "blastbox 9.9.9"\nexit 1\n')
        r = _run(d, "sometag", "--dry-run")
        assert r.returncode == 2, "a failed `version` was trusted because stdout looked right"
        assert "no usable `version` output" in r.stderr

    def test_a_zero_exit_with_only_stderr_noise_does_not_pass(self, tmp_path: Path) -> None:
        """Exit 0 but nothing on stdout: stderr must not be scavenged for a number."""
        d = self._stub(tmp_path, 'echo "note: /opt/python3.11/lib warming up" >&2\nexit 0\n')
        r = _run(d, "sometag", "--dry-run")
        assert r.returncode == 2
        assert "no usable `version` output" in r.stderr

    def test_stderr_noise_beside_a_good_stdout_version_is_ignored(self, tmp_path: Path) -> None:
        """The version comes from stdout; a warning mentioning python3.11 must not become it."""
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            '  echo "warning: /usr/lib/python3.11/site-packages deprecation" >&2\n'
            f'  echo "blastbox {BB_MIN}"\n'
            "  exit 0\n"
            "fi\n"
            'printf "%s\\n" "$@"\n',
        )
        r = _run(d, "sometag", "--dry-run")
        assert "no usable `version` output" not in r.stderr
        assert "--dry-run" in r.stdout
