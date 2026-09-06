#!/usr/bin/env bash
# Build and export ClippyShot's images. Thin wrapper around `blastbox
# build-images`, which reads blastbox-images.toml in this repo.
#
# There was no build path here at all before this. The chain lived in blastbox's
# deploy/redeploy-warm.sh as a block of preset variables, and rebuilding meant
# reading that script to find out what built what -- two of the four Dockerfiles
# live in blastbox rather than here. blastbox-images.toml is that knowledge,
# written down where the engine is, and `blastbox build-images` executes it.
#
# Usage:
#   scripts/build_images.sh <tag> [blastbox-version] [--dry-run]
#
# Env: BLASTBOX_SRC        REQUIRED. blastbox source tree; the two warm images
#                          are built from Dockerfiles that live there.
#      CLIPPYSHOT_FC_DIR      where the Firecracker rootfs is written
#      CLIPPYSHOT_GVISOR_DIR  where the gVisor tree is written
#                            (default /home/coz/clippyshot-gvisor)
set -euo pipefail

# Declared ONCE, above the first message that mentions it. Written out by hand
# in two places, this drifted before: the script told an operator to install a
# version it then rejected.
BB_MIN=0.1.39

# One check for EVERY way a version can arrive: the legacy bare argument and
# the `--blastbox-version V` / `--blastbox-version=V` option, which is
# forwarded verbatim in "$@". Gating only the bare form left the floor
# bypassable on the live-rootfs build path -- and this script constructs the
# option form itself, so it is not a hypothetical spelling.
require_floor() {  # <version>
  # PEP 440 semantics, not `sort -V`. They disagree exactly where it matters:
  # `0.1.38rc1` is BELOW `0.1.38` for pip, and `sort -V` puts it above -- so a
  # release candidate of the floor version, which predates the fixes the floor
  # exists for, would sail straight through. `v0.1.35` is the same trap from
  # the other side: pip normalizes away the `v`, `sort -V` does not.
  local want="$1" bb_py rc cand plain
  # Pick an interpreter that can actually DO the comparison, rather than
  # trusting one. Parsing blastbox's shebang is not enough on its own: a
  # console script written `#!/usr/bin/env python3` yields `/usr/bin/env`,
  # which then "runs" and fails, and the fallback silently applied the weaker
  # `sort -V` rules -- exactly the outcome this function exists to avoid.
  bb_py=""
  for cand in "$(head -1 "$(command -v blastbox)" | sed 's/^#!//; s/ .*//')" python3 python; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import packaging.version' >/dev/null 2>&1; then
      bb_py="$cand"; break
    fi
  done
  # An absent interpreter is not a verdict: keep it distinct from a real
  # "below the floor" answer, or every version -- including ones well above
  # the floor -- gets refused on a host without packaging installed.
  if [ -z "$bb_py" ]; then
    rc=127
  elif "$bb_py" -c 'import sys
from packaging.version import InvalidVersion, Version
try:
    floor, want = Version(sys.argv[1]), Version(sys.argv[2])
except InvalidVersion:
    sys.exit(3)
sys.exit(0 if want >= floor else 1)' "$BB_MIN" "$want" 2>/dev/null
  then
    return 0
  else
    rc=$?
  fi
  if [ "$rc" -eq 1 ]; then
    echo "refusing to build with blastbox $want: below the floor of $BB_MIN." >&2
    echo "That version has defects on the path that replaces a live rootfs." >&2
    exit 2
  fi
  if [ "$rc" -eq 3 ]; then
    echo "refusing to build with blastbox $want: not a PEP 440 version." >&2
    exit 2
  fi
  # Any other status means the comparison could not RUN. Fall back to sort -V
  # and say so, rather than silently applying weaker rules.
  echo "note: comparing $want against $BB_MIN with sort -V; PEP 440 was" >&2
  echo "unavailable, so pre-releases cannot be ranked." >&2
  # `sort -V` only ranks plain releases the way pip does. Rather than guess at
  # a spelling it cannot order -- `v0.1.35`, `0.1.38rc1`, `0.1.38.post1` -- the
  # fallback refuses, so the weaker path cannot be used to slip past the floor.
  plain="${want#[vV]}"
  case "$plain" in
    ""|*[!0-9.]*|*..*|.*|*.) plain="" ;;
  esac
  if [ -z "$plain" ]; then
    echo "refusing to build with blastbox $want: cannot rank that spelling" >&2
    echo "without PEP 440. Install \`packaging\` in blastbox's environment," >&2
    echo "or pass a plain release version such as $BB_MIN." >&2
    exit 2
  fi
  if [ "$(printf '%s\n%s\n' "$BB_MIN" "$plain" | sort -V | head -1)" != "$BB_MIN" ]; then
    echo "refusing to build with blastbox $want: below the floor of $BB_MIN." >&2
    exit 2
  fi
}

