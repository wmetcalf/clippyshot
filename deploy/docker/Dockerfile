# syntax=docker/dockerfile:1.7

# The base, as an ARG so a build can be PINNED to a digest and stamped with it.
# Declared before the first FROM, which is the only place an ARG can
# parameterize one. Both stages take it: they are the same image, and the
# builder's output is copied into the runtime, so a pin that covered only one
# of them would record a provenance the artifact does not have.
ARG BASE_IMAGE=ubuntu:24.04

#####################
# Stage 1 — builder #
#####################
FROM ${BASE_IMAGE} AS builder
ARG NSJAIL_REF=3.4

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        autoconf \
        bison \
        ca-certificates \
        curl \
        flex \
        g++ \
        gcc \
        git \
        libnl-route-3-dev \
        libprotobuf-dev \
        libtool \
        make \
        pkg-config \
        protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${NSJAIL_REF} https://github.com/google/nsjail.git /src/nsjail \
    && cd /src/nsjail \
    && make \
    && install -D nsjail /out/nsjail

# nono — the optional inner Landlock wrapper (clippyshot.sandbox.nono_wrap). Pinned +
# sha256-verified release. Used opt-in via CLIPPYSHOT_INNER_NONO on tiers whose kernel
# exposes Landlock (runc cold + the Firecracker guest); the gVisor Sentry returns ENOSYS,
# so it is NOT enabled there (select_sandbox fails fast if it is). ~0.61.1 is the version
# the perf-reversal + enforcement were validated on; bump NONO_REF/NONO_SHA256 together.
ARG NONO_REF=0.61.1
ARG NONO_SHA256=9186323e1deda74bda886ab57ae3cff820bc85861a9dd691b898a4d309bffa93
RUN curl -fsSL -o /tmp/nono.tgz \
        "https://github.com/always-further/nono/releases/download/v${NONO_REF}/nono-v${NONO_REF}-x86_64-unknown-linux-gnu.tar.gz" \
    && echo "${NONO_SHA256}  /tmp/nono.tgz" | sha256sum -c - \
    && tar xzf /tmp/nono.tgz -C /tmp \
    && install -D /tmp/nono /out/nono \
    && rm -f /tmp/nono.tgz


