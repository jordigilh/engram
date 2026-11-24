"""Guard the CocoIndex deprecation inventory against drift (#214).

The inventory is the contract used to decide when CocoIndex can be removed.
If a new ingestion app or memoized flow is added to the Python flow entrypoint,
this test fails until the inventory documents it.
"""

from __future__ import annotations

import ast
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "COCOINDEX_DEPRECATION.md"
FLOW = ROOT / "src" / "engram" / "flows" / "configured.py"


def _flow_apps() -> set[str]:
    tree = ast.parse(FLOW.read_text())
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
    tree = ast.parse(FLOW.read_text())
    flows: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = {ast.unparse(item) for item in node.decorator_list}
        if any("coco.fn" in decorator for decorator in decorators):
            flows.add(node.name)
    return flows


def test_the_deprecation_inventory_exists_and_covers_every_app() -> None:
    assert DOC.exists(), "docs/COCOINDEX_DEPRECATION.md is missing"
    text = DOC.read_text()
    for app in _flow_apps():
        assert f"`{app}`" in text, f"flow app {app!r} is not documented"


def test_every_cocoindex_flow_function_is_documented() -> None:
    text = DOC.read_text()
    for flow in _memoized_flows():
        assert flow in text, f"memoized flow {flow!r} is not documented"


def test_the_inventory_states_the_required_deprecation_sections() -> None:
    text = DOC.read_text()
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
    text = DOC.read_text()
    assert "zvec-grep" in text
    assert "engram-search-configured" in text
    assert "retired" in text.lower()


def test_flow_plists_do_not_select_the_legacy_code_app() -> None:
    for path in sorted((ROOT / "launchd").glob("io.vectorize.cocoindex*.plist")):
        if "cocoindex-code" in path.name or "watch" in path.name:
            continue
        arguments = plistlib.loads(path.read_bytes())["ProgramArguments"]
        assert "--apps" in arguments, f"{path.name} must select apps explicitly"
        selected = arguments[arguments.index("--apps") + 1 :]
        assert "code" not in selected, f"{path.name} still selects --apps code"


def test_the_old_code_search_search_template_is_gone() -> None:
    assert not (ROOT / "launchd" / "io.vectorize.cocoindex.search.plist").exists()


def test_systemd_flow_template_does_not_select_the_code_app() -> None:
    text = (ROOT / "systemd" / "engram-cocoindex.service").read_text()
    exec_start = next(
        line for line in text.splitlines() if line.startswith("ExecStart=")
    )
    assert "--apps docs issues" in exec_start
    assert "code" not in exec_start