TAG="${1:?usage: build_images.sh <tag> [blastbox-version] [--dry-run]}"
shift
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# A bare version argument is accepted, because that is how this script was
# called before it became a wrapper. Anything beginning with `-` is a flag and
# is passed straight through.
version_arg=()
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
  # Checked against the SAME floor as the CLI. Only BB_HAVE was compared, so
  # the documented `build_images.sh <tag> 0.1.35` form sailed through: it is
  # forwarded as --blastbox-version, the cold worker installs with --no-deps
  # (which never enforces this repo's pin), and the result is a stamped image
  # carrying a blastbox below the floor -- or a stamp naming a version the
  # image does not contain.
  require_floor "$1"
  version_arg=(--blastbox-version "$1")
  shift
fi

command -v blastbox >/dev/null || {
  echo "blastbox CLI not found. This script needs a blastbox providing" >&2
  echo "an executing \`blastbox build-images\` (>= $BB_MIN):" >&2
  echo "  pip install 'blastbox>=$BB_MIN'" >&2
  exit 2
}
# Having the SUBCOMMAND is not the same as having a version that can run it:
# 0.1.33 has `build-images` and it only validates, so an older blastbox exits 2
# saying execution is not implemented -- which reads like a broken script.
# stderr is CAPTURED, not discarded. `2>/dev/null` here cost a real diagnosis:
# blastbox installed without its `host` extra had a console script that died on
# `ModuleNotFoundError: No module named 'structlog'`, the traceback went to the
# stderr this line threw away, and the operator was told their current blastbox
# had "no usable version output" -- then handed a reinstall of the same thing.
# That specific cause is fixed in blastbox 0.1.40; this keeps the NEXT one
# visible.
# The streams are kept SEPARATE, and that is not tidiness. Merging them so the
# diagnostic could see stderr meant the version regex saw it too: a traceback
# mentioning `/usr/lib/python3.11/site-packages/...` yields `3.11`, which sorts
# far above the floor, so a CLI that cannot start would clear the version gate
# and go straight to `blastbox build-images` -- the check defeated by the very
# change meant to explain its failures (codex).
#
# So: the version is parsed from STDOUT of a run that EXITED ZERO. stderr is
# captured only to show the operator what happened.
# The second mktemp happens AFTER the trap is armed. Allocating both first meant a
# failure of the second one -- $TMPDIR out of space or inodes, under `set -e` --
# exited before any cleanup existed and leaked the first (codex).
BB_ERR_FILE="$(mktemp)"
BB_OUT_FILE=""
BB_PID_FILE=""
# Initialised BEFORE the traps that read it. An exported BB_PID from the caller
# would otherwise be the handler's fallback while the pidfile is still empty, and
# a signal in that window would send TERM and then KILL to an unrelated process
# GROUP that happened to have that number (codex).
BB_PID=""
# Same reason as BB_PID: an exported _bb_watchdog_pid would be the handler's target
# while the real one is still unassigned, so a signal in that window would TERM a
# process group the caller happens to name (codex).
_bb_watchdog_pid=""
BB_EXPIRY_FILE=""
_bb_version_cleanup() {
  rm -f "$BB_ERR_FILE" ${BB_OUT_FILE:+"$BB_OUT_FILE"} ${BB_PID_FILE:+"$BB_PID_FILE"} \
        ${BB_EXPIRY_FILE:+"$BB_EXPIRY_FILE"}
}
# `blastbox version` runs in the BACKGROUND and is waited on, which is the only
# arrangement that survives all three problems at once:
#
#  * a Ctrl-C or a supervisor's SIGTERM must not strand these temp files, so the
#    handlers clean up;
#  * bash defers a trap until the FOREGROUND command returns, so with a command
#    substitution a hung CLI made the wrapper unkillable -- worse than having no
#    trap at all, where the default disposition would have killed it outright.
#    `wait` is interruptible, so the handler runs immediately;
#  * a handler that only cleans up SWALLOWS the termination, so these reset the
#    trap and re-signal self, giving the caller the 128+n status.
#
# `exec` matters: it makes the subshell BECOME blastbox, so $! is the CLI's own
# pid and killing it kills the CLI rather than orphaning it behind a subshell.
# (codex, three rounds on this block; each of these was measured, not reasoned.)
# GUARD the pid, never default it: `kill 0` does not mean "kill nothing", it
# signals the wrapper's ENTIRE process group -- the caller and its siblings --
# and a signal arriving between installing this trap and assigning BB_PID would
# have done exactly that. Kill the CLI's process GROUP (`-$BB_PID`, which needs
# the `set -m` below), because killing the CLI alone leaves whatever it spawned
# reparented to init and still running. (codex)
# The pid comes from the FILE the job writes as its first act, not from BB_PID.
# A signal arriving between `&` and `BB_PID=$!` left the handler with nothing to
# signal, so it killed the wrapper and left the CLI and its descendants running
# (codex). Reading the file also means this path is exercised by every signal
# test rather than only in the race it was written for.
#
# The window is not fully closed and cannot be in POSIX shell: a signal in the
# instant between fork and the job's first command still finds no pid. What is
# left is a fork's width, against the whole runtime of the CLI before.
_bb_version_kill() {
  _bb_pid="$(cat "${BB_PID_FILE:-/nonexistent}" 2>/dev/null || true)"
  if [ -z "$_bb_pid" ]; then
    _bb_pid="${BB_PID:-}"
  fi
  if [ -n "$_bb_pid" ]; then
    kill -- "-$_bb_pid" 2>/dev/null || true
    # ESCALATE. A CLI that ignores TERM would otherwise survive in the isolated
    # process group while the wrapper exits -- an orphan a supervisor signalling
    # the wrapper can no longer reach, which is worse than the leak this whole
    # arrangement was built to prevent (codex). Half a second, then KILL, which
    # nothing can ignore.
    sleep 0.5
    kill -KILL -- "-$_bb_pid" 2>/dev/null || true
  fi
  # The watchdog dies with the probe it was watching; left running it would signal a
  # process group number that by then belongs to somebody else.
  if [ -n "${_bb_watchdog_pid:-}" ]; then
    kill -- "-$_bb_watchdog_pid" 2>/dev/null || true
  fi
}
trap '_bb_version_cleanup' EXIT
trap '_bb_version_kill; _bb_version_cleanup; trap - INT; kill -INT $$' INT
trap '_bb_version_kill; _bb_version_cleanup; trap - TERM; kill -TERM $$' TERM
# HUP too. A terminal that goes away is exactly when the CLI is left running with
# nobody watching, and `set -m` has just put it in its own process group -- so
# without this the EXIT cleanup runs, the wrapper dies, and the CLI and its
# descendants carry on orphaned (codex).
trap '_bb_version_kill; _bb_version_cleanup; trap - HUP; kill -HUP $$' HUP
# QUIT as well (Ctrl-\). The terminal sends it to the foreground group, which no
# longer contains the isolated probe, so without a handler `wait` is not
# interrupted and both the wrapper and a hung probe sit there (codex).
trap '_bb_version_kill; _bb_version_cleanup; trap - QUIT; kill -QUIT $$' QUIT
# BOTH streams go to files under `ulimit -f`. stdout was previously captured by a
# command substitution, which buffers the whole stream in shell memory -- and
# `ulimit -f` bounds regular-file writes, not a pipe, so the stderr cap did
# nothing for it. A flood on either stream now dies of SIGXFSZ at 512 KiB.
BB_OUT_FILE="$(mktemp)"
BB_PID_FILE="$(mktemp)"
# Derived rather than mktemp'd, but PREALLOCATED here: creating it inside the
# watchdog meant a $TMPDIR that filled up after startup killed the watchdog on
# `set -e` before it signalled anything -- losing the deadline to a disk problem
# (codex). Emptiness is the signal, so it can exist from the start.
BB_EXPIRY_FILE="$BB_PID_FILE.expired"
: > "$BB_EXPIRY_FILE"
# BOUND THE PROBE'S DURATION, rather than chase the ways a CLI can block. `</dev/null`
# does not cover a CLI that opens /dev/tty itself, and `set -m` means such a read is
# STOPPED by SIGTTIN and `wait` never returns (codex). Rather than add a mechanism per
# blocking mode, the probe gets a deadline: whatever the reason, it ends and the
# operator gets the diagnostic instead of a wrapper that hangs forever.
#
# The deadline is a shell WATCHDOG, not `timeout(1)`. Depending on the binary made the
# guarantee conditional on it being installed -- absent on stock macOS and on minimal
# build hosts, exactly where an odd CLI is likeliest -- and it failed OPEN, silently
# leaving no deadline at all (codex). One mechanism, always present, no branch.
_bb_version_deadline="${BLASTBOX_VERSION_TIMEOUT_S:-30}"
# REJECT a bad value rather than carry on without a deadline. `sleep bogus` fails,
# and the watchdog subshell inherits `set -e`, so it would exit before sending any
# signal -- the guarantee silently gone, which is the same fail-open shape as
# depending on `timeout(1)` (codex). A typo in an env var should say so.
case "$_bb_version_deadline" in
  ''|*[!0-9]*)
    echo "BLASTBOX_VERSION_TIMEOUT_S must be a whole number of seconds;" >&2
    echo "  got: $_bb_version_deadline" >&2
    exit 2
    ;;
