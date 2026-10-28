from __future__ import annotations

import pytest

from engram.search import engram as search


@pytest.fixture
def code_root(monkeypatch, tmp_path):
    root = tmp_path / "example"
    source = root / "pkg" / "handler.go"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package handler\n\n"
        "func HandleApprove() error {\n"
        "\treturn nil\n"
        "}\n"
    )
    roots = [("example-repo", root, ["**/*.go"], ["**/vendor/**"], "go", None)]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")
    monkeypatch.setattr(
        search,
        "_SCOPE_ALIASES",
        {"example-repo": frozenset({"example-repo", "example"})},
    )
    return root, source


def test_read_source_accepts_semantic_repository_prefixed_path(code_root):
    _root, _source = code_root

    result = search.read_source_code(
        "example/pkg/handler.go",
        repo="example",
        branch="main",
        start_line=3,
        end_line=5,
    )

    assert result["schema_version"] == "engram-code-read.v1"
    assert result["repo"] == "example"
    assert result["source_tag"] == "example-repo"
    assert result["filepath"] == "pkg/handler.go"
    assert result["truncated"] is False
    assert "func HandleApprove" in result["source"]


def test_read_source_short_file_without_end_line_is_not_truncated(code_root):
    result = search.read_source_code("pkg/handler.go", repo="example", max_chars=20000)

    assert result["end_line"] == result["total_lines"]
    assert result["truncated"] is False


def test_read_source_caps_characters_and_reports_truncation(code_root):
    _root, _source = code_root

    result = search.read_source_code(
        "pkg/handler.go",
        repo="example",
        max_chars=10,
    )

    assert result["returned_chars"] == 10
    assert result["total_chars"] > result["returned_chars"]
    assert result["truncated"] is True
    assert result["end_line"] == 1


def test_read_source_rejects_traversal_and_absolute_paths(code_root):
    _root, _source = code_root

    for filepath in ("../handler.go", "/etc/passwd"):
        with pytest.raises(ValueError, match="relative path|not found"):
            search.read_source_code(filepath, repo="example")


def test_read_source_rejects_invalid_ranges_and_excluded_files(code_root):
    root, _source = code_root
    excluded = root / "vendor" / "generated.go"
    excluded.parent.mkdir()
    excluded.write_text("package generated\n")

    invalid_calls = [
        {"filepath": "", "repo": "example"},
        {"filepath": "pkg/handler.go", "repo": "example", "start_line": 0},
        {"filepath": "pkg/handler.go", "repo": "example", "end_line": 1, "start_line": 2},
        {"filepath": "pkg/handler.go", "repo": "example", "max_chars": 0},
        {"filepath": "pkg/./handler.go", "repo": "example"},
    ]
    for call in invalid_calls:
        with pytest.raises(ValueError):
            search.read_source_code(**call)

    with pytest.raises(ValueError, match="not found"):
        search.read_source_code("vendor/generated.go", repo="example")


def test_read_source_prunes_directory_only_exclusions(code_root, monkeypatch):
    root, _source = code_root
    excluded = root / "vendor" / "generated.go"
    excluded.parent.mkdir()
    excluded.write_text("package generated\n")
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", [
        ("example-repo", root, ["**/*.go"], ["**/vendor"], "go", None),
    ])

    with pytest.raises(ValueError, match="not found"):
        search.read_source_code("vendor/generated.go", repo="example")


def test_read_source_rejects_ambiguous_scope_and_out_of_range(code_root, monkeypatch, tmp_path):
    root, _source = code_root
    second_root = tmp_path / "second-example"
    second_source = second_root / "pkg" / "handler.go"
    second_source.parent.mkdir(parents=True)
    second_source.write_text("package second\n")
    monkeypatch.setattr(
        search,
        "_PATTERN_SEARCH_ROOTS",
        [
            ("example-repo", root, ["**/*.go"], [], "go", None),
            ("example-repo-copy", second_root, ["**/*.go"], [], "go", None),
        ],
    )
    monkeypatch.setattr(
        search,
        "_SCOPE_ALIASES",
        {
            "example-repo": frozenset({"example"}),
            "example-repo-copy": frozenset({"example"}),
        },
    )

    with pytest.raises(ValueError, match="ambiguous"):
        search.read_source_code("pkg/handler.go", repo="example")

    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", [
        ("example-repo", root, ["**/*.go"], [], "go", None),
    ])
    with pytest.raises(ValueError, match="beyond"):
        search.read_source_code("pkg/handler.go", repo="example", start_line=100)


def test_read_source_resolves_release_branch_alias(code_root, monkeypatch, tmp_path):
    _root, _source = code_root
    release_root = tmp_path / "example-release-v1.5"
    release_source = release_root / "pkg" / "handler.go"
    release_source.parent.mkdir(parents=True)
    release_source.write_text("package release\n\nfunc Release() {}\n")
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", [
        ("example@release-v1.5", release_root, ["**/*.go"], [], "go", "v1.5"),
    ])
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")
    monkeypatch.setattr(search, "_SCOPE_ALIASES", {
        "example@release-v1.5": frozenset({"example", "example@release-v1.5"}),
    })

    result = search.read_source_code("pkg/handler.go", repo="example", branch="v1.5")

    assert result["branch"] == "v1.5"
    assert "func Release" in result["source"]


def test_read_source_rejects_symlink_escape(code_root):
    root, _source = code_root
    outside = root.parent / "outside.go"
    outside.write_text("package outside\n")
    link = root / "pkg" / "outside.go"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="not found"):
        search.read_source_code("pkg/outside.go", repo="example")


def test_read_source_rejects_unknown_scope(code_root):
    with pytest.raises(ValueError, match="unknown code repository scope"):
        search.read_source_code("pkg/handler.go", repo="unknown")


def test_code_read_formatter_exposes_provenance_and_truncation(code_root):
    result = search.read_source_code("pkg/handler.go", repo="example", max_chars=10)

    formatted = search._format_code_read_result(result)

    assert "schema engram-code-read.v1" in formatted
    assert "source_tag: example-repo" in formatted
    assert "truncated: true" in formatted


def test_mcp_code_tools_expose_bounded_read_and_actionable_scope_errors(monkeypatch, code_root):
    from engram import mcp_compat

    class FakeServer:
        def __init__(self):
            self.tools = {}

        def tool(self, *, name):
            def register(function):
                self.tools[name] = function
                return function

            return register

    server = FakeServer()
    monkeypatch.setattr(mcp_compat, "make_server", lambda *args, **kwargs: server)

    def stop_server(*args, **kwargs):
        raise RuntimeError("captured")

    monkeypatch.setattr(mcp_compat, "run_server", stop_server)
    with pytest.raises(RuntimeError, match="captured"):
        search._run_mcp_server(project="example")

    assert {"example_code_search", "example_code_read", "example_code_pattern_search"} <= set(server.tools)
    read = server.tools["example_code_read"](
        "pkg/handler.go", repo="example", start_line=3, end_line=5
    )
    assert "source_tag: example-repo" in read
    assert "Code search error" in server.tools["example_code_search"]("x", repo="unknown")
    assert "Code pattern search error" in server.tools["example_code_pattern_search"](
        "func \\NAME", language="go", repo="unknown"
    )
