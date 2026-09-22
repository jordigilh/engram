from __future__ import annotations

import pytest

from engram.configured_sources import load_configured_sources


def test_unset_config_keeps_extra_sources_empty(monkeypatch):
    monkeypatch.delenv("ENGRAM_EXTRA_SOURCES_CONFIG", raising=False)

    assert load_configured_sources() == ()


def test_loads_source_patterns_and_resolves_relative_root(tmp_path):
    config = tmp_path / "sources.toml"
    config.write_text(
        """
[[sources]]
tag = "presentation"
root = "workspace"
docs_include = ["**/*.md"]
code_include = ["**/*.py"]
code_exclude = ["**/vendor/**"]
""".strip()
    )

    [source] = load_configured_sources(config)

    assert source.tag == "presentation"
    assert source.root == tmp_path / "workspace"
    assert source.docs_include == ("**/*.md",)
    assert source.code_include == ("**/*.py",)
    assert source.code_exclude == ("**/vendor/**",)


def test_rejects_duplicate_tags(tmp_path):
    config = tmp_path / "sources.toml"
    config.write_text(
        """
[[sources]]
tag = "same"
root = "/tmp/one"
docs_include = ["**/*.md"]

[[sources]]
tag = "same"
root = "/tmp/two"
code_include = ["**/*.py"]
""".strip()
    )

    with pytest.raises(ValueError, match="duplicate extra source tag"):
        load_configured_sources(config)
