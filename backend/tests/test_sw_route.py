"""Tests for the root-scoped service worker route (`GET /sw.js`).

The PWA service worker must be served from the site root so its scope is `/`
(covering the SPA's client-side routes). The bundle emits `sw.js` under the
`/static/` prefix, so `main.py` re-serves the same file at `/sw.js` with the
correct MIME type and `Service-Worker-Allowed` header, registered before the
SPA catch-all so it is not shadowed.
"""

import app.main as main_module


async def test_sw_js_served_from_root(client, tmp_path, monkeypatch):
    """A sw.js present in the static dir is served at /sw.js with SW headers."""
    sw_body = "self.addEventListener('fetch', () => {})\n"
    (tmp_path / "sw.js").write_text(sw_body)
    monkeypatch.setattr(main_module, "_STATIC_DIR", tmp_path)

    resp = await client.get("/sw.js")

    assert resp.status_code == 200
    assert resp.text == sw_body
    assert resp.headers["content-type"].startswith("application/javascript")
    # Root scope must be explicitly permitted for a SW file.
    assert resp.headers["service-worker-allowed"] == "/"


async def test_sw_js_missing_returns_404(client, tmp_path, monkeypatch):
    """When the bundle has no sw.js, the route 404s instead of leaking index.html."""
    monkeypatch.setattr(main_module, "_STATIC_DIR", tmp_path)

    resp = await client.get("/sw.js")

    assert resp.status_code == 404


async def test_sw_route_does_not_shadow_api(client, tmp_path, monkeypatch):
    """The sw.js route must not interfere with API routing."""
    monkeypatch.setattr(main_module, "_STATIC_DIR", tmp_path)

    # A known API route still resolves to JSON, not the SW file or the SPA shell.
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
