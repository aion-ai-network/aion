import logging
import signal
from socket import gethostname
from threading import Event
from types import FrameType

from sqlalchemy import text

from aion.db.connection import record_heartbeat, sync_connection
from aion.jobs.celery_app import celery_app
from aion.settings import get_settings

logger = logging.getLogger(__name__)
LOCK_ID = 6490801


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    stopped = Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopped.is_set():
        try:
            with sync_connection(settings) as connection:
                acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"),
                                             {"key": LOCK_ID})
                connection.commit()
                if not acquired:
                    logger.info("Another scheduler holds the leader lock")
                    stopped.wait(settings.scheduler_interval_seconds)
                    continue
                try:
                    while not stopped.is_set():
                        record_heartbeat(connection, "scheduler", gethostname())
                        connection.commit()
                        celery_app.send_task("aion.system.probe", expires=30, retry=False)
                        stopped.wait(settings.scheduler_interval_seconds)
                finally:
                    connection.rollback()
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})
                    connection.commit()
        except Exception as exception:
            logger.error("Scheduler iteration failed: %s", type(exception).__name__)
            stopped.wait(settings.scheduler_interval_seconds)


if __name__ == "__main__":
    main()
