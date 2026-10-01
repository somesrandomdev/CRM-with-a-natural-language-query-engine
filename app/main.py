"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import auth, leads, query
from pipeline import ingest
from pipeline.worker import run_worker


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    stop = asyncio.Event()
    worker = (
        asyncio.create_task(run_worker(stop, settings.worker_poll_seconds))
        if settings.worker_enabled
        else None
    )
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            await worker


def create_app() -> FastAPI:
    app = FastAPI(title="Clearpipe", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router)
    app.include_router(leads.router)
    app.include_router(query.router)
    app.include_router(ingest.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
