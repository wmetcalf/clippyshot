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
    assert "CLIPPYSHOT_DATABASE_URL=postgresql://" in compose


def test_compose_stack_keeps_api_socket_free_and_on_shared_storage():
    compose = Path("deploy/docker/docker-compose.yml").read_text()
    api = _api_block(compose)

    assert "clippyshot-data:/var/lib/clippyshot" in api
    assert "/var/run/docker.sock" not in api
    assert "ports:" in api
    assert '"${CLIPPYSHOT_PORT:-8001}:8000"' in compose
    assert 'command: ["serve", "--host", "0.0.0.0", "--port", "8000", "--job-store", "sql"]' in compose
    assert api.count('- backend') == 1
    assert api.count('- frontend') == 1


def test_compose_stack_gives_dispatcher_the_docker_socket_and_worker_image():
    compose = Path("deploy/docker/docker-compose.yml").read_text()
    dispatcher = _dispatcher_block(compose)

    assert compose.count("/var/run/docker.sock:/var/run/docker.sock") == 1
    assert "clippyshot-data:/var/lib/clippyshot" in dispatcher
    assert "/var/run/docker.sock:/var/run/docker.sock" in dispatcher
    assert 'group_add:' in dispatcher
    assert '"${DOCKER_GID:-984}"' in dispatcher
    assert 'test: ["CMD", "docker", "info"]' in dispatcher
    assert "ports:" not in dispatcher
    assert "CLIPPYSHOT_WORKER_IMAGE=" in dispatcher
    assert "image: ${CLIPPYSHOT_IMAGE:-clippyshot:dev}" in compose
    assert "/opt/clippyshot/bin/python" in dispatcher
    assert "CLIPPYSHOT_DISPATCH_INTERVAL=" in dispatcher
    assert "CLIPPYSHOT_DISPATCH_CONCURRENCY=" in dispatcher
    assert "clippyshot.dispatcher" in dispatcher
    assert "SqlJobStore(" in dispatcher
