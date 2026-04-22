# syntax=docker/dockerfile:1.7

#####################
# Stage 1 — builder #
#####################
FROM ubuntu:24.04 AS builder
ARG NSJAIL_REF=3.4

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        autoconf \
        bison \
        ca-certificates \
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


#####################
# Stage 2 — runtime #
#####################
FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    CLIPPYSHOT_DATABASE_URL=sqlite:////var/lib/clippyshot/clippyshot-jobs.db \
    CLIPPYSHOT_JOB_ROOT=/var/lib/clippyshot/jobs

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

COPY --from=builder /out/nsjail /usr/local/bin/nsjail

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

# Install ClippyShot into a venv at /opt/clippyshot.
WORKDIR /tmp/build
COPY pyproject.toml /tmp/build/pyproject.toml
COPY src /tmp/build/src
RUN python3 -m venv /opt/clippyshot \
    && /opt/clippyshot/bin/pip install --upgrade pip \
    && /opt/clippyshot/bin/pip install /tmp/build \
    && rm -rf /tmp/build

ENV PATH=/opt/clippyshot/bin:$PATH

USER clippy
WORKDIR /var/lib/clippyshot
VOLUME ["/var/lib/clippyshot"]

HEALTHCHECK --interval=30s --timeout=15s --start-period=20s --retries=3 \
    CMD ["clippyshot", "selftest"]

ENTRYPOINT ["/usr/bin/tini", "--", "clippyshot"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
