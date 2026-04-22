#!/usr/bin/env bash
set -e

CORPUS_DIR="./tests/fixtures/corpus"

if [ ! -d "$CORPUS_DIR" ] || [ -z "$(ls -A "$CORPUS_DIR" 2>/dev/null)" ]; then
    echo "ERROR: No corpus at $CORPUS_DIR — run scripts/collect_and_submit.py first"
    exit 1
fi

SAMPLE_COUNT=$(ls "$CORPUS_DIR" | wc -l)
echo "==> Corpus: $SAMPLE_COUNT files in $CORPUS_DIR"
ls "$CORPUS_DIR" | awk -F. '{print tolower($NF)}' | sort | uniq -c | sort -rn

echo
echo "==> Building ClippyShot container..."
docker build -f deploy/docker/Dockerfile -t clippyshot:test .

echo
echo "==> Starting container on port 8000..."
docker stop clippyshot-test-server 2>/dev/null || true
docker rm clippyshot-test-server 2>/dev/null || true

docker run -d \
    --name clippyshot-test-server \
    --read-only \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    -e CLIPPYSHOT_WARN_ON_INSECURE=1 \
    -e CLIPPYSHOT_JOB_RETENTION_SECONDS=7200 \
    --tmpfs /tmp:rw,exec,nosuid,size=2g \
    --tmpfs /var/lib/clippyshot:rw,nosuid,size=1g,uid=10001,gid=10001 \
    -p 8000:8000 \
    clippyshot:test serve --host 0.0.0.0 --port 8000

echo "==> Waiting for API to become ready..."
while ! curl -s http://localhost:8000/v1/readyz > /dev/null; do
    sleep 1
done

echo
echo "==> Submitting $SAMPLE_COUNT files from local corpus..."
ok=0; fail=0
for file in "$CORPUS_DIR"/*; do
    [ -e "$file" ] || continue
    status=$(curl -s -o /dev/null -w "%{http_code}" -X POST -F "file=@\"${file}\"" http://localhost:8000/v1/jobs)
    if [ "$status" = "202" ]; then
        ok=$((ok+1))
    else
        fail=$((fail+1))
        echo "  WARN: $(basename "$file") => HTTP $status"
    fi
done

echo
echo "================================================================"
echo "Submitted $ok files ($fail rejected at upload)"
echo "Container is running — review results at:"
echo "    http://localhost:8000/"
echo "================================================================"
