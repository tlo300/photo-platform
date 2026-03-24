import logging
import sys
from contextvars import ContextVar

from pythonjsonlogger.jsonlogger import JsonFormatter

# Stores the current request ID for the duration of a single request.
# Set by RequestIdMiddleware; read by RequestIdFilter.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class _RequestIdFilter(logging.Filter):
    """Stamps every log record with the current request ID."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("")
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter("%(asctime)s %(name)s %(levelname)s %(request_id)s %(message)s")
    )
    handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]
