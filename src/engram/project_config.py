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
# Source tags are persisted in filepath/document identifiers.  Release-line
# deployments conventionally use ``repo@release-vX.Y``; keep the project name
# validator strict while allowing that generic source-label syntax.
_SOURCE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
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
    language: str = "auto"
    document_format: str = "markdown"
    branch: str | None = None


def effective_source_tag(source: ProjectSource) -> str:
    """Return the stable tag used in indexed paths for one source.

    A branch-qualified source must not collide with the same checkout's main
    branch rows.  Deployment files may provide either ``v1.5`` or
    ``release/v1.5``; both use the established ``repo@release-v1.5`` path
    convention.  Other branch names are normalized into a filesystem-safe
    ``@branch-*`` suffix.
    """
    branch = (source.branch or "").strip()
    if not branch or branch in {"main", "default"}:
        return source.tag
    if branch.startswith("release/"):
        branch = branch.removeprefix("release/")
        return f"{source.tag}@release-{branch}"
    if re.fullmatch(r"v\d+\.\d+", branch):
        return f"{source.tag}@release-{branch}"
    safe_branch = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    return f"{source.tag}@branch-{safe_branch or 'unknown'}"


@dataclass(frozen=True)
class ProjectIssueSource:
    """One generic issue/tracker connector configured for a project.

    ``provider`` is deliberately a capability name rather than a project name:
    the same connector can be reused for any deployment.  GitHub connectors
    use ``repos`` and ``item_kinds``; Jira connectors use ``jql`` (or a
    deployment-local ``jql_file`` plus ``jql_template``); project/discussion
    connectors use their corresponding GitHub GraphQL settings.
    """

    provider: str
    repos: tuple[str, ...] = ()
    item_kinds: tuple[str, ...] = ("issue", "pr")
    limit: int = 10000
    jira_server: str | None = None
    jira_email: str | None = None
    jira_jql: str | None = None
    jira_jql_file: pathlib.Path | None = None
    jira_jql_template: str = "key in ({keys}) order by updated desc"
    jira_keychain_account: str = "jira-cli"
    jira_keychain_service: str = "jira-cloud-api-token"
    github_organization: str | None = None
    github_project_numbers: tuple[int, ...] = ()
    tracker_project: str | None = None
    chunk_size: int = 1200
    chunk_overlap: int = 300
    document_prefix: str | None = None


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
    issue_sources: tuple[ProjectIssueSource, ...]
    docs_live: bool
    code_live: bool
    code_poll_seconds: int
    git_sync: bool
    git_sync_seconds: int
    transcript_enabled: bool
    transcript_bank: str | None
    transcript_dir: pathlib.Path | None
    transcript_watermarks: pathlib.Path | None
    workspace_prefixes: tuple[str, ...]
    default_branch: str | None

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


def _boolean(settings: ProjectSettings, key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"project {settings.project!r} setting {key!r} must be a boolean")
    return value


def _positive_integer(settings: ProjectSettings, key: str, default: int) -> int:
    value = settings.integer(key, default)
    if value <= 0:
        raise ValueError(f"project {settings.project!r} setting {key!r} must be positive")
    return value


def _optional_path(settings: ProjectSettings, key: str) -> pathlib.Path | None:
    return settings.path(key) if settings.table("paths").get(key) is not None else None


def _issue_kinds(raw: Any, project: str, source: str) -> tuple[str, ...]:
    if raw is None:
        return ("issue", "pr")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"project {project!r} issue source {source!r} item_kinds must be a non-empty string array")
    kinds = tuple(item.strip().lower() for item in raw)
    if any(item not in {"issue", "pr"} for item in kinds):
        raise ValueError(f"project {project!r} issue source {source!r} item_kinds must contain issue and/or pr")
    return kinds


