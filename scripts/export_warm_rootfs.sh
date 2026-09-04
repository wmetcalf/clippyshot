#!/usr/bin/env bash
# The warm rootfs artifacts are now exported by `scripts/build_images.sh`, in
# the same run that builds and VERIFIES the images they come from.
#
# Keeping a separate entry point is what allowed the two to disagree: the images
# could be rebuilt without the artifacts being replaced, so the warm tiers went
# on booting whatever they were last exported from while every tag said
# otherwise. Exporting separately also made it possible to export an image that
# had never been verified -- which is how a cold worker image with no /init
# reached the Firecracker tier and took it down.
set -euo pipefail
echo "scripts/export_warm_rootfs.sh has been folded into scripts/build_images.sh." >&2
echo "The export is part of that run, after the images it exports are verified." >&2
echo >&2
echo "  scripts/build_images.sh ${1:-<tag>}" >&2
exit 2
