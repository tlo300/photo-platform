"""Celery application instance.

Configured to use Redis as both the broker and the result backend.
The broker URL is read from settings.redis_url so no separate env var
is needed beyond what the API already requires.

Import tasks are auto-discovered from app.worker.takeout_tasks.
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "photo_platform",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.takeout_tasks", "app.worker.thumbnail_tasks", "app.worker.metadata_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Discard results after 1 hour — callers poll /import/jobs/{id} instead
    result_expires=3600,
)
