from socket import gethostname
from typing import Literal

from aion.db.connection import record_heartbeat, sync_connection
from aion.jobs.celery_app import celery_app
from aion.settings import get_settings


@celery_app.task(name="aion.system.probe")
def probe(nonce: str = "") -> dict[str, str]:
    with sync_connection(get_settings()) as connection:
        record_heartbeat(connection, "worker", gethostname())
        connection.commit()
    return {"status": "ok", "nonce": nonce}


@celery_app.task(name="aion.jobs.execute", ignore_result=True, soft_time_limit=90, time_limit=100)
def execute(job_id: str, user: str, token: str) -> None:
    from aion.jobs.runner import run
    run(job_id, user, token)


@celery_app.task(name="aion.system.queue_probe")
def queue_probe(component: Literal["worker_ai", "worker_delivery"]) -> None:
    with sync_connection(get_settings()) as connection:
        record_heartbeat(connection, component, gethostname())
        connection.commit()
