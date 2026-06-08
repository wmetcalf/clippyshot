# ClippyShot Cut-over to `blastbox.host`

**Date:** 2026-06-08
**Branch:** `feat/clippyshot-on-blastbox-host` (cut from `feat/blastbox-host-ingress`)
**Outcome:** delete ~3,650 LOC of bespoke host orchestration; ClippyShot RUNS ON the
blastbox.host framework (ingress + dispatch + cold worker), keeping only the engine
adapter, the ingress extension, and the in-process `convert`/`selftest` CLI.
**Disposition:** stage as a PR for the user's review. **Do NOT merge.** The PR is not
green until the corpus parity gate (Phase 8) passes.

---

## 0. Executive strategy

blastbox.host is already proven (0.1.5 on PyPI; the engine + ingress extension already
round-trip through it — see `tests/integration/test_blastbox_roundtrip.py` and
`tests/http/test_blastbox_ingress.py`). The two seams ClippyShot keeps
(`engine.py::ClippyShotEngine`, `blastbox_ingress.py::make_extension`) already satisfy the
framework contracts. Therefore the cleanest path is **delete-and-rewire**, NOT
build-alongside: there is no new ClippyShot code to write that needs a parity period
against the bespoke modules — the replacement code already lives in blastbox and is
tested there.

The one place we keep a safety margin is the **web UI + served `metadata.json` /
`/v1/jobs` JSON shape** (Risk 1), which is genuinely consumer-visible and changes shape.
We handle that with an explicit decision task (Phase 2) rather than a parallel
implementation.

Sequencing:
1. Branch + deps + CLI slimming (repo still imports bespoke modules — green).
2. Decide + execute the web-UI / response-shape disposition.
3. Rewire the compose stack to `blastbox serve` / `blastbox dispatch` / cold-worker.
4. Delete the bespoke host modules in one dedicated task.
5. Delete / migrate the host-coupled tests.
6. Docs.
7. Image build + smoke.
8. **Corpus parity gate (hard stop).**

Commit after every task. Run the kept unit suite (`pytest tests/unit -m "not integration and not docker"`) after every code task to confirm the pipeline tests stay green.

---

## 1. Inventory (verified against both repos)

### KEEP (the engine + the lean CLI + the two seams)
- Pipeline: `converter.py`, `detector.py`, `hasher.py`, `ocr.py`, `qr.py`, `trimmer.py`,
  `limits.py`, `types.py`, `errors.py`, `selftest.py`, `_argv.py`, `_version.py`,
  `libreoffice/`, `rasterizer/`, `sandbox/`, `observability/`.
- Seams: `engine.py` (`ClippyShotEngine`), `blastbox_ingress.py` (`make_extension`).
- CLI: `convert`, `selftest`, `version` (in-process, do NOT touch the host).

### DELETE (duplicates blastbox.host; LOC verified)
| Path | LOC | Replaced by |
|---|---|---|
| `src/clippyshot/api.py` | 1160 | `blastbox.host.ingress.app.build_app` + extension + `/v1/similar` |
| `src/clippyshot/dispatcher.py` | 742 | `blastbox.host.dispatch.Dispatcher` |
| `src/clippyshot/jobs/` (base/memory/redis_store/sql_store/retention) | ~1044 | `blastbox.host.jobs` |
| `src/clippyshot/runtime/` (docker_runtime/host_limits) | ~547 | `blastbox.host.runtime` |
| `src/clippyshot/worker.py` | 127 | `blastbox.worker.cold` / `blastbox.worker.harness` |
| `src/clippyshot/observability/metrics.py` (host metric registry) | 59 | `blastbox.observability` (KEEP `logging.py` if the pipeline imports it — verify in Phase 4) |

**Total deleted ≈ 3,620–3,680 LOC**, matching the stated ~3,650.

> NOTE on `observability/`: `engine.py`/`converter.py` import
> `clippyshot.observability` (`configure_logging`, `get_logger`, `record_conversion`,
> `set_sandbox_backend`). These are pipeline-side, NOT host-side. **Keep
> `observability/__init__.py` + `logging.py`**; only retire host-only metric emitters
> if nothing in the kept pipeline imports them. Confirm with
> `grep -rn "from clippyshot.observability" src/clippyshot` AFTER the api/dispatcher
> deletes — whatever still imports stays.

---

## 2. THE BIG DECISIONS (resolve these first)

### Decision A — CLI `serve` / `worker` subcommands: **DELETE them.**
`cli.py::_serve_cmd` imports `clippyshot.api.build_app`; `_worker_cmd` imports
`clippyshot.worker.run_worker`. Both target modules are being deleted, so the subcommands
cannot survive as-is.

**Recommendation: delete both subcommands** (and the `clippyshot worker` entry the old
dispatcher launched). Rationale:
- A thin `serve` shim that calls `blastbox.host.ingress.app.build_app` would force
  ClippyShot core to depend on `blastbox[host]` (fastapi/uvicorn/psycopg/...), defeating
  the lean-core goal (Decision D). The operator runs `blastbox serve` directly.
- The worker is now `python -m blastbox.worker.cold` with `BLASTBOX_ENGINE=clippyshot.engine:ClippyShotEngine`
  (already baked into `deploy/docker/Dockerfile.clippyshot-cold-worker` in the blastbox repo).
  A ClippyShot `worker` shim adds nothing.
- The `--job-store/--redis-url/--database-url` flags on `_serve_cmd` map to
  `BLASTBOX_DATABASE_URL` and are subsumed by `blastbox serve` + `build_job_store_from_env`.

