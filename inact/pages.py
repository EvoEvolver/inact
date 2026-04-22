import re
import tomllib
import tomli_w

import yaml


_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)


class MdContent:
    """Markdown page. Keyword arguments become frontmatter metadata."""
    def __init__(self, body: str = "", **meta):
        self.body = body
        self.meta = meta


class TomlContent:
    """TOML page. Optional annotation becomes leading # comment lines."""
    def __init__(self, data: dict, annotation: str | list[str] = ""):
        self.data = data
        self.annotation = annotation


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_frontmatter(content: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(content)
    if m:
        try:
            metadata = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}
        return metadata, content[m.end():]
    return {}, content


def parse_toml(content: str) -> dict:
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return {}


def dict_to_toml(data: dict) -> str:
    return tomli_w.dumps(data)


# ---------------------------------------------------------------------------
# Normalizers — called on whatever the route handler returns
# ---------------------------------------------------------------------------

def normalize_md(value) -> tuple[dict, str]:
    """Returns (metadata_dict, body_str)."""
    if isinstance(value, MdContent):
        return value.meta, value.body
    if isinstance(value, str):
        return parse_frontmatter(value)
    raise TypeError(
        f"inact_md handler must return InactMd or str, got {type(value).__name__}"
    )


def normalize_toml(value) -> tuple[dict, str]:
    """Returns (data_dict, toml_text_with_optional_annotation)."""
    if isinstance(value, TomlContent):
        ann = value.annotation
        if isinstance(ann, str):
            lines = ann.splitlines() if ann else []
        else:
            lines = [str(x) for x in ann]
        prefix = "\n".join(f"# {line}" for line in lines) + "\n\n" if lines else ""
        return value.data, prefix + dict_to_toml(value.data)
    if isinstance(value, str):
        return parse_toml(value), value
    raise TypeError(
        f"inact_toml handler must return InactToml or str, got {type(value).__name__}"
    )
