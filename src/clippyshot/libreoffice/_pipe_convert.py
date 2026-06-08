"""python-uno pipe converter — runs UNDER the LibreOffice-bundled interpreter.

This is **not** imported by ClippyShot. It is executed as a standalone script by
``uno_pipe.convert_via_pipe`` via the ``pyuno``-capable interpreter (the one that
ships LibreOffice's ``uno`` module — usually ``/usr/bin/python3`` with the distro
``python3-uno`` package; the ClippyShot venv interpreter does NOT have ``uno``).

It connects to a warm ``soffice --accept=pipe,name=<pipe>`` over a Unix-domain
**pipe** (no loopback / no TCP — works in a bare ``runsc`` bundle with
``--network=none``), loads one document, and exports a single PDF with the exact
export filter + filter-data the cold ``soffice --convert-to`` path uses, so the
output is byte-identical.

Usage:
    <pyuno-python> _pipe_convert.py <pipe_name> <in_url> <out_url> <filter_name> [<filter_data_json>]
    <pyuno-python> _pipe_convert.py --probe <pipe_name>     # readiness check: exit 0 iff connectable

``filter_data_json`` is an optional JSON object of FilterData properties (e.g.
``{"SinglePageSheets": true}`` for the Calc one-page-per-sheet parity). Exit code
0 on success; non-zero with a diagnostic on stderr otherwise — the caller treats
any non-zero as "fall back to the cold path".
"""
import json
import os
import sys
import time

# This file lives in clippyshot/libreoffice/, which ALSO contains ``uno.py`` (ClippyShot's
# UNO helper). Running ``python3 <this>`` prepends that directory to ``sys.path[0]``, so a
# bare ``import uno`` would resolve to ClippyShot's ``uno.py`` (which imports
# ``clippyshot.errors`` → ModuleNotFoundError under the LibreOffice interpreter) and SHADOW
# LibreOffice's real ``uno`` module. Drop the script's own dir so ``import uno`` finds pyuno.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

import uno  # noqa: E402  # type: ignore[import-not-found]  # the LibreOffice interpreter's pyuno
from com.sun.star.beans import PropertyValue  # noqa: E402  # type: ignore[import-not-found]
from com.sun.star.connection import NoConnectException  # noqa: E402  # type: ignore[import-not-found]

_CONNECT_ATTEMPTS = 120  # 60 s at 0.5 s — covers a cold soffice boot AND a post-restore settle
_CONNECT_SLEEP_S = 0.5


def _resolve(pipe_name: str):
    """Resolve a UNO context over the pipe, retrying NoConnectException.

    The acceptor may not be listening yet (cold boot) or may be re-establishing
    (post gVisor checkpoint/restore — the accept-retry LD_PRELOAD shim keeps the
    acceptor alive through the restore-time EINTR; the client just retries connect)."""
    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )
    url = f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext"
    last_exc: Exception | None = None
    for _ in range(_CONNECT_ATTEMPTS):
        try:
            return resolver.resolve(url)
        except NoConnectException as exc:  # acceptor not ready yet
            last_exc = exc
            time.sleep(_CONNECT_SLEEP_S)
    raise SystemExit(f"pipe-convert: never connected to soffice pipe {pipe_name!r} ({last_exc})")


def _prop(name: str, value: object) -> PropertyValue:
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def _probe(pipe_name: str) -> int:
    """Single non-retrying resolve — exit 0 iff the soffice pipe acceptor is listening."""
    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )
    try:
        resolver.resolve(f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext")
        return 0
    except NoConnectException:
        return 1


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--probe":
        return _probe(argv[2])
    if not (5 <= len(argv) <= 6):
        sys.stderr.write(
            "usage: _pipe_convert.py <pipe> <in_url> <out_url> <filter> [<filter_data_json>]\n"
        )
        return 2
    pipe_name, in_url, out_url, filter_name = argv[1], argv[2], argv[3], argv[4]
    filter_data = json.loads(argv[5]) if len(argv) == 6 and argv[5] else {}

    ctx = _resolve(pipe_name)
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    # Load hidden + read-only: the input is untrusted and we never write it back.
    load_props = (_prop("Hidden", True), _prop("ReadOnly", True))
    doc = desktop.loadComponentFromURL(in_url, "_blank", 0, load_props)
    if doc is None:
        sys.stderr.write("pipe-convert: loadComponentFromURL returned None\n")
        return 3
    try:
        store_props = [_prop("FilterName", filter_name), _prop("Overwrite", True)]
        if filter_data:
            store_props.append(
                _prop("FilterData", uno.Any(
                    "[]com.sun.star.beans.PropertyValue",
                    tuple(_prop(k, v) for k, v in filter_data.items()),
                ))
            )
        doc.storeToURL(out_url, tuple(store_props))
    finally:
        doc.close(False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
