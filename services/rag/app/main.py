from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Local development keeps service credentials in services/rag/.env. Production
# deployments inject environment variables directly; load_dotenv never
# overrides values already supplied by the process environment.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from fastapi import FastAPI

from app.application.bootstrap import build_container
from app.config import settings
from app.routers import admin, api, contact_profile, health, search

settings.validate_mvp()


@asynccontextmanager
async def lifespan(app: FastAPI):
    container = build_container()
    app.state.container = container
    try:
        yield
    finally:
        container.close()


app = FastAPI(title="info-agent-rag", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(search.router)
app.include_router(api.router)
app.include_router(contact_profile.router)
app.include_router(admin.router)
