"""Run M1 integration tests using separate PostgreSQL/Redis volumes and ports."""
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    repository = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "AION_INTEGRATION": "1",
        "AION_DB_HOST": "127.0.0.1",
        "AION_DB_NAME": "aion_test",
        "AION_DB_PORT": "55432",
        "AION_DB_USER": "aion_runtime",
        "AION_DB_PASSWORD": "local_runtime_password",
        "AION_DB_MIGRATION_USER": "aion_migrator",
        "AION_DB_MIGRATION_PASSWORD": "local_migration_password",
        "AION_REDIS_PORT": "56379",
        "AION_REDIS_URL": "redis://127.0.0.1:56379/0",
    }
    compose = ["docker", "compose", "-p", "aion-test", "-f", "infra/compose.dev.yaml"]

    def run(command: list[str]) -> None:
        subprocess.run(command, cwd=repository, env=environment, check=True)

    try:
        run([*compose, "up", "-d", "--wait", "postgres", "redis"])
        run([sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"])
        run([sys.executable, "-m", "pytest", "backend/tests", "-m", "integration", "-q"])
    finally:
        # Keep test volumes for repeat runs; never remove development data.
        subprocess.run([*compose, "down"], cwd=repository, env=environment, check=False)


if __name__ == "__main__":
    main()
