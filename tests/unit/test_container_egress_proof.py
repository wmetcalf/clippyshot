"""Egress is provable in exactly one direction: no interface, no egress.

`_runtime_hardening_reasons()` appended `network_egress_not_verified`
unconditionally, which made the container backend unselectable no matter how
locked down the container was. A worker launched with blastbox's own flags
(`--cap-drop=ALL --security-opt=no-new-privileges --read-only --network=none`)
was then refused by every backend and could not run at all -- measured on toolz2
against the deployed image, and filed as #56.

The fixture text is real `/proc/net/dev` output, captured on that node:

    docker run --network=none  ->  lo only,    0 IPv4 routes
    docker run (bridge)        ->  lo + eth0,  2 IPv4 routes
"""
from __future__ import annotations

from pathlib import Path

import pytest

from clippyshot.sandbox import container as C


_NETWORK_NONE = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets
    lo:       0       0    0    0    0     0          0         0        0       0
"""

_BRIDGED = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets
    lo:       0       0    0    0    0     0          0         0        0       0
  eth0:    1234      12    0    0    0     0          0         0      678       9
"""


def _with_proc_net_dev(monkeypatch, tmp_path: Path, text: str | None):
    """Point the check at a fixture, or at a path that does not exist."""
    target = tmp_path / "dev"
    if text is not None:
        target.write_text(text)
    real_path = C.Path

    class _Path(real_path):  # type: ignore[misc,valid-type]
        def __new__(cls, *args, **kw):
            if args and str(args[0]) == "/proc/net/dev":
                return real_path(target)
            return real_path(*args, **kw)

    monkeypatch.setattr(C, "Path", _Path)


def test_no_interface_but_loopback_proves_egress_is_blocked(monkeypatch, tmp_path):
    _with_proc_net_dev(monkeypatch, tmp_path, _NETWORK_NONE)
    assert C._egress_provably_blocked() is True
    assert "network_egress_not_verified" not in C._runtime_hardening_reasons()


def test_a_routable_interface_leaves_it_unverified(monkeypatch, tmp_path):
    """The positive control: with eth0 present the claim is not provable, so the
    reason stays and the backend still requires the explicit opt-in."""
    _with_proc_net_dev(monkeypatch, tmp_path, _BRIDGED)
    assert C._egress_provably_blocked() is False
    assert "network_egress_not_verified" in C._runtime_hardening_reasons()


@pytest.mark.parametrize(
    "text",
    [None, "", "Inter-|   Receive\n face |bytes\n", "garbage without a colon\n"],
    ids=["missing", "empty", "headers-only", "unparseable"],
)
def test_every_uncertainty_stays_insecure(monkeypatch, tmp_path, text):
    """Unreadable, empty or unparseable /proc must not read as 'no interfaces'."""
    _with_proc_net_dev(monkeypatch, tmp_path, text)
    assert C._egress_provably_blocked() is False
    assert "network_egress_not_verified" in C._runtime_hardening_reasons()
