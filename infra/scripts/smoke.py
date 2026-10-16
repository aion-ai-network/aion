"""Verify an already running API, database, scheduler and real Celery worker."""
import argparse
import json
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

from aion.jobs.celery_app import celery_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--wait-seconds", type=float, default=0)
    args = parser.parse_args()
    deadline = time.monotonic() + args.wait_seconds
    while True:
        try:
            with urlopen(f"{args.api}/api/v1/system/status", timeout=6) as response:
                current = json.load(response)
            if current["operational"]:
                break
        except (URLError, TimeoutError, OSError):
            pass
        if time.monotonic() >= deadline:
            raise SystemExit("Timed out waiting for the full runtime to become operational")
        time.sleep(2)
    with urlopen(f"{args.api}/health/ready", timeout=10) as response:
        status = json.load(response)
    if not status["ready"]:
        raise SystemExit("API dependencies are not ready")
    nonce = uuid4().hex
    result = celery_app.send_task("aion.system.probe", args=[nonce], expires=30, retry=False)
    try:
        received = result.get(timeout=30)
        if received != {"status": "ok", "nonce": nonce}:
            raise SystemExit("Worker returned an unexpected result")
    finally:
        result.forget()
    with urlopen(f"{args.api}/api/v1/system/status", timeout=10) as response:
        current = json.load(response)
    if not current["operational"]:
        raise SystemExit("Worker completed the probe, but the scheduler is not operational")
    print("OK: API, migrations, PostgreSQL, Redis, scheduler and worker")


if __name__ == "__main__":
    main()