esac
# Strip leading zeros first, so a fixed-width `000008` is judged on its value rather
# than its padding (codex). An all-zero value becomes empty and is caught below as 0.
_bb_version_deadline="$(printf '%s' "$_bb_version_deadline" | sed 's/^0*//')"
[ -n "$_bb_version_deadline" ] || _bb_version_deadline=0
# Length first: all-digits but astronomically large overflows the shell's integer
# comparison, which reports "integer expression expected" and does NOT take the
# rejection branch -- after which `sleep` fails and the watchdog dies silently
# (codex). A day is already far past any sane probe.
if [ "${#_bb_version_deadline}" -gt 5 ] || [ "$_bb_version_deadline" -lt 1 ] \
   || [ "$_bb_version_deadline" -gt 86400 ]; then
  echo "BLASTBOX_VERSION_TIMEOUT_S must be between 1 and 86400 seconds;" >&2
  echo "  got: $_bb_version_deadline" >&2
  exit 2
fi
# The deadline is only a guarantee if the thing that implements it exists. Without
# `sleep` the watchdog subshell dies on command-not-found -- with its stderr
# discarded -- and the wrapper waits forever on a hung CLI (codex).
if ! command -v sleep >/dev/null 2>&1; then
  echo "no \`sleep\` on PATH, so the version probe cannot be given a deadline" >&2
  exit 2
