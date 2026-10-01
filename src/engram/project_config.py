"""Deployment-local project settings.

Project names, checkout roots, repository lists, service endpoints, and other
operator choices belong in ``~/.engram/projects.toml`` rather than in the
ingestion/search modules.  The modules keep their domain algorithms and use
this small typed facade for the values supplied by a deployment.
"""
from __future__ import annotations

import os
import pathlib
import re
import tomllib
from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_PROJECT_CONFIG = pathlib.Path("~/.engram/projects.toml")
DEFAULT_HINDSIGHT_URL = "http://localhost:8888"
DEFAULT_PG_DSN = "postgresql://hindsight:hindsight@localhost:5432/hindsight"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_POOL_MIN_SIZE = 2
DEFAULT_POOL_MAX_SIZE = 5
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def project_config_path(config_path: str | os.PathLike[str] | None = None) -> pathlib.Path:
    """Resolve the deployment-local project config path."""
    raw = config_path or os.environ.get("ENGRAM_PROJECTS_CONFIG")
    if raw:
        return pathlib.Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
    return DEFAULT_PROJECT_CONFIG.expanduser()


def sql_identifier(value: str, label: str = "SQL identifier") -> str:
    """Validate an operator-supplied PostgreSQL identifier before interpolation."""
    if not _SQL_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _load_document(config_path: str | os.PathLike[str] | None = None) -> tuple[pathlib.Path, dict[str, Any]]:
    path = project_config_path(config_path)
    if not path.exists():
        return path, {}
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    if not isinstance(document, dict):
        raise ValueError(f"project config {path} must contain a TOML table")
    return path, document


def _table(document: Mapping[str, Any], key: str, path: pathlib.Path) -> dict[str, Any]:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"project config {path} [{key}] must be a table")
    return dict(value)


def _text_value(settings: "ProjectSettings", key: str, default: str | None = None) -> str | None:
    value = settings.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"project {settings.project!r} setting {key!r} must be a string")
    return value.strip()


@dataclass(frozen=True)
class ProjectSettings:
    """Merged defaults and one project's deployment settings."""

    project: str
    values: Mapping[str, Any]
    config_path: pathlib.Path

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def text(self, key: str, default: str | None = None) -> str | None:
        value = self.get(key, default)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"project {self.project!r} setting {key!r} must be a non-empty string")
        return value.strip()

    def integer(self, key: str, default: int) -> int:
        value = self.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"project {self.project!r} setting {key!r} must be an integer")
        return value

    def number(self, key: str, default: float) -> float:
        value = self.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"project {self.project!r} setting {key!r} must be a number")
        return float(value)

    def strings(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        value = self.get(key, default)
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"project {self.project!r} setting {key!r} must be a string array")
        return tuple(item.strip() for item in value)

    def table(self, key: str) -> dict[str, Any]:
        value = self.get(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"project {self.project!r} setting {key!r} must be a table")
        return dict(value)

    def records(self, key: str) -> tuple[dict[str, Any], ...]:
        value = self.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError(f"project {self.project!r} setting {key!r} must be an array of tables")
        return tuple(dict(item) for item in value)

    def path(self, key: str, default: str | os.PathLike[str] | None = None) -> pathlib.Path | None:
        raw = self.table("paths").get(key, default)
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"project {self.project!r} path {key!r} must be a non-empty string")
        expanded = pathlib.Path(os.path.expandvars(os.path.expanduser(raw.strip())))
        if not expanded.is_absolute():
            expanded = self.config_path.parent / expanded
        return expanded


def load_project_settings(
    project: str,
    config_path: str | os.PathLike[str] | None = None,
) -> ProjectSettings:
    """Load merged ``[defaults]`` and ``[projects.<project>]`` settings.

    Missing configuration is intentionally allowed so importing a generic
    module remains safe in a clean environment; a deployment that starts a
    flow/search service should provide the corresponding project table.
    """
    path, document = _load_document(config_path)
    defaults = _table(document, "defaults", path)
    projects = _table(document, "projects", path)
    project_values = projects.get(project, {})
    if not isinstance(project_values, dict):
        raise ValueError(f"project config {path} [projects.{project}] must be a table")

    merged = dict(defaults)
    for key, value in project_values.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return ProjectSettings(project=project, values=merged, config_path=path)


