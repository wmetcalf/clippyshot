# ClippyShot — Docker Deployment

Two deployment topologies ship in this repo:

1. **Compose stack** (`docker-compose.yml`) — API + Postgres + a dispatcher
   that launches one ephemeral worker container per job. This is the
   recommended production shape; every document is rendered in its own
   disposable sandboxed container.
2. **Single-container** — one container runs the API and renders documents
   in-process via the `ContainerSandbox` backend. Simpler to operate,
   usable for evaluation, but gives up the "one document, one container"
   isolation property.

This document covers both.

---

## Compose stack (recommended)

### Out-of-box quick start

**Requires Docker 20.10+ with Compose v2** and nothing else. Run:

```sh
export DOCKER_GID=$(stat -c %g /var/run/docker.sock)
docker compose -f deploy/docker/docker-compose.yml up --build -d
```

That's it. API at <http://localhost:8001/>, web UI at the same URL,
metrics at <http://localhost:8001/metrics>. Workers run with Docker's
default seccomp and (if present) default AppArmor; see §Hardening below
for the opt-in stricter-than-default profiles.

The `DOCKER_GID` export lets the unprivileged dispatcher container talk
to the host Docker socket without running as root.

### Command reference

This brings up:

| Service | Role | Ports |
|---|---|---|
| `api` | Uploads, job status, artifact serving. No Docker socket. | `localhost:8001 → 8000` |
| `dispatcher` | Claims queued jobs, launches one worker container per job. Only component with Docker socket access. | internal |
| `postgres` | Job store. | private network only |

The stack survives restarts; named volumes (`clippyshot-data`,
`postgres-data`) persist job artifacts and the DB across `docker compose
down` / `up`. Use `docker compose down -v` to wipe state.

### Access

- Web UI + API: <http://localhost:8001/>
- Metrics: <http://localhost:8001/metrics>
- Health: <http://localhost:8001/v1/readyz>

> **Note:** the stack is unauthenticated out of the box — bind it to
> `127.0.0.1` or place behind an auth proxy for anything beyond a local
> lab. See §4 Hardening below for opt-in bearer-token auth.

### Stop / teardown

```sh
docker compose -f deploy/docker/docker-compose.yml down          # keep data
docker compose -f deploy/docker/docker-compose.yml down -v       # wipe volumes
```

---

## Environment variables

Common tuning — set in the compose file or shell env.

| Var | Default | Notes |
|---|---|---|
| `CLIPPYSHOT_PORT` | `8001` | Host port the API binds to |
| `CLIPPYSHOT_DATABASE_URL` | (postgres URI from compose) | `postgresql://…` for prod, `sqlite:///…` for dev |
| `CLIPPYSHOT_JOB_RETENTION_SECONDS` | `0` (permanent) | Positive value = TTL in seconds, measured from finish time. `0` = jobs never auto-expire. |
| `CLIPPYSHOT_MAX_INPUT` | `104857600` | Max upload size in bytes (100 MiB) |
| `CLIPPYSHOT_TIMEOUT` | `60` | Per-conversion soffice timeout (seconds) |
| `CLIPPYSHOT_MAX_PAGES` | `50` | Page cap per document |
| `CLIPPYSHOT_DPI` | `150` | Rasterization DPI |
| `CLIPPYSHOT_API_KEY` | unset | If set, bearer-token auth required on `/v1/*` and `/metrics`. Only `/v1/healthz` and `/v1/version` stay public — static UI assets (`/`, `/assets/*`) also require auth, so browser use needs an auth-injecting proxy. |
| `CLIPPYSHOT_IMAGE` | `clippyshot:dev` | Image tag used for the api and dispatcher services. |
| `CLIPPYSHOT_WORKER_IMAGE` | `clippyshot:dev` | Image tag the dispatcher uses for worker containers. Usually the same as `CLIPPYSHOT_IMAGE`. |
| `CLIPPYSHOT_JOB_ROOT` | `/var/lib/clippyshot/jobs` | Where uploaded inputs + rendered output dirs live. Must be a writable volume. |
| `CLIPPYSHOT_HOST_STORAGE_ROOT` | auto-detected | Host path that corresponds to `/var/lib/clippyshot` inside the dispatcher; the dispatcher auto-discovers this via `docker inspect` of its own container, but you can pin it explicitly. |

