"""
File handlers for mounted directories.

Handlers transform specific file types into AI-readable text and can expose
virtual pagination so agents browse large files one chunk at a time via
  GET {mount_prefix}/{file}/p/{N}
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


class FileHandler(ABC):
    """
    Base class for custom file renderers injected into mounted directories.

    Subclass this, set `extensions`, and implement `serve()`.
    Override `page_count()` to enable pagination.
    """

    extensions: list[str] = []

    def page_count(self, abs_path: str) -> int | None:
        """Return total virtual pages, or None for single-page (unpaginated) content."""
        return None

    @abstractmethod
    def serve(self, abs_path: str, virtual_path: str, page: int = 1) -> tuple[str, int]:
        """
        Return (content_text, http_status).

        abs_path:     absolute filesystem path to the file
        virtual_path: the URL path the file is accessed at (for nav hints)
        page:         1-based page index
        """
        raise NotImplementedError


class PDFHandler(FileHandler):
    """
    Convert PDF files to plain text for AI consumption.

    Pages are virtual "chunks" of extracted text, not PDF page boundaries.
    Requires: pip install pypdf
    """

    extensions = [".pdf"]

    def __init__(self, chars_per_page: int = 3000):
        self.chars_per_page = chars_per_page

    def _extract_text(self, abs_path: str) -> str:
        try:
            import pypdf
        except ImportError:
            raise RuntimeError("pypdf is required for PDF handling: pip install pypdf")
        reader = pypdf.PdfReader(abs_path)
        parts = []
        for i, page_obj in enumerate(reader.pages, 1):
            text = (page_obj.extract_text() or "").strip()
            parts.append(f"--- PDF Page {i} ---\n{text}")
        return "\n\n".join(parts)

    def page_count(self, abs_path: str) -> int | None:
        try:
            text = self._extract_text(abs_path)
        except Exception:
            return None
        return max(1, (len(text) + self.chars_per_page - 1) // self.chars_per_page)

    def serve(self, abs_path: str, virtual_path: str, page: int = 1) -> tuple[str, int]:
        try:
            text = self._extract_text(abs_path)
        except RuntimeError as e:
            return str(e), 500
        except Exception as e:
            return f"ERROR reading PDF: {e}", 500

        total = max(1, (len(text) + self.chars_per_page - 1) // self.chars_per_page)

        if page < 1 or page > total:
            return f"Page {page} out of range. This file has {total} page(s).", 404

        chunk = text[(page - 1) * self.chars_per_page : page * self.chars_per_page]

        nav = [f"# {os.path.basename(abs_path)}", f"[Page {page} of {total}]"]
        if page > 1:
            nav.append(f"prev: {virtual_path}/p/{page - 1}")
        if page < total:
            nav.append(f"next: {virtual_path}/p/{page + 1}")
        nav.append("")

        return "\n".join(nav) + "\n" + chunk, 200