#####################
# Stage 2 — runtime #
#####################
FROM ${BASE_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    BLASTBOX_DATABASE_URL=sqlite:////var/lib/clippyshot/clippyshot-jobs.db \
    BLASTBOX_JOB_ROOT=/var/lib/clippyshot/jobs

# LibreOffice 25.8 via the official LibreOffice PPA. Ubuntu 24.04 LTS ships
# LO 24.2.x in its package archive and stays on that major version for the
# LTS lifetime; the PPA tracks the Fresh release branch (currently 25.8.x)
# which has newer parser fixes and matches what most LO users run locally.
# software-properties-common is needed for add-apt-repository; we remove it
# after the PPA is added to keep the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        gnupg \
        software-properties-common \
    && add-apt-repository -y ppa:libreoffice/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        apparmor-utils \
        bubblewrap \
        fonts-dejavu \
        fonts-liberation \
        fonts-linuxlibertine \
        fonts-noto-cjk \
        fonts-noto-core \
        fonts-noto-ui-core \
        fonts-noto-mono \
        fonts-noto-extra \
        fonts-noto-ui-extra \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        libreoffice-calc \
        libreoffice-core \
        libreoffice-draw \
        libreoffice-impress \
        libreoffice-writer \
        libnl-route-3-200 \
        libprotobuf32t64 \
        libseccomp2 \
        libx11-6 \
        libxext6 \
        libxrender1 \
        libxt6 \
        libdbus-1-3 \
        libxinerama1 \
        libxtst6 \
        libxi6 \
        libxcursor1 \
        dbus-x11 \
        libfontconfig1 \
        libfreetype6 \
        libmagic1 \
        poppler-utils \
        docker.io \
        python3 \
        python3-pip \
        python3-seccomp \
        python3-venv \
        tesseract-ocr \
        tesseract-ocr-all \
        tini \
        zxing-cpp-tools \
    && apt-get purge -y software-properties-common gnupg \
    && apt-get autoremove -y --purge \
    && (apt-get purge -y default-jre-headless 'openjdk-*' libreoffice-java-common 2>/dev/null || true) \
    && rm -rf /var/lib/apt/lists/*

# Warm-OCR tier: point tesserocr (bundled tesseract 5.5.1) at the system tessdata
# installed by tesseract-ocr-all (eng + Latin + osd). The `tesseract` CLI fallback
# uses its own compiled-in default; this only affects the warm-OCR helper subprocess.
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

COPY --from=builder /out/nsjail /usr/local/bin/nsjail
# Optional inner Landlock wrapper (opt-in via CLIPPYSHOT_INNER_NONO); resolved by
# clippyshot.sandbox.nono_wrap on PATH. No-op unless explicitly enabled.
COPY --from=builder /out/nono /usr/local/bin/nono

# Deploy the seccomp policy to a stable path that the nsjail backend
# looks up at runtime (src/clippyshot/sandbox/nsjail.py:_SECCOMP_POLICY_CANDIDATES).
COPY deploy/seccomp/clippyshot.seccomp.policy /etc/clippyshot/seccomp.policy
# Docker-format seccomp profile for the compose worker container. The
# dispatcher passes this via `--security-opt=seccomp=<path>` when launching
# workers, replacing Docker's default profile with our tighter allowlist.
COPY deploy/seccomp/clippyshot.seccomp.json /etc/clippyshot/seccomp.json

# Create unprivileged user for the runtime.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin clippy \
    && mkdir -p /var/lib/clippyshot \
    && chown clippy:clippy /var/lib/clippyshot

# Install ClippyShot + the host extra into a venv at /opt/clippyshot. The
# `clippyshot[host]` extra pulls `blastbox[host]` (the detonation framework's
# ingress + dispatcher) from PyPI, putting the `blastbox` console script
# (serve/dispatch) + fastapi/uvicorn/psycopg on PATH. The cold WORKER image
# (Dockerfile.clippyshot-cold-worker, blastbox repo) needs only core, not [host].
WORKDIR /tmp/build
COPY pyproject.toml /tmp/build/pyproject.toml
COPY src /tmp/build/src
# The blastbox version this image INSTALLS. Without it, `clippyshot[host]`
# resolves the >= floor to whatever is newest on PyPI at build time, so the
# provenance label names one version while the venv holds another -- the
# label/contents disagreement `blastbox doctor` exists to catch. Caught for
# real on toolz2: the stamp said 0.1.34 and the image contained 0.1.35.
# Keep the default in sync with the floor in pyproject.toml.
ARG BLASTBOX_VERSION=0.1.35
RUN python3 -m venv /opt/clippyshot \
    && /opt/clippyshot/bin/pip install --upgrade pip \
    && /opt/clippyshot/bin/pip install '/tmp/build[host]' "blastbox[s3]==${BLASTBOX_VERSION}" \
    && rm -rf /tmp/build

ENV PATH=/opt/clippyshot/bin:$PATH

USER clippy
WORKDIR /var/lib/clippyshot
VOLUME ["/var/lib/clippyshot"]

# Image-default healthcheck = the pipeline self-test (engine health). The
# compose `api` service overrides this with an HTTP probe of /v1/healthz.
HEALTHCHECK --interval=30s --timeout=15s --start-period=20s --retries=3 \
    CMD ["clippyshot", "selftest"]

# Default to the blastbox ingress; compose overrides `command` per service
# (api → blastbox serve, dispatcher → blastbox dispatch). The CLI `clippyshot`
# (convert/selftest/version) is still on PATH for one-shot in-process use.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["blastbox", "serve", "--host", "0.0.0.0", "--port", "8000", "--allowed-engines", "clippyshot"]
