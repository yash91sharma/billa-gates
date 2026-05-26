"""FastAPI application factory.

Route registration order matters:
  1. API routers (all /api/* routes).
  2. Static file mount for the built React bundle.
  3. Catch-all SPA route — must be LAST so it does not shadow API routes.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.routes import jobs, mounts, runs
from app.api.routes import settings as settings_router
from app.core.logging import RequestLoggingMiddleware, get_logger, setup_logging
from app.core.scheduler import shutdown_scheduler, start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Run startup tasks (logging, scheduler) then yield for the app lifetime."""
    setup_logging()
    logger = get_logger(__name__)
    logger.info("billa-gates startup: initialising scheduler")
    await start_scheduler()
    logger.info("billa-gates ready")
    yield
    logger.info("billa-gates shutdown: stopping scheduler")
    await shutdown_scheduler()
    logger.info("billa-gates stopped")


app = FastAPI(
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Flatten Pydantic v2 validation errors into a single human-readable string.

    FastAPI's default 422 response has detail as a list; tests expect a string
    so they can do substring checks like 'assert "field" in detail.lower()'.
    """
    errors = exc.errors()
    parts: list[str] = []
    for e in errors:
        # loc is a tuple like ('body', 'field_name') or ('body',) for model errors.
        loc = ".".join(str(s) for s in e["loc"] if s != "body")
        msg = e["msg"]
        parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "; ".join(parts)
    return JSONResponse(status_code=422, content={"detail": detail})


app.add_middleware(RequestLoggingMiddleware)

# ── API routers ───────────────────────────────────────────────────────────────

app.include_router(jobs.router, prefix="/api")
app.include_router(runs.router, prefix="/api")
app.include_router(mounts.router, prefix="/api")
app.include_router(settings_router.router, prefix="/api")

# ── Static files (built React bundle) ────────────────────────────────────────

_STATIC_DIR = Path("/app/static")
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── Catch-all SPA route (must be registered last) ────────────────────────────


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_catch_all(full_path: str) -> Response:
    """Serve index.html for any path not matched by an API route.

    Enables client-side routing in the React SPA.
    """
    index = _STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return Response(status_code=404)
