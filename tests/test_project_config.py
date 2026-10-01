from __future__ import annotations

import pytest

from engram.project_config import load_project_settings, project_config_path, sql_identifier


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
