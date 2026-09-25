from pathlib import Path

from dotenv import load_dotenv

# Local development keeps service credentials in services/rag/.env. Production
# deployments inject environment variables directly; load_dotenv never
# overrides values already supplied by the process environment.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from fastapi import FastAPI

from app.routers import api, health, search
from app.routers import contact_profile

app = FastAPI(title="info-agent-rag", version="0.1.0")
app.include_router(health.router)
app.include_router(search.router)
app.include_router(api.router)
app.include_router(contact_profile.router)
