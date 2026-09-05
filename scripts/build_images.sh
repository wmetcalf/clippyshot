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
BB_VERSION_OUT="$(blastbox version 2>&1)" || true
BB_HAVE="$(printf '%s\n' "$BB_VERSION_OUT" | grep -oE '[0-9]+(\.[0-9]+)+' | head -1 || true)"
[ -n "$BB_HAVE" ] || {
  echo "this blastbox has no usable \`version\` output; need >= $BB_MIN" >&2
  echo "\`blastbox version\` printed:" >&2
  if [ -n "$BB_VERSION_OUT" ]; then
    printf '%s\n' "$BB_VERSION_OUT" | tail -5 | sed 's/^/  /' >&2
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
