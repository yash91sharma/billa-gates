"""FastAPI routes for AppSettings, ntfy testing, restic update checks, and health."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas.settings import (
    HealthResponse,
    NtfyTestResult,
    ResticUpdateCheck,
    SettingsResponse,
    SettingsUpdate,
)
from app.core import scheduler as scheduler_module
from app.core.logging import get_logger, log_call
from app.db.models import AppSettings

logger = get_logger(__name__)

router = APIRouter(tags=["settings"])

# GitHub releases API endpoint for restic.
_GITHUB_RESTIC_URL = "https://api.github.com/repos/restic/restic/releases/latest"


@log_call
async def _get_or_create_settings(session: AsyncSession) -> AppSettings:
    """Return AppSettings(id=1), creating it with defaults if it does not exist."""
    settings = await session.get(AppSettings, 1)
    if settings is None:
        settings = AppSettings(
            id=1,
            ntfy_server_url="https://ntfy.sh",
            ntfy_topic="",
            default_job_timeout_hours=24,
        )
        session.add(settings)
        await session.commit()
    return settings


@log_call
def _settings_response(settings: AppSettings) -> dict[str, object]:
    """Build a SettingsResponse dict with ntfy_token always set to None."""
    return {
        "id": settings.id,
        "ntfy_server_url": settings.ntfy_server_url,
        "ntfy_topic": settings.ntfy_topic,
        "ntfy_token": None,  # never returned
        "notify_on_start": settings.notify_on_start,
        "notify_on_success": settings.notify_on_success,
        "notify_on_failure": settings.notify_on_failure,
        "notify_on_warning": settings.notify_on_warning,
        "notify_on_verification": settings.notify_on_verification,
        "default_job_timeout_hours": settings.default_job_timeout_hours,
        "keep_last_runs": settings.keep_last_runs,
        "auto_unlock": settings.auto_unlock,
        "metadata_timeout_seconds": settings.metadata_timeout_seconds,
        "restic_version": settings.restic_version,
    }


# ── GET /api/settings ─────────────────────────────────────────────────────────


@router.get("/settings", response_model=SettingsResponse)
@log_call
async def get_settings(
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Return the current AppSettings, creating the singleton row if needed."""
    settings = await _get_or_create_settings(session)
    return _settings_response(settings)


# ── PUT /api/settings ─────────────────────────────────────────────────────────


@router.put("/settings", response_model=SettingsResponse)
@log_call
async def update_settings(
    body: SettingsUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Upsert AppSettings.  ntfy_token is accepted but never returned."""
    settings = await _get_or_create_settings(session)

    settings.ntfy_server_url = body.ntfy_server_url
    settings.ntfy_topic = body.ntfy_topic
    if body.ntfy_token is not None:
        settings.ntfy_token = body.ntfy_token
    settings.notify_on_start = body.notify_on_start
    settings.notify_on_success = body.notify_on_success
    settings.notify_on_failure = body.notify_on_failure
    settings.notify_on_warning = body.notify_on_warning
    settings.notify_on_verification = body.notify_on_verification
    settings.default_job_timeout_hours = body.default_job_timeout_hours
    settings.keep_last_runs = body.keep_last_runs
    settings.auto_unlock = body.auto_unlock
    settings.metadata_timeout_seconds = body.metadata_timeout_seconds

    await session.commit()
    logger.info(
        "app settings updated ntfy_server=%s ntfy_topic=%s timeout_hours=%s",
        settings.ntfy_server_url,
        settings.ntfy_topic,
        settings.default_job_timeout_hours,
    )
    return _settings_response(settings)


# ── POST /api/settings/test-ntfy ─────────────────────────────────────────────


@router.post("/settings/test-ntfy", response_model=NtfyTestResult)
@log_call
async def test_ntfy(session: AsyncSession = Depends(get_session)) -> NtfyTestResult:
    """Send a test notification via ntfy.

    Returns 422 if no topic is configured.  Any network error is returned as
    ok=False with an error message rather than raising an HTTP exception, so
    the frontend can display a useful message.
    """
    import httpx

    settings = await _get_or_create_settings(session)
    if not settings.ntfy_topic:
        raise HTTPException(
            status_code=422,
            detail="ntfy_topic is not configured",
        )

    # ntfy JSON publishing goes to the server ROOT with the topic in the
    # payload; posting JSON to {server}/{topic} delivers the raw JSON string
    # as the message body with no title (same contract as send_notification).
    url = settings.ntfy_server_url.rstrip("/")
    headers: dict[str, str] = {}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json={
                    "topic": settings.ntfy_topic,
                    "title": "Billa-Gates Test",
                    "message": "Test notification from Billa-Gates",
                },
            )
        if resp.status_code == 200:
            logger.info(
                "ntfy test notification delivered topic=%s", settings.ntfy_topic
            )
            return NtfyTestResult(ok=True)
        logger.warning(
            "ntfy test notification failed status=%s topic=%s",
            resp.status_code,
            settings.ntfy_topic,
        )
        return NtfyTestResult(ok=False, error=f"HTTP {resp.status_code}: {resp.text}")
    except Exception as exc:
        logger.warning("ntfy test notification errored: %s", exc)
        return NtfyTestResult(ok=False, error=str(exc))


# ── GET /api/settings/restic-update-check ────────────────────────────────────


@router.get("/settings/restic-update-check", response_model=ResticUpdateCheck)
@log_call
async def restic_update_check(
    session: AsyncSession = Depends(get_session),
) -> ResticUpdateCheck:
    """Compare the installed restic version against the latest GitHub release.

    Network failures return latest=None, update_available=None instead of an
    error response, so the frontend can degrade gracefully.
    """
    import httpx

    settings = await session.get(AppSettings, 1)
    current = settings.restic_version if settings else None

    if current is None:
        return ResticUpdateCheck(current=None, latest=None, update_available=None)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(_GITHUB_RESTIC_URL)
        tag = resp.json().get("tag_name", "")
        latest = tag.lstrip("v") or None
    except Exception as exc:
        logger.warning("restic update check failed: %s", exc)
        return ResticUpdateCheck(current=current, latest=None, update_available=None)

    if latest is None:
        update_available = None
    else:
        update_available = latest != current

    return ResticUpdateCheck(
        current=current,
        latest=latest,
        update_available=update_available,
    )


# ── GET /api/health ───────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse)
@log_call
async def health(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    """Return scheduler state, restic version, and DB liveness.

    Always returns HTTP 200 — individual fields signal any degradation.
    """
    scheduler_running = scheduler_module.scheduler.running

    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        logger.error("health db check failed: %s", exc)
        db_ok = False

    settings = await session.get(AppSettings, 1)
    restic_version = settings.restic_version if settings else None

    return HealthResponse(
        scheduler_running=scheduler_running,
        restic_version=restic_version,
        db_ok=db_ok,
    )
