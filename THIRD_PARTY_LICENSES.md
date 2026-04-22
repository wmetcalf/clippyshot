# Third-party software notices

ClippyShot itself is MIT-licensed (see [LICENSE](LICENSE)). It depends on
and — when distributed as a Docker image — bundles software released under
other open-source licenses. This file enumerates them so operators can
review their obligations before redistributing the image.

The ClippyShot Python code does not statically link against any of the
software listed below; it invokes binaries as separate processes (in the
case of `soffice`, `bwrap`, `nsjail`, `pdftoppm`) or imports Python packages
at runtime (in the case of the pip dependencies). Combining MIT-licensed
ClippyShot with the listed software in a single Docker image is a "mere
aggregation" — the ClippyShot source remains MIT, while each bundled
component continues to be governed by its own license.

## Bundled in the Docker image

| Component | Version (approx.) | License | Source |
|---|---|---|---|
| LibreOffice (core/writer/calc/impress/draw) | 24.2 | MPL-2.0 | https://www.libreoffice.org/ |
| bubblewrap (`bwrap`) | 0.9.x | LGPL-2.0-or-later | https://github.com/containers/bubblewrap |
| nsjail | 3.4 | Apache-2.0 | https://github.com/google/nsjail |
| poppler-utils (`pdftoppm`) | 24.x | GPL-2.0-or-later | https://poppler.freedesktop.org/ |
| tini | 0.19 | MIT | https://github.com/krallin/tini |
| libseccomp | 2.5+ | LGPL-2.1-only | https://github.com/seccomp/libseccomp |
| libnl-route-3 | 3.7+ | LGPL-2.1-or-later | https://www.infradead.org/~tgr/libnl/ |
| libprotobuf | 3.x | BSD-3-Clause | https://github.com/protocolbuffers/protobuf |
| Python 3.12 | 3.12.x | PSF-2.0 | https://www.python.org/ |
| fonts-dejavu | n/a | Bitstream Vera + Public Domain | https://dejavu-fonts.github.io/ |
| fonts-liberation | n/a | OFL-1.1 | https://github.com/liberationfonts/liberation-fonts |
| fonts-noto-core, fonts-noto-cjk | n/a | OFL-1.1 | https://fonts.google.com/noto |
| Ubuntu base image (`ubuntu:24.04`) | 24.04 | various (see /usr/share/doc inside the image) | https://ubuntu.com/legal |

The two GPL-licensed components (`poppler-utils` and any GPL-licensed pieces
inside the Ubuntu base) are invoked as standalone binaries and are not
linked into ClippyShot's address space, so ClippyShot itself remains MIT.
If you redistribute the Docker image as a binary artifact, you should
include the corresponding source for the GPL components or a written offer
to provide it (the Ubuntu apt source archive satisfies this for the
Ubuntu-packaged versions). The MPL-2.0 LibreOffice component requires that
modifications to LibreOffice itself be released under MPL-2.0; ClippyShot
does not modify LibreOffice, only invokes it.

## Python dependencies (installed via pip into the runtime venv)

| Package | License | Source |
|---|---|---|
| magika | Apache-2.0 | https://github.com/google/magika |
| ImageHash | BSD-2-Clause | https://github.com/JohannesBuchner/imagehash |
| Pillow | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| FastAPI | MIT | https://github.com/tiangolo/fastapi |
| uvicorn | BSD-3-Clause | https://github.com/encode/uvicorn |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |
| structlog | Apache-2.0 / MIT dual | https://github.com/hynek/structlog |
| prometheus-client | Apache-2.0 | https://github.com/prometheus/client_python |
| redis-py | MIT | https://github.com/redis/redis-py |
| pypdf | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| onnxruntime (transitive via magika) | MIT | https://github.com/microsoft/onnxruntime |

## Development-only dependencies (not in the production Docker image)

| Package | License | Source |
|---|---|---|
| pytest | MIT | https://github.com/pytest-dev/pytest |
| pytest-asyncio | Apache-2.0 | https://github.com/pytest-dev/pytest-asyncio |
| httpx | BSD-3-Clause | https://github.com/encode/httpx |
| fakeredis | BSD-3-Clause | https://github.com/cunla/fakeredis-py |
| ruff | MIT | https://github.com/astral-sh/ruff |
| mypy | MIT | https://github.com/python/mypy |

## Verification

To regenerate the exact installed versions and licenses for the Python
dependencies, run inside the Docker image:

```sh
docker run --rm --entrypoint /opt/clippyshot/bin/pip clippyshot:dev \
    install --quiet pip-licenses
docker run --rm --entrypoint /opt/clippyshot/bin/pip-licenses clippyshot:dev \
    --format=markdown --with-urls
```

For the apt-installed system packages, the canonical license text lives
under `/usr/share/doc/<package>/copyright` inside the running container.