fi
_bb_watchdog_pid=""
# stdin comes from /dev/null. `set -m` puts this job in a process group that is NOT
# the terminal's foreground group, so a CLI that tried to read the terminal would be
# STOPPED by SIGTTIN and the `wait` below would block forever -- a hang introduced by
# the very isolation that makes the group killable (codex). A version probe has no
# business reading stdin anyway.
# `set -m` gives the background job its own process group, which is what makes
# `kill -- -$BB_PID` above able to take the CLI's descendants with it.
set -m
# `${BASHPID:-}`, not `$BASHPID`: the variable is Bash 4+, and under `set -u` on a
# Bash 3.2 (the stock /bin/bash on macOS) referencing it would abort this subshell
# before the CLI ever ran -- reporting every valid install as having no usable
# version output. Where it is absent the pidfile stays empty and the handler falls
# back to $BB_PID, i.e. to the behaviour before the race fix, rather than to a
# broken wrapper (codex). This file already keeps pre-4.4 array handling for the
# same reason.
# LOWER the file-size limit, never raise it. `ulimit -f 512` under a caller that
# already imposed something stricter -- `(ulimit -f 100; scripts/build_images.sh ...)`
# -- fails, and under `set -e` that kills the subshell before the CLI ever runs, so
# a perfectly good install is reported as having no usable version output (codex).
( _bb_fsize="$(ulimit -f)"
  # 8 MiB, not 512 KiB. RLIMIT_FSIZE is PROCESS-wide, not a cap on these two files:
  # a CLI that legitimately appends to a cache or log bigger than the limit takes
  # SIGXFSZ and is rejected as unusable (codex). 8 MiB leaves ordinary writes alone
  # while still bounding a runaway flood, which is measured in gigabytes.
  if [ "$_bb_fsize" = unlimited ] || { [ "$_bb_fsize" -gt 8192 ] 2>/dev/null; }; then
    ulimit -f 8192
  fi
  _bb_bashpid="${BASHPID:-}"
  # `|| true`: this file is an OPTIMISATION that closes a race window, not a
  # requirement. Under `set -e` a failed write here would abort the probe and
  # report a working CLI as unusable -- trading a real failure for a hypothetical
  # one.
  if [ -n "$_bb_bashpid" ]; then echo "$_bb_bashpid" > "$BB_PID_FILE" || true; fi
  exec blastbox version ) \
  >"$BB_OUT_FILE" 2>"$BB_ERR_FILE" </dev/null &
