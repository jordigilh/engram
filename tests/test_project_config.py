from __future__ import annotations

import pytest

from engram.project_config import (
    effective_source_tag,
    load_adapter_config,
    load_project_settings,
    project_config_path,
    sql_identifier,
)


def test_missing_config_is_safe(tmp_path):
    settings = load_project_settings("example", tmp_path / "missing.toml")

    assert settings.values == {}
    assert settings.strings("missing", ("one", "two")) == ("one", "two")
    assert settings.path("missing", "workspace") == tmp_path / "workspace"


def test_defaults_and_project_values_merge_nested_tables(tmp_path):
    config = tmp_path / "projects.toml"
    config.write_text(
        """
[defaults]
hindsight_url = "http://hindsight.internal:8888"
issues_poll_seconds = 300

[defaults.paths]
state = "state"
shared = "shared"

[projects.demo]
issues_poll_seconds = 60
issues_repos = ["org/demo"]

[projects.demo.paths]
state = "demo-state"
""".strip()
    )

    settings = load_project_settings("demo", config)

    assert settings.text("hindsight_url") == "http://hindsight.internal:8888"
    assert settings.integer("issues_poll_seconds", 0) == 60
    assert settings.strings("issues_repos") == ("org/demo",)
    assert settings.path("state") == config.parent / "demo-state"
    assert settings.path("shared") == config.parent / "shared"


def test_explicit_config_path_wins_over_environment(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.toml"
    explicit.write_text("[projects.demo]\nname = \"explicit\"\n")
    monkeypatch.setenv("ENGRAM_PROJECTS_CONFIG", str(tmp_path / "other.toml"))

    assert project_config_path(explicit) == explicit.resolve()
    assert load_project_settings("demo", explicit).text("name") == "explicit"


def test_invalid_values_are_rejected(tmp_path):
    config = tmp_path / "projects.toml"
    config.write_text("[projects.demo]\nissues_poll_seconds = \"slow\"\n")

    settings = load_project_settings("demo", config)
    with pytest.raises(ValueError, match="must be an integer"):
        settings.integer("issues_poll_seconds", 300)


def test_sql_identifier_rejects_injection():
    assert sql_identifier("code_embeddings") == "code_embeddings"
    with pytest.raises(ValueError, match="unsafe"):
        sql_identifier("code_embeddings; DROP TABLE memories")


def test_load_adapter_config_is_project_data_not_source_code(tmp_path):
    config_path = tmp_path / "projects.toml"
    config_path.write_text(
        """
[defaults]
hindsight_url = "http://hindsight:8888"

[projects.demo]
docs_bank = "demo-docs"
issues_bank = "demo-issues"
code_table = "demo_code_embeddings"
issues_repos = ["org/demo"]
workspace_prefixes = ["Users-demo"]

[projects.demo.paths]
cocoindex_db = "state/demo.db"

[[projects.demo.sources]]
tag = "demo"
root = "checkout"
language = "go"
docs_include = ["**/*.md"]
code_include = ["**/*.go"]
code_exclude = ["**/vendor/**"]
""".strip()
    )

    settings = load_adapter_config("demo", config_path)

    assert settings.docs_bank == "demo-docs"
    assert settings.issues_bank == "demo-issues"
    assert settings.code_table == "demo_code_embeddings"
    assert settings.issues_repos == ("org/demo",)
    assert settings.cocoindex_db == tmp_path / "state/demo.db"
    assert settings.docs_sources[0].root == tmp_path / "checkout"
    assert settings.code_sources[0].code_include == ("**/*.go",)
    assert settings.code_sources[0].language == "go"


def test_invalid_code_table_is_rejected():
    with pytest.raises(ValueError, match="unsafe"):
        sql_identifier("demo;drop_table")


def test_code_only_source_does_not_require_a_docs_bank(tmp_path):
    config_path = tmp_path / "projects.toml"
    config_path.write_text(
        """
[projects.code]
code_table = "code_embeddings"

[[projects.code.sources]]
tag = "repo"
root = "."
code_include = ["**/*.rs"]
""".strip()
    )

    settings = load_adapter_config("code", config_path)

    assert settings.docs_bank is None
    assert settings.code_sources[0].code_include == ("**/*.rs",)


def test_jira_issue_provider_is_configurable_without_github_repositories(tmp_path):
    config_path = tmp_path / "projects.toml"
    config_path.write_text(
        """
[projects.jira]
issues_bank = "jira-issues"
issue_provider = "jira"

[projects.jira.jira]
server = "https://jira.example"
email = "operator@example.com"
jql = "parent = EX-1 OR key = EX-1 order by created asc"
""".strip()
    )

    settings = load_adapter_config("jira", config_path)

    assert settings.issues_provider == "jira"
    assert settings.jira_jql.startswith("parent = EX-1")
    assert settings.issues_repos == ()


def test_release_source_tag_is_branch_qualified(tmp_path):
    config_path = tmp_path / "projects.toml"
    config_path.write_text(
        """
[projects.release]
code_table = "release_code_embeddings"

[[projects.release.sources]]
tag = "repo"
root = "checkout"
branch = "release/v1.5"
language = "go"
code_include = ["**/*.go"]
""".strip()
    )

    settings = load_adapter_config("release", config_path)

    assert effective_source_tag(settings.code_sources[0]) == "repo@release-v1.5"


def test_explicit_issue_sources_can_combine_github_prs_and_jira(tmp_path):
    config_path = tmp_path / "projects.toml"
    config_path.write_text(
        """
[projects.combo]
issues_bank = "combo-issues"
pr_repos = ["org/repo"]
jira_project = "COST"

[projects.combo.jira]
server = "https://jira.example"
email = "operator@example.com"
jql = "project = COST"
""".strip()
    )

    settings = load_adapter_config("combo", config_path)

    assert [(source.provider, source.item_kinds) for source in settings.issue_sources] == [
        ("github", ("pr",)),
        ("jira", ("issue", "pr")),
    ]
