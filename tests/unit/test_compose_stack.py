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
    # GID is fail-loud (no silent :-984 default): unset -> compose errors, run node_env_sync.sh
    assert '"${DOCKER_GID:?' in dispatcher
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


def test_tier_routing_env_wired_in_compose():
    """Tier routing is an operator/test knob: the API gates submit on BLASTBOX_ALLOW_TIER_ROUTING
    (default off), and every dispatcher carries BLASTBOX_MAX_QUEUED_AGE_S so a job pinned to a
    down tier can't accumulate. Default-off values → no behavior change unless an operator opts in."""
    base = Path("deploy/docker/docker-compose.yml").read_text(encoding="utf-8")
    assert "BLASTBOX_ALLOW_TIER_ROUTING=${BLASTBOX_ALLOW_TIER_ROUTING:-}" in base
    for fname in ("docker-compose.yml", "docker-compose.firecracker.yml", "docker-compose.gvisor.yml"):
        compose = Path(f"deploy/docker/{fname}").read_text(encoding="utf-8")
        assert "BLASTBOX_MAX_QUEUED_AGE_S=${BLASTBOX_MAX_QUEUED_AGE_S:-0}" in compose, fname


def test_all_dispatchers_declare_clippyshot_reserved_keys():
    """blastbox core is engine-agnostic; ClippyShot declares its OWN security-posture knobs
    as reserved here. Every dispatcher (cold + FC + gVisor warm sidecars) MUST set
    BLASTBOX_ENGINE_CLIPPYSHOT_RESERVED_KEYS with the 4 sandbox/insecure/disclose/warm-diag
    keys, dropped unconditionally from client job.params — a warm tier missing it would let a
    client flip that worker's posture."""
    must = {
        "CLIPPYSHOT_WARM_DIAG_FILE", "CLIPPYSHOT_SANDBOX",
        "CLIPPYSHOT_WARN_ON_INSECURE", "CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS",
    }
    for fname in (
        "docker-compose.yml",
        "docker-compose.firecracker.yml",
        "docker-compose.gvisor.yml",
    ):
        compose = Path(f"deploy/docker/{fname}").read_text(encoding="utf-8")
        assert "BLASTBOX_ENGINE_CLIPPYSHOT_RESERVED_KEYS=" in compose, fname
        for key in must:
            assert key in compose, f"{key} not reserved in {fname}"


def test_firecracker_dispatcher_declares_both_kvm_and_docker_gids():
    """Compose overlays REPLACE list values, so the FC dispatcher's group_add must repeat
    the docker gid it inherits from the base compose — else it has /dev/kvm but no docker
    socket, falls back to insecure runc, REQUIRE_SECURE_RUNTIME refuses every job, and the
    Firecracker tier silently never launches (the gVisor tier absorbs the whole engine)."""
    compose = Path("deploy/docker/docker-compose.firecracker.yml").read_text(encoding="utf-8")
    fc = compose.split("group_add:", 1)[1].split("environment:", 1)[0]
    assert "KVM_GID:?" in fc, "FC dispatcher must fail loud on unset KVM_GID"
    assert "DOCKER_GID:?" in fc, "FC dispatcher must repeat DOCKER_GID (overlays drop it)"
