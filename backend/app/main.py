import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.admin import router as admin_router
from app.api.albums import router as albums_router
from app.api.assets import router as assets_router
from app.api.auth import router as auth_router
from app.api.import_ import router as import_router
from app.api.map import router as map_router
from app.api.shares import router as shares_router
from app.api.upload import router as upload_router
from app.api.users import router as users_router
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import configure_logging, request_id_var
from app.services.storage import storage_service

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage_service.ensure_bucket_exists()
    yield


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generate a unique ID per request, inject it into the logging context,
    and surface it as an X-Request-ID response header for traceability."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response to protect against common web vulnerabilities."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app = FastAPI(
    title="Photo Platform API",
    lifespan=lifespan,
    root_path="/api",
    # Suppress detailed server info from OpenAPI schema
    servers=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(admin_router)
app.include_router(shares_router)
app.include_router(import_router)
app.include_router(upload_router)
app.include_router(assets_router)
app.include_router(albums_router)
app.include_router(map_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
