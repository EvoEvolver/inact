"""
Tavily web search — mounted as a plain-text HTTP route.

mount_search(prefix, api_key=None) registers:

  GET  {prefix}?q=your+query      web search results (TOML)
  GET  {prefix}?q=...&max=10      limit results (default 5, max 20)

api_key defaults to the TAVILY_API_KEY environment variable.
"""

from __future__ import annotations

import os

import httpx
from flask import request

from ..utils import text_response, toml_str

_TAVILY_URL = "https://api.tavily.com/search"


def attach_search(inact_app, prefix: str, api_key: str | None) -> None:
    prefix = "/" + prefix.strip("/")
    ep = "_inact_search_" + prefix.replace("/", "__")

    def _search():
        key = api_key or os.environ.get("TAVILY_API_KEY", "")
        if not key:
            return text_response(
                "ERROR 503: Tavily API key not configured.\n"
                "Set TAVILY_API_KEY env var or pass api_key= to mount_search().\n",
                503,
            )
        q = request.args.get("q", "").strip()
        if not q:
            return text_response(
                f"ERROR 400: ?q= required\nUsage: GET {prefix}?q=your+query\n", 400
            )
        max_r = min(int(request.args.get("max", 5)), 20)
        try:
            resp = httpx.post(
                _TAVILY_URL,
                json={"api_key": key, "query": q, "max_results": max_r},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            return text_response(
                f"ERROR 502: Tavily returned HTTP {exc.response.status_code}\n", 502
            )
        except Exception as exc:
            return text_response(f"ERROR 502: {exc}\n", 502)

        results = data.get("results", [])
        lines = [
            f"# Search: {toml_str(q)}\n",
            f"# {len(results)} result(s)\n\n",
        ]
        if data.get("answer"):
            lines.append(f"answer = {toml_str(data['answer'])}\n\n")
        for r in results:
            snippet = (r.get("content") or "")[:400].replace("\n", " ").strip()
            lines += [
                "[[results]]\n",
                f"title   = {toml_str(r.get('title', ''))}\n",
                f"url     = {toml_str(r.get('url', ''))}\n",
                f"score   = {r.get('score', 0.0):.3f}\n",
                f"snippet = {toml_str(snippet)}\n",
                "\n",
            ]
        return text_response("".join(lines))

    inact_app.app.add_url_rule(prefix, endpoint=ep, view_func=_search)

    def _human(path: str):
        from inact.render import render_template
        from inact.utils import html_response
        from inact.render import workspace_nav
        return html_response(render_template("search_human.html",
            title="Search", prefix=prefix, nav="", pills=[],
            workspace_links=workspace_nav("/_human/search/"),
            show_identity=True))

    inact_app._human_views[prefix] = _human
    inact_app.add_nav_item(prefix.rsplit("/", 1)[-1] or prefix.strip("/"),
                           "/_human" + prefix + "/")


def mount_search(inact_app, prefix: str, api_key: str | None = None) -> None:
    """
    Mount Tavily web search at *prefix*.

    *api_key* — Tavily API key; falls back to the ``TAVILY_API_KEY`` env var.

    Example::

        app.mount_search("/search")
        app.mount_search("/search", api_key="tvly-...")
    """
    p = "/" + prefix.strip("/")
    attach_search(inact_app, p, api_key)
    inact_app._app_mounts.append((p, (
        f"\nSearch: {p}\n"
        f"  GET  {p}?q=your+query   web search (TOML)\n"
        f"  GET  {p}?q=...&max=10   limit results (default 5, max 20)\n"
    )))
