"""HTTP entry point for the Agent service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Local development keeps Agent credentials in services/agent/.env. Production
# deployments inject environment variables directly; load_dotenv never
# overrides values already supplied by the process environment.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from fastapi import FastAPI  # noqa: E402

from app.config import settings  # noqa: E402
from app.container import build_container  # noqa: E402
from app.routers import health, tasks  # noqa: E402

logger = logging.getLogger("agent.main")


@asynccontextmanager
async def lifespan(application: FastAPI):
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    container = build_container(settings)
    tasks.set_container(container)
    logger.info("agent api started with %s store", type(container.store).__name__)
    try:
        yield
    finally:
        container.close()


app = FastAPI(title="info-agent-agent", version="0.1.0", lifespan=lifespan)

app.include_router(health.router)
app.include_router(tasks.router)
