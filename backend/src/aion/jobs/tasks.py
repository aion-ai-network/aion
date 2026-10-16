from socket import gethostname

from aion.db.connection import record_heartbeat, sync_connection
from aion.jobs.celery_app import celery_app
from aion.settings import get_settings


@celery_app.task(name="aion.system.probe")
def probe(nonce: str = "") -> dict[str, str]:
    with sync_connection(get_settings()) as connection:
        record_heartbeat(connection, "worker", gethostname())
        connection.commit()
    return {"status": "ok", "nonce": nonce}
