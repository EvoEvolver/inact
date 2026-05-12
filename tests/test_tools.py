"""Tests for inact.apps.tools."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from inact import Inact
from inact.apps.tools import mount_tool_tree


@dataclass(frozen=True)
class Tool:
    name: str
    folder: str
    description: str


def _row(tool: Tool) -> dict:
    return {
        "name": tool.name,
        "folder": tool.folder,
        "description": tool.description,
        "call": f"POST /tools/{tool.name}",
    }


def test_mount_tool_tree_navigates_nested_folders():
    app = Inact("tool-tree-test")
    mount_tool_tree(
        app,
        "/tools",
        tools=[
            Tool("alpha", "analysis/connectivity", "Alpha tool."),
            Tool("beta", "analysis/measurements", "Beta tool."),
        ],
        row_fn=_row,
        title="Example",
        folder_descriptions={"analysis": "Inspect things."},
    )
    client = TestClient(app.app)

    root = client.get("/tools")
    assert root.status_code == 200
    assert "# Example tool folder: /" in root.text
    assert 'name = "analysis"' in root.text
    assert 'url = "/tools/analysis"' in root.text
    assert 'name = "alpha"' not in root.text

    folder = client.get("/tools/analysis/connectivity")
    assert folder.status_code == 200
    assert "# Example tool folder: analysis/connectivity" in folder.text
    assert 'name = "alpha"' in folder.text
    assert 'name = "beta"' not in folder.text


def test_mount_tool_tree_resolves_tool_detail_before_folder():
    app = Inact("tool-tree-detail-test")
    mount_tool_tree(
        app,
        "/tools",
        tools=[Tool("analysis", "analysis/connectivity", "Tool named like folder.")],
        row_fn=_row,
        title="Example",
    )
    client = TestClient(app.app)

    detail = client.get("/tools/analysis")
    assert detail.status_code == 200
    assert "# Example tool: analysis" in detail.text
    assert 'description = "Tool named like folder."' in detail.text


def test_mount_tool_tree_collapses_sparse_nested_folders():
    app = Inact("tool-tree-collapse-test")
    mount_tool_tree(
        app,
        "/tools",
        tools=[
            Tool("alpha", "analysis/connectivity", "Alpha tool."),
            Tool("beta", "analysis/measurements", "Beta tool."),
            Tool("gamma", "analysis/symmetry", "Gamma tool."),
            Tool("delta", "editing/atoms", "Delta tool."),
            Tool("epsilon", "editing/atoms", "Epsilon tool."),
            Tool("zeta", "editing/atoms", "Zeta tool."),
        ],
        row_fn=_row,
        title="Example",
        min_folder_tools=3,
    )
    client = TestClient(app.app)

    root = client.get("/tools")
    assert root.status_code == 200
    assert 'name = "analysis"' in root.text
    assert 'name = "editing"' in root.text
    assert 'name = "alpha"' not in root.text

    analysis = client.get("/tools/analysis")
    assert analysis.status_code == 200
    assert 'name = "alpha"' in analysis.text
    assert 'name = "connectivity"' not in analysis.text

    sparse_leaf = client.get("/tools/analysis/connectivity")
    assert sparse_leaf.status_code == 404

    dense_leaf = client.get("/tools/editing/atoms")
    assert dense_leaf.status_code == 200
    assert 'name = "delta"' in dense_leaf.text


def test_mount_tool_tree_rejects_bad_or_unknown_paths():
    app = Inact("tool-tree-error-test")
    mount_tool_tree(app, "/tools", tools=[], row_fn=_row)
    client = TestClient(app.app)

    invalid = client.get("/tools/%2E%2E/bad")
    assert invalid.status_code == 400
    assert "invalid tool path" in invalid.text

    missing = client.get("/tools/missing")
    assert missing.status_code == 404
    assert "unknown tool or folder 'missing'" in missing.text
