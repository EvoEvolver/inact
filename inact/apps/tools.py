"""Reusable hierarchical tool discovery routes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..utils import text_response, toml_str


ToolSource = Iterable[Any] | Callable[[], Iterable[Any]]


def _value(obj: Any, key: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if value is None:
        return '""'
    return toml_str(str(value))


def _key_values(data: dict[str, Any]) -> str:
    return "".join(f"{key} = {_toml_value(value)}\n" for key, value in data.items())


def _array_table(name: str, rows: Iterable[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        lines.append(f"[[{name}]]\n")
        for key, value in row.items():
            lines.append(f"{key} = {_toml_value(value)}\n")
        lines.append("\n")
    return "".join(lines)


def clean_tool_path(raw_path: str) -> str | None:
    parts = [part for part in str(raw_path or "").strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        return None
    return "/".join(parts)


class ToolTree:
    def __init__(
        self,
        *,
        prefix: str,
        tools: ToolSource,
        row_fn: Callable[[Any], dict[str, Any]],
        title: str = "Tools",
        folder_descriptions: dict[str, str] | None = None,
        folder_getter: Callable[[Any], str] | None = None,
        min_folder_tools: int = 1,
        name_getter: Callable[[Any], str] | None = None,
    ) -> None:
        self.prefix = "/" + prefix.strip("/")
        self.tools_source = tools
        self.row_fn = row_fn
        self.title = title
        self.folder_descriptions = folder_descriptions or {}
        self.folder_getter = folder_getter
        self.min_folder_tools = max(1, int(min_folder_tools))
        self.name_getter = name_getter

    def tools(self) -> list[Any]:
        source = self.tools_source() if callable(self.tools_source) else self.tools_source
        return list(source)

    def tool_name(self, tool: Any) -> str:
        if self.name_getter is not None:
            try:
                return str(self.name_getter(tool) or "")
            except Exception:
                pass
        return str(_value(tool, "name", ""))

    def tool_folder(self, tool: Any) -> str:
        raw = self.raw_tool_folder(tool)
        if self.min_folder_tools <= 1:
            return raw
        counts = self._folder_descendant_counts(self.tools())
        ancestors = self._ancestor_paths(raw)
        eligible = [path for path in ancestors if counts.get(path, 0) >= self.min_folder_tools]
        if eligible:
            return eligible[-1]
        return ""

    def raw_tool_folder(self, tool: Any) -> str:
        raw = ""
        if self.folder_getter is not None:
            try:
                raw = str(self.folder_getter(tool) or "")
            except Exception:
                raw = ""
        else:
            raw = str(
                _value(
                    tool,
                    "folder",
                    _value(tool, "group", _value(tool, "category", "")),
                )
                or ""
            )
            raw = raw.replace(".", "/")
        return clean_tool_path(raw) or ""

    def folder_url(self, folder_path: str) -> str:
        return f"{self.prefix}/{folder_path}" if folder_path else self.prefix

    def folder(self, folder_path: str = "") -> dict[str, Any] | None:
        cleaned = clean_tool_path(folder_path)
        if cleaned is None:
            return None
        folder_path = cleaned
        tools = self.tools()
        known_paths = {ancestor for tool in tools for ancestor in self._ancestor_paths(self.tool_folder(tool))}
        if folder_path and folder_path not in known_paths:
            return None

        child_names: set[str] = set()
        direct_tools: list[Any] = []
        descendant_count = 0
        for tool in tools:
            tool_folder = self.tool_folder(tool)
            if folder_path:
                if tool_folder == folder_path:
                    direct_tools.append(tool)
                    descendant_count += 1
                    continue
                prefix_path = folder_path + "/"
                if not tool_folder.startswith(prefix_path):
                    continue
                descendant_count += 1
                remainder = tool_folder[len(prefix_path):]
            else:
                descendant_count += 1
                remainder = tool_folder
            if remainder:
                child_names.add(remainder.split("/", 1)[0])

        folders = []
        for name in sorted(child_names):
            child_path = f"{folder_path}/{name}" if folder_path else name
            folders.append(
                {
                    "name": name,
                    "path": child_path,
                    "description": self.folder_descriptions.get(child_path, ""),
                    "url": self.folder_url(child_path),
                    "tool_count": sum(
                        1
                        for tool in tools
                        if self.tool_folder(tool) == child_path
                        or self.tool_folder(tool).startswith(child_path + "/")
                    ),
                }
            )

        return {
            "path": folder_path,
            "description": self.folder_descriptions.get(folder_path, f"{self.title} folders."),
            "url": self.folder_url(folder_path),
            "folder_count": len(folders),
            "tool_count": descendant_count,
            "folders": folders,
            "tools": sorted(direct_tools, key=self.tool_name),
        }

    def folder_sections(self) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        paths = sorted({self.tool_folder(tool) for tool in self.tools()})
        for path in paths:
            if not path:
                continue
            folder = self.folder(path)
            if folder and (folder["folders"] or folder["tools"]):
                sections.append(folder)
        return sections

    def render_folder(self, folder: dict[str, Any]) -> str:
        rows = [self.safe_row(tool) for tool in folder["tools"]]
        return (
            f"# {self.title} tool folder: {folder['path'] or '/'}\n\n"
            + _key_values(
                {
                    "path": folder["path"],
                    "description": folder["description"],
                    "url": folder["url"],
                    "folder_count": folder["folder_count"],
                    "tool_count": folder["tool_count"],
                }
            )
            + "\n# Folders\n"
            + _array_table("folders", folder["folders"])
            + "# Tools\n"
            + _array_table("tools", rows)
        )

    def render_detail(self, tool: Any, path: str) -> str:
        return (
            f"# {self.title} tool: {path}\n\n"
            + _array_table("tools", [self.safe_row(tool)])
        )

    def safe_row(self, tool: Any) -> dict[str, Any]:
        try:
            row = self.row_fn(tool)
            if isinstance(row, dict):
                row = dict(row)
                row["folder"] = self.tool_folder(tool)
                return row
        except Exception:
            pass
        return {
            "name": self.tool_name(tool),
            "folder": self.tool_folder(tool),
            "description": str(_value(tool, "description", "")),
        }

    def _ancestor_paths(self, folder_path: str) -> list[str]:
        cleaned = clean_tool_path(folder_path) or ""
        parts = cleaned.split("/") if cleaned else []
        return ["/".join(parts[:idx]) for idx in range(1, len(parts) + 1)]

    def _folder_descendant_counts(self, tools: list[Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tool in tools:
            for ancestor in self._ancestor_paths(self.raw_tool_folder(tool)):
                counts[ancestor] = counts.get(ancestor, 0) + 1
        return counts


def mount_tool_tree(
    inact_app,
    prefix: str,
    *,
    tools: ToolSource,
    row_fn: Callable[[Any], dict[str, Any]],
    title: str = "Tools",
    folder_descriptions: dict[str, str] | None = None,
    folder_getter: Callable[[Any], str] | None = None,
    min_folder_tools: int = 1,
    name_getter: Callable[[Any], str] | None = None,
) -> ToolTree:
    """Mount graceful recursive GET discovery for tools under *prefix*."""

    tree = ToolTree(
        prefix=prefix,
        tools=tools,
        row_fn=row_fn,
        title=title,
        folder_descriptions=folder_descriptions,
        folder_getter=folder_getter,
        min_folder_tools=min_folder_tools,
        name_getter=name_getter,
    )

    def _root():
        try:
            folder = tree.folder("")
            if folder is None:
                return text_response("ERROR 404: unknown tool folder\n", 404)
            return text_response(tree.render_folder(folder))
        except Exception as exc:
            return text_response(f"ERROR 500: tool tree failed: {exc}\n", 500)

    def _path(tool_path: str):
        cleaned = clean_tool_path(tool_path)
        if cleaned is None:
            return text_response(f"ERROR 400: invalid tool path {tool_path!r}\n", 400)
        try:
            by_name = {tree.tool_name(tool): tool for tool in tree.tools()}
            if cleaned in by_name:
                return text_response(tree.render_detail(by_name[cleaned], cleaned))
            folder = tree.folder(cleaned)
            if folder is not None:
                return text_response(tree.render_folder(folder))
            return text_response(f"ERROR 404: unknown tool or folder {cleaned!r}\n", 404)
        except Exception as exc:
            return text_response(f"ERROR 500: tool tree failed: {exc}\n", 500)

    p = "/" + prefix.strip("/")
    route_key = p.strip("/").replace("/", "__") or "root"
    inact_app.app.add_api_route(
        p,
        _root,
        methods=["GET"],
        name=f"_inact_tools_{route_key}_root",
    )
    inact_app.app.add_api_route(
        p + "/{tool_path:path}",
        _path,
        methods=["GET"],
        name=f"_inact_tools_{route_key}_path",
    )
    return tree
