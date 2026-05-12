"""Tests for workspace admin human routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from inact import Inact
from inact.apps.workspace import mount_workspace


def test_human_admin_route_shows_login_form(tmp_path: Path):
    app = Inact("workspace-admin-test")
    mount_workspace(app, f"sqlite:///{tmp_path / 'workspace.db'}", admin_key="secret")
    client = TestClient(app.app)

    response = client.get("/_human/admin")

    assert response.status_code == 200
    assert "Enter the admin key" in response.text