BB_PID=$!
set +m
# TERM, then KILL: a process stopped by SIGTTIN cannot act on TERM, and only KILL
# moves it. The group, so the CLI's own children go too.
# The watchdog gets its OWN process group as well, because cancelling it means
# killing the `sleep` it is blocked in: killing the subshell alone leaves that
# sleep reparented to init, ticking away until the full deadline (codex).
set -m
( # `10#`: a zero-padded value like `08` passes the all-digits and range checks and
  # is then read as OCTAL by the arithmetic below, which aborts the watchdog -- the
  # deadline gone, from a value that looks perfectly ordinary (codex).
  _bb_left=$((10#$_bb_version_deadline))
  # POLL rather than sleep the whole deadline. A signal arriving between this fork
  # and `_bb_watchdog_pid=$!` would leave a watchdog nobody has the pid of, alive
  # for up to a day and then signalling a process group number that by then belongs
  # to someone else. Checking the target each second makes an orphaned watchdog
  # self-limiting -- it exits within a second of the probe it was watching -- which
  # closes the race by design instead of by another guard (codex).
  while [ "$_bb_left" -gt 0 ]; do
    sleep 1
    kill -0 "$BB_PID" 2>/dev/null || exit 0
    _bb_left=$((_bb_left - 1))
  done
  # RECORD the expiry rather than let it be inferred from the exit status. A hung CLI
  # that traps the watchdog's TERM, prints a plausible version and exits 0 is
  # indistinguishable by status from one that simply answered -- so the wrapper would
  # accept a version from a probe that had already blown its deadline (codex).
  echo expired > "$BB_EXPIRY_FILE" 2>/dev/null || true
  kill -TERM -- "-$BB_PID" 2>/dev/null || true
  sleep 5
  kill -KILL -- "-$BB_PID" 2>/dev/null || true ) >/dev/null 2>&1 &
_bb_watchdog_pid=$!
set +m
wait "$BB_PID" && BB_RC=0 || BB_RC=$?
# The escalation happens HERE, not in the watchdog's grace period. `wait` returns as
# soon as the direct process dies, so cancelling the watchdog at that moment cut
# short the five seconds in which it would have KILLed a descendant that ignored
# TERM (codex). Doing it inline is also faster and safer than a detached process
# signalling a group number five seconds later.
kill -- "-$_bb_watchdog_pid" 2>/dev/null || true
wait "$_bb_watchdog_pid" 2>/dev/null || true
_bb_watchdog_pid=""
# ONLY when the probe was killed by us. If it exited on its own, `wait` has already
# reaped it and its process-group id is free to be reused -- sweeping then could
# signal a group that now belongs to someone else. When our watchdog fired, any
# stubborn descendant is still holding that pgid, so it cannot have been recycled
# (codex).
_bb_deadline_expired=""
if [ -s "$BB_EXPIRY_FILE" ]; then
  _bb_deadline_expired=1
fi
# Sweep whenever the group STILL HAS MEMBERS, which is the honest test. A probe that
# exits 0 after leaving a background helper behind also leaves that helper in the
# group (codex), so keying the sweep off expiry alone let it outlive the wrapper --
# but sweeping unconditionally could signal a pgid that was freed and recycled the
# instant `wait` reaped the leader. `kill -0` on the GROUP distinguishes the two: a
# surviving member keeps the pgid allocated, so it cannot have been reused. The
# residue is the check-then-act window, which no pgid-based cleanup can close.
if kill -0 -- "-$BB_PID" 2>/dev/null; then
  kill -TERM -- "-$BB_PID" 2>/dev/null || true
  sleep 1
  kill -KILL -- "-$BB_PID" 2>/dev/null || true
fi
# The pid is spent: `wait` has reaped it, so from here it names nothing of ours and a
# late signal handler must not act on it (codex).
BB_PID=""
# The version is grepped from the WHOLE file, which `ulimit -f` has already capped
# at 512 KiB. Reading a 4 KiB prefix instead meant a CLI that prints a long banner
# before its version was rejected as unusable even though it exited zero (codex).
# The two variables below exist only for the diagnostic, so they stay small.
# `tail -n`, not `tail -c`: a byte cut can land inside a multibyte character, and the
# `grep` below then decides the diagnostic is a binary file and prints
# "binary file matches" instead of the error (codex). Whole lines cannot split a
# character, and `grep -a` covers a CLI whose output is genuinely binary.
# Bounded three ways: the last 64 KiB, of that the last 20 lines, and each line cut
# to 500 characters. Line COUNT alone is not a bound -- a multi-megabyte stream with
# no newlines is one line, and the diagnostic would print all of it (codex).
# The TAIL of an over-long line, not its head. `cut -c1-500` kept the first 500
# characters, and a CLI that writes a long preamble and then its actual error puts
# that error at the END -- so the bound threw away the only part worth printing
# (codex).
_bb_tail_of_line='{ if (length($0) > 500) print substr($0, length($0) - 499); else print }'
# `LC_ALL=C awk`: the byte-wise `tail -c` can split a multibyte character, and a
# locale-sensitive awk then chokes on the invalid leading byte. Bytes are the right
# unit for a bounded diagnostic anyway (codex).
BB_VERSION_OUT="$(tail -c 65536 "$BB_OUT_FILE" 2>/dev/null | tail -n 20 \
  | LC_ALL=C awk "$_bb_tail_of_line" || true)"
BB_VERSION_ERR="$(tail -c 65536 "$BB_ERR_FILE" 2>/dev/null | tail -n 20 \
  | LC_ALL=C awk "$_bb_tail_of_line" || true)"
# Everything read out of the files happens HERE, before the cleanup below deletes
# them -- including the version itself, which is grepped from the whole capped
# file rather than from a truncated copy of it.
BB_HAVE=""
# An expired probe is not trusted whatever it exited with. A hung CLI that traps the
# watchdog's TERM, prints a plausible version and exits 0 looks by status exactly
# like one that simply answered (codex).
if [ "$BB_RC" -eq 0 ] && [ -z "$_bb_deadline_expired" ]; then
  BB_HAVE="$(grep -aoE '[0-9]+(\.[0-9]+)+' "$BB_OUT_FILE" 2>/dev/null | head -1 || true)"
fi
_bb_version_cleanup
trap - EXIT INT TERM HUP QUIT
[ -n "$BB_HAVE" ] || {
  if [ -n "${_bb_deadline_expired:-}" ]; then
    echo "\`blastbox version\` exceeded its ${_bb_version_deadline}s deadline and was killed;" >&2
    echo "  anything it printed is not trusted. Raise BLASTBOX_VERSION_TIMEOUT_S if the CLI is" >&2
    echo "  merely slow." >&2
  fi
  echo "this blastbox has no usable \`version\` output; need >= $BB_MIN" >&2
  echo "\`blastbox version\` exited $BB_RC and printed:" >&2
  if [ -n "$BB_VERSION_OUT$BB_VERSION_ERR" ]; then
    # A slice of EACH stream. Concatenating and tailing dropped the actionable error
    # whenever it was on stdout and stderr had five or more lines after it -- the
    # diagnostic discarding the diagnosis (codex).
    if [ -n "$BB_VERSION_OUT" ]; then
      printf '%s\n' "$BB_VERSION_OUT" | grep -a -v '^$' | tail -3 | sed 's/^/  [stdout] /' >&2
    fi
    if [ -n "$BB_VERSION_ERR" ]; then
      printf '%s\n' "$BB_VERSION_ERR" | grep -a -v '^$' | tail -5 | sed 's/^/  [stderr] /' >&2
    fi
  else
    echo "  (nothing at all)" >&2
  fi
  echo "If that names a missing module, the CLI is installed without the extra" >&2
  echo "it needs: pip install --upgrade 'blastbox>=$BB_MIN'" >&2
  exit 2
}
# sort -V puts the smaller first, so the minimum leading means it is satisfied.
# The regex already reduced a PEP 440 local version (0.1.34+gabc) to its release
# segment, so a source build of the minimum counts as meeting it.
[ "$(printf '%s\n%s\n' "$BB_MIN" "$BB_HAVE" | sort -V | head -1)" = "$BB_MIN" ] || {
  echo "blastbox $BB_HAVE is too old; need >= $BB_MIN." >&2
  echo "Earlier versions have defects on the path that REPLACES a live" >&2
  echo "rootfs: 0.1.35 refuses to rebuild an artifact someone had grown," >&2
  echo "and versions before 0.1.38 can leak secret build args into failure" >&2
  echo "output and be hung by a FIFO planted at the publish lock path." >&2
  # ESCAPED. Inside a double-quoted string bash reads backticks as command
  # substitution, so this line RAN `docker build -t` while composing a
  # refusal -- printing an unrelated docker error, or `command not found`,
  # and dropping the command text out of the explanation entirely.
  echo "Before 0.1.39, \`docker build -t\` moved the LIVE fleet tag as soon as" >&2
  echo "one image succeeded -- so a worker could pull an image nothing had" >&2
  echo "verified, and a mid-chain failure left the tags on a mixture of two" >&2
  echo "builds. Its published children also recorded a base reference the" >&2
  echo "same run then deleted." >&2
  echo "  pip install --upgrade 'blastbox>=$BB_MIN'" >&2
  exit 2
}

# `${a[@]+"${a[@]}"}` rather than `"${a[@]}"`: bash before 4.4 treats an empty
# array as unset under `set -u` and aborts. Bash 5 does not, so the test suite
# here cannot tell the two apart -- that mutant survives, and the guard is kept
# for the older shells rather than because a local test justifies it.
# Any pass-through occurrence of the option, in either spelling.
prev=""
for a in "$@"; do
  case "$a" in
    --blastbox-version=*) require_floor "${a#--blastbox-version=}" ;;
    *) [ "$prev" = "--blastbox-version" ] && require_floor "$a" ;;
  esac
  prev="$a"
done

exec blastbox build-images "$REPO" --tag "$TAG" ${version_arg[@]+"${version_arg[@]}"} "$@"
