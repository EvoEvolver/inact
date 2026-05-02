"""
File-based memory system for the agent.

Layout: MEMORY_DIR/<YYYY-MM-DD-HH-MM>.md files, auto-archived into date
bucket subdirectories when any directory exceeds 7 date-named entries.

Public API:
    load_memory(limit, per_file_chars) -> str
    auto_save_output(output)
    archive_if_needed()
"""

import os
import re
from datetime import datetime

import logfire


MEMORY_DIR    = os.environ.get("MEMORY_DIR", "./memory")
_MEMORY_INDEX = "MEMORY.md"

# Archive levels: finest → coarsest.
# Each entry: (pattern matching a dir entry name, fn returning its parent bucket name).
_ARCHIVE_LEVELS: list[tuple[re.Pattern, object]] = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}.*\.md$"), lambda n: n[:13]),
    (re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}$"),             lambda n: n[:10]),
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"),                   lambda n: n[:7]),
    (re.compile(r"^\d{4}-\d{2}$"),                         lambda n: n[:4]),
]


def _memory_path() -> str:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return MEMORY_DIR


def _bucket(name: str) -> str | None:
    """Return the parent bucket dir name for a memory entry, or None if ungroupable."""
    for pat, key_fn in _ARCHIVE_LEVELS:
        if pat.match(name):
            return key_fn(name)
    return None


def _unique_path(base: str) -> str:
    if not os.path.exists(base):
        return base
    root, ext = os.path.splitext(base)
    n = 1
    while True:
        cand = f"{root}-{n}{ext}"
        if not os.path.exists(cand):
            return cand
        n += 1


def _move_into(src: str, dest_dir: str) -> None:
    """Move a file or directory into dest_dir, merging directories if the target exists."""
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(src.rstrip(os.sep))
    dest_path = os.path.join(dest_dir, name)
    try:
        if os.path.isdir(src):
            if not os.path.exists(dest_path):
                os.rename(src, dest_path)
                return
            if os.path.isdir(dest_path):
                for child in os.listdir(src):
                    _move_into(os.path.join(src, child), dest_path)
                try:
                    os.rmdir(src)
                except OSError:
                    pass
                return
            dest_path = _unique_path(dest_path)
            os.rename(src, dest_path)
        else:
            dest_path = _unique_path(dest_path)
            os.rename(src, dest_path)
    except OSError:
        import shutil
        if os.path.isdir(src):
            if not os.path.exists(dest_path):
                shutil.move(src, dest_path)
            else:
                for child in os.listdir(src):
                    _move_into(os.path.join(src, child), dest_path)
                try:
                    os.rmdir(src)
                except OSError:
                    pass
        else:
            shutil.move(src, dest_path)


def _compact(dirpath: str) -> None:
    """Recursively compact dirpath to <= 7 date-named children by moving them into coarser buckets."""
    dir_name = os.path.basename(dirpath)
    while True:
        all_entries = [
            e for e in os.listdir(dirpath)
            if not e.startswith(".") and e != _MEMORY_INDEX
        ]
        date_entries = [e for e in all_entries if _bucket(e) is not None]
        if len(date_entries) <= 7:
            break
        moved = False
        date_entries_sorted = sorted(
            date_entries,
            key=lambda n: (0 if os.path.isfile(os.path.join(dirpath, n)) else 1, n),
        )
        for name in list(date_entries_sorted):
            key = _bucket(name)
            if key == dir_name:
                continue
            subdir = os.path.join(dirpath, key)
            _move_into(os.path.join(dirpath, name), subdir)
            logfire.info("memory: {name} → {key}/", name=name, key=key)
            moved = True
        if not moved:
            break
    for entry in os.listdir(dirpath):
        full = os.path.join(dirpath, entry)
        if os.path.isdir(full) and not entry.startswith("."):
            _compact(full)


def _rebuild_index() -> None:
    mem_dir = _memory_path()
    all_files: list[str] = []
    for root, dirs, files in os.walk(mem_dir):
        dirs.sort()
        for fname in sorted(files):
            if fname.endswith(".md") and fname != _MEMORY_INDEX:
                rel = os.path.relpath(os.path.join(root, fname), mem_dir)
                all_files.append(rel)
    all_files.sort(reverse=True)
    with open(os.path.join(mem_dir, _MEMORY_INDEX), "w", encoding="utf-8") as fp:
        for rel in all_files:
            fp.write(f"- [{rel}]({rel})\n")


def archive_if_needed() -> None:
    """Compact the memory tree and rebuild the flat index."""
    _compact(_memory_path())
    _rebuild_index()


def auto_save_output(output: str) -> None:
    """Write output to a dated memory file and trigger archival."""
    mem_dir = _memory_path()
    base = datetime.now().strftime("%Y-%m-%d-%H-%M")
    path = _unique_path(os.path.join(mem_dir, f"{base}.md"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(output)
    logfire.info("memory: saved → {path}", path=path)
    archive_if_needed()


def load_memory(limit: int = 5, per_file_chars: int = 1500) -> str:
    """Return the N most recent memory entries as a formatted string."""
    mem_dir = _memory_path()
    all_files: list[str] = []
    for root, dirs, files in os.walk(mem_dir):
        dirs.sort()
        for fname in sorted(files):
            if fname.endswith(".md") and fname != _MEMORY_INDEX:
                rel = os.path.relpath(os.path.join(root, fname), mem_dir)
                all_files.append(rel)
    if not all_files:
        return ""
    all_files.sort(reverse=True)

    chunks: list[str] = ["Recent memory (newest first):\n"]
    for rel in all_files[: max(1, limit)]:
        path = os.path.join(mem_dir, rel)
        try:
            text = open(path, encoding="utf-8").read().strip()
        except OSError:
            continue
        snippet = text if len(text) <= per_file_chars else text[:per_file_chars] + "\n…"
        chunks.append(f"\n### {rel}\n{snippet}\n")

    chunks.append(f"\n(Full index: {os.path.join(mem_dir, _MEMORY_INDEX)})")
    return "\n".join(chunks)
