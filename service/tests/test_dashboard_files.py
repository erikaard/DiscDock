from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from discdock.api import DashboardFiles


def test_the_dashboard_page_is_checked_again_so_an_update_shows_up(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    chunks = tmp_path / "_next" / "static" / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "0k1u.js").write_text("", encoding="utf-8")
    client = TestClient(Starlette(routes=[Mount("/", DashboardFiles(directory=tmp_path, html=True))]))

    page = client.get("/")
    unchanged = client.get("/", headers={"If-None-Match": page.headers["etag"]})
    asset = client.get("/_next/static/chunks/0k1u.js")

    assert page.headers["cache-control"] == "no-cache"
    assert unchanged.status_code == 304
    assert unchanged.headers["cache-control"] == "no-cache"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
