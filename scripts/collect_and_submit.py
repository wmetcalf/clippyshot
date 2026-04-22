#!/usr/bin/env python3
"""Fast parallel collection: single find pass, MIME validation, API submission."""
import magic
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

SOURCE = os.path.expanduser("~/cstorage/mbzdls")
API = "http://127.0.0.1:8000"
N_PER_TYPE = int(sys.argv[1]) if len(sys.argv) > 1 else 50

WANTED_EXTS = {
    "doc", "docx", "docm", "rtf", "txt", "csv",
    "xls", "xlsx", "xlsm", "xlsb",
    "ppt", "pptx", "pptm", "pps", "ppsx",
    "odt", "ods", "odp",
}

# Broad MIME validation
VALID_MIME_PREFIXES = [
    "application/msword", "application/vnd.ms-", "application/vnd.openxmlformats",
    "application/vnd.oasis.opendocument", "application/x-ole", "application/CDFV2",
    "application/cdfv2", "application/x-cdf", "application/zip", "application/x-zip",
    "application/octet-stream",
    "text/", "application/rtf",
]

m = magic.Magic(mime=True)
counts = defaultdict(int)
submitted = defaultdict(int)
rejected = defaultdict(int)

print(f"Single-pass scan of {SOURCE} for {N_PER_TYPE} per type...", flush=True)
print(f"Types: {' '.join(sorted(WANTED_EXTS))}", flush=True)

proc = subprocess.Popen(
    ["find", SOURCE, "-maxdepth", "3", "-type", "f"],
    stdout=subprocess.PIPE, text=True
)

total_scanned = 0
total_submitted = 0

for line in proc.stdout:
    path = line.strip()
    if not path:
        continue
    total_scanned += 1

    if total_scanned % 5000 == 0:
        print(f"  scanned {total_scanned}... submitted {total_submitted}", flush=True)

    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in WANTED_EXTS:
        continue
    if counts[ext] >= N_PER_TYPE:
        if all(counts.get(e, 0) >= N_PER_TYPE for e in WANTED_EXTS):
            break
        continue

    try:
        detected = m.from_file(path)
    except Exception:
        continue

    valid = any(detected.startswith(p) for p in VALID_MIME_PREFIXES)
    if not valid:
        rejected[ext] += 1
        continue

    counts[ext] += 1

    try:
        r = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "POST", f"{API}/v1/jobs",
             "-F", f"file=@{path}", "--max-time", "10"],
            capture_output=True, text=True, timeout=15
        )
        if r.stdout.strip() == "202":
            submitted[ext] += 1
            total_submitted += 1
    except Exception:
        pass

proc.terminate()
proc.wait()

print(flush=True)
print("=" * 60, flush=True)
print(f"Scanned {total_scanned} files total", flush=True)
print(f"Submitted {total_submitted} validated samples", flush=True)
print(flush=True)
for ext in sorted(WANTED_EXTS):
    c = counts.get(ext, 0)
    s = submitted.get(ext, 0)
    r = rejected.get(ext, 0)
    print(f"  {ext:5s}: found={c:3d}  submitted={s:3d}  rejected_mime={r:3d}", flush=True)