`cli.py` after this task keeps only `convert`, `selftest`, `version`. Drop the
`from clippyshot.worker import run_worker` import and the `import os`/`uvicorn` usages that
only `_serve_cmd` needed.

### Decision B — Web UI (`src/clippyshot/static/`): **DROP from the served surface (this PR), file a follow-up.**
The bespoke `api.py` served the UI at `/` + `/assets` and the UI is **deeply coupled** to
the bespoke response shapes:
- `index.html` reads `job.detected`, `job.pages_done`, `job.pages_total` from `/v1/jobs`
  (lines 339, 719, 733, 747, 753, 770), and reads the rich `metadata.json` via
  `inp.detected` at `/v1/jobs/{id}/metadata` (lines 425–431).
- blastbox `Job.to_public_dict()` has NO `detected`/`pages_done`/`pages_total`
  (it has `engine`, `input_sha256`, `params`, `result_summary={status,artifact_count,warning_count}`).
- blastbox's served `metadata.json` is the **sealed Envelope** (`engine/status/input_sha256/
  detected/artifacts[]/warnings[]/payload`), NOT the converter's native dict.

So the UI breaks under blastbox.host as-is. `build_app` does not mount a UI and has no
extension hook for a `/` HTML root (the extension seam mounts routers, which CAN add a `/`
route, but porting the JS to the new shapes is real work).

**Recommendation for THIS PR:** do not serve the UI. The blastbox ingress is API-first
(intended to sit behind a proxy). Delete `src/clippyshot/static/` from the *served*
surface and the `static` package-data entry in `pyproject.toml`. Keep the HTML in-tree
(or move under `docs/legacy-ui/`) and file a follow-up issue "port the ClippyShot web UI
to the blastbox Envelope + Job shapes (add a `/` router to the ingress extension)".
`test_static_ui.py` (asserts on the HTML file content only) can stay if the file stays in
tree, or be deleted with the UI — your call; the plan deletes it in Phase 5 for a clean cut.

> If the user wants the UI preserved in this PR, that becomes a *separate, larger* task:
> add an HTML `/` + `/assets` router to `blastbox_ingress.py`, and rewrite the JS to read
> `result_summary` + walk the Envelope `payload` tree for page count/detection. Flag this
> explicitly at PR time; do not silently ship a broken UI.

### Decision C — served `metadata.json` schema parity: **ACCEPT the shape change; document it loudly (THIS IS RISK 1).**
See Phase 9 Risk 1 for the full analysis. The decision: the served `metadata.json` shape
**changes** for downstream consumers (it becomes the blastbox Envelope). There is no way to
keep the old rich dict served while running on blastbox.host (the dispatcher overwrites
`metadata.json` with the host-sealed Envelope by design — `dispatch.py::_write_sealed_metadata`,
and `seal_envelope` even *reserves* the `metadata.json` name). The CLI `convert --json`
path is UNAFFECTED (it prints `converter.convert().metadata` directly, never the host).

### Decision D — dependency split: **core = lean (engine + CLI); `clippyshot[host]` = `blastbox[host]`.**
See Phase 1.3.

---

## Phase 1 — Branch, dependencies, CLI slim (repo stays green)

### Task 1.1 — Branch
Create `feat/clippyshot-on-blastbox-host` from `feat/blastbox-host-ingress`.
```
git switch -c feat/clippyshot-on-blastbox-host
```
No file change. (Confirm `git status` clean first.)

### Task 1.2 — Bump the blastbox floor
**Modify** `pyproject.toml`: `"blastbox>=0.1.4"` → `"blastbox>=0.1.5"`.
(0.1.5 ships the generic `/v1/similar`, `build_job_store_from_env`, and the on-DONE page-hash
indexer the compose stack relies on.)

### Task 1.3 — Re-cut the dependency list (Decision D)
ClippyShot core must install lean: engine + the `convert`/`selftest` CLI only. The host deps
(`fastapi`, `uvicorn`, `python-multipart`, `structlog`, `prometheus-client`, `psycopg`,
`redis`) are pulled by `blastbox[host]` — ClippyShot drops them from its own core deps.

**Modify** `pyproject.toml`:

`[project].dependencies` (core) becomes:
```toml
dependencies = [
    "magika>=0.5.1",
    "rosetta-squint>=1.1.0",
    "Pillow>=11.0.0",
    "pypdf>=4.0.0",
    "pypdfium2>=4.30.0",
    "python-magic>=0.4.27",
    "olefile>=0.46",
    "blastbox>=0.1.5",            # core only — gives the contract + Engine SDK
]
```
Removed from core: `fastapi`, `uvicorn[standard]`, `python-multipart`, `structlog`,
`prometheus-client`, `psycopg[binary,pool]`, `redis`, `pyzipper`.

- `pyzipper` was used ONLY by `api.py::_zip_dir_to_file` (encrypted result zip). blastbox's
  `/result` ships a **plain** zip (`zipfile.ZipFile`, no password). So `pyzipper` is dropped
  from core. **If** the operator still needs the "infected"-password convention, that is a
  follow-up feature request against blastbox, not ClippyShot — flag at PR time (Risk 3).
- `structlog`/`prometheus-client` were imported by the pipeline's `observability/`? Verify:
  if `observability/logging.py` imports `structlog`, **keep `structlog` in core**. Run
  `grep -rn "structlog\|prometheus" src/clippyshot/{converter,engine,detector,observability,libreoffice,rasterizer,sandbox,selftest}.py src/clippyshot/observability/` and pin core deps to exactly what the KEPT code imports. (Likely outcome: keep `structlog`; drop `prometheus-client` if only the deleted host metrics used it.)

Add an extra so an operator who wants to run the host from the ClippyShot image gets the stack:
```toml
[project.optional-dependencies]
host = ["blastbox[host]>=0.1.5"]
dev = [
    "blastbox[host]>=0.1.5",      # the ingress/extension tests need the host stack
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "httpx>=0.27.0",
    "fakeredis>=2.21.0",
    "ruff>=0.3.0",
    "mypy>=1.9.0",
]
```
(`dev` needs `blastbox[host]` because `tests/http/test_blastbox_ingress.py` imports
`blastbox.host.ingress.app`.)

**Verify (do NOT skip):** create a throwaway venv, `pip install .` (core only, no extras),
then `python -c "import clippyshot.engine; import clippyshot.cli"` and
`clippyshot selftest`/`clippyshot convert --help`. Confirm NO fastapi/uvicorn/psycopg got
pulled (`pip list | grep -iE 'fastapi|uvicorn|psycopg|redis'` is empty). This is the proof
of the lean-core split.

### Task 1.4 — Slim the CLI (Decision A)
**Modify** `src/clippyshot/cli.py`:
- Delete `_serve_cmd`, `_worker_cmd`, their subparsers (`psv`, `pw`), and the
  `from clippyshot.worker import run_worker` import.
- Keep `convert`, `selftest`, `version`.

**TDD:** `tests/cli/test_cli.py` is the guard. First read it; if it asserts on `serve`/`worker`
subcommands, update those assertions to expect they are GONE (e.g. `clippyshot serve` exits
non-zero / argparse error), and that `convert/selftest/version` still work. Run:
```
pytest tests/cli/test_cli.py -q
```
Expect: green after the test is updated to the slim surface.

**Commit:** `chore(cli): drop serve/worker subcommands (moved to blastbox serve/dispatch)`.

At this point the repo still imports `api.py`/`dispatcher.py`/etc. from the TESTS only;
`cli.py` no longer does. Run `pytest tests/unit -m "not integration and not docker"` — still green.

---

## Phase 2 — Static UI disposition (Decision B)

### Task 2.1 — Remove the UI from the package
**Modify** `pyproject.toml`: delete the `[tool.setuptools.package-data]` `static/*` lines.
**Move** `src/clippyshot/static/` → `docs/legacy-ui/` (git mv) OR delete it. Recommend
`git mv` so the JS is preserved for the follow-up port.
**Commit:** `chore(ui): unbundle the web UI (ingress is API-first on blastbox.host)`.
File the follow-up issue referenced in Decision B.

---

## Phase 3 — Compose / Docker rewire (the heart of "runs on blastbox.host")

The goal: `api`, `dispatcher`, `worker` services run blastbox images/commands with
`BLASTBOX_*` env. `postgres` (pg_bktree) stays. The runtime ClippyShot image stays the
engine base; the cold-worker image is built from blastbox's
`Dockerfile.clippyshot-cold-worker` (already in the blastbox repo).

### Task 3.1 — Env var rename map (apply throughout compose + docs)
Host-orchestration vars (DELETE / rename to `BLASTBOX_*`):
| OLD (CLIPPYSHOT_*) | NEW (BLASTBOX_*) | Notes |
|---|---|---|
| `CLIPPYSHOT_DATABASE_URL` | `BLASTBOX_DATABASE_URL` | shared by serve + dispatch |
| `CLIPPYSHOT_JOB_ROOT` | `BLASTBOX_JOB_ROOT` | default `/var/lib/blastbox/jobs` |
| `CLIPPYSHOT_JOB_RETENTION_SECONDS` | `BLASTBOX_JOB_RETENTION_SECONDS` | |
| `CLIPPYSHOT_DISPATCH_CONCURRENCY` | `BLASTBOX_DISPATCH_CONCURRENCY` | |
| `CLIPPYSHOT_DISPATCH_INTERVAL` | `--poll-interval` flag / `BLASTBOX` default | dispatch CLI flag |
| `CLIPPYSHOT_WORKER_IMAGE` | `BLASTBOX_ENGINES=clippyshot=<image:tag>` | EngineSpec registry |
| `CLIPPYSHOT_WORKER_RUNTIME` | (blastbox runtime selection — verify env name in `blastbox.host.runtime.docker`) | runsc/runc |
| `CLIPPYSHOT_WORKER_MEMORY/PIDS_LIMIT/CPUS/NOFILE` | blastbox worker-cap envs (verify exact names in `blastbox.host.runtime`/`Limits`) | |
| `CLIPPYSHOT_API_WORKERS` | `BLASTBOX_API_WORKERS` | |
| `CLIPPYSHOT_API_KEY` | `BLASTBOX_API_KEY` | bearer auth |
| `CLIPPYSHOT_SECCOMP_JSON_HOST` / `CLIPPYSHOT_APPARMOR_PROFILES` | blastbox equivalents (verify in `blastbox.host.runtime.docker`) | |
| `CLIPPYSHOT_WORKER_TIMEOUT_S` (implicit) | `BLASTBOX_WORKER_TIMEOUT_S` | |
| n/a | `BLASTBOX_ALLOWED_ENGINES=clippyshot` | NEW — ingress allowlist |
| n/a | `BLASTBOX_INGRESS_EXTENSION=clippyshot.blastbox_ingress:make_extension` | NEW — mounts typed routes |
| n/a | `BLASTBOX_ENGINE=clippyshot.engine:ClippyShotEngine` | worker only (already in cold Dockerfile) |

Engine/pipeline vars that **STAY `CLIPPYSHOT_*`** (consumed by the engine inside the worker,
NOT by the host): `CLIPPYSHOT_SANDBOX`, `CLIPPYSHOT_RASTERIZER`, `CLIPPYSHOT_WARN_ON_INSECURE`,
`CLIPPYSHOT_ENABLE_QR`, `CLIPPYSHOT_ENABLE_OCR`, `CLIPPYSHOT_OCR_ALL`, `CLIPPYSHOT_OCR_LANG`,
`CLIPPYSHOT_OCR_PSM`, `CLIPPYSHOT_OCR_TIMEOUT_S`, `CLIPPYSHOT_QR_FORMATS`,
`CLIPPYSHOT_ZXING_TIMEOUT_S`, `CLIPPYSHOT_WARM_UNO`. These ride to the worker via the
dispatcher's per-job env passthrough (verify blastbox's env-passthrough/allowlist supports
`CLIPPYSHOT_*` keys; if it key-allowlists, the worker IMAGE bakes the scanner defaults via
`ENV` instead — the cold Dockerfile already bakes `CLIPPYSHOT_SANDBOX`/`CLIPPYSHOT_WARN_ON_INSECURE`).

> ACTION before writing compose: open `blastbox/src/blastbox/host/runtime/docker.py`,
> `blastbox/src/blastbox/host/dispatch.py`, and `blastbox/src/blastbox/limits.py::from_env`
> and record the EXACT `BLASTBOX_*` names for: worker runtime selection, per-worker
> memory/cpu/pids/nofile caps, seccomp/apparmor passthrough, and the per-job env-passthrough
> mechanism. The table above is the intent; the names must be copied verbatim from blastbox.

### Task 3.2 — Rewrite `deploy/docker/docker-compose.yml`
**Modify** the three service blocks. Keep `postgres` as-is (already builds
`clippyshot-postgres:dev` with pg_bktree). Keep networks/volumes; rename the data volume
mount to `/var/lib/blastbox` (or keep `/var/lib/clippyshot` and set
`BLASTBOX_JOB_ROOT=/var/lib/clippyshot/jobs` — fewer moving parts; recommend the latter to
minimise churn).

`api` service:
- `image:` stays the ClippyShot engine image (it now also carries `blastbox[host]` via the
  `clippyshot[host]` extra installed in the Dockerfile — Task 3.3), OR use a dedicated
  blastbox-host image. Recommend: install `blastbox[host]` into the existing image so
  `blastbox serve` is on PATH.
- `command: ["blastbox", "serve", "--host", "0.0.0.0", "--port", "8000", "--allowed-engines", "clippyshot"]`
- env:
  ```yaml
  - BLASTBOX_DATABASE_URL=postgresql://${POSTGRES_USER:-clippyshot}:${POSTGRES_PASSWORD:-clippyshot-dev}@postgres:5432/${POSTGRES_DB:-clippyshot}
  - BLASTBOX_JOB_ROOT=/var/lib/clippyshot/jobs
  - BLASTBOX_JOB_RETENTION_SECONDS=${BLASTBOX_JOB_RETENTION_SECONDS:-0}
  - BLASTBOX_ALLOWED_ENGINES=clippyshot
  - BLASTBOX_INGRESS_EXTENSION=clippyshot.blastbox_ingress:make_extension
  ```
- keep `read_only`, `cap_drop: ALL`, `no-new-privileges`, tmpfs, ports `8001:8000`.
- healthcheck: the old `clippyshot selftest` checks the pipeline, not the API. Replace with
  an HTTP probe: `["CMD", "curl", "-fsS", "http://localhost:8000/v1/healthz"]`
  (or `/v1/readyz`, which does a store round-trip). (Confirm `curl` is in the image; the
  Dockerfile installs poppler-utils etc. but maybe not curl — add it, or use a python one-liner.)

`dispatcher` service:
- Replace the inline `python -c "...clippyshot.dispatcher.Dispatcher..."` command with
  `command: ["blastbox", "dispatch"]` (entrypoint cleared to `[]` as today, or point
  entrypoint at the blastbox console script).
- env:
  ```yaml
  - BLASTBOX_DATABASE_URL=postgresql://...@postgres:5432/...
  - BLASTBOX_JOB_ROOT=/var/lib/clippyshot/jobs
  - BLASTBOX_JOB_RETENTION_SECONDS=${BLASTBOX_JOB_RETENTION_SECONDS:-0}
  - BLASTBOX_ENGINES=clippyshot=${CLIPPYSHOT_WORKER_IMAGE:-clippyshot-cold-worker:dev}
  - BLASTBOX_DISPATCH_CONCURRENCY=${BLASTBOX_DISPATCH_CONCURRENCY:-1}
  - BLASTBOX_WORKER_TIMEOUT_S=${BLASTBOX_WORKER_TIMEOUT_S:-300}
  # worker runtime + caps + seccomp/apparmor: exact BLASTBOX_* names from Task 3.1 recon
  # scanner vars baked into the worker image (cold Dockerfile) OR passed per-job if blastbox supports it
  ```
- keep the docker.sock mount + `group_add` + `docker info` healthcheck (the dispatcher still
  launches `docker run` workers via `blastbox.host.runtime.docker`).

`worker`:
- The compose file has no standing `worker` service (workers are launched on demand by the
  dispatcher). The change is the **image** the dispatcher launches: `BLASTBOX_ENGINES` points
  at the **cold-worker image** built from blastbox's `Dockerfile.clippyshot-cold-worker`
  (ENTRYPOINT `python -m blastbox.worker.cold`, `BLASTBOX_ENGINE=clippyshot.engine:ClippyShotEngine`).

### Task 3.3 — `deploy/docker/Dockerfile` (the engine/serve/dispatch image)
**Modify** `deploy/docker/Dockerfile`:
- Install `clippyshot[host]` (so `blastbox` console script + fastapi/uvicorn/psycopg land in
  `/opt/clippyshot/bin`). Change the pip line to `pip install '/tmp/build[host]'`.
- Change the default ENV to `BLASTBOX_*`:
  `BLASTBOX_DATABASE_URL=sqlite:////var/lib/clippyshot/clippyshot-jobs.db`,
  `BLASTBOX_JOB_ROOT=/var/lib/clippyshot/jobs`.
- ENTRYPOINT/CMD: the old `ENTRYPOINT ["tini","--","clippyshot"]` + `CMD ["serve",...]` no
  longer works (no `serve`). Options: (a) `ENTRYPOINT ["/usr/bin/tini","--"]`,
  `CMD ["blastbox","serve","--host","0.0.0.0","--port","8000","--allowed-engines","clippyshot"]`;
  (b) keep `clippyshot` entry but only for `selftest`. Recommend (a): the image's default
  is the ingress; compose overrides `command` per-service anyway.
- HEALTHCHECK: `clippyshot selftest` still works (pipeline check) and is fine as the image
  default; the compose `api` service overrides it with the HTTP probe.
- Add `curl` to the apt install list if the healthcheck uses it.

### Task 3.4 — Build the cold-worker image in the ClippyShot flow
The cold-worker Dockerfile lives in the **blastbox** repo
(`deploy/docker/Dockerfile.clippyshot-cold-worker`, build context = blastbox repo root,
`BASE_IMAGE=clippyshot:dev`). Document the two-step build in `deploy/docker/README.md`:
```
# 1. engine/serve/dispatch base image (ClippyShot repo)
docker build -f deploy/docker/Dockerfile -t clippyshot:dev .
# 2. cold-worker overlay (blastbox repo)
docker build -f deploy/docker/Dockerfile.clippyshot-cold-worker \
  --build-arg BASE_IMAGE=clippyshot:dev -t clippyshot-cold-worker:dev /path/to/blastbox
```
Set `CLIPPYSHOT_WORKER_IMAGE=clippyshot-cold-worker:dev` (compose `BLASTBOX_ENGINES`).
Since 0.1.5 is on PyPI, the cold Dockerfile's force-reinstall-from-source overlay can be
simplified to `pip install 'blastbox[...]>=0.1.5'` if desired — flag as optional cleanup.

### Task 3.5 — `clippyshot-compose` wrapper
**Keep** `deploy/docker/clippyshot-compose` essentially as-is (it only auto-detects
`DOCKER_GID` for the dispatcher's socket access — still needed). No code change beyond any
comment refresh.

**Commit (Phase 3):** `feat(deploy): run on blastbox.host (serve/dispatch/cold-worker)`.

---

## Phase 4 — Delete the bespoke host modules (one dedicated task)

Do this AFTER Phases 1–3 are committed (compose no longer references the modules) and BEFORE
the test cleanup commit, OR interleave with Phase 5 so the suite is collectable at each step.
Recommended order: delete tests that import these (Phase 5) FIRST, then delete the modules,
so `pytest` collection never errors mid-way.

### Task 4.1 — Delete
- `rm src/clippyshot/api.py`
- `rm src/clippyshot/dispatcher.py`
- `rm src/clippyshot/worker.py`
- `rm -r src/clippyshot/jobs/`
- `rm -r src/clippyshot/runtime/`
- Prune host-only `observability/metrics.py` **only if** nothing kept imports it
  (re-grep first; the pipeline's `record_conversion` may live here — if so, KEEP the file
  and only remove the HTTP/job metric symbols, or leave it entirely).

### Task 4.2 — Fix dangling imports
`grep -rn "clippyshot.api\|clippyshot.dispatcher\|clippyshot.worker\|clippyshot.jobs\|clippyshot.runtime" src/clippyshot` must return NOTHING after this task. Notably:
- `engine.py` imports `clippyshot.converter`, `clippyshot.libreoffice.uno`, etc. — all kept. Good.
- Confirm `selftest.py`, `converter.py` don't import any deleted module.

**Verify:** `python -c "import clippyshot.engine, clippyshot.blastbox_ingress, clippyshot.cli"`
and `pytest tests/unit -m "not integration and not docker" -q` — green.

**Commit:** `refactor: delete bespoke host orchestration (~3.65k LOC; now on blastbox.host)`.

---

## Phase 5 — Tests: delete vs migrate

### DELETE (test bespoke modules that no longer exist)
- `tests/http/test_api.py` — TestClient against `clippyshot.api.build_app`. DELETE.
- `tests/http/test_pdf_endpoint.py` — pdf route on bespoke api. Coverage moves to
  `tests/http/test_blastbox_ingress.py` (already covers `/pdf` + page PNG variants). DELETE.
- `tests/http/test_qr_ocr_params.py` — bespoke `/v1/convert` + `/v1/jobs` form params on
  api.py. The host has no per-request QR/OCR form params (the engine fixes
  `qr_enabled=True, ocr_enabled=False`; scanner config is image/env-level now). DELETE, and
  note the behavior change (Risk 4: no per-request QR/OCR toggles via the host).
- `tests/unit/test_dispatcher.py` — DELETE (blastbox owns the dispatcher; its tests live in
  the blastbox repo).
- `tests/unit/test_validate_metadata_scanners.py` — tests `Dispatcher._validate_metadata`. DELETE.
- `tests/unit/test_jobs.py` — tests `clippyshot.jobs`. DELETE.
- `tests/unit/test_job_retention.py` — tests `JobArtifactRegistry`/`clippyshot.jobs`. DELETE.
- `tests/unit/test_docker_runtime.py` — tests `clippyshot.runtime.docker_runtime`. DELETE.
- `tests/unit/test_audit_fixes.py` — imports `clippyshot.runtime.docker_runtime`
  (`select_worker_runtime`, `InsecureRuntimeRefused`) AND pipeline things
  (`assert_positional`, `_correct_odf_label_via_mimetype`, `validate_lang`, `validate_formats`,
  `Limits`). **MIGRATE:** split — drop the `runtime` cases, KEEP the pipeline/limits/detector/
  ocr/qr cases. Re-run to confirm green.
- `tests/unit/test_zip_encrypted.py` — tests `api._zip_dir_to_file` (pyzipper). DELETE
  (blastbox ships a plain zip; pyzipper removed from core). Flag Risk 3.
- `tests/unit/test_compose_stack.py` — asserts the OLD compose shape
  (`CLIPPYSHOT_DATABASE_URL=postgresql://`, api/dispatcher blocks, no docker.sock on api).
  **MIGRATE:** rewrite assertions to the new compose (`BLASTBOX_DATABASE_URL=postgresql://`,
  `blastbox serve`/`blastbox dispatch` commands, `BLASTBOX_ALLOWED_ENGINES=clippyshot`,
  `BLASTBOX_INGRESS_EXTENSION=...`, api still has no docker.sock, dispatcher still has it).
- `tests/unit/test_docker_docs.py` — asserts README/run_tests.sh/Dockerfile contain the OLD
  api/dispatcher/worker prose + `clippyshot serve`. **MIGRATE** to the new wording (or DELETE
  if too brittle; recommend rewrite to assert the blastbox-host architecture lines exist).
- `tests/unit/test_static_ui.py` — only valid if the UI file stays in tree. With Decision B
  (unbundle/move UI), DELETE (or repoint at `docs/legacy-ui/`).
- `tests/docker/test_image.py`, `tests/docker/test_runsc_worker.py` — likely assert
  `clippyshot serve`/`worker` entrypoints + old env. **MIGRATE** to `blastbox serve`/cold-worker.
  These are `@docker`-marked (only run against a built image), so update them in lockstep with
  Phase 7 and verify there.

### KEEP unchanged (pipeline + seams)
- `tests/http/test_blastbox_ingress.py` — the extension test. KEEP (it is the replacement).
- `tests/integration/test_blastbox_roundtrip.py` — engine→harness→trust round-trip. KEEP.
- All pipeline unit tests: `test_converter*`, `test_detector`, `test_hasher`, `test_ocr*`,
  `test_qr*`, `test_trimmer`, `test_rasterizer`, `test_sandbox*`, `test_libreoffice*`,
  `test_mht_unpack`, `test_altchunk`, `test_limits`, `test_types`, `test_convert_options`,
  `test_uno`, `test_engine_warmup`, `test_observability`, `test_extraction_caps`,
  `test_scanner_image_selection`, `test_ocr_image_gating`, `test_converter_pdf_preserved`,
  `test_converter_scanner_failure`, `test_sandbox_seccomp*`. KEEP.
- `tests/integration/test_format_families.py`, `test_sandbox_escape.py`,
  `test_security_assertions.py`, `test_scanner_sandboxes.py`. KEEP.

### `tests/unit/test_metadata_schema_stable.py` — KEEP, with a clarified contract
This guards the CONVERTER's native metadata dict (`pages[].qr/ocr/image_count`,
`render.scanners`, etc.) — which is still produced by `converter.convert()` and surfaced by
`clippyshot convert --json`. It does NOT touch the host. **KEEP.** Add a module docstring note:
"this guards the in-process converter metadata (CLI `--json` + the source the engine maps
FROM), NOT the served `metadata.json`, which is now the blastbox Envelope." (See Risk 1.)

**Verify:** `pytest tests/unit tests/http -m "not integration and not docker" -q` — all green.
**Commit:** `test: drop/migrate host-coupled tests; keep pipeline + extension suites`.

---

## Phase 6 — Docs

### Task 6.1 — README.md
**Modify** the Architecture (§129), Quick start (§33), Deployment modes (§167), and the
component table (§155, the `clippyshot.dispatcher` row). New framing: "ClippyShot is a
blastbox **engine**. The host (ingress/dispatch/worker) is provided by
[blastbox](https://github.com/wmetcalf/blastbox); ClippyShot supplies
`ClippyShotEngine` + the typed-artifact ingress extension." Update the compose section to
`blastbox serve`/`blastbox dispatch`, the env table to `BLASTBOX_*`, and the API examples
(`http://localhost:8001/v1/jobs` still valid; note `metadata` shape is the Envelope, no web UI).
Note the lean-core install (`pip install clippyshot`) vs `clippyshot[host]`.

### Task 6.2 — CLAUDE.md
**Modify** §51 (CLI subcommands → `{convert,selftest,version}`), §87–90 (three-process split:
replace api.py/dispatcher.py/worker.py bullets with "host = blastbox.host; ClippyShot = engine
+ ingress extension"), §92 (JobStore → `blastbox.host.jobs`), §109–112 (project layout).

### Task 6.3 — deploy/docker/README.md + deploy/docker/.env
Update the two-image build (Task 3.4), the `BLASTBOX_*` env, and the cold-worker image var.

**Commit:** `docs: ClippyShot runs ON blastbox.host (engine + ingress extension)`.

---

## Phase 7 — Image build + smoke

### Task 7.1 — Build both images
```
docker build -f deploy/docker/Dockerfile -t clippyshot:dev .
docker build -f deploy/docker/Dockerfile.clippyshot-cold-worker \
  --build-arg BASE_IMAGE=clippyshot:dev -t clippyshot-cold-worker:dev /path/to/blastbox
```
Expect both to build. Confirm `docker run --rm clippyshot:dev blastbox version` prints a
blastbox version, and `docker run --rm clippyshot:dev clippyshot selftest` passes.

### Task 7.2 — `@docker` tests
Update + run `pytest tests/docker -m docker` against the built images. Green.

### Task 7.3 — Local compose smoke (single doc, NOT the corpus)
```
./deploy/docker/clippyshot-compose up -d --build
# submit one known-good docx:
curl -fsS -F "file=@tests/fixtures/<some>.docx" -F "engine=clippyshot" http://localhost:8001/v1/jobs
# poll /v1/jobs/{id}, then fetch /metadata, /pdf, /pages/1.png, /result(zip)
```
Confirm: 202 on submit; job reaches DONE; `/metadata` returns a blastbox Envelope JSON;
`/pdf` + `/pages/1.png` (extension routes) serve; `/result` returns a (plain) zip; if Postgres
is up, `/v1/similar?phash=<a page phash from metadata>` returns rows (proves the on-DONE
indexer + generic search work end-to-end).

**Commit:** `chore(deploy): verified compose smoke on blastbox.host`.

---

## Phase 8 — CORPUS PARITY GATE (HARD STOP — the user's rule)

Do NOT mark the PR ready until this passes.

### Task 8.1 — Baseline (bespoke, for the diff)
The bespoke baseline is ~277/342 on the 342-doc mbzdls corpus (toolz2 reference at
172.18.101.15). Record the per-format success counts of the bespoke stack as the comparison
target (from the last known-good run or by checking out the pre-cutover commit if a fresh
baseline is required).

### Task 8.2 — Run the corpus through the blastbox-host stack
The corpus is submitted via `scripts/collect_and_submit.py` (pulls from `~/cstorage/mbzdls`,
MIME-validates, POSTs to the API) and/or `run_tests.sh` (builds the image, starts the
container, submits `tests/fixtures/corpus/*`). **Both need updating for blastbox.host:**
- `run_tests.sh`: change `docker run ... clippyshot:test serve ...` →
  `blastbox serve --allowed-engines clippyshot` with `BLASTBOX_*` env + the
  `BLASTBOX_INGRESS_EXTENSION` + a running dispatcher + cold-worker image (single-container
  `serve` is no longer self-contained: it needs a dispatcher to actually process jobs).
  Prefer driving the full compose stack for the corpus run rather than the old single
  container. Update the readiness probe to `/v1/healthz`/`/v1/readyz`.
- `scripts/collect_and_submit.py`: the POST must include `-F engine=clippyshot`
  (the blastbox ingress REQUIRES the `engine` form field; the old api.py did not).
  Update the submit call accordingly, and update the scoring read-back to walk the
  Envelope/`result_summary` (it previously may have read the rich `metadata.json`).
- Submit on toolz2 (172.18.101.15) per the established workflow.

### Task 8.3 — Compare and gate
Tally DONE/total per format from `/v1/jobs?status=done` (+ `result_summary`) and compare to
the ~277/342 bespoke baseline. **Acceptance: blastbox-host total >= bespoke baseline (within
a small, explained delta).** Because the SAME `ClippyShotEngine` runs the SAME pipeline,
parity should be exact on render success; any delta is a host-layer difference (e.g. a job
the new trust gate rejected) and must be root-caused, not waved through.

If parity holds → the PR is ready for the user's review (staged, NOT merged).
If parity regresses → systematic-debug the host-layer difference before claiming done.

**Commit:** `test(parity): corpus parity vs bespoke baseline on blastbox.host (<N>/342)`.

---

## Phase 9 — Risks

### RISK 1 (BIGGEST) — served `metadata.json` schema changes shape for downstream consumers.
- OLD served file (bespoke): the CONVERTER's native dict —
  top-level `clippyshot_version`, `input{filename,size_bytes,sha256,detected{...magika/libmagic...}}`,
  `render{engine,rasterizer,dpi,page_count_total,page_count_rendered,truncated,blank_pages,
  image_page_count,total_image_count,scanners{qr,ocr},duration_ms}`, `security{...}`,
  `pages[]{index,file,qr,ocr,image_count,phash,colorhash,sha256,trimmed,focused,width_mm,height_mm}`,
  `sheets{...}`, `warnings`, `errors`. (Written by `converter.py:949`; served verbatim by
  `api.py:1044 get_metadata`.)
- NEW served file (blastbox.host): the host-SEALED **Envelope** —
  `engine, status, input_sha256, detected{label,mime,confidence,source}, artifacts[]{id,path,kind,
  sha256,bytes}, warnings[]{code,message}, payload` (an `EmbeddedResource` tree with `Page`
  children carrying `hashes[]` + a `Record` child for scanner data). Written by
  `harness.run_detonation` and re-sealed by `dispatch._write_sealed_metadata`;
  `seal_envelope` even RESERVES the `metadata.json` filename.
- The engine maps only a SUBSET of the converter dict into the Envelope. **Dropped from the
  served metadata:** `input.size_bytes`, `detected.extension_hint/magika_*/libmagic_mime/
  agreed_with_extension`, `render.engine/rasterizer/dpi/duration_ms/blank_pages`, the
  `security` block, `sheets`, per-page `image_count`, `ocr.psm/lang`, `errors`. Scanner data
  survives but RESHAPED (under each page's `Record` child: `qr_count`/`ocr_text`/`ocr_char_count`
  — NOT the old `qr[]` list of decoded payloads / full `ocr{}` object). pHash/colorhash/sha256
  survive as `Page.hashes[]`.
- **Consumer impact:** ANY downstream parser of `/v1/jobs/{id}/metadata` breaks. The web UI
  (Decision B) is the in-repo example. `tests/unit/test_metadata_schema_stable.py` guards the
  OLD shape but only for the in-process converter dict (CLI `--json`), which is UNCHANGED — so
  that test stays green and is NOT a contradiction, but it must not be read as "the served
  shape is stable." **Mitigation:** Decision C accepts + documents the change; the follow-up UI
  port consumes the Envelope; if an external consumer needs the rich fields, the path forward is
  a richer typed payload in `ClippyShotEngine.detonate` (add `Record` fields) — NOT resurrecting
  the bespoke api. **Flag this prominently in the PR description.**

### RISK 2 — `/v1/jobs` (list/status) JSON shape also changes.
OLD `Job.to_public_dict`: `pages_done`, `pages_total`, `detected`, `worker_runtime`,
`security_warnings`. NEW (`blastbox.host.jobs.base.Job`): `engine`, `input_sha256`, `params`,
`result_summary{status,artifact_count,warning_count}` — no page counts / detection. The UI and
any list consumer break. Same disposition as Risk 1 (accept + document + follow-up port).
Sub-note: blastbox indexes ONE `/v1/similar` row per `page_index` (no trimmed/focused variant
rows), where the bespoke store indexed variants too — minor recall difference for similarity
search; acceptable, document it.

### RISK 3 — result zip is no longer AES-encrypted ("infected" convention lost).
Bespoke `/result` produced a pyzipper AES-256 zip (password "infected", malware-signaling
convention). blastbox `/result` ships a plain `ZIP_DEFLATED` zip. Analysts/AV pipelines that
relied on the password convention change behavior, and `pyzipper` leaves ClippyShot core.
**Mitigation:** document the change; if required, propose an encrypted-zip option upstream in
blastbox rather than re-adding bespoke serving.

### RISK 4 — per-request QR/OCR toggles disappear from the host path.
Bespoke `/v1/convert` and `/v1/jobs` accepted `qr/ocr/ocr_lang/ocr_psm/...` form params per
request. The blastbox ingress submit takes only `engine` + generic `params`; the
`ClippyShotEngine` currently HARD-CODES `qr_enabled=True, ocr_enabled=False`. So OCR-on and
per-request scanner tuning are not reachable via the host without either (a) baking via the
worker image's `CLIPPYSHOT_*` ENV (global, not per-request) or (b) teaching
`ClippyShotEngine.detonate` to read `params`/`CLIPPYSHOT_*` env at detonation time.
**Mitigation:** for parity-on-render this is fine (corpus runs with QR on, OCR off — matching
the engine default). Document the loss of per-request toggles; if needed, a small follow-up
wires `job.params` → `ConvertOptions` in the engine.

### Minor risks
- Env-passthrough of `CLIPPYSHOT_*` scanner vars to the worker: confirm blastbox's dispatcher
  passes/allowlists them, else bake into the cold-worker image ENV (Task 3.1).
- `BLASTBOX_JOB_ROOT` vs `/var/lib/clippyshot/jobs`: keep the ClippyShot path to minimise
  volume churn; just point the env at it.
- Healthcheck: old `clippyshot selftest` checked the pipeline, not the API liveness — the new
  `api` service must probe `/v1/healthz`; ensure `curl` exists in the image.
- The cold-worker Dockerfile's `--force-reinstall --no-deps` overlay predates PyPI 0.1.5;
  optional cleanup to `pip install 'blastbox>=0.1.5'`.

---

## Definition of done
1. `pytest tests/unit tests/http -m "not integration and not docker"` green (pipeline + extension).
2. `tests/integration/test_blastbox_roundtrip.py` green (engine round-trip).
3. Both images build; `tests/docker -m docker` green.
4. Compose smoke: submit→DONE→metadata(Envelope)/pdf/page/result/similar all work.
5. `grep -rn "clippyshot.api\|clippyshot.dispatcher\|clippyshot.worker\|clippyshot.jobs\|clippyshot.runtime" src/ tests/` returns nothing.
6. **Corpus parity >= bespoke ~277/342 baseline, root-caused if any delta.**
7. README + CLAUDE.md + deploy docs say "runs on blastbox.host".
8. PR staged for the user's review. NOT merged.
