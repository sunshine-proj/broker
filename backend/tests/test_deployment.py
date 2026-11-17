"""Deployment tooling (Phase 11): the .env generator and production checker, the
Compose files, and the backup metrics."""

import importlib.util
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from prometheus_client.parser import text_string_to_metric_families

from app.monitoring import database_metrics
from app.monitoring.database_metrics import BackupState, DatabaseSnapshot

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY / "scripts"


def load(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


env_file = load("env_file")
check_env = load("check_env")
generate_env = load("generate_env")

GOOD = {
    "COMPOSE_FILE": "docker-compose.yml:docker-compose.prod.yml",
    "DOMAIN": "data.example.org",
    "ACME_EMAIL": "ops@example.org",
    "POSTGRES_DB": "marketdata",
    "POSTGRES_USER": "marketdata",
    "POSTGRES_PASSWORD": "Kq3vR8nZ1xLp9sT4yW2bH7cJ",
    "BACKEND_DB_PASSWORD": "Mf6gD2hS9kQ1wE5rT8yU3iO7",
    "GRAFANA_ADMIN_USER": "operator",
    "GRAFANA_ADMIN_PASSWORD": "Zx4cV7bN1mQ8wE3rT6yU9iO2",
    "GRAFANA_DB_PASSWORD": "Pl0oK9iJ8uH7yG6tF5rD4eS3",
    "ADMIN_API_KEY": "",
    "TWELVEDATA_API_KEY": "",
}


# --- .env parsing ---------------------------------------------------------------------


def test_env_values_are_read_as_compose_reads_them() -> None:
    values = env_file.parse("# comment\nA=1\nB='$2a$14$x y'\nC=\"quoted\"\n  # D=not a value\nE=\n")
    assert values == {"A": "1", "B": "$2a$14$x y", "C": "quoted", "E": ""}


def test_values_with_dollars_are_single_quoted() -> None:
    assert env_file.quote("plain-value_1") == "plain-value_1"
    assert env_file.quote("$2a$14$hash") == "'$2a$14$hash'"


# --- Production check -----------------------------------------------------------------


def test_a_complete_production_env_passes() -> None:
    assert check_env.problems(GOOD, 0o600) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"COMPOSE_FILE": ""}, "COMPOSE_FILE must be"),
        ({"DOMAIN": "localhost"}, "DOMAIN must be a public host name"),
        ({"ACME_EMAIL": "nobody"}, "ACME_EMAIL is not an email address"),
        ({"API_PASSWORD_HASH": "$2a$14$x"}, "API_PASSWORD_HASH is no longer used"),
        ({"POSTGRES_PASSWORD": "change_me"}, "POSTGRES_PASSWORD is a placeholder"),
        ({"GRAFANA_DB_PASSWORD": ""}, "GRAFANA_DB_PASSWORD is empty"),
        ({"BACKEND_DB_PASSWORD": "short1234"}, "shorter than 20"),
        (
            {"GRAFANA_DB_PASSWORD": GOOD["POSTGRES_PASSWORD"]},
            "GRAFANA_DB_PASSWORD is the same as POSTGRES_PASSWORD",
        ),
        ({"GRAFANA_ADMIN_USER": "admin"}, "well-known 'admin'"),
        ({"ADMIN_API_KEY": "abc"}, "ADMIN_API_KEY must be empty"),
        ({"TWELVEDATA_API_KEY": "change_me"}, "TWELVEDATA_API_KEY holds a placeholder"),
        ({"BIND_ADDRESS": "0.0.0.0"}, "BIND_ADDRESS should stay 127.0.0.1"),
    ],
)
def test_production_problems_are_reported(change: dict[str, str], problem: str) -> None:
    found = check_env.problems(GOOD | change, 0o600)

    assert any(problem in p for p in found), found


def test_a_file_others_can_read_is_reported() -> None:
    assert any("chmod 600" in p for p in check_env.problems(GOOD, 0o644))


def test_the_shipped_example_is_not_deployable_as_it_is() -> None:
    values = env_file.read(REPOSITORY / ".env.example")

    # No password ships with a value.
    for key in check_env.PASSWORDS:
        assert values[key] == "", key
    assert check_env.problems(values, 0o600)


# --- Generation -----------------------------------------------------------------------


def test_generated_development_env_has_distinct_strong_secrets() -> None:
    values = env_file.parse(generate_env.generate(False, None, None))

    secrets = [values[key] for key in generate_env.GENERATED]
    assert len(set(secrets)) == len(secrets)
    assert all(len(secret) >= 40 for secret in secrets)
    # Everything else is taken from the example.
    assert values["POSTGRES_DB"] == "marketdata"
    assert values["BACKUP_KEEP"] == "7"
    assert "COMPOSE_FILE" not in values


