"""FastAPI application entrypoint.

Wires the two adapters onto one service layer and configures structured
JSON logging (the spec's observability requirement: the final collected
payload must reach stdout).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

load_dotenv()

from app.adapters import rest, voice  # noqa: E402
from app.infra.db import engine, init_db  # noqa: E402

# Fields we attach to log records via `extra=`; everything else on a
# LogRecord is noise for structured output.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line - greppable in any hosting provider's logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    init_db()
    logging.getLogger(__name__).info("app.started")
    yield
    engine.dispose()


app = FastAPI(
    title="Voice Patient Intake",
    version="1.0.0",
    description=(
        "Patient registration over the phone. A Vapi voice agent collects "
        "demographics turn by turn; every field is validated and persisted "
        "server-side as it is spoken."
    ),
    lifespan=lifespan,
)

app.include_router(rest.router)
app.include_router(voice.router)


@app.get("/health", tags=["ops"])
def health() -> dict[str, object]:
    """Liveness + database reachability, for the host's health check."""
    from sqlalchemy import text

    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
        logging.getLogger(__name__).exception("health.db_unreachable")

    return {"data": {"status": "ok" if db_ok else "degraded", "database": db_ok},
            "error": None}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Never leak a stack trace to a caller; always keep the envelope shape."""
    logging.getLogger(__name__).exception(
        "request.unhandled_error", extra={"path": request.url.path}
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "data": None,
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred.",
            },
        },
    )
