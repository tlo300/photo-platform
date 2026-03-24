from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.auth import router as auth_router
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import configure_logging
from app.services.storage import storage_service

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage_service.ensure_bucket_exists()
    yield


app = FastAPI(title="Photo Platform API", lifespan=lifespan, root_path="/api")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
