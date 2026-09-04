"""What blastbox-images.toml declares must match the Dockerfiles it names.

There was no build path here before: the chain lived in blastbox's
deploy/redeploy-warm.sh as a block of preset variables, so nothing in this repo
described how its own images are built. These tests are that description made
checkable.

The generic checks live in blastbox and are tested there. What is REPO business
is that this declaration matches the Dockerfiles it points at -- including the
two that live in blastbox rather than here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

ROOT = Path(__file__).resolve().parents[1]

if TYPE_CHECKING:  # the real type, so mypy checks the attribute access below
    from blastbox.host.images import ImageSpec

# importorskip at RUNTIME: these tests must still collect where blastbox is not
# installed. The TYPE_CHECKING import is erased at runtime.
images = pytest.importorskip("blastbox.host.images")

PLAN = images.load_plan(ROOT)
LOCAL = [i for i in PLAN.images if i.context == "."]
FOREIGN = [i for i in PLAN.images if i.context != "."]


def test_the_declaration_names_every_tier() -> None:
    """A tier missing from the spec is a tier nothing rebuilds -- the fleet then
    runs two versions while every tag says one."""
    names = {i.name for i in PLAN.images}
    assert {"clippyshot", "clippyshot-cold-worker"} <= names
    assert {"clippyshot-warm-gvisor", "clippyshot-fc-worker"} <= names


def test_only_the_engine_image_is_built_from_this_repo() -> None:
    """Three of the four Dockerfiles live in BLASTBOX, and are stamped with that
    tree's revision -- ours would name a commit that does not contain the file
    which built them. Pinned so the split stays visible."""
    assert [i.name for i in LOCAL] == ["clippyshot"]
    assert {i.name for i in FOREIGN} == {
        "clippyshot-cold-worker",
        "clippyshot-warm-gvisor",
        "clippyshot-fc-worker",
    }
    assert all(i.context == "$BLASTBOX_SRC" for i in FOREIGN)


@pytest.mark.parametrize("spec", LOCAL, ids=lambda s: str(s.name))
def test_each_declared_base_arg_selects_that_dockerfiles_base(spec: ImageSpec) -> None:
    """docker discards a --build-arg the Dockerfile does not declare, so the
    build resolves its own default while the stamp claims the pinned base.

    This repo's Dockerfile hardcoded `FROM ubuntu:24.04` until now, which could
    not be pinned at all.
    """
    from blastbox.host.stamp import StampError, assert_arg_selects_base

    path = ROOT / spec.dockerfile
    assert path.is_file(), f"the plan names {spec.dockerfile}, which does not exist"
    try:
        assert_arg_selects_base(path, spec.base_arg)
    except StampError as exc:
        pytest.fail(f"blastbox-images.toml declares base_arg={spec.base_arg}: {exc}")


def test_both_dockerfile_stages_take_the_base_arg() -> None:
    """The builder's output is COPIED into the runtime, so a pin covering only
    one stage records a provenance the artifact does not have."""
    text = (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    froms = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    assert froms, "no FROM lines found; this test is asserting nothing"
    assert all(f == "${BASE_IMAGE}" for f in froms), froms


def test_the_declared_base_matches_the_dockerfiles_own_default() -> None:
    """The plan pins it; the Dockerfile defaults it. If they drift, a plain
    `docker build` and a planned build produce images on different bases while
    both look correct."""
    spec = next(i for i in PLAN.images if i.name == "clippyshot")
    declared = re.search(
        r"^ARG BASE_IMAGE=(\S+)",
        (ROOT / spec.dockerfile).read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert declared, "the Dockerfile no longer defaults ARG BASE_IMAGE"
    assert spec.base == declared.group(1)


def test_the_gvisor_shim_uses_its_own_arg_name() -> None:
    """`BASE`, not `BASE_IMAGE` -- it is the shared shim, not an engine file.

    Not a style note: docker ignores the wrong one silently, so this pins
    nothing and stamps a digest the build never used. Recorded here because a
    copy-paste from another engine's spec is the obvious way to break it.
    """
    shim = next(i for i in PLAN.images if i.name == "clippyshot-warm-gvisor")
    assert shim.base_arg == "BASE"
    assert shim.dockerfile.endswith("Dockerfile.shim")


def test_the_firecracker_rootfs_declares_what_it_must_contain() -> None:
    """The guest boots /init, which execs run_guest.py against a baked
    guest.env. A rootfs without them hangs every warm guest to the boot
    timeout -- which is how the titanarum tier went down, with nothing checking
    because nothing had written down what the artifact needed."""
    fc = [r for r in PLAN.rootfs if r.kind == "ext4"]
    assert len(fc) == 1
    assert {"/init", "/opt/blastbox/guest.env"} <= set(fc[0].requires)
    assert fc[0].resolved_size_mib({}) == 7000, "LibreOffice and tessdata"
    assert fc[0].resolved_size_mib({"ROOTFS_MIB": "8000"}) == 8000


def test_both_rootfs_artifacts_are_declared() -> None:
    assert {r.kind for r in PLAN.rootfs} == {"dir", "ext4"}


def test_the_floor_matches_what_pyproject_pins() -> None:
    """The wrapper's gate and the package's own floor must agree."""
    text = (ROOT / "scripts" / "build_images.sh").read_text(encoding="utf-8")
    floor = re.search(r"^BB_MIN=(\S+)", text, re.MULTILINE)
    assert floor, "build_images.sh no longer states a minimum the way this test reads it"
    pins = re.findall(
        r"blastbox(?:\[[^\]]*\])?>=(\d+\.\d+\.\d+)",
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert pins, "pyproject no longer pins blastbox the way this test reads it"
    assert set(pins) == {floor.group(1)}, (
        f"build_images.sh requires >= {floor.group(1)} but pyproject pins {sorted(set(pins))}"
    )