def load_default_settings(
    config_path: str | os.PathLike[str] | None = None,
) -> ProjectSettings:
    """Load deployment-wide defaults without selecting a project."""
    path, document = _load_document(config_path)
    return ProjectSettings(project="defaults", values=_table(document, "defaults", path), config_path=path)


def load_all_project_settings(
    config_path: str | os.PathLike[str] | None = None,
) -> dict[str, ProjectSettings]:
    """Load every configured project for generic cross-project helpers.

    An empty/missing file returns an empty mapping. This lets reusable library
    modules import cleanly on hosts that have not opted into a deployment yet.
    """
    path, document = _load_document(config_path)
    defaults = _table(document, "defaults", path)
    projects = _table(document, "projects", path)
    result: dict[str, ProjectSettings] = {}
    for project, project_values in projects.items():
        if not isinstance(project, str) or not isinstance(project_values, dict):
            raise ValueError(f"project config {path} [projects] entries must be named tables")
        merged = dict(defaults)
        for key, value in project_values.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        result[project] = ProjectSettings(project=project, values=merged, config_path=path)
    return result


def load_project_configs(
    config_path: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return normalized project metadata used by maintenance/reporting code.

    The shape deliberately matches the historical ``PROJECT_CONFIGS`` public
    mapping so callers can keep using the existing reporting helpers while
    the deployment-specific catalog lives in TOML.
    """
    configs: dict[str, dict[str, Any]] = {}
    for project, settings in load_all_project_settings(config_path).items():
        config: dict[str, Any] = {
            "banks": list(settings.strings("banks")),
            "mental_models": {},
            "probes": [],
            "recall_banks": set(settings.strings("recall_banks")),
            "workspace_prefixes": list(settings.strings("workspace_prefixes")),
            "log_suffix": _text_value(settings, "log_suffix", "") or "",
        }

        for bank, model_ids in settings.table("mental_models").items():
            if not isinstance(model_ids, list) or not all(
                isinstance(model_id, str) and model_id.strip() for model_id in model_ids
            ):
                raise ValueError(
                    f"project {project!r} setting 'mental_models.{bank}' must be a string array"
                )
            config["mental_models"][bank] = tuple(model_id.strip() for model_id in model_ids)

        for probe in settings.records("probes"):
            bank = probe.get("bank")
            query = probe.get("query")
            if not isinstance(bank, str) or not bank.strip() or not isinstance(query, str) or not query.strip():
                raise ValueError(
                    f"project {project!r} probe entries require non-empty 'bank' and 'query' strings"
                )
            config["probes"].append((bank.strip(), query.strip()))

        for key in ("code_bank", "coverage_prefix"):
            value = _text_value(settings, key)
            if value is not None:
                config[key] = value

        if settings.get("issues_repos") is not None or settings.get("pr_repos") is not None:
            repo_key = "issues_repos" if settings.get("issues_repos") is not None else "pr_repos"
            config["issues_repos"] = list(settings.strings(repo_key))

        code_table = _text_value(settings, "code_table")
        if code_table is None and settings.get("coverage_tables") is not None:
            # Accept the short-lived pre-release spelling while deployments
            # migrate to the single canonical ``code_table`` setting.
            code_table = _text_value(
                settings,
                "code_table",
                settings.table("coverage_tables").get("code"),
            )
        if code_table is not None:
            config["code_table"] = sql_identifier(
                code_table, f"project {project!r} code_table"
            )

        configs[project] = config
    return configs


def default_project_path(project: str) -> pathlib.Path:
    """Return a generic, non-organization-specific watch root."""
    return pathlib.Path("~/.engram/watch").expanduser() / project
