from fastapi import FastAPI

from app.routers import health, search

app = FastAPI(title="info-agent-rag", version="0.1.0")
app.include_router(health.router)
app.include_router(search.router)
