"""ntfy push notification helper."""

from typing import Dict, Optional

from app.core.logging import get_logger, log_call

logger = get_logger(__name__)


@log_call
async def send_notification(
    server_url: Optional[str],
    topic: Optional[str],
    title: str,
    message: str,
    token: Optional[str] = None,
) -> None:
    """Send a push notification via ntfy.

    Uses ntfy's JSON publishing endpoint: POST to the server ROOT with the
    topic inside the payload. Posting JSON to ``{server}/{topic}`` would make
    ntfy treat the serialized JSON blob as the literal message body (no
    title) — that endpoint only accepts raw message text.

    Silently no-ops when server_url or topic are empty, so callers do not need
    to guard against unconfigured notification settings. The Authorization
    header is included only when a token is provided.
    """
    if not topic or not server_url:
        logger.debug("notification skipped: empty topic")
        return

    import httpx

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = server_url.rstrip("/")
    logger.info(f"sending notification to {url} topic={topic} title={title}")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json={"topic": topic, "title": title, "message": message},
            )
            resp.raise_for_status()
        logger.info(f"notification sent successfully to {topic}")
    except Exception as exc:
        logger.error(f"notification failed to {topic}: {exc}")
