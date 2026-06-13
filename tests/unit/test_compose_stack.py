"""The compose stack runs ClippyShot ON blastbox.host.

`api` → `blastbox serve` (ingress + ClippyShot extension); `dispatcher` →
`blastbox dispatch` (claims jobs, launches the cold-worker image); `postgres`
provides pg_bktree for /v1/similar. All host config is `BLASTBOX_*`.
"""
from pathlib import Path


def _api_block(compose: str) -> str:
    return compose.split("\n  api:\n", 1)[1].split("\n  dispatcher:\n", 1)[0]


def _dispatcher_block(compose: str) -> str:
    return compose.split("\n  dispatcher:\n", 1)[1].split("\nnetworks:\n", 1)[0]


def test_compose_stack_has_first_class_api_dispatcher_and_postgres_roles():
    compose = Path("deploy/docker/docker-compose.yml").read_text()

    assert "api:" in compose
    assert "dispatcher:" in compose
    assert "postgres:" in compose
    assert "app:" not in compose
    assert "internal: true" in compose
    assert "frontend:" in compose
    assert "5432:" not in compose
    # Host config is BLASTBOX_*, not the retired CLIPPYSHOT_* host vars.
    assert "BLASTBOX_DATABASE_URL=postgresql://" in compose
    assert "CLIPPYSHOT_DATABASE_URL" not in compose


def test_compose_stack_api_runs_blastbox_serve_socket_free_on_shared_storage():
    compose = Path("deploy/docker/docker-compose.yml").read_text()
    api = _api_block(compose)

    # job_root is a host-CONSISTENT bind (host path == container path), NOT a
    # named volume — so dispatcher-launched workers can bind-mount job dirs by
    # host path. (See the docker-in-docker note in the compose.)
    assert "${CLIPPYSHOT_DATA_DIR:-/var/lib/clippyshot}:${CLIPPYSHOT_DATA_DIR:-/var/lib/clippyshot}" in api
    assert "clippyshot-data:" not in compose
    assert "/var/run/docker.sock" not in api
    assert "ports:" in api
    assert '"${CLIPPYSHOT_PORT:-8001}:8000"' in compose
    # ingress = blastbox serve + the ClippyShot extension (routes + web UI)
    assert 'command: ["blastbox", "serve", "--host", "0.0.0.0", "--port", "8000"]' in api
    assert "BLASTBOX_ALLOWED_ENGINES=clippyshot" in api
    assert "BLASTBOX_INGRESS_EXTENSION=clippyshot.blastbox_ingress:make_extension" in api
    assert api.count('- backend') == 1
    assert api.count('- frontend') == 1


def test_compose_stack_dispatcher_runs_blastbox_dispatch_with_socket_and_worker_image():
    compose = Path("deploy/docker/docker-compose.yml").read_text()
    dispatcher = _dispatcher_block(compose)

    assert compose.count("/var/run/docker.sock:/var/run/docker.sock") == 1
    assert "${CLIPPYSHOT_DATA_DIR:-/var/lib/clippyshot}:${CLIPPYSHOT_DATA_DIR:-/var/lib/clippyshot}" in dispatcher
    assert "/var/run/docker.sock:/var/run/docker.sock" in dispatcher
    assert 'group_add:' in dispatcher
    assert '"${DOCKER_GID:-984}"' in dispatcher
    assert 'test: ["CMD", "docker", "info"]' in dispatcher
    assert "ports:" not in dispatcher
    assert "image: ${CLIPPYSHOT_IMAGE:-clippyshot:dev}" in compose
    # dispatch = blastbox dispatch; the worker image is the cold-worker overlay
    assert 'command: ["blastbox", "dispatch"]' in dispatcher
    assert "BLASTBOX_ENGINES=clippyshot=${CLIPPYSHOT_WORKER_IMAGE:-clippyshot-cold-worker:dev}" in dispatcher
    assert "BLASTBOX_DISPATCH_CONCURRENCY=" in dispatcher
    # fail-closed worker runtime policy is exposed
    assert "BLASTBOX_WORKER_RUNTIME=" in dispatcher
    assert "BLASTBOX_ALLOW_RUNC=" in dispatcher
    # the retired bespoke-dispatcher inline python is gone
    assert "clippyshot.dispatcher" not in dispatcher
    assert "SqlJobStore(" not in dispatcher


def test_compose_gvisor_sidecar_has_operator_memory_ceiling():
    """The gVisor warm sidecar must expose an operator memory ceiling (the host memory
    cgroup GvisorConfig defers to — it deliberately doesn't RLIMIT_AS the worker tree).
    cold (BLASTBOX_WORKER_MEMORY) and FC (BLASTBOX_FC_MEM_MIB) are already bounded; without
    this, gVisor warm runs unbounded. Default 0 = unbounded so existing deploys don't
    regress, but the knob is wired and documented."""
    compose = Path("deploy/docker/docker-compose.gvisor.yml").read_text()
    assert "mem_limit: ${CLIPPYSHOT_GVISOR_MEMORY:-0}" in compose
