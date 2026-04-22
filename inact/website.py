"""
Website proxy for inact.

Fetches remote pages and converts HTML to plain text for AI agents.
Uses only stdlib (html.parser, urllib.parse) — no new dependencies.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "iframe",
    "svg", "template", "canvas",
})
# Content from these tags is suppressed in plain-text output but child tags are still visited.
_NO_CONTENT_TAGS = frozenset({"head", "nav", "footer", "aside"})
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "main", "aside",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "dt", "dd", "blockquote", "pre", "tr",
    "br", "hr", "figure", "figcaption",
})
_HEADERS = {"User-Agent": "inact/0.1 (AI-friendly web proxy; +https://github.com/EvoEvolver/inact)"}


class _PageParser(HTMLParser):
    """Single-pass extraction of title, plain text, and links from HTML."""

    def __init__(self, page_url: str):
        super().__init__()
        self._page_url = page_url
        self.title = ""
        self.links: list[tuple[str, str]] = []   # (abs_href, link_text)
        self._text: list[str] = []
        self._skip: int = 0       # depth inside _SKIP_TAGS (content + children suppressed)
        self._nocontent: int = 0  # depth inside _NO_CONTENT_TAGS (text suppressed, children visited)
        self._in_title = False
        self._a_href = ""
        self._a_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _NO_CONTENT_TAGS:
            self._nocontent += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            attrs_d = dict(attrs)
            href = attrs_d.get("href", "")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self._a_href = urljoin(self._page_url, href)
                self._a_buf = []
        if tag in _BLOCK_TAGS and not self._nocontent:
            self._text.append("\n")

    def handle_endtag(self, tag: str):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in _NO_CONTENT_TAGS:
            self._nocontent = max(0, self._nocontent - 1)
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._a_href:
            link_text = "".join(self._a_buf).strip()
            if link_text:
                self.links.append((self._a_href, link_text))
            self._a_href = ""
            self._a_buf = []
        if tag in _BLOCK_TAGS and not self._nocontent:
            self._text.append("\n")

    def handle_data(self, data: str):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._a_href:
            self._a_buf.append(data)
        if not self._nocontent:
            self._text.append(data)

    def get_text(self) -> str:
        raw = "".join(self._text)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


class WebsiteProxy:
    """Fetch and convert remote web pages for inact routes."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _url(self, subpath: str) -> str:
        return self.base_url + "/" + subpath.lstrip("/") if subpath else self.base_url + "/"

    def fetch_text(
        self, subpath: str = "", params: dict | None = None
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """
        Fetch a page and return ``(title, plain_text, links)``.

        For non-HTML responses returns ``("", raw_text, [])``.
        """
        resp = httpx.get(
            self._url(subpath), params=params, timeout=30,
            follow_redirects=True, headers=_HEADERS,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type:
            return "", resp.text, []
        parser = _PageParser(str(resp.url))
        parser.feed(resp.text)
        return parser.title.strip(), parser.get_text(), parser.links

    def fetch_raw(self, subpath: str = "", params: dict | None = None) -> tuple[str, str]:
        """Return ``(content_type, raw_body)`` without any conversion."""
        resp = httpx.get(
            self._url(subpath), params=params, timeout=30,
            follow_redirects=True, headers=_HEADERS,
        )
        resp.raise_for_status()
        return resp.headers.get("content-type", "text/plain"), resp.text
