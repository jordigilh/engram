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
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BANK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except OSError as exc:
        raise ValueError(f"cannot read project config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid project config {path}: {exc}") from exc
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


def default_project_path(project: str) -> pathlib.Path:
    """Return a generic, non-organization-specific watch root."""
    return pathlib.Path("~/.engram/watch").expanduser() / project


def load_configured_mental_models(
    config_path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Load optional fully described mental models from project TOML."""
    models: list[dict[str, Any]] = []
    for project, settings in load_all_project_settings(config_path).items():
        for index, model in enumerate(settings.records("models")):
            bank = model.get("bank")
            model_id = model.get("id")
            source_query = model.get("source_query")
            if not all(isinstance(value, str) and value.strip() for value in (bank, model_id, source_query)):
                raise ValueError(
                    f"project {project!r} models[{index}] requires non-empty bank, id, and source_query"
                )
            item = dict(model)
            item.setdefault("name", model_id)
            item.setdefault("max_tokens", 2048)
            item.setdefault("trigger", {"mode": "full", "refresh_after_consolidation": False})
            item["bank"] = bank.strip()
            item["id"] = model_id.strip()
            item["source_query"] = source_query.strip()
            models.append(item)
    return models


@dataclass(frozen=True)
class ProjectSource:
    """One repository/workspace source consumed by the generic adapters."""

    tag: str
    root: pathlib.Path
    docs_include: tuple[str, ...]
    docs_exclude: tuple[str, ...]
    code_include: tuple[str, ...]
    code_exclude: tuple[str, ...]
    language: str = "python"


@dataclass(frozen=True)
class ProjectAdapterConfig:
    """Validated settings required by the generic flow and search adapters."""

    project: str
    config_path: pathlib.Path
    hindsight_url: str
    pg_dsn: str
    embedding_model: str
    pg_pool_min_size: int
    pg_pool_max_size: int
    docs_bank: str | None
    issues_bank: str | None
    code_table: str | None
    code_sources: tuple[ProjectSource, ...]
    docs_sources: tuple[ProjectSource, ...]
    issues_repos: tuple[str, ...]
    issues_provider: str
    jira_server: str | None
    jira_email: str | None
    jira_jql: str | None
    jira_keychain_account: str
    jira_keychain_service: str
    cocoindex_db: pathlib.Path
    issues_poll_seconds: int

    @property
    def sources(self) -> tuple[ProjectSource, ...]:
        return tuple(dict.fromkeys((*self.docs_sources, *self.code_sources)))


def _patterns(raw: Any, field: str, project: str, source: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise ValueError(f"project {project!r} source {source!r} {field} must be a string array")
    return tuple(item.strip() for item in raw)


def _source_root(raw: Any, settings: ProjectSettings, source: str) -> pathlib.Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"project {settings.project!r} source {source!r} root must be a non-empty string")
    value = pathlib.Path(os.path.expandvars(os.path.expanduser(raw.strip())))
    if not value.is_absolute():
        value = settings.config_path.parent / value
    return value


def _load_sources(settings: ProjectSettings) -> tuple[ProjectSource, ...]:
    sources: list[ProjectSource] = []
    seen: set[str] = set()
    for index, raw in enumerate(settings.records("sources")):
        tag = raw.get("tag", raw.get("name"))
        if not isinstance(tag, str) or not _NAME_RE.fullmatch(tag):
            raise ValueError(f"project {settings.project!r} source {index} tag must be a simple name")
        if tag in seen:
            raise ValueError(f"project {settings.project!r} has duplicate source tag {tag!r}")
        language = raw.get("language", "python")
        if not isinstance(language, str) or not language.strip():
            raise ValueError(f"project {settings.project!r} source {tag!r} language must be a non-empty string")
        source = ProjectSource(
            tag=tag,
            root=_source_root(raw.get("root"), settings, tag),
            docs_include=_patterns(raw.get("docs_include"), "docs_include", settings.project, tag),
            docs_exclude=_patterns(raw.get("docs_exclude"), "docs_exclude", settings.project, tag),
            code_include=_patterns(raw.get("code_include"), "code_include", settings.project, tag),
            code_exclude=_patterns(raw.get("code_exclude"), "code_exclude", settings.project, tag),
            language=language.strip(),
        )
        if not source.docs_include and not source.code_include:
            raise ValueError(f"project {settings.project!r} source {tag!r} must configure docs or code patterns")
        seen.add(tag)
        sources.append(source)
    return tuple(sources)


def _bank(settings: ProjectSettings, explicit: str, suffix: str) -> str | None:
    value = settings.text(explicit)
    if value is not None:
        if not _BANK_RE.fullmatch(value):
            raise ValueError(f"unsafe project {settings.project!r} {explicit}: {value!r}")
        return value
    banks = settings.strings("banks")
    matches = tuple(bank for bank in banks if bank.endswith(suffix))
    if len(matches) > 1:
        raise ValueError(f"project {settings.project!r} has multiple {suffix} banks; set {explicit!r}")
    if matches and not _BANK_RE.fullmatch(matches[0]):
        raise ValueError(f"unsafe project {settings.project!r} bank: {matches[0]!r}")
    return matches[0] if matches else None


def load_adapter_config(
    project: str,
    config_path: str | os.PathLike[str] | None = None,
) -> ProjectAdapterConfig:
    """Load and validate one project's generic adapter configuration."""
    if not _NAME_RE.fullmatch(project):
        raise ValueError(f"invalid project name: {project!r}")
    settings = load_project_settings(project, config_path)
    sources = _load_sources(settings)
    docs_sources = tuple(source for source in sources if source.docs_include)
    code_sources = tuple(source for source in sources if source.code_include)
    docs_bank = _bank(settings, "docs_bank", "-docs")
    issues_bank = _bank(settings, "issues_bank", "-issues")
    code_table = settings.text("code_table")
    if code_table is not None:
        code_table = sql_identifier(code_table, f"project {project!r} code_table")
    configured_jira = settings.get("issue_provider", "github") == "jira"
    if not docs_sources and not code_sources and not settings.strings("issues_repos") and not configured_jira:
        raise ValueError(f"project {project!r} must configure sources or issues_repos")
    if docs_sources and not docs_bank:
        raise ValueError(f"project {project!r} has documentation sources but no docs_bank")
    if code_sources and not code_table:
        raise ValueError(f"project {project!r} has code sources but no code_table")
    issues_repos = (
        settings.strings("issues_repos")
        if settings.get("issues_repos") is not None
        else settings.strings("pr_repos")
    )
    issue_provider = settings.text("issue_provider", "github") or "github"
    if issue_provider not in {"github", "jira"}:
        raise ValueError(f"project {project!r} issue_provider must be 'github' or 'jira'")
    jira = settings.table("jira")
    jira_server = jira.get("server")
    jira_email = jira.get("email")
    jira_jql = jira.get("jql")
    for key, value in (("server", jira_server), ("email", jira_email), ("jql", jira_jql)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"project {project!r} jira.{key} must be a non-empty string")
    if issue_provider == "jira" and not all(
        isinstance(value, str) and value.strip() for value in (jira_server, jira_email, jira_jql)
    ):
        raise ValueError(f"project {project!r} Jira configuration requires server, email, and jql")
    jira_keychain_account = jira.get("keychain_account", "jira-cli")
    jira_keychain_service = jira.get("keychain_service", "jira-cloud-api-token")
    if not isinstance(jira_keychain_account, str) or not jira_keychain_account.strip():
        raise ValueError(f"project {project!r} jira.keychain_account must be a non-empty string")
    if not isinstance(jira_keychain_service, str) or not jira_keychain_service.strip():
        raise ValueError(f"project {project!r} jira.keychain_service must be a non-empty string")
    return ProjectAdapterConfig(
        project=project,
        config_path=settings.config_path,
        hindsight_url=os.environ.get(
            "HINDSIGHT_URL",
            settings.text("hindsight_url", DEFAULT_HINDSIGHT_URL) or DEFAULT_HINDSIGHT_URL,
        ),
        pg_dsn=os.environ.get(
            "COCOINDEX_PG_URL",
            settings.text("pg_dsn", DEFAULT_PG_DSN) or DEFAULT_PG_DSN,
        ),
        embedding_model=settings.text("embedding_model", DEFAULT_EMBEDDING_MODEL) or DEFAULT_EMBEDDING_MODEL,
        pg_pool_min_size=settings.integer("pg_pool_min_size", DEFAULT_POOL_MIN_SIZE),
        pg_pool_max_size=settings.integer("pg_pool_max_size", DEFAULT_POOL_MAX_SIZE),
        docs_bank=docs_bank,
        issues_bank=issues_bank,
        code_table=code_table,
        docs_sources=docs_sources,
        code_sources=code_sources,
        issues_repos=issues_repos,
        issues_provider=issue_provider,
        jira_server=jira_server.strip() if isinstance(jira_server, str) else None,
        jira_email=jira_email.strip() if isinstance(jira_email, str) else None,
        jira_jql=jira_jql.strip() if isinstance(jira_jql, str) else None,
        jira_keychain_account=jira_keychain_account.strip(),
        jira_keychain_service=jira_keychain_service.strip(),
        cocoindex_db=settings.path("cocoindex_db", f"~/.engram/{project}-cocoindex.db")
        or pathlib.Path.home() / ".engram" / f"{project}-cocoindex.db",
        issues_poll_seconds=settings.integer("issues_poll_seconds", 300),
    )
