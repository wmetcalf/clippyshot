# ClippyShot configuration reference

Every `CLIPPYSHOT_*` knob, by group, with its default. This is the **engine** surface; the
blastbox **host/runtime** knobs (`BLASTBOX_*` — dispatch, warm pool, FC/gVisor tiers) are in
[blastbox's `docs/CONFIGURATION.md`](https://github.com/wmetcalf/blastbox/blob/main/docs/CONFIGURATION.md).

> **Render/limit knobs funnel through `Limits.from_env()`**, so the CLI (`clippyshot convert`)
> and the HTTP API (`/v1/jobs` `params`) honor the same vars — add a tunable there and both
> pick it up. The rest are read directly (sandbox selection, scanners, warm-UNO). A
> set-but-empty value is treated as unset.

## Render + resource limits (`Limits.from_env()`)

| Var | Default | Range | Notes |
|---|---|---|---|
| `CLIPPYSHOT_TIMEOUT` | `60` | 1–600 | per-conversion wall-clock (s) |
| `CLIPPYSHOT_DPI` | `150` | 36–600 | rasterization DPI |
| `CLIPPYSHOT_MAX_PAGES` | `50` | 1–1000 | page cap |
| `CLIPPYSHOT_MEM` | `8 GiB` | ≤64 GiB | RLIMIT_AS (VADDR — soffice mmaps 4–8 GB at ~500 MB RSS; the container `--memory` is the real RSS cap) |
| `CLIPPYSHOT_TMPFS` | `1 GiB` | ≤64 GiB | RLIMIT_FSIZE / scratch cap |
| `CLIPPYSHOT_MAX_INPUT` | `100 MiB` | ≤64 GiB | reject larger uploads |
| `CLIPPYSHOT_MAX_WIDTH` / `CLIPPYSHOT_MAX_HEIGHT` | `32768` | ≤262144 | per-page pixel ceiling (decompression-bomb guard) |
| `CLIPPYSHOT_SKIP_BLANKS` | `true` | bool | drop blank pages |
| `CLIPPYSHOT_RASTERIZER` | `pdfium` | `pdfium`/`pdftoppm` | PDF→PNG backend (pHash not comparable across a switch) |
| `CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS` | `false` | bool | include sandbox/AppArmor names in `metadata.security` (redacted by default) |

## Sandbox selection

| Var | Default | Notes |
|---|---|---|
| `CLIPPYSHOT_SANDBOX` | auto | force the soffice sandbox: `nsjail` → `bwrap` → `container`. Auto picks the best available; `container` inside an OCI host. |
| `CLIPPYSHOT_WARN_ON_INSECURE` | off | let the backend self-check run leniently (set by the dispatcher under runsc / opted-in runc). |
| `CLIPPYSHOT_INNER_NONO` | off | **optional** nested Landlock layer (nono) inside the selected backend. Fails fast where Landlock is absent (the gVisor Sentry — ENOSYS); works on runc + the FC guest. See [DEPLOYMENT.md](DEPLOYMENT.md). |
| `CLIPPYSHOT_INNER_NONO_PROFILE` | `""` | a nono profile JSON (e.g. profiler-generated) for the inner layer; else auto-derived grants. |
| `CLIPPYSHOT_NONO_BIN` | `nono` on PATH | nono binary path/name. |

## Scanners (OCR / QR)

| Var | Default | Notes |
|---|---|---|
| `CLIPPYSHOT_OCR` | off | master switch for tesseract OCR (image-gated unless `OCR_ALL`). |
| `CLIPPYSHOT_OCR_ALL` | off | OCR every page, not just image-heavy ones. |
| `CLIPPYSHOT_OCR_LANG` | `""` | tesseract `-l` language(s). |
| `CLIPPYSHOT_OCR_PSM` | `3` | tesseract page-segmentation mode. |
| `CLIPPYSHOT_QR` | off | ZXing QR/barcode scan. |

Scanner crashes are **non-fatal** — they record `ocr.skipped`/`qr_skipped` + a warning and the conversion still succeeds. The forwardable subset is allowlisted per-engine on the host (`BLASTBOX_ENGINE_CLIPPYSHOT_PARAM_KEYS`).

## Warm-UNO tier (opt-in)

| Var | Default | Notes |
|---|---|---|
| `CLIPPYSHOT_WARM_UNO` | off | convert through a persistent `unoserver`/`unoconvert` instead of cold `soffice --convert-to`. Parity-preserving + fail-closed (any UNO hiccup → cold fallback). |
| `CLIPPYSHOT_WARM_UNO_TRANSPORT` | `socket` | `socket` (UDS) vs `pipe`. |
| `CLIPPYSHOT_WARM_PRIME` | `1` | prime the PDF-export filters at warmup (avoids the first-convert filter-warmup tax). |
| `CLIPPYSHOT_WARM_PROFILE_DIR` | `/tmp/.clippyshot-warm-profile` | LO user-profile dir for the warm server. |
| `CLIPPYSHOT_UNO_PYTHON` | bundled | python that runs unoserver. |
| `CLIPPYSHOT_WARM_DIAG_FILE` | unset | opt-in path for a warm/cold-decision breadcrumb (appended). |

## Decompression-bomb caps (zip / MHT)

| Var | Notes |
|---|---|
| `CLIPPYSHOT_MAX_EXTRACT_ENTRIES` / `_ENTRY_BYTES` / `_TOTAL_BYTES` | per-archive entry count + per-entry + total extracted byte caps (OOXML/ODF are zips). |
| `CLIPPYSHOT_MAX_MHT_PARTS` / `_TOTAL_BYTES` | MHT (`.mht`/`.mhtml`) part count + total byte caps. |