def test_generated_production_env_passes_the_check() -> None:
    text = generate_env.generate(True, "data.example.org", "ops@example.org")
    values = env_file.parse(text)
    values["GRAFANA_ADMIN_USER"] = "operator"

    assert check_env.problems(values, 0o600) == []
    # No shared account: users sign in with their own (PRD FR-017).
    assert "API_PASSWORD_HASH" not in values


# --- Compose -----------------------------------------------------------------------------


def compose(name: str) -> str:
    """A Compose file without its comments."""
    text = (REPOSITORY / name).read_text()
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def services(name: str) -> dict[str, str]:
    """Each service's block of a Compose file, by name."""
    body = compose(name).split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    blocks = re.split(r"^  ([a-z_]+):$", body, flags=re.MULTILINE)[1:]
    return dict(zip(blocks[::2], blocks[1::2], strict=True))


def test_the_base_stack_publishes_no_ports() -> None:
    assert not re.search(r"^\s+ports:", compose("docker-compose.yml"), re.MULTILINE)
    # In production only the proxy publishes ports.
    assert [
        name for name, block in services("docker-compose.prod.yml").items() if "ports:" in block
    ] == ["proxy"]
    assert re.search(r"internal:\n\s+internal: true", compose("docker-compose.yml"))


def test_the_database_is_on_the_internal_network_only() -> None:
    db = services("docker-compose.yml")["db"]

    assert db.rstrip().endswith("networks:\n      - internal")


def test_every_service_has_limits_logging_and_a_restart_policy() -> None:
    base = services("docker-compose.yml")

    assert set(base) == {
        "db",
        "migrate",
        "backend",
        "grafana",
        "prometheus",
        "alertmanager",
        "backup",
    }
    for name, block in base.items():
        assert "memory:" in block, name
        assert "logging: *logging" in block, name
        assert "restart:" in block, name


def test_the_backend_connects_as_its_own_role() -> None:
    backend = services("docker-compose.yml")["backend"]

    assert "POSTGRES_USER: ${BACKEND_DB_USER:-marketdata_app}" in backend
    assert "app-role" in services("docker-compose.yml")["migrate"]


def test_production_grafana_trusts_only_the_proxy() -> None:
    """Grafana takes the signed-in user from the proxy, and from nobody else (PRD FR-017)."""
    prod = services("docker-compose.prod.yml")
    caddyfile = (REPOSITORY / "proxy" / "Caddyfile").read_text()

    assert 'GF_AUTH_PROXY_ENABLED: "true"' in prod["grafana"]
    assert "GF_AUTH_PROXY_WHITELIST: ${AUTH_PROXY_ADDRESS:-172.31.254.2}" in prod["grafana"]
    assert 'GF_AUTH_DISABLE_LOGIN_FORM: "true"' in prod["grafana"]
    assert "ipv4_address: ${AUTH_PROXY_ADDRESS:-172.31.254.2}" in prod["proxy"]
    assert 'SESSION_COOKIE_SECURE: "true"' in prod["backend"]
    # Client-sent user headers are dropped before the backend's answer is copied in,
    # and Grafana is reached on the dedicated network only.
    assert caddyfile.index("request_header -X-Webauth-User") < caddyfile.index(
        "forward_auth backend:8000"
    )
    assert "uri /auth/verify" in caddyfile
    assert "reverse_proxy grafana-auth:3000" in caddyfile
    assert "basic_auth" not in caddyfile


def test_backup_and_restore_scripts_exist_and_are_executable() -> None:
    for path in (
        "backup/run-backup.sh",
        "backup/loop.sh",
        "scripts/restore.sh",
        "scripts/test_backup_restore.sh",
    ):
        mode = (REPOSITORY / path).stat().st_mode
        assert mode & stat.S_IXUSR, path


# --- Backup metrics -----------------------------------------------------------------------


def test_backup_state_is_exported_once_there_is_a_backup() -> None:
    finished = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
    snapshot = DatabaseSnapshot(backup=BackupState(False, 86400, finished, 123456))

    samples = {
        sample.name: sample.value
        for family in text_string_to_metric_families(database_metrics.render(snapshot).decode())
        for sample in family.samples
    }

    assert samples["marketdata_backup_last_success_timestamp_seconds"] == finished.timestamp()
    assert samples["marketdata_backup_last_size_bytes"] == 123456
    assert samples["marketdata_backup_last_failed"] == 0
    assert samples["marketdata_backup_expected_interval_seconds"] == 86400
    without = database_metrics.render(DatabaseSnapshot()).decode()
    assert "marketdata_backup_" not in without
