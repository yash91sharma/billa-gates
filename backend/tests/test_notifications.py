"""Tests for the ntfy publish format in notifications.send_notification.

ntfy has two publish styles:
  1. POST to ``{server}/{topic}`` — the request *body* is the raw message.
  2. POST JSON to the server *root* — topic/title/message are payload fields.

Posting a JSON object to ``{server}/{topic}`` (the old behavior) delivers
the serialized JSON blob as the literal message text with no title. The
sender must therefore use style 2: root URL, topic inside the payload.
"""

import sys
from unittest.mock import MagicMock, patch

from app.services import notifications


class _FakeResp:
    def raise_for_status(self) -> None:
        return None


def _make_fake_httpx(sent: list):
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a, **kw):
            return False

        async def post(self, url: str, headers=None, json=None):
            sent.append({"url": url, "headers": headers or {}, "json": json})
            return _FakeResp()

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeClient
    return fake_httpx


async def test_send_notification_posts_json_to_server_root():
    """The POST must target the server root — not {server}/{topic} — with the
    topic carried inside the JSON payload, so ntfy renders title and message
    as a proper notification instead of a raw JSON blob."""
    sent: list = []
    with patch.dict(sys.modules, {"httpx": _make_fake_httpx(sent)}):
        await notifications.send_notification(
            "https://ntfy.example",
            "backups",
            "Backup succeeded: NAS",
            "Duration: 42s",
        )

    assert len(sent) == 1
    assert sent[0]["url"] == "https://ntfy.example"
    assert sent[0]["json"] == {
        "topic": "backups",
        "title": "Backup succeeded: NAS",
        "message": "Duration: 42s",
    }


async def test_send_notification_strips_trailing_slash_from_server_url():
    sent: list = []
    with patch.dict(sys.modules, {"httpx": _make_fake_httpx(sent)}):
        await notifications.send_notification(
            "https://ntfy.example/",
            "backups",
            "t",
            "m",
        )

    assert sent[0]["url"] == "https://ntfy.example"


async def test_send_notification_sends_bearer_token_header():
    sent: list = []
    with patch.dict(sys.modules, {"httpx": _make_fake_httpx(sent)}):
        await notifications.send_notification(
            "https://ntfy.example",
            "backups",
            "t",
            "m",
            token="tk_secret",
        )

    assert sent[0]["headers"].get("Authorization") == "Bearer tk_secret"


async def test_send_notification_noop_without_topic_or_server():
    sent: list = []
    with patch.dict(sys.modules, {"httpx": _make_fake_httpx(sent)}):
        await notifications.send_notification("https://ntfy.example", "", "t", "m")
        await notifications.send_notification(None, "backups", "t", "m")

    assert sent == []
