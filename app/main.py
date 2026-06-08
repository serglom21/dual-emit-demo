"""FastAPI app with Sentry init in the lifespan."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.metrics_helper import configure
from app.routes import critical, high_volume, stress, validation
from app.sentry_config import init_sentry


@asynccontextmanager
async def lifespan(app: FastAPI):
    critical_client = init_sentry()
    configure(critical_client)
    yield


app = FastAPI(title="Dual-Emit Critical Metrics Demo", lifespan=lifespan)
app.include_router(high_volume.router, prefix="/api")
app.include_router(critical.router, prefix="/api/v1")
app.include_router(stress.router)
app.include_router(validation.router)
