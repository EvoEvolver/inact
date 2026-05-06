"""Runtime settings singleton, sourced from environment variables."""

from __future__ import annotations

import os


def _envbool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0") == "1"


class Config:
    _instance: "Config | None" = None

    def __init__(self) -> None:
        self.bypass_auth: bool = _envbool("INACT_BYPASS_AUTH", False)

    @classmethod
    def get(cls) -> "Config":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


settings = Config.get()
