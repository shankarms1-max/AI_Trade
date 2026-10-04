from pathlib import Path

import pytest

from app.core.config import Settings
from scripts.deployment_smoke_test import contains_configured_secret, contains_sensitive_key


ROOT = Path(__file__).resolve().parents[2]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def service_block(compose: str, service: str, next_service: str | None) -> str:
    start = compose.index(f"  {service}:\n")
    end = compose.index(f"  {next_service}:\n", start) if next_service else compose.index("\nvolumes:", start)
    return compose[start:end]


def test_only_caddy_publishes_host_ports() -> None:
    compose = read("docker-compose.prod.yml")
    postgres = service_block(compose, "postgres", "migrate")
    backend = service_block(compose, "backend", "collector")
    collector = service_block(compose, "collector", "frontend")
    frontend = service_block(compose, "frontend", "caddy")
    caddy = service_block(compose, "caddy", None)

    for internal in (postgres, backend, collector, frontend):
        assert "\n    ports:" not in internal
    assert '      - "80:80"' in caddy
    assert '      - "443:443"' in caddy
    assert '      - "443:443/udp"' in caddy
    assert '      - "5432"' in postgres
    assert '      - "8000"' in backend
    assert '      - "3000"' in frontend


def test_production_service_separation_startup_and_persistence() -> None:
    compose = read("docker-compose.prod.yml")
    postgres = service_block(compose, "postgres", "migrate")
    migrate = service_block(compose, "migrate", "backend")
    backend = service_block(compose, "backend", "collector")
    collector = service_block(compose, "collector", "frontend")

    assert "postgres_data:/var/lib/postgresql/data" in postgres
    assert "pg_isready" in postgres
    assert 'restart: "no"' in migrate
    assert '"upgrade", "head"' in migrate
    assert "service_completed_successfully" in backend
    assert "service_completed_successfully" in collector
    assert '"uvicorn"' in backend
    assert '"/app/worker/collector.py"' in collector
    assert "--reload" not in backend
    assert compose.count("restart: unless-stopped") == 5


def test_healthchecks_and_bounded_container_logs_are_configured() -> None:
    compose = read("docker-compose.prod.yml")
    assert "http://127.0.0.1:8000/api/health" in compose
    assert "http://127.0.0.1:3000/" in compose
    assert 'max-size: "10m"' in compose
    assert 'max-file: "5"' in compose


def test_production_defaults_keep_optional_integrations_off() -> None:
    template = read(".env.production.example")
    assert "PIPELINE_AFTER_SNAPSHOT=true" in template
    assert "PIPELINE_RUN_AI_RESEARCH=false" in template
    assert "TELEGRAM_ENABLED=false" in template
    assert "ENABLE_API_DOCS=false" in template
    assert "NEXT_PUBLIC_API_BASE_URL=\n" in template


def test_templates_and_images_do_not_contain_real_secrets() -> None:
    files = (
        ".env.production.example",
        "backend/Dockerfile",
        "frontend/Dockerfile",
        "docker-compose.prod.yml",
        "Caddyfile",
    )
    combined = "\n".join(read(path) for path in files)
    assert "CHANGE_ME_STRONG_PASSWORD" in combined
    assert "KOTAK_CONSUMER_KEY=\n" in combined
    assert "OPENAI_API_KEY=\n" in combined
    assert "TELEGRAM_BOT_TOKEN=\n" in combined
    assert ".env.production" in read(".gitignore")
    assert "COPY .env" not in combined


def test_runtime_code_has_no_broker_order_commands() -> None:
    forbidden = ("place_" + "order", "modify_" + "order", "cancel_" + "order", "square_" + "off")
    runtime_roots = (ROOT / "backend" / "app", ROOT / "worker", ROOT / "scripts")
    for runtime_root in runtime_roots:
        for path in runtime_root.rglob("*.py"):
            if path.name == "deployment_smoke_test.py":
                continue
            content = path.read_text(encoding="utf-8").lower()
            assert not any(name in content for name in forbidden), path


def test_cors_allows_local_and_configured_production_origin() -> None:
    settings = Settings(
        _env_file=None,
        kotak_consumer_key="configured",
        cors_allowed_origins="https://trade.example.com/, https://research.example.com",
    )
    assert settings.configured_cors_origins == (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://trade.example.com",
        "https://research.example.com",
    )


def test_cors_rejects_wildcard() -> None:
    settings = Settings(
        _env_file=None, kotak_consumer_key="configured", cors_allowed_origins="*"
    )
    with pytest.raises(ValueError, match="wildcard"):
        _ = settings.configured_cors_origins


def test_frontend_supports_relative_production_api_and_standalone_output() -> None:
    assert 'NEXT_PUBLIC_API_BASE_URL: ${NEXT_PUBLIC_API_BASE_URL:-}' in read(
        "docker-compose.prod.yml"
    )
    assert 'ARG NEXT_PUBLIC_API_BASE_URL=""' in read("frontend/Dockerfile")
    assert 'output: "standalone"' in read("frontend/next.config.ts")
    assert "`${API_BASE}${path}`" in read("frontend/lib/api.ts")


def test_backup_and_smoke_assets_are_safe_and_documented() -> None:
    backup = read("scripts/backup_postgres.sh")
    smoke = read("scripts/deployment_smoke_test.py")
    readme = read("README.md")
    assert "pg_dump --format=custom" in backup
    assert "BACKUP_RETENTION_DAYS" in backup
    assert "-mtime" in backup
    assert "broker, AI, or order APIs" in smoke
    assert 'fetch(args.base_url, "/api/health")' in smoke
    assert "pg_restore" in readme
    assert "AWS LIGHTSAIL DEPLOYMENT" in readme


def test_smoke_secret_detection_handles_nested_keys_and_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert contains_sensitive_key({"outer": [{"access_token": "not-inspected"}]})
    assert not contains_sensitive_key({"components": {"database": "HEALTHY"}})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "configured-secret-value")
    assert contains_configured_secret('{"unexpected":"configured-secret-value"}')
    assert not contains_configured_secret('{"status":"HEALTHY"}')
