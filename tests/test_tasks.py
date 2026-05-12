"""Tests for inact.apps.tasks."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from inact import Inact
from inact.apps.tasks import mount_tasks


def _make_client(tmp_path: Path) -> TestClient:
    app = Inact("tasks-test")
    mount_tasks(app, "/tasks", f"sqlite:///{tmp_path / 'tasks.db'}")
    return TestClient(app.app)


def _create(client: TestClient, title: str, **body) -> str:
    response = client.post("/tasks/", json={"title": title, **body})
    assert response.status_code == 200
    match = re.search(r'id\s+=\s+"?([^"\n]+)"?', response.text)
    assert match, response.text
    return match.group(1)


def test_default_task_list_hides_done_until_requested(tmp_path):
    client = _make_client(tmp_path)
    todo_id = _create(client, "todo task")
    done_id = _create(client, "done task")
    assert client.post(f"/tasks/{done_id}/.done").status_code == 200

    default = client.get("/tasks/")
    assert default.status_code == 200
    assert "# Tasks (todo)" in default.text
    assert "# page 1 of 1 (1 total)" in default.text
    assert f'id       = "{todo_id}"' in default.text
    assert f'id       = "{done_id}"' not in default.text

    done = client.get("/tasks/", params={"status": "done"})
    assert done.status_code == 200
    assert "# Tasks (done)" in done.text
    assert f'id       = "{todo_id}"' not in done.text
    assert f'id       = "{done_id}"' in done.text

    all_tasks = client.get("/tasks/", params={"status": "all"})
    assert all_tasks.status_code == 200
    assert "# Tasks (all)" in all_tasks.text
    assert f'id       = "{todo_id}"' in all_tasks.text
    assert f'id       = "{done_id}"' in all_tasks.text


def test_task_list_is_paginated(tmp_path):
    client = _make_client(tmp_path)
    for i in range(25):
        _create(client, f"task {i:02d}")

    page = client.get("/tasks/", params={"page": "2", "per_page": "10"})
    assert page.status_code == 200
    assert "# page 2 of 3 (25 total)" in page.text
    assert page.text.count("[[tasks]]") == 10
    assert "# ?page=1&per_page=10 for prev" in page.text
    assert "# ?page=3&per_page=10 for next" in page.text


def test_unassigned_and_children_are_paginated(tmp_path):
    client = _make_client(tmp_path)
    parent_id = _create(client, "parent")
    child_ids = [_create(client, f"child {i:02d}", parent_id=parent_id) for i in range(12)]
    assert client.post(f"/tasks/{child_ids[0]}/.done").status_code == 200

    unassigned = client.get("/tasks/.unassigned", params={"per_page": "5"})
    assert unassigned.status_code == 200
    assert "# page 1 of 3 (12 total)" in unassigned.text
    assert unassigned.text.count("[[tasks]]") == 5

    children = client.get(f"/tasks/{parent_id}/children", params={"per_page": "5"})
    assert children.status_code == 200
    assert "# Children: parent (todo)" in children.text
    assert "# page 1 of 3 (11 total)" in children.text
    assert children.text.count("[[tasks]]") == 5
    assert f'id       = "{child_ids[0]}"' not in children.text

    done_children = client.get(
        f"/tasks/{parent_id}/children",
        params={"status": "done", "per_page": "5"},
    )
    assert done_children.status_code == 200
    assert "# Children: parent (done)" in done_children.text
    assert "# page 1 of 1 (1 total)" in done_children.text
    assert f'id       = "{child_ids[0]}"' in done_children.text


def test_invalid_status_filter_is_rejected(tmp_path):
    client = _make_client(tmp_path)
    response = client.get("/tasks/", params={"status": "closed"})
    assert response.status_code == 400
    assert "valid: todo | done | all" in response.text