Per-worker container caps (applied by the dispatcher when launching
workers; NOT read by the worker itself):

| Var | Default | Notes |
|---|---|---|
| `CLIPPYSHOT_WORKER_MEMORY` | `4g` | Cgroup RSS cap. Drop below 2g at your own risk — wide spreadsheet rasters need headroom. |
| `CLIPPYSHOT_WORKER_PIDS_LIMIT` | `256` | PID cap (soffice forks a lot). |
| `CLIPPYSHOT_WORKER_CPUS` | `1.0` | CPU quota (1.0 = one core). |
| `CLIPPYSHOT_WORKER_NOFILE` | `4096` | Open-file rlimit. |
| `CLIPPYSHOT_SECCOMP_JSON_HOST` | unset | **Host filesystem path** to the ClippyShot seccomp JSON (see §Hardening). Without this, docker-default seccomp applies. |
| `CLIPPYSHOT_APPARMOR_PROFILES` | unset | Comma-separated list of AppArmor profile names that are loaded on the host. Required in the compose topology because the dispatcher can't read `/sys/kernel/security/` from inside its own container. Typical value: `clippyshot-soffice`. |
| `CLIPPYSHOT_WARN_ON_INSECURE` | `0` | When `1`, ContainerSandbox activates even when hardening checks fail. Auto-set to `1` by the dispatcher for `runsc` runtimes (gVisor's own isolation is not reflected in `/proc/self/status`). |
| `CLIPPYSHOT_DISPATCH_CONCURRENCY` | `4` | Max parallel worker containers. Consumed by the compose dispatcher bootstrap; see `docker-compose.yml` command block. |
| `CLIPPYSHOT_DISPATCH_INTERVAL` | `5` | Poll interval (seconds) between dispatch ticks. Same consumer as above. |

---

## API reference

Base URL: `http://localhost:8001` (compose) or `http://localhost:8000`
(single-container). All responses are JSON except the binary artifact
downloads (PNG / zip).

### Health / discovery

```sh
GET  /v1/healthz                   # liveness — returns {status:"ok"}
GET  /v1/readyz                    # readiness — checks sandbox, returns {status:"ready"}
GET  /v1/version                   # {version, sandbox, git_sha}
GET  /metrics                      # Prometheus exposition format
```

### Sync convert (one request = one rendered zip)

```sh
# Convert a single file synchronously; response body is a zip containing
# metadata.json + page-NNN.png (+ -trimmed/-focused derivatives).
curl -o result.zip \
     -F 'file=@document.docx' \
     http://localhost:8001/v1/convert
unzip -l result.zip
```

Response codes:
- `200` — success, body is the zip
- `400` — detection rejected the input (unsupported type, bomb, etc.)
- `422` — LibreOffice could not render the input
- `413` — upload exceeds `CLIPPYSHOT_MAX_INPUT` (default 100 MiB)
- `503/504` — sandbox unavailable or conversion timed out

### Async jobs (preferred for batch / UI)

```sh
# Submit — returns 202 with a job_id
curl -X POST -F 'file=@document.docx' http://localhost:8001/v1/jobs
# {"job_id":"abc123…", "status":"queued", "links":{…}}

# Poll status
curl http://localhost:8001/v1/jobs/abc123…
# {"status":"running"|"done"|"failed", "pages_done", "pages_total",
#  "worker_runtime", "detected":{…}, "security_warnings":[…], …}

# List / paginate / sort / filter / search
curl 'http://localhost:8001/v1/jobs?offset=0&limit=50&status=done&sort=finished_at&order=desc&q=invoice'

# Retrieve artifacts
curl http://localhost:8001/v1/jobs/abc123…/metadata         # JSON metadata
curl http://localhost:8001/v1/jobs/abc123…/pages/1.png       # raw PNG of page 1
curl http://localhost:8001/v1/jobs/abc123…/pages/trimmed/1.png
curl http://localhost:8001/v1/jobs/abc123…/pages/focused/1.png
curl -o result.zip http://localhost:8001/v1/jobs/abc123…/result  # full zip
```

### Result zip

The zip is **always password-protected** with the standard
malware-handling password `infected` (AES-256 via pyzipper). This is a
signaling convention to prevent accidental execution of extracted
artifacts — not a secrecy mechanism. Override the password globally
with `CLIPPYSHOT_ZIP_PASSWORD`.

Automation pipelines that want the rendered PDF without the zip
wrapper use `GET /v1/jobs/{id}/pdf` (unencrypted).

```sh
# Delete
curl -X DELETE http://localhost:8001/v1/jobs/abc123…
```

### `GET /v1/jobs/{id}/pdf`

Stream the rendered `document.pdf` for a completed job. Useful for
downstream tools that want the rendered PDF without the rest of the
zip. Response: `application/pdf`.

Returns 404 for unknown jobs, 409 if the job isn't DONE, 410 if the
PDF has been swept out by retention.

`GET /v1/jobs` query parameters:

| Param | Values | Default | Notes |
|---|---|---|---|
| `offset` | int | `0` | |
| `limit` | int | `100` | |
| `status` | `queued`/`running`/`done`/`failed`/`expired` | _(all)_ | |
| `sort` | `created_at`/`finished_at`/`filename`/`ext`/`status`/`pages` | `created_at` | |
| `order` | `asc`/`desc` | `desc` | |
| `q` | string | _(none)_ | Case-insensitive filename substring match |

`POST /v1/convert` and `POST /v1/jobs` optional query parameters:

| Param | Values | Default | Notes |
|---|---|---|---|
| `qr` | bool-ish | `1` | Enable QR scanning (env: `CLIPPYSHOT_ENABLE_QR`) |
| `qr_formats` | string | `qr_code,micro_qr_code,rmqr_code` | zxing format filter |
| `ocr` | bool-ish | `0` | Enable OCR. When on, only pages with embedded raster images are OCR'd (PDF text-layer pages are skipped with `ocr.skipped="no_images"`). Set `ocr_all=1` to override (env: `CLIPPYSHOT_ENABLE_OCR`) |
| `ocr_all` | bool-ish | `0` | OCR every non-blank page regardless of image presence. Useful for scanned PDFs where the text layer is empty (env: `CLIPPYSHOT_OCR_ALL`) |
| `ocr_lang` | string | `eng` | Passed to tesseract `-l` (env: `CLIPPYSHOT_OCR_LANG`) |
| `ocr_psm` | int 0-13 | `6` | Passed to tesseract `--psm` |
| `ocr_timeout_s` | int 1-600 | `60` | Total per-job OCR wall-clock budget. Once exhausted, remaining pages are marked `ocr.skipped="timeout_budget"`. Per-call floor is 30s so no single page can wedge indefinitely (env: `CLIPPYSHOT_OCR_TIMEOUT_S`) |

### JSON schema

#### Job record (`GET /v1/jobs`, `GET /v1/jobs/{id}`)

```json
{
  "job_id": "afaab8ecd1924e9e8ac89c68a930a9e6",
  "filename": "sample.docx",
  "status": "queued | running | done | failed | expired",
  "created_at": 1776689423.98,
  "started_at": 1776690944.57,
  "finished_at": 1776690951.11,
  "pages_done": 3,
  "pages_total": 3,
  "error": null,
  "worker_runtime": "runsc",
  "security_warnings": [],
  "detected": {
    "label": "docx",
    "source": "magika",
    "confidence": 0.999,
    "magika_label": "docx",
    "magika_mime": "application/…",
    "libmagic_mime": "application/…",
    "extension_hint": "doc",
    "warnings": []
  },
  "expires_at": null
}
```

| Field | Type | Notes |
|---|---|---|
| `job_id` | uuid-hex | Dispatch identifier (UUID4 without dashes) |
| `filename` | string | Post-sanitization safe basename |
| `status` | enum | One of five states above |
| `created_at/started_at/finished_at` | float | Unix epoch seconds; null until set |
| `pages_done/pages_total` | int | Populated after completion. Re-derived by the dispatcher from the output dir, not trusted from the worker |
| `error` | string \| null | Short failure reason if `status=failed`. Internal paths stripped |
| `worker_runtime` | string \| null | `runsc` (gVisor) or `runc` (fallback) |
| `security_warnings` | string[] | e.g. `["runsc unavailable; falling back to runc"]` |
| `detected` | object \| null | Detection-stage result; set even for rejected uploads |
| `expires_at` | float \| null | Unix epoch when retention will sweep this job. `null` = permanent |

#### metadata.json (`GET /v1/jobs/{id}/metadata` and inside the result zip)

```json
{
  "clippyshot_version": "0.1.0",
  "input": {
    "filename": "sample.docx",
    "size_bytes": 119136,
    "sha256": "sha256 of the upload",
    "detected": {
      "source": "magika",
      "label": "docx",
      "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "confidence": 0.9999,
      "extension_hint": "doc",
      "agreed_with_extension": false,
      "magika_label": "docx",
      "magika_mime": "application/…",
      "libmagic_mime": "application/…"
    }
  },
  "render": {
    "engine": "libreoffice",
    "rasterizer": "pdftoppm",
    "dpi": 150,
    "page_count_total": 3,
    "page_count_rendered": 3,
    "truncated": false,
    "blank_pages_skipped": 0,
    "blank_pages": [],
    "image_page_count": 1,
    "total_image_count": 3,
    "duration_ms": {
      "detect": 102, "soffice": 3169, "rasterize": 295,
      "hash_original": 621, "trim": 298, "focus": 0,
      "hash_derivatives": 79, "hash": 1001,
      "qr": 204, "ocr": 0, "total": 4571
    },
    "scanners": {
      "qr": {"enabled": true, "formats": "qr_code,micro_qr_code,rmqr_code"},
      "ocr": {"enabled": false, "lang": null, "psm": null, "all_pages": false}
    }
  },
  "security": {
    "macro_security_level": 3,
    "network": "denied",
    "macros": "disabled",
    "java": "disabled"
  },
  "pages": [
    {
      "index": 1,
      "file": "page-001.png",
      "width_px": 1241, "height_px": 1754,
      "width_mm": 210.01, "height_mm": 297.0,
      "phash": "be3e3e3cc1c1c1c1",
      "colorhash": "0f00700000e000",
      "sha256": "sha256 of the PNG",
      "is_blank": false,
      "image_count": 3,
      "trimmed": {
        "file": "page-001-trimmed.png",
        "width_px": 1241, "height_px": 332,
        "original_height_px": 1754,
        "removed_percent": 81.1,
        "background_color": "#ffffff",
        "phash": "bec1c13e9c25c33c",
        "colorhash": "0f00700000e000",
        "sha256": "sha256 of the trimmed PNG",
        "is_blank": false
      },
      "focused": { "... same shape as trimmed ..." },
      "qr": [
        {
          "format": "qr_code",
          "value": "https://example.com",
          "position": "10,10 50,10 50,50 10,50",
          "error_correction_level": "L",
          "is_mirrored": false,
          "raw_bytes_hex": "00112233"
        }
      ],
      "qr_skipped": "blank_page",
      "ocr": {
        "text": "Dear customer, ...",
        "char_count": 128,
        "duration_ms": 312
      }
    }
  ],
  "warnings": [
    { "code": "extension_mismatch",
      "message": "input extension did not agree with detected type" }
  ],
  "errors": []
}
```

| Top-level key | Notes |
|---|---|
| `input` | Provenance. `sha256` is of the uploaded bytes (streamed, not buffered). `detected` is the full output of the detection layer — Magika label/score, libmagic MIME, extension hint, and whether they agreed |
| `render` | Rendering parameters and stage timings. `duration_ms.hash` is wall-clock for the parallelised hash+trim+focus stage; `hash_original/trim/focus/hash_derivatives` are CPU-time sums (so they can exceed `hash` on multi-core boxes) |
| `security` | What the hardened LibreOffice profile enforced. `macros: disabled` = `DisableMacrosExecution=true` in the XCU. Extra fields appear when `disclose_security_internals=1` |
| `pages[]` | One entry per rendered page. `phash` is 64-bit perceptual hash (8 hex chars), `colorhash` is a 14-char color-histogram fingerprint, `sha256` is cryptographic |
| `pages[].trimmed` | Present when the renderer found a solid-color bottom margin worth cropping (>10% and leaves ≥100px of content). Has its own hashes so trimmed output is independently identifiable |
| `pages[].focused` | Present only for spreadsheets with solid-color margins on all four sides. Skipped when the result would be a useless sliver (aspect >8:1) |
| `pages[].image_count` | Count of embedded raster images pypdf found on the source PDF page. Drives OCR image-gating. `0` on text-only pages |
| `pages[].qr` | Always present (possibly empty list). Each entry has `format`, `value`, `position`, `error_correction_level`, `is_mirrored`, `raw_bytes_hex` |
| `pages[].qr_skipped` | Only present when the QR scan couldn't run. Values: `"blank_page"`, `"disabled"`, `"timeout"`, `"error"`. Detailed messages appear in `warnings[]` with `code="qr_scan_error"` |
| `pages[].ocr` | Always present. When the scan ran: `text`, `char_count`, `duration_ms`. When it didn't: also has `skipped` with one of `"blank_page"` / `"disabled"` / `"no_images"` (default mode, page had no embedded images) / `"timeout"` (single-call) / `"timeout_budget"` (per-job budget exhausted) / `"error"` |
| `render.image_page_count` / `render.total_image_count` | Aggregates over `pages[].image_count`: how many pages contained ≥1 image, and the total image count across all pages. Useful for picking between `ocr=1` (image-gated) and `ocr_all=1` |
| `render.scanners` | Scanner configuration echoed back — `qr.enabled`, `qr.formats`, `ocr.enabled`, `ocr.lang`, `ocr.psm`, `ocr.all_pages` |
| `render.duration_ms.qr` / `.ocr` | Scanner time attribution, summed across pages |
| `warnings[]` | Non-fatal advisories — `extension_mismatch`, `macro_enabled_format`, `magika_unrecognized_content`, `qr_scan_error`, `ocr_scan_error`, etc. |
| `errors[]` | Reserved for future use; always empty in successful runs |

Page artifact filenames follow `^page-\d{1,4}(-trimmed|-focused)?\.png$` — the dispatcher validates this schema before accepting the worker's metadata.json to prevent an RCE'd soffice from writing attacker-controlled paths into Postgres.

### Authentication

By default the API is **unauthenticated**. Set
`CLIPPYSHOT_API_KEY=<token>` in the api service environment to require
`Authorization: Bearer <token>` on `/v1/*` and `/metrics`. Only
`/v1/healthz` and `/v1/version` stay public.

```sh
curl -H "Authorization: Bearer $TOKEN" \
     -F 'file=@document.docx' \
     http://localhost:8001/v1/jobs
```

### Minimal Python example

```python
import requests, time

base = "http://localhost:8001"

# Submit
r = requests.post(f"{base}/v1/jobs", files={"file": open("sample.docx", "rb")})
job_id = r.json()["job_id"]

# Poll
while True:
    j = requests.get(f"{base}/v1/jobs/{job_id}").json()
    if j["status"] in ("done", "failed"):
        break
    time.sleep(1)

if j["status"] == "done":
    meta = requests.get(f"{base}/v1/jobs/{job_id}/metadata").json()
    print(f"rendered {meta['render']['page_count_rendered']} pages")
    for p in meta["pages"]:
        png = requests.get(f"{base}/v1/jobs/{job_id}/pages/{p['index']}.png").content
        open(f"page-{p['index']:03d}.png", "wb").write(png)
else:
    print(f"failed: {j.get('error')}")
```

---

## Hardening

**Out of the box** the compose stack already applies:

- `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`
  on every service container
- Per-worker `--memory`, `--memory-swap`, `--pids-limit`, `--cpus`,
  `--ulimit nofile=…` caps
- `--network=none` on the worker (no network namespace access at all)
- `--user 10001:10001` on the worker
- Read-only bind of the input file; read-write bind of the output dir only
- Docker-default seccomp profile on the worker (blocks `bpf`, `keyctl`,
  `mount`, `ptrace`, clock-set, `kexec_*`, `io_uring_*`, `userfaultfd`,
  etc. — already quite strict)
- Docker's default AppArmor profile if the host has AppArmor enabled
- metadata.json schema validation in the dispatcher (prevents an RCE'd
  soffice from writing attacker-controlled routing fields to Postgres)
- Malware input file is deleted from the shared volume immediately
  after conversion

That's enough for most deployments. The **optional hardening below**
replaces the two Docker defaults (AppArmor, seccomp) with ClippyShot's
tighter profiles and runs workers under gVisor for syscall-level
isolation. None are required to make the stack work:

### 1. Install gVisor and set it as a runtime

Follow <https://gvisor.dev/docs/user_guide/install/>. Verify:

```sh
docker info --format '{{json .Runtimes}}'
# should include "runsc"
```

The dispatcher auto-detects and prefers `runsc` for workers. Without
gVisor you get `runc` + namespace-only isolation.

### 2. Load the AppArmor profile

```sh
sudo cp deploy/apparmor/clippyshot-soffice /etc/apparmor.d/
sudo apparmor_parser -r /etc/apparmor.d/clippyshot-soffice
```

Then tell the dispatcher the profile is loaded — it can't see
`/sys/kernel/security/` from inside its own container:

```sh
# Create/append an .env file next to docker-compose.yml (compose reads it
# automatically), then recreate the stack so the new env reaches the
# dispatcher.
cd deploy/docker
echo "CLIPPYSHOT_APPARMOR_PROFILES=clippyshot-soffice" >> .env
docker compose down && docker compose up -d
```

The dispatcher will now attach `clippyshot-soffice` to every worker via
`--security-opt=apparmor=…`. If you skip this step you get
`docker-default` and a `security_warnings` entry on each job record.

(On bare-metal deployments the dispatcher reads
`/sys/kernel/security/apparmor/profiles` directly; the env var is only
needed in containerized deployments where that file isn't visible.)

### 3. Attach the ClippyShot seccomp profile

Docker reads seccomp JSON from the *host* filesystem, not the
dispatcher's view. Two-step setup:

```sh
# Copy the profile out of the image to a host-readable path.
# --entrypoint overrides the image's `clippyshot` ENTRYPOINT.
sudo mkdir -p /opt/clippyshot
docker run --rm --entrypoint cat clippyshot:dev /etc/clippyshot/seccomp.json \
  | sudo tee /opt/clippyshot/seccomp.json > /dev/null
sudo chmod 644 /opt/clippyshot/seccomp.json

# Or just copy from the source tree (same content):
sudo cp deploy/seccomp/clippyshot.seccomp.json /opt/clippyshot/seccomp.json

# Tell the dispatcher where it lives on the host and recreate the stack
cd deploy/docker
echo "CLIPPYSHOT_SECCOMP_JSON_HOST=/opt/clippyshot/seccomp.json" >> .env
docker compose down && docker compose up -d
```

Without this, Docker's default seccomp profile still applies (which
already blocks the most dangerous syscalls — `keyctl`, `bpf`,
`clock_settime`, etc). The ClippyShot profile is strictly tighter:
restricts `socket()` to `AF_UNIX`, blocks `perf_event_open`,
`userfaultfd`, `io_uring_*`, `ptrace`, etc.

### 4. Optional: bearer-token auth

**By default ClippyShot has no authentication.** Anyone with network
access to the API port can upload documents, view jobs, and download
artifacts. That's fine for an isolated lab; not fine for anything
internet-facing. Either put it behind an auth proxy (Caddy, nginx,
oauth2-proxy) or enable the built-in bearer-token auth:

Set `CLIPPYSHOT_API_KEY=<random-token>` in the API service env. Once
set, clients must send `Authorization: Bearer <token>` on `/v1/*`
endpoints and `/metrics`. Only `/v1/healthz` and `/v1/version` stay
public — the web UI (`/`) and its static assets (`/assets/*`) also
require auth under this mode, so browser-based use needs an auth-
injecting reverse proxy.

---

## Local bare-metal / VM runs (nsjail or bwrap)

When you're running ClippyShot directly on a host (no outer container),
the strongest sandbox backends are `nsjail` and `bwrap`. Validated on
Ubuntu 24.04 with LibreOffice 24.2. The setup has more moving parts than
the compose stack — use compose unless you have a specific reason to
run bare metal.

### One-time host setup

```sh
# 1. LibreOffice, pdftoppm, bubblewrap, nsjail, and libseccomp's Python
#    bindings. The seccomp bindings ship as `python3-seccomp` on Ubuntu;
#    they are not installable via pip.
sudo apt install -y \
    libreoffice libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-draw \
    poppler-utils bubblewrap python3-seccomp apparmor-utils
# nsjail: Ubuntu 24.04 ships /usr/local/bin/nsjail via nsjail source or
# you can grab a build from the ClippyShot Dockerfile. No apt package yet.

# 2. Load the AppArmor profiles. On Ubuntu 24.04+
#    apparmor_restrict_unprivileged_userns=1 blocks unprivileged
#    user-namespace creation except for profiles that permit it.
sudo cp deploy/apparmor/clippyshot-nsjail  /etc/apparmor.d/
sudo cp deploy/apparmor/clippyshot-bwrap   /etc/apparmor.d/
sudo cp deploy/apparmor/clippyshot-runtime /etc/apparmor.d/
sudo cp deploy/apparmor/clippyshot-soffice /etc/apparmor.d/
sudo apparmor_parser -r /etc/apparmor.d/clippyshot-*

# Verify all four are loaded
sudo aa-status | grep clippyshot
```

See `deploy/apparmor/README.md` for details on each profile.

### Install ClippyShot into a venv

```sh
# --system-site-packages so python3-seccomp (apt-installed) is visible
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
```

`--system-site-packages` is important — the bwrap backend requires
`import seccomp` which lives in the distro's site-packages and cannot be
pip-installed on its own.

### Smoketest

```sh
CLIPPYSHOT_SANDBOX=nsjail .venv/bin/clippyshot selftest
```

A successful run ends with `selftest_passed` and `"secure": true,
"insecurity_reasons": []`. If you see `insecurity_reasons` set,
something is missing (AppArmor profile not loaded, libseccomp Python
bindings missing, etc.).

### Force a specific backend

Auto-selection tries `nsjail` → `bwrap` → `container`. Override with
`CLIPPYSHOT_SANDBOX=nsjail|bwrap|container`:

```sh
CLIPPYSHOT_SANDBOX=nsjail .venv/bin/clippyshot convert input.docx -o out/
CLIPPYSHOT_SANDBOX=bwrap  .venv/bin/clippyshot convert input.docx -o out/
```

For `bwrap` on a host that hasn't installed `python3-seccomp`, you also
need `CLIPPYSHOT_WARN_ON_INSECURE=1` — the backend refuses to activate
without the BPF filter otherwise.

### Run the API server locally (nsjail backend)

```sh
CLIPPYSHOT_SANDBOX=nsjail \
CLIPPYSHOT_DATABASE_URL=sqlite:///./clippyshot-jobs.db \
.venv/bin/clippyshot serve --host 0.0.0.0 --port 8000 --job-store sql
```

Only the sync `/v1/convert` endpoint works in this topology — async
`/v1/jobs` requires the dispatcher architecture (i.e. the compose
stack).

Notes:

- `nsjail` gives you user+mount+PID+IPC+UTS+cgroup+network namespaces,
  rlimits, and KAFEL-based seccomp via `--seccomp_policy
  /etc/clippyshot/seccomp.policy` (denylist-style — blocks only the
  dangerous syscalls, same philosophy as Docker's default).
- `bwrap` gives you the same namespace model with a libseccomp BPF
  filter. Slightly faster to spin up than nsjail; fewer knobs.
- Both refuse to activate when any of their security probes fail
  (AppArmor missing, seccomp unavailable, etc.); set
  `CLIPPYSHOT_WARN_ON_INSECURE=1` to override.

### When to prefer each backend

| Environment | Best choice | Why |
|---|---|---|
| Containerized service (Docker/K8s) | `container` sandbox inside the outer container | Nested user namespaces add complexity with no extra isolation beyond what the runtime already gives |
| Docker Compose with dispatcher + runsc | `runsc` worker + `container` backend | gVisor does syscall interposition; strongest available without needing AppArmor |
| Bare metal / VM | `nsjail` | Tightest per-process isolation; KAFEL seccomp policy is tighter than Docker's default |
| Bare metal without nsjail installed | `bwrap` | Slightly less featureful but easier to install |

---

## Single-container (evaluation / dev)

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

Or run the API + inline sync endpoint (no dispatcher; use `/v1/convert`
rather than `/v1/jobs`):

```sh
docker run --rm -p 8000:8000 \
    --read-only --cap-drop=ALL --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,exec,nosuid,size=512m \
    --tmpfs /var/lib/clippyshot:rw,nosuid,size=64m,uid=10001,gid=10001 \
    -e CLIPPYSHOT_WARN_ON_INSECURE=1 \
    clippyshot:dev serve --host 0.0.0.0 --port 8000 --job-store memory
```

The async `/v1/jobs` endpoint will accept uploads but leave them queued
forever — nothing in this topology dispatches them. Use the compose
stack if you want async jobs.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| API container crashloops with `sqlite3.OperationalError: unable to open database file` | SQL store default path not writable on read-only rootfs | Pass `CLIPPYSHOT_DATABASE_URL=sqlite:////var/lib/clippyshot/jobs.db` or use `--job-store memory` |
| Every worker fails with "Some container hardening features are missing" | `CLIPPYSHOT_WARN_ON_INSECURE=0` and the worker's runtime (e.g. runsc/gVisor) virtualizes `/proc/self/status` | Dispatcher auto-sets `=1` for runsc; if you see this under runc, load the AppArmor profile and seccomp JSON |
| Workers OOM-killed on large spreadsheets (`exit=-9` from pdftoppm) | `CLIPPYSHOT_WORKER_MEMORY=2g` too tight for wide renders | Raise to `4g` (default) or higher |
| `[WARN tini (2)] Tini is not running as PID 1` in worker output | `--init` passed alongside image ENTRYPOINT that already runs tini | Don't pass `--init`; the image already does. (Fixed in current build.) |
| Jobs queue forever, never dispatch | Single-container `serve` mode has no inline dispatcher | Use the compose stack, or switch to `/v1/convert` (sync) |
| Search bar returns everything | Old API without `q=` support | Rebuild; `q=<substring>` is now supported on `/v1/jobs` |
| Local bwrap: `ImportError: No module named 'seccomp'` | venv created without `--system-site-packages` | Recreate: `python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e .` |
| Local bwrap: soffice dies with `ERROR 4 forking process` | AppArmor profile denies `socketpair(AF_UNIX)` or file-lock | Ensure the latest `clippyshot-soffice` profile is loaded (commit includes `network unix` and `k` on sandbox paths) |
| Local nsjail: soffice exit 1 with `ERROR: /proc not mounted` | Stale code with `--disable_proc` still in the nsjail backend | Pull latest — `--disable_proc` removed from `src/clippyshot/sandbox/nsjail.py` |
| Local nsjail: soffice exit 159 / SIGSYS on startup | Outdated KAFEL allowlist missing syscalls used by newer LO | Pull latest — policy is now denylist-style, survives LO version changes |
| Local nsjail: `sh: cannot create /dev/null: Directory nonexistent` | nsjail doesn't populate `/dev` automatically | Pull latest — `nsjail.py` now bind-mounts `/dev/{null,zero,random,urandom}` |

---

## What's inside a worker container

Each job launches with roughly this `docker run`:

```
docker run --rm
    --runtime=runsc                            # gVisor syscall-level sandbox
    --user 10001:10001
    --network=none
    --cap-drop=ALL
    --security-opt=no-new-privileges
    --security-opt=apparmor=clippyshot-soffice # if host profile loaded
    --security-opt=seccomp=<host path>         # if *_HOST env set
    --read-only
    --memory 4g --memory-swap 4g               # disables swap
    --pids-limit 256
    --cpus 1.0
    --ulimit nofile=4096:4096
    --tmpfs /tmp:rw,nosuid,noexec,size=512m
    -e CLIPPYSHOT_SANDBOX=container
    -e HOME=/tmp
    -e CLIPPYSHOT_WARN_ON_INSECURE=<0 or 1>
    --mount type=bind,src=<input>,dst=/tmp/input/<name>,readonly
    --mount type=bind,src=<output>,dst=/tmp/output
    --workdir /job
    --label clippyshot.role=worker
    --label clippyshot.job_id=<uuid>
    clippyshot:dev worker --job-dir /tmp --input /tmp/input/<name> --output /tmp/output --job-id <uuid> --quiet
```

The container is one-shot (`--rm`): when the worker exits, Docker tears
down the overlayfs, the container's tmpfs, and any processes that
survived. The only persistent state is what the worker wrote to
`/tmp/output`, which is a bind mount of the host's job output directory.

After the worker exits, the dispatcher:

1. Reads and validates `metadata.json` from the output dir
2. Writes the job's terminal status + page counts + `expires_at` to
   Postgres
3. **Deletes the original input file** from the host volume (malware
   samples are not retained)

The output directory stays until `CLIPPYSHOT_JOB_RETENTION_SECONDS`
expires (default: never) or the operator calls `DELETE /v1/jobs/<id>`.
