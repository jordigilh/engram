"""Config B: synthetic contradiction / non-contradiction pairs.

Validates the contradiction-check prompt (Config A: Sonnet, see classify.py
check_contradiction) in isolation, before it is ever pointed at real
cursor-memory content. Several cases are drawn from real project facts found
in spike/ground_truth.py to keep them realistic rather than purely
artificial; a few are deliberately adversarial (high lexical overlap but no
real conflict, or a "blanket rule vs. narrow exception" case flagged as a
hard case).

Each case: existing_memories (list[str]), new_statement (str),
expected_contradicts (bool), expected_conflict_idx (int | None), note (str).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ContradictionCase:
    existing_memories: list[str]
    new_statement: str
    expected_contradicts: bool
    expected_conflict_idx: int | None
    note: str


CASES: list[ContradictionCase] = [
    ContradictionCase(
        existing_memories=["The project uses Helm charts for all Kubernetes deployments."],
        new_statement="we don't use helm charts, we use programmatic go to install the services and dependencies",
        expected_contradicts=True,
        expected_conflict_idx=0,
        note="Direct opposite tech choice (real fact from ground_truth.py)",
    ),
    ContradictionCase(
        existing_memories=["Configuration should be passed via environment variables at startup."],
        new_statement="we don't use env variables",
        expected_contradicts=True,
        expected_conflict_idx=0,
        note="Direct opposite convention (real fact, appears 4x in ground_truth.py)",
    ),
    ContradictionCase(
        existing_memories=["Every code change follows RED, GREEN, REFACTOR. No exceptions."],
        new_statement="NEVER create new types or components in REFACTOR -- enhance existing only",
        expected_contradicts=False,
        expected_conflict_idx=None,
        note="Elaboration/sub-rule of an existing process, not a conflict with it",
    ),
    ContradictionCase(
        existing_memories=["We don't use env variables for configuration."],
        new_statement="The database migration pod is stuck in ContainerCreating.",
        expected_contradicts=False,
        expected_conflict_idx=None,
        note="Unrelated topic",
    ),
    ContradictionCase(
        existing_memories=["We use programmatic Go to install services, not Helm charts."],
        new_statement="Please avoid Helm, prefer Go-based installation for services.",
        expected_contradicts=False,
        expected_conflict_idx=None,
        note="Paraphrase/restatement of the same fact -- must not be flagged as a conflict",
    ),
    ContradictionCase(
        existing_memories=["CHECKPOINT W (Wiring Verification) happens after the GREEN phase, before REFACTOR."],
        new_statement="CHECKPOINT W should be run during the REFACTOR phase, not GREEN.",
        expected_contradicts=True,
        expected_conflict_idx=0,
        note="Genuine workflow-ordering contradiction, same domain as the motivating TDD/checkpoint confusion",
    ),
    ContradictionCase(
        existing_memories=["Never use env variables for configuration in this project."],
        new_statement="For local-only dev scripts, environment variables are fine to use.",
        expected_contradicts=True,
        expected_conflict_idx=0,
        note="HARD CASE: blanket rule vs. narrow exception -- scored as a contradiction here since it "
             "directly negates 'never'; a reasonable system could instead treat this as a scoped "
             "refinement. Worth inspecting model behavior on this one specifically.",
    ),
    ContradictionCase(
        existing_memories=["FMC caches metadata from remote clusters and exposes it via REST API."],
        new_statement="FMC needs to cache the metadata from the remote clusters via MCP K8s and expose them via REST API to the kubernaut services",
        expected_contradicts=False,
        expected_conflict_idx=None,
        note="Elaborates the mechanism (via MCP K8s), does not contradict the existing fact",
    ),
    ContradictionCase(
        existing_memories=["The service is named HAPI (HolmesGPT API)."],
        new_statement="do not use HAPI as service name, we deprecated it in favor of KA (Kubernaut Agent)",
        expected_contradicts=True,
        expected_conflict_idx=0,
        note="Deprecated naming, real fact from ground_truth.py",
    ),
    ContradictionCase(
        existing_memories=["We don't use testcontainers for IT tests; use the existing Redis IT infra."],
        new_statement="The E2E tests require a Kind cluster to run.",
        expected_contradicts=False,
        expected_conflict_idx=None,
        note="Same general domain (testing infra) but different tool/context -- not a conflict",
    ),
    ContradictionCase(
        existing_memories=["The default timeout for AF startup is configured via a YAML file, not environment variables."],
        new_statement="Make the timeouts configurable via environment variables, read them during AF startup",
        expected_contradicts=True,
        expected_conflict_idx=0,
        note="Direct mechanism contradiction (YAML vs env vars), real fact from ground_truth.py",
    ),
    ContradictionCase(
        existing_memories=[
            "We use programmatic Go to install services, not Helm charts.",
            "Istio mesh network should be disabled to reduce memory consumption.",
            "We don't use ACM or similar control planes for metadata.",
        ],
        new_statement="Actually let's use Helm charts going forward for the new operator services.",
        expected_contradicts=True,
        expected_conflict_idx=0,
        note="Multiple candidate memories -- tests whether the model correctly identifies index 0 "
             "as the conflicting one rather than 1 or 2",
    ),
    ContradictionCase(
        existing_memories=["Checkpoint B requires searching existing implementations before creating a new test file."],
        new_statement="Checkpoint D requires investigating build errors before proceeding.",
        expected_contradicts=False,
        expected_conflict_idx=None,
        note="High lexical/structural overlap (both are 'Checkpoint X requires Y') but different "
             "checkpoints entirely -- stress-tests against superficial-similarity false positives",
    ),
]
