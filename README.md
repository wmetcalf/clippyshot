# ClippyShot

<p align="center">
  <img src="src/clippyshot/static/assets/logo.png" alt="ClippyShot logo" width="320">
</p>

Sandboxed office-document → image rasterizer.

ClippyShot takes a Microsoft Office, OpenDocument, or text-family file and
produces a deterministic set of per-page PNGs plus a `metadata.json`
manifest. Conversion runs LibreOffice headless inside a hardened sandbox
(`nsjail` preferred, `bwrap` fallback) with macros, scripting, Java,
network egress, OLE link updates, and remote resource fetching all
disabled. PDFs are then rasterized via `pdftoppm`.

The intended use case is taking untrusted user-uploaded documents from a
web service, rendering them safely on a worker, and serving the result as
images.

🎶 [Theme song](https://suno.com/s/2PVm6rdBfu3ooG9K)

## Quick start

### Docker Compose stack (recommended, works out of the box)

Only requires Docker 20.10+ with Compose v2. Nothing to install on the
host beyond that.

```sh
export DOCKER_GID=$(stat -c %g /var/run/docker.sock)
docker compose -f deploy/docker/docker-compose.yml up --build -d
```

Web UI + API at <http://localhost:8001/>.

Validated: ~76% success on an 801-sample malware corpus, every worker
fully isolated under its own short-lived container (gVisor if installed,
`runc` + namespace/cgroup caps otherwise).

The stack brings up:
- `api` on `http://localhost:8001`
- `dispatcher` — claims jobs from Postgres and launches one worker
  container per job. Only component with Docker socket access.
- `postgres` on a private internal network (no host-exposed port)
- persistent named volumes for shared job/artifact tree and database data

Each worker is launched with `--network=none --cap-drop=ALL
--security-opt=no-new-privileges --read-only --memory=4g --pids-limit=256
--cpus=1.0`, input bind-mounted read-only, metadata.json validated by
the dispatcher before being trusted. The malware input file is deleted
from the shared volume immediately after conversion.

`CLIPPYSHOT_WARN_ON_INSECURE=1` is set on the api/dispatcher inside the
compose file because the API container is itself the sandbox boundary
in that mode. For stricter deployment, install gVisor (auto-detected)
and/or load the ClippyShot AppArmor + seccomp profiles — see
`deploy/docker/README.md#hardening`.

### Single-container Docker conversion

```sh
docker build -f deploy/docker/Dockerfile -t clippyshot:dev .
docker run --rm \
    --read-only \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,exec,nosuid,size=512m \
    --tmpfs /var/lib/clippyshot:rw,nosuid,size=64m,uid=10001,gid=10001 \
    -v "$PWD":/work \
    clippyshot:dev convert /work/input.docx -o /work/out
```

Note: on Ubuntu 24.04+ hosts you must first load the AppArmor profiles in
`deploy/apparmor/` to allow `nsjail` and `bwrap` to create user namespaces
inside the container. See `deploy/apparmor/README.md` for the procedure.

See `deploy/docker/README.md` for the full reference — environment
variables, hardening knobs (gVisor, custom seccomp JSON, custom AppArmor
profile), and a bare-metal / local nsjail-or-bwrap setup guide.

### Local development

```sh
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
.venv/bin/pytest tests/unit tests/cli tests/http
```

The unit/cli/http suite runs without LibreOffice or a working sandbox; the
integration suite (`tests/integration`) requires both and is intended to
run inside the Docker image.

To exercise the Compose stack locally:

```sh
export DOCKER_GID=$(stat -c %g /var/run/docker.sock)
docker compose -f deploy/docker/docker-compose.yml up --build
```

To stop it and remove the containers:

```sh
docker compose -f deploy/docker/docker-compose.yml down
```

## Architecture

Three sandbox backends are available: **nsjail** and **bwrap** for bare-metal
or VM hosts where AppArmor user-namespace profiles are loaded, and
**container** for Docker/OCI deployments where the container itself provides
namespace isolation, dropped capabilities, read-only rootfs, and seccomp —
making nested bwrap/nsjail redundant. The best available backend is
auto-selected at startup via a `/bin/true` smoketest before being accepted.
Override with `CLIPPYSHOT_SANDBOX=nsjail|bwrap|container`.

The conversion pipeline is composed of small, independently-testable
modules wired together by `clippyshot.converter.Converter`:

| Module | Responsibility |
|---|---|
| `clippyshot.detector` | Magika-primary content-type detection with extension fallback |
| `clippyshot.libreoffice.profile` | Hardened `UserInstallation` generator |
| `clippyshot.libreoffice.runner` | soffice argv builder + sandbox dispatch |
| `clippyshot.sandbox.{base,bwrap,nsjail,container,detect}` | Sandbox protocol + three backends + auto-selection |
| `clippyshot.rasterizer.{base,pdftoppm}` | PDF → per-page PNG via pdftoppm |
| `clippyshot.hasher` | pHash + colorhash + SHA-256 of each rendered page |
| `clippyshot.converter` | The orchestration layer |
| `clippyshot.cli` | argparse CLI: `convert`, `selftest`, `serve`, `version` |
| `clippyshot.api` | FastAPI HTTP server: sync `/v1/convert` + async `/v1/jobs` lifecycle |
| `clippyshot.jobs` | JobStore protocol with in-memory, Redis, and SQL backends |
| `clippyshot.dispatcher` | Claims queued jobs and launches one worker container per job |
| `clippyshot.worker` | One-shot worker entry point for a single mounted job directory |
| `clippyshot.runtime.docker_runtime` | Docker runtime selection and narrow worker `docker run` argv |
| `clippyshot.observability` | structlog + prometheus_client |
| `clippyshot.selftest` | Deployment health check |

The deployment split is:

- API: uploads, job status, artifact serving, no Docker socket.
- Dispatcher: claims jobs, chooses `runsc`/`runc`, launches workers, has the Docker socket.
- Worker: one job, one mounted directory, no Postgres credentials.

## Deployment modes

Five shipping shapes, trading setup effort for isolation depth:

| Mode | Outer boundary | Inner sandbox | Seccomp | AppArmor profile required | Works on |
|---|---|---|---|---|---|
| Compose + gVisor (runsc) | `runsc` per-job container | `ContainerSandbox` | `runsc` + docker-default | none | anywhere Docker + gVisor run |
| Compose + runc | `runc` per-job container | `ContainerSandbox` | docker-default | none | anywhere Docker runs |
| Single container (inner bwrap/nsjail) | `docker run` | `bwrap` or `nsjail` inside | libseccomp or KAFEL | `clippyshot-{bwrap,nsjail}` on host kernel | Linux w/ unprivileged userns |
| Host-native bwrap | — | `bwrap` | libseccomp BPF | `clippyshot-bwrap` + `clippyshot-soffice` | AppArmor distros, kernel ≥ 3.8 |
| Host-native nsjail | — | `nsjail` | KAFEL DSL | `clippyshot-nsjail` + `clippyshot-soffice` | AppArmor distros + source build |

**Pick Compose + gVisor** unless you have a specific reason not to — it has the lowest host-assumption count, the best blast-radius story (gVisor intercepts syscalls at the VM-like boundary), and works on RHEL/SUSE/etc. where AppArmor isn't a thing. Host-native bwrap/nsjail are fallbacks for bare-metal installs where running Docker isn't acceptable; nsjail specifically adds KAFEL-expressed seccomp and `--cgroup-pids` ergonomics at the cost of needing a from-source build.

Notes that matter in practice:
- On **Ubuntu 24.04+**, unprivileged user namespaces are restricted by default (`kernel.apparmor_restrict_unprivileged_userns=1`). bwrap/nsjail won't work until the shipped `deploy/apparmor/clippyshot-{bwrap,nsjail}` profiles are loaded. See `deploy/apparmor/README.md`.
- AppArmor-specific — on **RHEL/Fedora/Arch**, the `aa-exec` wrapper is a no-op (not installed), so the `clippyshot-soffice` MAC layer drops off; namespace + seccomp + caps still apply.
- **nsjail inside Docker** is difficult — AppArmor profiles attach by host-visible binary path, and the container overlay path doesn't match. Compose avoids this by using `ContainerSandbox` (no nested userns) under `runsc`.
- Seccomp policies are x86_64-only today (syscall numbers hardcoded in `deploy/seccomp/clippyshot.seccomp.policy`). arm64 would need revalidation.

## Defense in depth

Each layer is independently sufficient against most attacks; together they
compose:

1. **Magika-validated input type** — malformed-on-purpose files are rejected
   before LibreOffice sees them. PDF bytes saved as `.docx` get rejected
   with `unsupported_type`.
2. **Hardened LibreOffice profile** — macros, OfficeBasic, Java, update
   checks, and remote resources are disabled at the application layer via
   `registrymodifications.xcu` and `javasettings_Linux_X86_64.xml`.
3. **bwrap / nsjail sandbox** — namespace isolation (user, mount, PID, IPC,
   UTS, cgroup, network), no capabilities, no network, rlimits on memory,
   CPU, fsize, nofile. The sandbox is the only thing soffice sees.
4. **AppArmor profiles** (when loaded) — kernel MAC layer enforcing file,
   network, exec, and ptrace restrictions independent of the sandbox. The
   `clippyshot-soffice` profile covers both the LibreOffice run and the
   QR/OCR scanners (they execute under the same profile; the scanner PNG
   mount is read-only at `/sandbox/scan`). See `deploy/apparmor/`.
5. **Unprivileged container user** + **read-only rootfs** — even if every
   layer above were compromised, the blast radius is confined to a tmpfs
   inside an unprivileged container.

Additional input-handling hardening on the HTTP entry point:

- HTTP requests larger than `CLIPPYSHOT_MAX_INPUT` bytes are rejected with
  HTTP 413 before any body is read (Content-Length check) or as soon as the
  streaming body exceeds the limit (chunked uploads). Previously the limit
  was only enforced by the detector, after the full upload had already been
  spooled to disk.
- Client-supplied filenames are sanitized to a safe basename matching
  `[A-Za-z0-9._-]+`, truncated to 255 chars, with empty/hidden names mapped
  to `upload.bin`. Path traversal via filename is not possible.
- Files that Magika labels as `zip` or `xml` (generic container labels) are
  structurally sanity-checked before the extension-fallback path trusts
  them. Zip-bombs (compression ratio > 100:1, > 5000 entries, or missing
  `[Content_Types].xml`) and billion-laughs XML (more than 64 entity
  declarations) are rejected at the detector.
- The HTTP API honours all `CLIPPYSHOT_*` env-var overrides via
  `Limits.from_env()` on both `/v1/convert` and `/v1/jobs`, matching the
  CLI behaviour.

## Configuration

All limits are set via `clippyshot.limits.Limits.from_env()`. The env var
names use the prefix `CLIPPYSHOT_` with the suffix shown below:

| Env var | Default | Effect |
|---|---|---|
| `CLIPPYSHOT_SANDBOX` | _(auto)_ | Force `nsjail`, `bwrap`, or `container`; fail loudly if unavailable |
| `CLIPPYSHOT_TIMEOUT` | `60` | Per-conversion soffice timeout (seconds) |
| `CLIPPYSHOT_MAX_PAGES` | `50` | Page count cap (truncates beyond this) |
| `CLIPPYSHOT_DPI` | `150` | Rasterization DPI |
| `CLIPPYSHOT_MAX_INPUT` | `104857600` | Max accepted upload size (100 MiB) |
| `CLIPPYSHOT_MEM` | `1073741824` | Per-conversion RSS cap (1 GiB) |
| `CLIPPYSHOT_TMPFS` | `536870912` | Per-conversion tmpfs cap (512 MiB) |
| `CLIPPYSHOT_DATABASE_URL` | `sqlite:///./clippyshot-jobs.db` | SQL job metadata backend; use `postgresql://...` in Compose/prod |

## Supported formats

ClippyShot accepts any document format LibreOffice can render. The detector
classifies inputs by content (via Magika) and falls back to the extension
allowlist below when Magika returns a generic container label.

**Microsoft Office (OOXML):** `.docx`, `.docm`, `.dotx`, `.dotm`,
`.xlsx`, `.xlsm`, `.xltx`, `.xltm`, `.xlsb`,
`.pptx`, `.pptm`, `.ppsx`, `.ppsm`, `.potx`, `.potm`

**Microsoft Office (legacy):** `.doc`, `.dot`, `.xls`, `.xlt`, `.ppt`, `.pps`, `.pot`

**OpenDocument:** `.odt`, `.ott`, `.fodt`, `.ods`, `.ots`, `.fods`,
`.odp`, `.otp`, `.fodp`, `.odg`, `.otg`, `.fodg`

**Text / markup:** `.rtf`, `.txt`, `.csv`, `.md`

**Microsoft XPS:** `.xps`, `.oxps`

Macro-enabled formats (`.docm`, `.xlsm`, `.pptm`, `.dotm`, `.xltm`, `.ppsm`,
`.potm`, `.xlsb`) are accepted: ClippyShot's hardened LibreOffice profile
prevents macros from running (`MacroSecurityLevel=4`,
`DisableMacrosExecution=true`), and a `macro_enabled_format` warning is
recorded in `metadata.warnings` so downstream consumers can apply their own
audit policy.

## QR / OCR scanners

Rendered pages can be scanned for QR codes (via `zxing-cpp`) and OCR'd
(via `tesseract`) as part of the pipeline. Output ends up under
`pages[].qr` and `pages[].ocr` in `metadata.json` and is always present
(possibly empty) so downstream consumers can rely on a stable shape.

**Defaults:** QR scanning is **on**; OCR is **off**. QR is cheap enough
(median ~300ms/page in our test corpus) to justify running by default on
untrusted content. OCR is opt-in because it is the single most expensive
stage when enabled.

**Enable per-request** via `POST /v1/jobs` form params (or the UI
checkboxes):

```sh
curl -F "file=@doc.pdf" -F "qr=1" -F "ocr=1" http://localhost:8001/v1/jobs
```

**Or globally** via dispatcher env vars in `docker-compose.yml`
(`CLIPPYSHOT_ENABLE_QR`, `CLIPPYSHOT_ENABLE_OCR`, `CLIPPYSHOT_OCR_ALL`,
`CLIPPYSHOT_OCR_LANG`, `CLIPPYSHOT_OCR_PSM`, `CLIPPYSHOT_OCR_TIMEOUT_S`).

### Image-gating

`ocr=1` defaults to **"only OCR pages where OCR would add signal"** —
specifically any page that (a) carries raster images, (b) contains
vector drawings, charts, stamps, or other non-text graphics, or (c)
has an empty PDF text layer (scanned PDFs). The threat-analysis use
case needs OCR anywhere there's visual content beyond pure text,
because malicious payloads frequently live in diagrams, QR-like
shapes, or overlay drawings the PDF text layer can't see.

Pure-text pages with a populated text layer and zero drawings are
skipped with `ocr.skipped="no_images"` because running tesseract on
them would just duplicate the existing text.

Set `ocr_all=1` to override the gating entirely and OCR every non-blank
page, regardless of signal. `render.image_page_count` and
`render.total_image_count` in the output help you decide which mode to
pick.

### Budget semantics

`ocr_timeout_s` (default 60s) is a **total per-job wall-clock budget**,
not a per-page timeout. Once exhausted, remaining pages are marked
`ocr.skipped="timeout_budget"` and the job still completes successfully.
A per-call floor of 30s ensures tesseract can always fail cleanly even
when the budget is nearly exhausted.

### Failure policy

Scanner failures are **never fatal**. A tesseract or ZXing crash produces
`ocr.skipped="error"` or `qr_skipped="error"` plus a warning in
`metadata.warnings[]` with code `ocr_scan_error` / `qr_scan_error`. The
conversion pipeline continues and the job finishes normally.

### Prerequisites

The Docker image bundles `tesseract-ocr`, `tesseract-ocr-eng`, and
`zxing-cpp-tools`. For host-native (bwrap/nsjail) deployments install:

```sh
sudo apt install tesseract-ocr tesseract-ocr-eng zxing-cpp-tools
```

Both binaries must be reachable at `/usr/bin/<name>` — user-local
installs (`~/.local/bin/`) won't work because the sandboxes only
bind-mount `/usr`. If you want OCR in other languages, install the
corresponding `tesseract-ocr-<lang>` package.

## Project layout

```
src/clippyshot/        # library + CLI + API
tests/
  unit/                # pure unit tests, no soffice or sandbox required
  cli/                 # CLI subprocess tests
  http/                # FastAPI TestClient tests
  integration/         # full pipeline; requires soffice + working sandbox
  docker/              # exercises the built Docker image
  fixtures/safe/       # safe, hand-built input fixtures
  fixtures/malicious/  # safety probes (no exploits, just feature exercises)
deploy/
  docker/              # Dockerfile + .dockerignore
  apparmor/            # AppArmor profiles + load instructions
docs/superpowers/      # design spec, plan, brainstorm notes
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | input rejected (unsupported type, extension/content mismatch, too large) |
| 3 | sandbox unavailable, conversion failed, or LO/rasterize error |
| 4 | internal error |

## Metrics

The HTTP server exposes Prometheus metrics on `/metrics`:

- `clippyshot_conversions_total{outcome,format}` — counter
- `clippyshot_conversion_duration_seconds{stage}` — histogram
- `clippyshot_sandbox_backend{backend}` — gauge
- `clippyshot_jobs_in_flight` — gauge
- `clippyshot_input_bytes` — histogram
- `clippyshot_rejections_total{reason}` — counter

## License

ClippyShot is MIT-licensed — see [LICENSE](LICENSE).

The Docker image bundles LibreOffice (MPL-2.0), bubblewrap (LGPL-2.0),
nsjail (Apache-2.0), poppler-utils (GPL-2.0), and other open-source
components. Each is invoked as a separate process and not linked into
ClippyShot itself, so ClippyShot's source remains MIT. See
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the full list with
upstream sources and notes on redistribution obligations.
