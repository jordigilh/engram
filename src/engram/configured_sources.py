"""Deployment-configured extra filesystem sources.

The repository ships with family-specific defaults, while installations may
need to add local workspaces that are not Git repositories or are not part of
the upstream repository list. Those paths belong in deployment-local config,
not in this package. The optional TOML file is selected with
``ENGRAM_EXTRA_SOURCES_CONFIG``.
"""
from __future__ import annotations

import os
import pathlib
import re
import tomllib
from dataclasses import dataclass
from typing import Any


_TAG_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


@dataclass(frozen=True)
class ConfiguredSource:
    """One deployment-local source root and the file classes to ingest."""

    tag: str
    root: pathlib.Path
    docs_include: tuple[str, ...] = ()
    docs_exclude: tuple[str, ...] = ()
    code_include: tuple[str, ...] = ()
    code_exclude: tuple[str, ...] = ()


def _patterns(entry: dict[str, Any], key: str) -> tuple[str, ...]:
    value = entry.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{key} must be a non-empty string list when present")
    return tuple(value)


def _source_root(raw_root: str, config_path: pathlib.Path) -> pathlib.Path:
    expanded = os.path.expandvars(os.path.expanduser(raw_root))
    root = pathlib.Path(expanded)
    if not root.is_absolute():
        root = config_path.parent / root
    return root


def load_configured_sources(config_path: str | os.PathLike[str] | None = None) -> tuple[ConfiguredSource, ...]:
    """Load optional deployment-local extra source roots from TOML.

    The expected shape is::

        [[sources]]
        tag = "workspace-name"
        root = "/path/to/workspace"
        docs_include = ["**/*.md"]
        code_include = ["**/*.py"]

    An unset configuration path keeps the built-in behavior unchanged.
    """
    configured_path = config_path or os.environ.get("ENGRAM_EXTRA_SOURCES_CONFIG")
    if not configured_path:
        return ()

    path = pathlib.Path(os.path.expandvars(os.path.expanduser(str(configured_path))))
    with path.open("rb") as stream:
        document = tomllib.load(stream)

    raw_sources = document.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError("extra sources config must define [[sources]] entries")

    sources: list[ConfiguredSource] = []
    seen_tags: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, dict):
            raise ValueError(f"sources[{index}] must be a table")
        tag = raw_source.get("tag")
        raw_root = raw_source.get("root")
        if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
            raise ValueError(f"sources[{index}].tag must contain only letters, numbers, '.', '_', '@', or '-'")
        if tag in seen_tags:
            raise ValueError(f"duplicate extra source tag: {tag}")
        if not isinstance(raw_root, str) or not raw_root:
            raise ValueError(f"sources[{index}].root must be a non-empty string")

        source = ConfiguredSource(
            tag=tag,
            root=_source_root(raw_root, path),
            docs_include=_patterns(raw_source, "docs_include"),
            docs_exclude=_patterns(raw_source, "docs_exclude"),
            code_include=_patterns(raw_source, "code_include"),
            code_exclude=_patterns(raw_source, "code_exclude"),
        )
        if not source.docs_include and not source.code_include:
            raise ValueError(f"extra source {tag} must configure docs_include or code_include")
        seen_tags.add(tag)
        sources.append(source)

    return tuple(sources)
