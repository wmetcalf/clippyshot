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

ROOT = Path(__file__).resolve().parents[2]

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


def test_the_rootfs_defaults_match_what_compose_mounts() -> None:
    """compose is what consumes these artifacts, so its defaults are the ones
    that matter.

    The first version of this spec took its defaults from blastbox's
    redeploy-warm.sh (`/home/coz/...`), so an unset build wrote where nothing
    reads — leaving both warm tiers on a stale rootfs while the build reported
    success. The deployed .env sets both variables, so it only bit a default
    build, which is exactly the one a newcomer runs.
    """
    compose = (ROOT / "deploy" / "docker").glob("docker-compose.*.yml")
    mounts = "\n".join(f.read_text(encoding="utf-8") for f in compose)
    for rf in PLAN.rootfs:
        default = rf.resolved_dest({})  # nothing set: the default path
        base = default.rsplit("/", 1)[0] if rf.kind == "ext4" else default[: -len("/rootfs")]
        assert base in mounts, (
            f"{rf.kind} rootfs defaults to {default!r}, which no compose file mounts"
        )


def test_the_image_installs_the_blastbox_version_it_is_stamped_with() -> None:
    """Unpinned, `pip install 'blastbox[s3]'` takes whatever is newest on PyPI
    at build time while the stamp records the pyproject floor. The two drift the
    moment a release lands, and the image then carries a provenance label that
    is simply false.

    Caught for real on toolz2 by the export's `verify_contents` check:
    `clippyshot-cold-worker: label says 0.1.34, image contains 0.1.35`.
    """
    text = (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert 'blastbox[s3]==${BLASTBOX_VERSION}' in text, (
        "the blastbox install is not pinned to the stamped version"
    )
    spec = next(i for i in PLAN.images if i.name == "clippyshot")
    assert spec.build_args.get("BLASTBOX_VERSION") == "$BLASTBOX_VERSION", (
        "the plan does not pass the version the Dockerfile pins on"
    )


def test_the_dockerfile_default_matches_the_pyproject_pin() -> None:
    """A default that drifts produces the same lie one level down: a plain
    `docker build` installs one version while the label names another."""
    m = re.search(
        r"^ARG BLASTBOX_VERSION=(\S+)",
        (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert m, "the Dockerfile no longer defaults ARG BLASTBOX_VERSION"
    pins = set(
        re.findall(
            r"blastbox(?:\[[^\]]*\])?>=(\d+\.\d+\.\d+)",
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )
    )
    assert pins == {m.group(1)}, f"Dockerfile defaults {m.group(1)}, pyproject pins {sorted(pins)}"
