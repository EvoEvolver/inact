"""
File handlers for mounted directories.

Handlers transform specific file types into AI-readable text and can expose
virtual pagination so agents browse large files one chunk at a time via
  GET {mount_prefix}/{file}/p/{N}
"""

from __future__ import annotations

import csv
import io
import os
import re
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

        nav = [f"# {os.path.basename(virtual_path)}", f"[Page {page} of {total}]"]
        if page > 1:
            nav.append(f"prev: {virtual_path}/p/{page - 1}")
        if page < total:
            nav.append(f"next: {virtual_path}/p/{page + 1}")
        nav.append("")

        return "\n".join(nav) + "\n" + chunk, 200


class CSVHandler(FileHandler):
    """
    Serve CSV files as paginated TOML for AI agents.

    Each page shows ``rows_per_page`` data rows as ``[[rows]]`` TOML entries
    keyed by the header row.  The file is also writable via ``/.append``
    (registered automatically by :func:`~inact.apps.files.mount_files`).

    No extra dependencies — uses the stdlib ``csv`` module.

    Example::

        from inact.handlers import CSVHandler
        mount_files(app, "/data", "./data", handlers=[CSVHandler()])
        mount_files(app, "/data", "./data", handlers=[CSVHandler(rows_per_page=100)])
    """

    extensions = [".csv"]

    def __init__(self, rows_per_page: int = 50):
        self.rows_per_page = rows_per_page

    def _read(self, abs_path: str) -> tuple[list[str], list[list[str]]]:
        with open(abs_path, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        if not rows:
            return [], []
        return rows[0], rows[1:]

    def _safe_key(self, name: str) -> str:
        key = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "col"
        return key

    def page_count(self, abs_path: str) -> int | None:
        try:
            _, data = self._read(abs_path)
        except Exception:
            return None
        return max(1, (len(data) + self.rows_per_page - 1) // self.rows_per_page)

    def serve(self, abs_path: str, virtual_path: str, page: int = 1) -> tuple[str, int]:
        try:
            headers, data = self._read(abs_path)
        except Exception as e:
            return f"ERROR reading CSV: {e}", 500

        total_rows = len(data)
        total_pages = max(1, (total_rows + self.rows_per_page - 1) // self.rows_per_page)

        if page < 1 or page > total_pages:
            return f"Page {page} out of range (1–{total_pages}).", 404

        start = (page - 1) * self.rows_per_page
        page_rows = data[start : start + self.rows_per_page]
        keys = [self._safe_key(h) for h in headers]

        lines = [
            f"# {os.path.basename(virtual_path)}\n",
            f"# {total_rows} rows  —  page {page} of {total_pages}"
            f"  (rows {start + 1}–{start + len(page_rows)})\n",
        ]
        if page > 1:
            lines.append(f"# prev: {virtual_path}/p/{page - 1}\n")
        if page < total_pages:
            lines.append(f"# next: {virtual_path}/p/{page + 1}\n")
        lines.append(f"# append: POST {virtual_path}/.append\n\n")

        for row in page_rows:
            lines.append("[[rows]]\n")
            for i, key in enumerate(keys):
                val = row[i] if i < len(row) else ""
                lines.append(f'{key} = "{val}"\n')
            lines.append("\n")

        return "".join(lines), 200

    def append_row(self, abs_path: str, values: list[str]) -> None:
        """Append one row to the CSV file."""
        buf = io.StringIO()
        csv.writer(buf).writerow(values)
        with open(abs_path, "a", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())

    def header(self, abs_path: str) -> list[str]:
        """Return the header row (empty list if file is empty)."""
        try:
            h, _ = self._read(abs_path)
            return h
        except Exception:
            return []
