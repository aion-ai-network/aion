"""Consistent local-volume backup and restore drill in a newly created test database.

Uses docker compose, not host PostgreSQL tools. No source database is ever restored over.
Pause writers for a coordinated backup; the command refuses running writers.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def digest_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "verify"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--compose", default="infra/compose.dev.yaml")
    parser.add_argument("--project", default="aion-dev")
    args = parser.parse_args()
    folder = args.directory.resolve()
    compose = ["docker", "compose", "-p", args.project, "-f", args.compose]

    def run(cmd, **kw):
        return subprocess.run(cmd, cwd=ROOT, check=True, **kw)

    def postgres(cmd, **kw):
        return run([*compose, "exec", "-T", "postgres", *cmd], **kw)

    def sql(db, query):
        return postgres(
            [
                "psql",
                "-U",
                "postgres",
                "-d",
                db,
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                query,
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()

    if args.command == "create":
        running = run(
            [*compose, "ps", "--services", "--status", "running"],
            capture_output=True,
            text=True,
        )
        writers = {"api", "worker", "worker-ai", "worker-delivery", "scheduler"}
        if writers.intersection(running.stdout.splitlines()):
            parser.error(
                "Stop api, worker, worker-ai, worker-delivery and scheduler first."
            )
        if os.getenv("AION_STORAGE_BACKEND", "local") != "local":
            parser.error(
                "This command backs up local volumes. Use S3 versioned backup for S3 mode."
            )
        folder.mkdir(parents=True, exist_ok=False)
        source = os.getenv("AION_DB_NAME", "aion")
        if not re.fullmatch(r"[a-zA-Z0-9_]+", source):
            parser.error("Unsupported database name")
        with (folder / "database.dump").open("wb") as out:
            postgres(["pg_dump", "-U", "postgres", "-d", source, "-Fc"], stdout=out)
        # The read-only helper only archives the private volume, never reads host directories.
        with (folder / "private.tar").open("wb") as out:
            run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--entrypoint",
                    "tar",
                    "api",
                    "-C",
                    "/data/private",
                    "-cf",
                    "-",
                    ".",
                ],
                stdout=out,
            )
        tables = json.loads(
            sql(
                source,
                "SELECT json_agg(tablename) FROM pg_tables WHERE schemaname='app'",
            )
        )
        counts = {
            t: int(sql(source, f'SELECT count(*) FROM app."{t}"')) for t in tables
        }
        hashes = {p.name: digest_file(p) for p in folder.iterdir()}
        (folder / "manifest.json").write_text(
            json.dumps(
                {
                    "created_at": datetime.now(UTC).isoformat(),
                    "source_db": source,
                    "counts": counts,
                    "sha256": hashes,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Backup created: {folder}")
    else:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        for name, digest in manifest["sha256"].items():
            if name not in ("database.dump", "private.tar"):
                raise ValueError("Unexpected backup member")
            if digest_file(folder / name) != digest:
                raise ValueError("Backup checksum mismatch")
        target = f"aion_restore_{uuid4().hex[:12]}_test"
        assert re.fullmatch(r"aion_restore_[a-f0-9]{12}_test", target)
        postgres(["createdb", "-U", "postgres", target])
        try:
            with (folder / "database.dump").open("rb") as source:
                postgres(
                    ["pg_restore", "-U", "postgres", "--exit-on-error", "-d", target],
                    stdin=source,
                )
            for table, expected in manifest["counts"].items():
                if not re.fullmatch(r"[a-z_]+", table):
                    raise ValueError("Invalid table name in manifest")
                if int(sql(target, f'SELECT count(*) FROM app."{table}"')) != expected:
                    raise ValueError(f"Restored row count mismatch: {table}")
            for table in (
                "tasks",
                "articles",
                "memories",
                "messages",
                "user_profiles",
                "sessions",
            ):
                visible = sql(
                    target,
                    "SET ROLE aion_runtime; " + f"SELECT count(*) FROM app.{table}",
                )
                if visible.splitlines()[-1] != "0":
                    raise ValueError(f"Restored tenant boundary failed: {table}")
            keys = json.loads(
                sql(
                    target,
                    "SELECT coalesce(json_agg(storage_key),'[]') FROM "
                    "(SELECT storage_key FROM app.attachments WHERE state='ready' UNION ALL "
                    "SELECT storage_key FROM app.exports WHERE state='ready' AND storage_key IS NOT NULL "
                    "UNION ALL SELECT source_key FROM app.messages WHERE source_key IS NOT NULL) k",
                )
            )
            with tarfile.open(folder / "private.tar") as archive:
                members = {m.name.removeprefix("./"): m for m in archive.getmembers()}
                for key in keys:
                    member = members.get(key)
                    if (
                        member is None
                        or not member.isfile()
                        or member.issym()
                        or member.islnk()
                    ):
                        raise ValueError(
                            "Referenced private object missing from backup"
                        )
                    stream = archive.extractfile(member)
                    assert stream
                    while stream.read(1024 * 1024):
                        pass
            print(
                f"Restore verified: {len(manifest['counts'])} tables, {len(keys)} private objects."
            )
        finally:
            # Exact newly created test target only; never accept a restore target from user input.
            postgres(["dropdb", "-U", "postgres", "--force", target])


if __name__ == "__main__":
    main()
