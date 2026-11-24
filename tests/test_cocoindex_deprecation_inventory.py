"""Guard the CocoIndex deprecation inventory against drift (#214).

The inventory is the contract used to decide when CocoIndex can be removed.
If a new ingestion app, memoized flow, flow plist, or systemd app selection is
added, this test fails until the inventory documents it.
"""

from __future__ import annotations

import ast
import plistlib
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "COCOINDEX_DEPRECATION.md"
FLOW_MODULES = (
    ROOT / "src" / "engram" / "flows" / "configured.py",
    ROOT / "src" / "engram" / "code_overlay_cocoindex.py",
)


def _doc_text() -> str:
    assert DOC.exists(), "docs/COCOINDEX_DEPRECATION.md is missing"
    return DOC.read_text()


def _flow_apps() -> set[str]:
    tree = ast.parse(FLOW_MODULES[0].read_text())
    apps: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
            continue
        options = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
        if "--apps" not in options:
            continue
        for keyword in node.keywords:
            if keyword.arg == "choices":
                apps.update(ast.literal_eval(keyword.value))
    return apps


def _memoized_flows() -> set[str]:
    flows: set[str] = set()
    for module in FLOW_MODULES:
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = {ast.unparse(item) for item in node.decorator_list}
            if any("coco.fn" in decorator for decorator in decorators):
                flows.add(node.name)
    return flows


def _documented_flow_rows() -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {}
    for line in _doc_text().splitlines():
        match = re.match(r"^\|\s*`([^`]+)`\s*\|[^|]*\|([^|]*)\|$", line)
        if not match or not match.group(1).startswith("io.vectorize.cocoindex."):
            continue
        rows[match.group(1)] = set(re.findall(r"`([^`]+)`", match.group(2)))
    return rows


def test_the_deprecation_inventory_exists_and_covers_every_app() -> None:
    text = _doc_text()
    for app in _flow_apps():
        assert f"`{app}`" in text, f"flow app {app!r} is not documented"


def test_every_cocoindex_flow_function_is_documented() -> None:
    text = _doc_text()
    for flow in _memoized_flows():
        assert flow in text, f"memoized flow {flow!r} is not documented"


def test_the_inventory_states_the_required_deprecation_sections() -> None:
    text = _doc_text()
    for heading in (
        "## What CocoIndex is",
        "## Live footprint",
        "## Behaviors to preserve",
        "## Data and state",
        "## What deprecation means",
        "## Removal gates",
        "## Open risks",
    ):
        assert heading in text, f"missing section {heading!r}"


def test_code_search_retirement_is_explicit() -> None:
    text = _doc_text()
    assert "zvec-grep" in text
    assert "engram-search-configured" in text
    assert "retired" in text.lower()


def _flow_plists() -> dict[str, tuple[set[str], Path]]:
    """Discover deployed flow plists from their ProgramArguments.

    The generic `configured` template has a placeholder label and is handled
    separately; every other plist that runs `engram-flows-configured` must
    appear in the inventory table.
    """
    discovered: dict[str, tuple[set[str], Path]] = {}
    for path in sorted((ROOT / "launchd").glob("io.vectorize.cocoindex*.plist")):
        plist = plistlib.loads(path.read_bytes())
        arguments = plist.get("ProgramArguments", [])
        if "engram-flows-configured" not in " ".join(arguments):
            continue
        label = plist["Label"]
        if "__PROJECT__" in label:
            continue
        assert "--apps" in arguments, f"{path.name} must select apps explicitly"
        selected = set(arguments[arguments.index("--apps") + 1 :])
        discovered[label] = (selected, path)
    return discovered


def test_flow_plists_match_the_documented_table_exactly() -> None:
    documented = _documented_flow_rows()
    discovered = _flow_plists()
    for label, (selected, path) in discovered.items():
        assert "code" not in selected, f"{path.name} still selects --apps code"
        assert documented.get(label) == selected, (
            f"{path.name} apps {sorted(selected)} do not match the documented row "
            f"{sorted(documented.get(label, set()))}"
        )
    assert set(discovered) == set(documented), (
        "documented flow rows and discovered flow plists differ: "
        f"documented-only={sorted(set(documented) - set(discovered))}, "
        f"plist-only={sorted(set(discovered) - set(documented))}"
    )


def test_the_old_code_search_search_template_is_gone() -> None:
    assert not (ROOT / "launchd" / "io.vectorize.cocoindex.search.plist").exists()


def test_systemd_flow_template_does_not_select_the_code_app() -> None:
    text = (ROOT / "systemd" / "engram-cocoindex.service").read_text()
    exec_start = next(
        line for line in text.splitlines() if line.startswith("ExecStart=")
    )
    argv = shlex.split(exec_start.removeprefix("ExecStart="))
    selected = set(argv[argv.index("--apps") + 1 :])
    assert selected == {"docs", "issues"}


def test_configured_template_is_docs_issues_only() -> None:
    path = ROOT / "launchd" / "io.vectorize.cocoindex.configured.plist"
    arguments = plistlib.loads(path.read_bytes())["ProgramArguments"]
    selected = set(arguments[arguments.index("--apps") + 1 :])
    assert selected == {"docs", "issues"}


def test_cursor_examples_use_zvec_code_search() -> None:
    for path in sorted((ROOT / "cursor" / "examples").glob("*.mdc")):
        text = path.read_text()
        assert "cocoindex_search" not in text, f"{path.name} still names cocoindex_search"
        assert "user-cocoindex-code" not in text, f"{path.name} still names the old server"
