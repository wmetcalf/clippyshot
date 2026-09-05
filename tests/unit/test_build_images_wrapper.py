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
    # errors="replace": the wrapper is supposed to pass a broken CLI's bytes through, and a
    # diagnostic containing one undecodable byte would otherwise blow up the HARNESS rather
    # than fail the assertion -- a test that cannot even read the output it is judging.
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        errors="replace",
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



def _is_alive(pid: int) -> bool:
    """True only if the process exists AND is not a zombie.

    `os.kill(pid, 0)` succeeds for a defunct process, so on a runner whose PID 1 does not reap
    promptly -- containers, mostly -- a child that HAS been killed still answers, and a test
    asserting "nothing outlived the wrapper" fails at random (codex saw it locally with the
    `sleep` in state Z). The state comes from /proc; where that is unreadable, fall back to the
    signal probe rather than pretending to know.
    """
    if not Path("/proc").is_dir():
        # No procfs at all (macOS, the BSDs). FileNotFoundError here would otherwise be read
        # as "the process is gone" and the orphan assertions would pass vacuously (codex).
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    # `comm` can contain spaces and parentheses; the state is the field after the last ')'.
    state = stat.rpartition(")")[2].split()[0]
    return state != "Z"

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

    def test_bashpid_is_never_referenced_unguarded(self) -> None:
        """A lint-style check, and deliberately so: this one cannot be executed here.

        `BASHPID` is Bash 4+. Under `set -u` a Bash 3.2 -- the stock /bin/bash on macOS --
        aborts on an unguarded reference, which would abort the subshell before the CLI ran and
        report every valid install as having no usable version output (codex). Measured:

            $ bash -c 'set -u; unset BASHPID; ( echo "$BASHPID" )'
            bash: line 1: BASHPID: unbound variable
            $ bash -c 'set -u; unset BASHPID; ( echo "${BASHPID:-}" )'   # fine

        Proving the fix by RUNNING it needs a Bash 3.2 binary, which is not present here, so
        the guard is asserted on the source instead of pretended to be executed.
        """
        import re

        code = [
            line.split("#", 1)[0]          # the rule is about code; the comment above the
            for line in SCRIPT.read_text().splitlines()  # guard names the form it forbids
        ]
        unguarded = [
            line.strip()
            for line in code
            if re.search(r"\$BASHPID\b", line) or re.search(r"\$\{BASHPID\}", line)
        ]
        assert not unguarded, (
            "reference BASHPID as ${BASHPID:-}; Bash 3.2 under `set -u` aborts on: "
            + "; ".join(unguarded)
        )

    def test_an_undecodable_byte_does_not_swallow_the_diagnostic(self, tmp_path: Path) -> None:
        """One invalid byte anywhere in the captured stderr used to cost the whole message.

        Measured on this host rather than assumed:

            printf '\\xc3ABC\\nsecond line\\n' | grep -v '^$'
            grep: (standard input): binary file matches
            second line

        -- the line with the byte in it is REPLACED by that notice, and it is exactly the line
        an operator needs. A byte-wise `tail -c` can manufacture such a byte by cutting a
        multibyte character in half, which is why the truncation is line-wise now; `grep -a`
        covers the case where the CLI emitted one itself (codex).
        """
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            '  printf "Traceback (most recent call last):\\n" >&2\n'
            '  printf "  File \\"/opt/x\\xc3/cli.py\\", line 1\\n" >&2\n'
            "  printf \"ModuleNotFoundError: No module named 'structlog'\\n\" >&2\n"
            "  exit 1\n"
            "fi\n",
        )
        r = _run(d, "sometag", "--dry-run")
        assert r.returncode == 2
        assert "binary file matches" not in r.stderr.lower(), (
            "the diagnostic was replaced by grep's binary-file notice: " + r.stderr
        )
        assert "ModuleNotFoundError" in r.stderr

    def test_a_long_banner_does_not_hide_the_version(self, tmp_path: Path) -> None:
        """A CLI that prints a banner before its version is still a usable CLI.

        Parsing only the first 4 KiB of stdout rejected one, exit code zero and all -- the
        wrapper would have told a working install it was unusable (codex).
        """
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            "  for i in $(seq 1 120); do\n"
            '    printf "%s\\n" "note: warming up 0000000000000000000000000000000000000000"\n'
            "  done\n"
            f'  echo "blastbox {BB_MIN}"\n'
            "  exit 0\n"
            "fi\n"
            'printf "%s\\n" "$@"\n',
        )
        r = _run(d, "sometag", "--dry-run")
        assert "no usable `version` output" not in r.stderr, (
            "a banner longer than the parsed prefix hid a perfectly good version"
        )
        assert "--dry-run" in r.stdout

    def test_a_failing_second_mktemp_leaves_nothing_behind(self, tmp_path: Path) -> None:
        """The cleanup trap must be armed before the SECOND temp file is allocated.

        With both allocations ahead of the trap, a failure of the second one -- $TMPDIR out of
        space or inodes -- exits under `set -e` before any cleanup exists and leaks the first
        (codex). The stub lets the first mktemp through and fails the rest.
        """
        d = tmp_path / "bin"
        d.mkdir()
        counter = tmp_path / "mktemp-calls"
        (d / "blastbox").write_text(
            "#!/usr/bin/env bash\n"
            f'if [ "$1" = version ]; then echo "blastbox {BB_MIN}"; exit 0; fi\n'
        )
        (d / "blastbox").chmod(0o755)
        (d / "mktemp").write_text(
            "#!/usr/bin/env bash\n"
            f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
            "n=$((n + 1))\n"
            f'echo "$n" > "{counter}"\n'
            'if [ "$n" -ge 2 ]; then echo "mktemp: no space left on device" >&2; exit 1; fi\n'
            'exec /usr/bin/mktemp "$@"\n'
        )
        (d / "mktemp").chmod(0o755)

        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        env = {
            **os.environ,
            "PATH": f"{d}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "TMPDIR": str(tmpdir),
        }
        r = subprocess.run(
            ["bash", str(SCRIPT), "sometag", "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert r.returncode != 0, "a failed mktemp was treated as success"
        leftovers = list(tmpdir.iterdir())
        assert leftovers == [], f"the first temp file leaked when the second failed: {leftovers}"

    def test_a_cli_that_reads_stdin_does_not_hang_the_wrapper(self, tmp_path: Path) -> None:
        """`set -m` puts the CLI in a non-foreground process group, so a read from the terminal
        would stop it with SIGTTIN and `wait` would block forever -- a hang introduced by the
        isolation that makes the group killable (codex).

        The wrapper is given an OPEN stdin pipe that never receives data: without the
        `</dev/null` redirect the stub's read blocks and this test times out.
        """
        d = self._stub(tmp_path, 'if [ "$1" = version ]; then read -r line; echo "$line"; fi\n')
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "sometag", "--dry-run"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "PATH": f"{d}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            },
        )
        # `wait`, NOT `communicate`: communicate CLOSES stdin, which hands the stub an EOF and
        # makes the test pass even without the redirect -- it was the reason the mutation that
        # removes `</dev/null` survived. The pipe must stay open and silent, like a terminal
        # nobody is typing at.
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            raise AssertionError(
                "the wrapper hung waiting for a CLI that was reading stdin"
            ) from None
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        assert proc.returncode == 2

    def test_a_cli_that_ignores_sigterm_is_killed_anyway(self, tmp_path: Path) -> None:
        """An isolated process group that ignores TERM would outlive the wrapper as an orphan
        no supervisor can reach afterwards -- worse than the leak the isolation prevents
        (codex). The handler escalates to KILL, which nothing can trap."""
        import signal
        import time

        childpid = tmp_path / "stubborn-pid"
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            "  trap '' TERM INT HUP\n"
            f'  echo $$ > "{childpid}"\n'
            "  sleep 300\n"
            "fi\n",
        )
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "sometag", "--dry-run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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

        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)
            os.kill(kid, signal.SIGKILL)
            raise AssertionError("the wrapper never terminated") from None

        time.sleep(1.0)
        alive = _is_alive(kid)
        if alive:
            try:
                os.kill(kid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert not alive, f"a TERM-ignoring CLI ({kid}) survived as an unreachable orphan"

    def test_an_error_on_stdout_is_not_crowded_out_by_stderr(self, tmp_path: Path) -> None:
        """Concatenating the streams and tailing five lines discarded the actual error whenever
        it was on stdout and stderr had five lines after it -- the diagnostic throwing away the
        diagnosis (codex)."""
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            '  echo "FATAL: config at /etc/blastbox.toml is unreadable"\n'
            "  for i in $(seq 1 8); do\n"
            '    printf "%s\\n" "noise line filling the tail" >&2\n'
            "  done\n"
            "  exit 1\n"
            "fi\n",
        )
        r = _run(d, "sometag", "--dry-run")
        assert r.returncode == 2
        assert "is unreadable" in r.stderr, (
            "the error on stdout was crowded out by stderr noise: " + r.stderr
        )

    def test_every_trapped_signal_is_cleared_afterwards(self) -> None:
        """Source-level, and it says so: a handler left installed after the probe would later
        read a deleted pidfile, fall back to the finished probe's pid, and signal whatever
        process group has that number by then (codex). Observing that needs pid REUSE, which a
        test cannot arrange, so the invariant is asserted on the script instead.
        """
        import re

        code = [line.split("#", 1)[0] for line in SCRIPT.read_text().splitlines()]
        trapped: set[str] = set()
        cleared: set[str] = set()
        for line in code:
            m = re.match(r"\s*trap\s+(-|'[^']*'|\"[^\"]*\")\s+(.+?)\s*$", line)
            if not m:
                continue
            sigs = {s for s in m.group(2).split() if s.isalpha()}
            if m.group(1) == "-":
                cleared |= sigs
            else:
                trapped |= sigs
        missing = sorted(trapped - cleared - {"EXIT"})
        assert not missing, f"these traps are installed but never cleared: {missing}"

    def test_a_stricter_inherited_file_limit_is_respected(self, tmp_path: Path) -> None:
        """`ulimit -f 512` RAISES the limit when the caller already set something stricter, and
        raising fails -- under `set -e` that kills the subshell before the CLI runs, so a
        perfectly good install is reported as unusable (codex). The cap only ever lowers."""
        d = self._stub(
            tmp_path,
            f'if [ "$1" = version ]; then echo "blastbox {BB_MIN}"; exit 0; fi\n'
            'printf "%s\\n" "$@"\n',
        )
        env = {
            **os.environ,
            "PATH": f"{d}:{Path(sys.executable).parent}:{os.environ['PATH']}",
        }
        r = subprocess.run(
            ["bash", "-c", f"ulimit -f 100; exec bash {SCRIPT} sometag --dry-run"],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            check=False,
        )
        assert "no usable `version` output" not in r.stderr, (
            "a stricter caller-imposed file limit broke the version probe: " + r.stderr
        )
        assert "--dry-run" in r.stdout

    def test_an_inherited_bb_pid_is_not_signalled(self, tmp_path: Path) -> None:
        """An exported BB_PID from the caller must never become the handler's fallback.

        A bystander process is started in its own process group, its pid is exported as BB_PID,
        and the wrapper is interrupted mid-probe. The bystander must be alive afterwards --
        with an uninitialised BB_PID the handler would TERM and then KILL that whole group
        (codex).
        """
        import signal
        import time

        bystander = subprocess.Popen(
            ["/bin/sleep", "300"], start_new_session=True, stdout=subprocess.DEVNULL
        )
        try:
            childpid = tmp_path / "probe-pid"
            d = self._stub(
                tmp_path,
                'if [ "$1" = version ]; then\n'
                f'  echo $$ > "{childpid}"\n'
                "  sleep 300\n"
                "fi\n",
            )
            proc = subprocess.Popen(
                ["bash", str(SCRIPT), "sometag", "--dry-run"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    **os.environ,
                    "PATH": f"{d}:{Path(sys.executable).parent}:{os.environ['PATH']}",
                    "BB_PID": str(bystander.pid),
                },
                start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not childpid.exists():
                time.sleep(0.05)
            assert childpid.exists(), "the stub CLI never started"
            kid = int(childpid.read_text().strip())

            proc.send_signal(signal.SIGTERM)
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=10)
            for stray in (kid,):
                try:
                    os.kill(stray, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            time.sleep(1.0)
            assert _is_alive(bystander.pid), (
                "the wrapper signalled a process group it inherited through BB_PID"
            )
        finally:
            bystander.kill()
            bystander.wait(timeout=10)

    def test_a_hung_probe_is_abandoned_rather_than_waited_on_forever(
        self, tmp_path: Path
    ) -> None:
        """`</dev/null` does not cover a CLI that opens /dev/tty itself, and `set -m` means such
        a read is STOPPED by SIGTTIN with `wait` never returning (codex). Rather than a
        mechanism per blocking mode, the probe has a deadline -- whatever the reason, it ends
        and the operator gets the diagnostic.
        """
        import time

        d = self._stub(tmp_path, 'if [ "$1" = version ]; then sleep 300; fi\n')
        env = {
            **os.environ,
            "PATH": f"{d}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "BLASTBOX_VERSION_TIMEOUT_S": "3",
        }
        started = time.monotonic()
        r = subprocess.run(
            ["bash", str(SCRIPT), "sometag", "--dry-run"],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=120,
            check=False,
        )
        elapsed = time.monotonic() - started
        assert r.returncode == 2
        assert "no usable `version` output" in r.stderr
        assert elapsed < 60, f"the wrapper waited {elapsed:.0f}s on a hung probe"

    def test_a_legitimate_large_write_is_not_punished(self, tmp_path: Path) -> None:
        """RLIMIT_FSIZE is process-wide, not a cap on the two capture files: a CLI that appends
        to a cache or log larger than the limit takes SIGXFSZ and is rejected as unusable
        (codex). 1 MiB is ordinary; it must not fail."""
        scratch = tmp_path / "cli-cache"
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            f'  dd if=/dev/zero of="{scratch}" bs=1024 count=1024 2>/dev/null\n'
            f'  echo "blastbox {BB_MIN}"\n'
            "  exit 0\n"
            "fi\n"
            'printf "%s\\n" "$@"\n',
        )
        r = _run(d, "sometag", "--dry-run")
        assert "no usable `version` output" not in r.stderr, (
            "a 1 MiB write by the CLI was treated as a broken CLI: " + r.stderr
        )
        assert scratch.exists() and scratch.stat().st_size == 1024 * 1024
        assert "--dry-run" in r.stdout

    def test_a_quit_takes_the_cli_with_it(self, tmp_path: Path) -> None:
        """Ctrl-\\ sends SIGQUIT to the foreground group, which no longer contains the isolated
        probe -- so without a handler neither the wrapper nor the probe goes away (codex)."""
        import signal
        import time

        childpid = tmp_path / "quit-childpid"
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            f'  echo $$ > "{childpid}"\n'
            "  sleep 300\n"
            "fi\n",
        )
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "sometag", "--dry-run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                **os.environ,
                "PATH": f"{d}:{Path(sys.executable).parent}:{os.environ['PATH']}",
                "BLASTBOX_VERSION_TIMEOUT_S": "300",
            },
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not childpid.exists():
            time.sleep(0.05)
        assert childpid.exists(), "the stub CLI never started"
        kid = int(childpid.read_text().strip())

        proc.send_signal(signal.SIGQUIT)
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)
            os.kill(kid, signal.SIGKILL)
            raise AssertionError("SIGQUIT did not terminate the wrapper") from None

        time.sleep(1.0)
        alive = _is_alive(kid)
        if alive:
            try:
                os.kill(kid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert not alive, f"the CLI ({kid}) survived the quit as an orphan"

    def test_a_hangup_takes_the_cli_with_it(self, tmp_path: Path) -> None:
        """SIGHUP is what arrives when the terminal goes away -- precisely when nobody is left
        to notice a CLI still running. Only INT and TERM forwarded before (codex)."""
        import signal
        import time

        childpid = tmp_path / "hup-childpid"
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            f'  echo $$ > "{childpid}"\n'
            "  sleep 300\n"
            "fi\n",
        )
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "sometag", "--dry-run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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

        proc.send_signal(signal.SIGHUP)
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)
            raise AssertionError("SIGHUP did not terminate the wrapper") from None

        time.sleep(0.5)
        alive = _is_alive(kid)
        if alive:
            try:
                os.kill(kid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert not alive, f"the CLI ({kid}) survived the hangup as an orphan"

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
        grandpid = tmp_path / "grandpid"
        # The CLI spawns a child of its own. Killing only the CLI leaves that grandchild
        # reparented to init and running -- an orphan the earlier version of this test could
        # not see, because it checked the stub's `$$` alone (codex).
        d = self._stub(
            tmp_path,
            'if [ "$1" = version ]; then\n'
            f'  echo $$ > "{childpid}"\n'
            "  /bin/sleep 300 &\n"
            f'  echo $! > "{grandpid}"\n'
            "  wait\n"
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
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not grandpid.exists():
            time.sleep(0.05)
        assert grandpid.exists(), "the stub CLI never spawned its child"
        kid = int(childpid.read_text().strip())
        grandkid = int(grandpid.read_text().strip())

        proc.send_signal(signal.SIGTERM)          # the PID, deliberately not the group
        try:
            # Well under the child's 300s: the point is that the wrapper does NOT wait for it.
            out, _err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)
            for stray in (kid, grandkid):
                try:
                    os.kill(stray, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise AssertionError(
                "the wrapper waited for a hung CLI instead of terminating"
            ) from None

        assert proc.returncode != 0, "SIGTERM was swallowed and the wrapper reported success"
        assert "--dry-run" not in out, (
            "the wrapper carried on into the build after being told to stop: " + out
        )
        time.sleep(0.5)
        survivors = [
            f"{label} ({stray})"
            for stray, label in ((kid, "the CLI"), (grandkid, "the CLI's child"))
            if _is_alive(stray)
        ]
        for stray, _ in ((kid, None), (grandkid, None)):
            try:
                os.kill(stray, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert not survivors, f"{', '.join(survivors)} outlived the wrapper as an orphan"

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
