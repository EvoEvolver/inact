"""Tests for inact.apps.skills."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inact import Inact, SkillStore, mount_skills


# -- fixtures ----------------------------------------------------------------

def _write_skill(root: Path, name: str, *, description: str = "Use when ...",
                 tags: list[str] | None = None, body: str = "Body.\n") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = [f"name: {name}", f"description: {description}"]
    if tags is not None:
        fm.append(f"tags: {tags}")
    text = "---\n" + "\n".join(fm) + "\n---\n\n" + body
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")


def _make_app(tmp_path: Path) -> tuple[TestClient, SkillStore, Path, Path]:
    root_a = tmp_path / "module_a" / "skills"
    root_b = tmp_path / "module_b" / "skills"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)

    app = Inact("skills-test")
    store = mount_skills(app, "/skills")
    return TestClient(app.app), store, root_a, root_b


# -- store-level tests -------------------------------------------------------

def test_register_root_loads_skills(tmp_path):
    client, store, root_a, _ = _make_app(tmp_path)
    _write_skill(root_a, "alpha")
    _write_skill(root_a, "beta", tags=["x"])
    added = store.register_root(root_a, default_tags=["module_a"])
    assert added == 2
    assert sorted(s.name for s in store.list()) == ["alpha", "beta"]
    # default_tags applied
    alpha = store.get("alpha")
    assert "module_a" in alpha.tags
    # frontmatter tags appended
    beta = store.get("beta")
    assert "module_a" in beta.tags and "x" in beta.tags


def test_register_root_skips_missing_dir(tmp_path):
    _, store, _, _ = _make_app(tmp_path)
    added = store.register_root(tmp_path / "does_not_exist")
    assert added == 0
    assert len(store) == 0


def test_register_root_skips_missing_frontmatter(tmp_path):
    _, store, root_a, _ = _make_app(tmp_path)
    bad = root_a / "no-frontmatter"
    bad.mkdir()
    (bad / "SKILL.md").write_text("just body, no frontmatter\n")
    # Missing required name/description -> skipped silently.
    added = store.register_root(root_a)
    assert added == 0


def test_duplicate_name_across_roots_fails_fast(tmp_path):
    _, store, root_a, root_b = _make_app(tmp_path)
    _write_skill(root_a, "shared")
    _write_skill(root_b, "shared")
    store.register_root(root_a, default_tags=["a"])
    with pytest.raises(ValueError, match="duplicate skill name 'shared'"):
        store.register_root(root_b, default_tags=["b"])


def test_list_filter_by_tag_and_q(tmp_path):
    _, store, root_a, _ = _make_app(tmp_path)
    _write_skill(root_a, "orca-input", description="Build ORCA input.",
                 tags=["expert"])
    _write_skill(root_a, "geometry-gen", description="Generate geometry.",
                 tags=["expert"])
    _write_skill(root_a, "planner", description="Plan workflows.",
                 tags=["invoker"])
    store.register_root(root_a)

    # tag filter
    expert = [s.name for s in store.list(tag="expert")]
    assert sorted(expert) == ["geometry-gen", "orca-input"]
    # q filter (name match)
    by_name = [s.name for s in store.list(q="orca")]
    assert by_name == ["orca-input"]
    # q filter (description match, case-insensitive)
    by_desc = [s.name for s in store.list(q="WORKFLOW")]
    assert by_desc == ["planner"]
    # combined
    both = [s.name for s in store.list(tag="expert", q="geom")]
    assert both == ["geometry-gen"]


# -- HTTP tests --------------------------------------------------------------

def test_http_index_lists_all_skills(tmp_path):
    client, store, root_a, root_b = _make_app(tmp_path)
    _write_skill(root_a, "alpha", description="Use alpha.")
    _write_skill(root_b, "bravo", description="Use bravo.")
    store.register_root(root_a, default_tags=["a"])
    store.register_root(root_b, default_tags=["b"])

    resp = client.get("/skills")
    assert resp.status_code == 200
    body = resp.text
    assert '[[skills]]' in body
    assert 'name = "alpha"' in body
    assert 'name = "bravo"' in body
    assert 'description = "Use alpha."' in body
    # body of SKILL.md NOT inlined in index
    assert "Body." not in body


def test_http_index_filter_by_tag(tmp_path):
    client, store, root_a, root_b = _make_app(tmp_path)
    _write_skill(root_a, "alpha")
    _write_skill(root_b, "bravo")
    store.register_root(root_a, default_tags=["a"])
    store.register_root(root_b, default_tags=["b"])

    resp = client.get("/skills", params={"tag": "a"})
    assert resp.status_code == 200
    assert 'name = "alpha"' in resp.text
    assert 'name = "bravo"' not in resp.text


def test_http_detail_returns_raw_markdown(tmp_path):
    client, store, root_a, _ = _make_app(tmp_path)
    _write_skill(root_a, "alpha", description="Use alpha.",
                 body="# Heading\n\nbody text\n")
    store.register_root(root_a)

    resp = client.get("/skills/alpha")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    # frontmatter included verbatim
    assert resp.text.startswith("---\n")
    assert "name: alpha" in resp.text
    assert "# Heading" in resp.text
    assert "body text" in resp.text


def test_http_detail_unknown_returns_404(tmp_path):
    client, store, root_a, _ = _make_app(tmp_path)
    _write_skill(root_a, "alpha")
    store.register_root(root_a)

    resp = client.get("/skills/does_not_exist")
    assert resp.status_code == 404
    assert resp.text.startswith("ERROR 404:")


def test_http_index_empty_when_no_skills(tmp_path):
    client, _, _, _ = _make_app(tmp_path)
    resp = client.get("/skills")
    assert resp.status_code == 200
    assert "No skills mounted" in resp.text


def test_tags_yaml_loaded_from_root(tmp_path):
    client, store, root_a, _ = _make_app(tmp_path)
    _write_skill(root_a, "alpha", tags=["custom"])
    (root_a / "TAGS.yaml").write_text(
        'custom: "A custom tag for testing."\n'
        'unused: "Should not appear."\n',
        encoding="utf-8",
    )
    store.register_root(root_a)

    # tag_descriptions only surfaces tags actually in use
    descs = store.tag_descriptions()
    assert descs == {"custom": "A custom tag for testing."}

    # surfaced in TOML index
    resp = client.get("/skills")
    assert resp.status_code == 200
    assert "[[tags]]" in resp.text
    assert 'name = "custom"' in resp.text
    assert "A custom tag for testing." in resp.text
    assert "unused" not in resp.text

    # surfaced in human view
    human = client.get("/_human/skills/")
    assert human.status_code == 200
    assert "custom" in human.text
    assert "A custom tag for testing." in human.text


def test_human_view_index_and_detail(tmp_path):
    client, store, root_a, _ = _make_app(tmp_path)
    _write_skill(root_a, "alpha", description="Use alpha here.",
                 body="# Alpha\n\nbody.\n")
    store.register_root(root_a, default_tags=["mod_a"])

    index = client.get("/_human/skills/")
    assert index.status_code == 200
    assert "alpha" in index.text
    assert "Use alpha here." in index.text

    detail = client.get("/_human/skills/alpha")
    assert detail.status_code == 200
    # rendered markdown produces an <h1> for the H1 in body
    assert "Alpha" in detail.text

    missing = client.get("/_human/skills/nope")
    assert missing.status_code == 404


def test_reload_picks_up_new_skill(tmp_path):
    client, store, root_a, _ = _make_app(tmp_path)
    _write_skill(root_a, "alpha")
    store.register_root(root_a)
    assert len(store) == 1

    _write_skill(root_a, "beta")
    store.reload()
    assert len(store) == 2
    names = sorted(s.name for s in store.list())
    assert names == ["alpha", "beta"]
