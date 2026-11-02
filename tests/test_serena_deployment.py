"""Deployment-template guards for workspace-aware Serena routing."""
from __future__ import annotations

import pathlib
import re
import shlex


REPO_ROOT = pathlib.Path(__file__).parents[1]
EXPECTED_CANONICAL_ROOTS = {
    "kubernaut",
    "kubernaut-operator",
    "kubernaut-console",
    "kubernaut-demo-scenarios",
    "kubernaut-v1.5",
    "kubernaut-v1.6",
}


def _allowed_root_values(arguments: list[str]) -> set[str]:
    return {
        arguments[index + 1]
        for index, argument in enumerate(arguments[:-1])
        if argument == "--allowed-root"
    }


def _launchd_program_arguments(template: pathlib.Path) -> list[str]:
    """Extract arguments without parsing comments as XML.

    The checked-in plist contains explanatory comments with ``--`` command
    options. Those comments are useful to humans but are invalid XML, so
    macOS's plist tools accept the deployed/rendered form while Python's
    strict plist parser rejects this source template.
    """
    text = template.read_text()
    match = re.search(
        r"<key>ProgramArguments</key>\s*<array>(.*?)</array>",
        text,
        flags=re.DOTALL,
    )
    assert match is not None
    return re.findall(r"<string>(.*?)</string>", match.group(1))


def _assert_kubernaut_roots(values: set[str], prefix: str) -> None:
    assert values
    assert {
        f"{prefix}/go/src/github.com/jordigilh/{name}"
        for name in EXPECTED_CANONICAL_ROOTS
    } <= values


def test_kubernaut_launchd_template_declares_workspace_roots() -> None:
    template = REPO_ROOT / "launchd" / "io.vectorize.serena-multiplex.kubernaut-family.plist"
    arguments = _launchd_program_arguments(template)

    values = _allowed_root_values(arguments)
    _assert_kubernaut_roots(values, "__HOME__")


def test_kubernaut_systemd_template_declares_workspace_roots() -> None:
    template = REPO_ROOT / "systemd" / "engram-serena-multiplex-kubernaut-family.service"
    exec_start = next(
        line.removeprefix("ExecStart=")
        for line in template.read_text().splitlines()
        if line.startswith("ExecStart=")
    )

    values = _allowed_root_values(shlex.split(exec_start))
    _assert_kubernaut_roots(values, "%h")
