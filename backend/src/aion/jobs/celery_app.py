from celery import Celery

from aion.settings import get_settings

settings = get_settings()
celery_app = Celery(
    "aion",
    broker=settings.redis_url.get_secret_value(),
    backend=settings.result_backend.get_secret_value(),
    include=["aion.jobs.tasks"],
)
celery_app.conf.update(
    accept_content=["json"], task_serializer="json", result_serializer="json",
    task_default_queue="system", task_track_started=True,
    task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True,
    result_expires=300, timezone="UTC", enable_utc=True, worker_hijack_root_logger=False,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
)