def _issue_source_path(raw: Any, settings: ProjectSettings, source: str) -> pathlib.Path | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"project {settings.project!r} issue source {source!r} jql_file must be a non-empty string")
    value = pathlib.Path(os.path.expandvars(os.path.expanduser(raw.strip())))
    if not value.is_absolute():
        value = settings.config_path.parent / value
    return value


def _issue_sources(
    settings: ProjectSettings,
    *,
    issues_repos: tuple[str, ...],
    pr_repos: tuple[str, ...],
    issue_provider: str,
    jira_server: str | None,
    jira_email: str | None,
    jira_jql: str | None,
    jira_jql_file: pathlib.Path | None,
    jira_jql_template: str,
    jira_project: str | None,
    jira_limit: int,
    jira_keychain_account: str,
    jira_keychain_service: str,
) -> tuple[ProjectIssueSource, ...]:
    """Load explicit connector records, with compatibility for old flat keys."""
    raw_sources = settings.records("issue_sources")
    if not raw_sources:
        sources: list[ProjectIssueSource] = []
        if issues_repos and issue_provider != "jira":
            sources.append(
                ProjectIssueSource(
                    provider="github",
                    repos=issues_repos,
                    item_kinds=("issue",) if pr_repos else ("issue", "pr"),
                    limit=10000,
                )
            )
        if pr_repos:
            sources.append(
                ProjectIssueSource(
                    provider="github",
                    repos=pr_repos,
                    item_kinds=("pr",),
                    limit=_positive_integer(settings, "pr_limit", 10000),
                )
            )
        if issue_provider == "jira" or jira_project is not None:
            effective_jql = jira_jql
            if effective_jql is None and jira_project is not None:
                effective_jql = f"project = {jira_project} order by created desc"
            sources.append(
                ProjectIssueSource(
                    provider="jira",
                    limit=jira_limit,
                    jira_server=jira_server,
                    jira_email=jira_email,
                    jira_jql=effective_jql,
                    jira_jql_file=jira_jql_file,
                    jira_jql_template=jira_jql_template,
                    jira_keychain_account=jira_keychain_account,
                    jira_keychain_service=jira_keychain_service,
                    tracker_project=jira_project,
                )
            )
        return tuple(sources)

    sources: list[ProjectIssueSource] = []
    for index, raw in enumerate(raw_sources):
        provider = raw.get("provider", raw.get("kind"))
        source_name = str(raw.get("name", f"{provider or 'source'}-{index}"))
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} requires provider")
        provider = provider.strip().lower()
        if provider not in {"github", "jira", "github_discussions", "github_projects"}:
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} has unsupported provider {provider!r}")

        repos_raw = raw.get("repos", raw.get("issues_repos", ()))
        if isinstance(repos_raw, str):
            repos_raw = [item.strip() for item in repos_raw.split(",") if item.strip()]
        elif isinstance(repos_raw, tuple):
            repos_raw = list(repos_raw)
        if not isinstance(repos_raw, list) or not all(isinstance(item, str) and item.strip() for item in repos_raw):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} repos must be a string array")
        repos = tuple(item.strip() for item in repos_raw)
        if provider in {"github", "github_discussions"} and not repos:
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} requires repos")

        limit = raw.get("limit", 10000)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} limit must be positive")
        source_jira = raw.get("jira", {})
        if not isinstance(source_jira, dict):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} jira must be a table")
        # A nested jira table is accepted for symmetry with the project-level
        # [projects.<name>.jira] table; flat keys remain convenient in small
        # deployment files.
        def jira_value(key: str, default: Any = None) -> Any:
            return raw.get(key, source_jira.get(key, default))

        source_server = jira_value("server", jira_server)
        source_email = jira_value("email", jira_email)
        source_jql = jira_value("jql", jira_jql)
        jql_file = _issue_source_path(jira_value("jql_file", jira_jql_file), settings, source_name)
        template = jira_value("jql_template", jira_jql_template)
        account = jira_value("keychain_account", jira_keychain_account)
        service = jira_value("keychain_service", jira_keychain_service)
        tracker_project = jira_value("project", jira_value("tracker_project", jira_project))
        if provider == "jira" and not source_jql and not jql_file and isinstance(tracker_project, str):
            source_jql = f"project = {tracker_project} order by created desc"
        for key, value in (("server", source_server), ("email", source_email), ("jql", source_jql), ("jql_template", template), ("keychain_account", account), ("keychain_service", service)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"project {settings.project!r} issue source {source_name!r} {key} must be a non-empty string")
        if not isinstance(template, str) or not template.strip():
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} jql_template must be a non-empty string")
        if provider == "jira" and not all(isinstance(value, str) and value.strip() for value in (source_server, source_email)):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} Jira configuration requires server and email")
        if provider == "jira" and not source_jql and not jql_file:
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} requires jql or jql_file")
        if tracker_project is not None and (not isinstance(tracker_project, str) or not tracker_project.strip()):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} project must be a non-empty string")

        numbers_raw = raw.get("project_numbers", raw.get("numbers", []))
        if isinstance(numbers_raw, int) and not isinstance(numbers_raw, bool):
            numbers_raw = [numbers_raw]
        elif isinstance(numbers_raw, tuple):
            numbers_raw = list(numbers_raw)
        if not isinstance(numbers_raw, list) or not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in numbers_raw):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} project_numbers must be positive integers")
        organization = raw.get("organization", raw.get("github_organization"))
        if organization is not None and (not isinstance(organization, str) or not organization.strip()):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} organization must be a non-empty string")
        if provider == "github_projects" and (not organization or not numbers_raw):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} requires organization and project_numbers")

        chunk_size = raw.get("chunk_size", 1200)
        chunk_overlap = raw.get("chunk_overlap", 300)
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (chunk_size, chunk_overlap)):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} chunk sizes must be positive integers")
        if chunk_overlap >= chunk_size:
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} chunk_overlap must be smaller than chunk_size")
        prefix = raw.get("document_prefix")
        if prefix is not None and (not isinstance(prefix, str) or not prefix.strip()):
            raise ValueError(f"project {settings.project!r} issue source {source_name!r} document_prefix must be a non-empty string")
        sources.append(
            ProjectIssueSource(
                provider=provider,
                repos=repos,
                item_kinds=_issue_kinds(raw.get("item_kinds", raw.get("kinds")), settings.project, source_name),
                limit=limit,
                jira_server=source_server.strip() if isinstance(source_server, str) else None,
                jira_email=source_email.strip() if isinstance(source_email, str) else None,
                jira_jql=source_jql.strip() if isinstance(source_jql, str) else None,
                jira_jql_file=jql_file,
                jira_jql_template=template.strip(),
                jira_keychain_account=account.strip(),
                jira_keychain_service=service.strip(),
                github_organization=organization.strip() if isinstance(organization, str) else None,
                github_project_numbers=tuple(numbers_raw),
                tracker_project=tracker_project.strip() if isinstance(tracker_project, str) else None,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                document_prefix=prefix.strip() if isinstance(prefix, str) else None,
            )
        )
    return tuple(sources)


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
        if not isinstance(tag, str) or not _SOURCE_TAG_RE.fullmatch(tag):
            raise ValueError(f"project {settings.project!r} source {index} tag must be a simple name")
        language = raw.get("language", "auto")
        if not isinstance(language, str) or not language.strip():
            raise ValueError(f"project {settings.project!r} source {tag!r} language must be a non-empty string")
        document_format = raw.get("document_format", raw.get("docs_format", "markdown"))
        if not isinstance(document_format, str) or document_format.strip().lower() not in {"markdown", "md", "pdf"}:
            raise ValueError(
                f"project {settings.project!r} source {tag!r} document_format must be 'markdown' or 'pdf'"
            )
        branch = raw.get("branch")
        if branch is not None and (not isinstance(branch, str) or not branch.strip()):
            raise ValueError(f"project {settings.project!r} source {tag!r} branch must be a non-empty string")
        source = ProjectSource(
            tag=tag,
            root=_source_root(raw.get("root"), settings, tag),
            docs_include=_patterns(raw.get("docs_include"), "docs_include", settings.project, tag),
            docs_exclude=_patterns(raw.get("docs_exclude"), "docs_exclude", settings.project, tag),
            code_include=_patterns(raw.get("code_include"), "code_include", settings.project, tag),
            code_exclude=_patterns(raw.get("code_exclude"), "code_exclude", settings.project, tag),
            language=language.strip().lower(),
            document_format="pdf" if document_format.strip().lower() == "pdf" else "markdown",
            branch=branch.strip() if isinstance(branch, str) else None,
        )
        if not source.docs_include and not source.code_include:
            raise ValueError(f"project {settings.project!r} source {tag!r} must configure docs or code patterns")
        effective_tag = effective_source_tag(source)
        if effective_tag in seen:
            raise ValueError(f"project {settings.project!r} has duplicate source tag {effective_tag!r}")
        seen.add(effective_tag)
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
    raw_issue_sources = settings.records("issue_sources")
    configured_issue_provider = settings.get("issue_provider", "github")
    if not isinstance(configured_issue_provider, str):
        raise ValueError(f"project {project!r} issue_provider must be a string")
    configured_issue_provider = configured_issue_provider.strip().lower()
    configured_jira = configured_issue_provider == "jira" or settings.get("jira_project") is not None
    if not docs_sources and not code_sources and not settings.strings("issues_repos") and not settings.strings("pr_repos") and not configured_jira and not raw_issue_sources:
        raise ValueError(f"project {project!r} must configure sources or issues_repos")
    if docs_sources and not docs_bank:
        raise ValueError(f"project {project!r} has documentation sources but no docs_bank")
    if code_sources and not code_table:
        raise ValueError(f"project {project!r} has code sources but no code_table")
    configured_issues_repos = settings.strings("issues_repos") if settings.get("issues_repos") is not None else ()
    configured_pr_repos = settings.strings("pr_repos") if settings.get("pr_repos") is not None else ()
    issues_repos = configured_issues_repos or configured_pr_repos
    pr_repos = configured_pr_repos
    issue_provider = configured_issue_provider
    if issue_provider not in {"github", "jira"}:
        raise ValueError(f"project {project!r} issue_provider must be 'github' or 'jira'")
    jira = settings.table("jira")
    jira_server = jira.get("server", settings.get("jira_server"))
    jira_email = jira.get("email", settings.get("jira_email"))
    jira_jql = jira.get("jql", settings.get("jira_jql"))
    jira_jql_file = _issue_source_path(jira.get("jql_file", settings.get("jira_jql_file")), settings, "jira")
    jira_jql_template = jira.get("jql_template", settings.get("jira_jql_template", "key in ({keys}) order by updated desc"))
    jira_project = settings.text("jira_project")
    if jira_project is None:
        candidate = jira.get("project", jira.get("tracker_project"))
        if candidate is not None:
            if not isinstance(candidate, str) or not candidate.strip():
                raise ValueError(f"project {project!r} jira.project must be a non-empty string")
            jira_project = candidate.strip()
    for key, value in (("server", jira_server), ("email", jira_email), ("jql", jira_jql), ("jql_template", jira_jql_template)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"project {project!r} jira.{key} must be a non-empty string")
    if not isinstance(jira_jql_template, str) or not jira_jql_template.strip():
        raise ValueError(f"project {project!r} jira.jql_template must be a non-empty string")
    if issue_provider == "jira" and (
        not all(isinstance(value, str) and value.strip() for value in (jira_server, jira_email))
        or (not isinstance(jira_jql, str) or not jira_jql.strip()) and jira_jql_file is None and jira_project is None
    ):
        raise ValueError(f"project {project!r} Jira configuration requires server, email, and jql or jql_file")
    jira_keychain_account = jira.get("keychain_account", "jira-cli")
    jira_keychain_service = jira.get("keychain_service", "jira-cloud-api-token")
    if not isinstance(jira_keychain_account, str) or not jira_keychain_account.strip():
        raise ValueError(f"project {project!r} jira.keychain_account must be a non-empty string")
    if not isinstance(jira_keychain_service, str) or not jira_keychain_service.strip():
        raise ValueError(f"project {project!r} jira.keychain_service must be a non-empty string")
    jira_limit = _positive_integer(
        settings,
        "jira_limit",
        10000,
    )
    issue_sources = _issue_sources(
        settings,
        issues_repos=configured_issues_repos,
        pr_repos=pr_repos,
        issue_provider=issue_provider,
        jira_server=jira_server.strip() if isinstance(jira_server, str) else None,
        jira_email=jira_email.strip() if isinstance(jira_email, str) else None,
        jira_jql=jira_jql.strip() if isinstance(jira_jql, str) else None,
        jira_jql_file=jira_jql_file,
        jira_jql_template=jira_jql_template.strip(),
        jira_project=jira_project,
        jira_limit=jira_limit,
        jira_keychain_account=jira_keychain_account.strip(),
        jira_keychain_service=jira_keychain_service.strip(),
    )
    if issue_sources and not issues_bank:
        raise ValueError(f"project {project!r} has issue sources but no issues_bank")
    workspace_prefixes = settings.strings("workspace_prefixes")
    transcript_bank = settings.text("transcript_bank", settings.text("transcripts_bank"))
    transcript_setting = settings.get("transcripts", settings.get("transcript_enabled", False))
    if not isinstance(transcript_setting, bool):
        raise ValueError(f"project {project!r} setting 'transcripts' must be a boolean")
    transcript_enabled = transcript_setting
    transcript_dir = settings.path("transcripts_dir")
    transcript_watermarks = settings.path("transcript_watermarks")
    if transcript_enabled and (not transcript_bank or not transcript_dir):
        raise ValueError(f"project {project!r} transcripts requires transcript_bank and paths.transcripts_dir")
    if transcript_enabled and not workspace_prefixes:
        raise ValueError(f"project {project!r} transcripts requires workspace_prefixes")
    if transcript_bank is not None and not _BANK_RE.fullmatch(transcript_bank):
        raise ValueError(f"unsafe project {project!r} transcript_bank: {transcript_bank!r}")
    default_branch = settings.text("default_branch")
    pg_pool_min_size = _positive_integer(settings, "pg_pool_min_size", DEFAULT_POOL_MIN_SIZE)
    pg_pool_max_size = _positive_integer(settings, "pg_pool_max_size", DEFAULT_POOL_MAX_SIZE)
    if pg_pool_max_size < pg_pool_min_size:
        raise ValueError(f"project {project!r} pg_pool_max_size must be at least pg_pool_min_size")
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
        pg_pool_min_size=pg_pool_min_size,
        pg_pool_max_size=pg_pool_max_size,
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
        issues_poll_seconds=_positive_integer(settings, "issues_poll_seconds", 300),
        issue_sources=issue_sources,
        docs_live=_boolean(settings, "docs_live", True),
        code_live=_boolean(settings, "code_live", True),
        code_poll_seconds=_positive_integer(settings, "code_poll_seconds", 300),
        git_sync=_boolean(settings, "git_sync", False),
        git_sync_seconds=_positive_integer(settings, "git_sync_seconds", 6 * 3600),
        transcript_enabled=transcript_enabled,
        transcript_bank=transcript_bank,
        transcript_dir=transcript_dir,
        transcript_watermarks=transcript_watermarks,
        workspace_prefixes=workspace_prefixes,
        default_branch=default_branch,
    )
