"""Hand-labeled ground truth for the Semantic Correction Detection Spike.

Assembled from two rounds of manual review against real transcripts:
  1. The 16 corrections + 5 benign near-misses found on 2026-07-08 while
     diagnosing why regex-based CORRECTION_PATTERNS missed 100% of real
     corrections over 7 days (single project, single correction type:
     methodology violations).
  2. A wider 30-day, both-project (kubernaut + dcm) scan on 2026-07-08,
     narrowed from an initial 1154 low-precision keyword hits down to 48
     candidates via a tighter filter, then hand-labeled one by one to add
     category diversity: convention violations ("we don't use env
     variables"), unauthorized/unwanted actions ("why did you merge the PR
     without permission?"), technical misstatements, undo/revert requests,
     repeated-mistake callouts, and scope corrections.

Each entry has a `split` of "seed" or "eval". The eval split must NEVER be
used to seed cocoindex.correction_embeddings or as few-shot examples in any
prompt -- it exists solely to score the pipeline variants without train/test
contamination (see docs/FINDINGS.md 2026-07-08 and the spike plan for why
this matters: the original draft seeded and scored against the same 16
examples, which trivially inflates recall since a message matches itself).

A few genuinely ambiguous candidates found during the 30-day scan (e.g.
"did we use the wrong model?", "we don't use the dynamic API" embedded in a
bug report) were excluded entirely rather than force-labeled, since noisy
ground truth is worse than a smaller clean set.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Example:
    text: str
    is_correction: bool
    category: str
    project: str
    split: str = field(default="")  # assigned by split_dataset()


# --- Positive examples (real corrections), by category ---------------------

_POSITIVE: list[tuple[str, str, str]] = [
    # category, project, text
    ("methodology_violation", "kubernaut", "again, you're not following AGENTS.md"),
    ("methodology_violation", "kubernaut", "no, you're still not following the project's methodology"),
    ("methodology_violation", "kubernaut", "you're not following the project's methodology"),
    ("methodology_violation", "kubernaut", "confidence score, and you are not following the project's methodology"),
    ("methodology_violation", "kubernaut", "you keep making the same mistake with refactor phase: you're not aligned with the AGENTS.md"),
    ("methodology_violation", "kubernaut", "why does REFACTOR still show checkpoint tasks? it should be split. You're still not following the AGENTS.md"),
    ("methodology_violation", "kubernaut", "these tests are not following project convention https://github.com/jordigilh/kubernaut"),
    ("methodology_violation", "kubernaut", "I'm finding often that the model tends to mistake TDD refactoring for checkpoint"),
    ("methodology_violation", "kubernaut", "stop. You are not following TDD methodology"),
    ("methodology_violation", "kubernaut", "we have a clear development methodology in kubernaut you are not following"),
    ("methodology_violation", "kubernaut", "wire in cmd/, write IT tests proving the wiring sounds to me you're still not following the TDD methodology, why not?"),
    ("methodology_violation", "kubernaut", "the todos don't show the project's methodology. from my perspective, you are not following it. I am yet to see the SOC 2 audit events being shown in the plan: is the code in this branch SOC2 audit compliant? That's what I asked you to triage and I'm yet to see it in the plan reflected with the gaps"),
    ("methodology_violation", "kubernaut", "you're not following the project's development methodology, why?"),
    ("convention_violation", "kubernaut", "do not use patent search engine"),
    ("convention_violation", "kubernaut", "> normalize table output remember we don't use table output anymore"),
    ("convention_violation", "kubernaut", "> Valkey-backed IT test for ValkeyCacheReader/ValkeyWriter (requires testcontainers — deferred to CI pipeline setup) we don't use testcontainers, reassess. And we already have a IT test infra in place for redis deployment"),
    ("convention_violation", "kubernaut", "> Fix: Make the timeouts configurable via environment variables, read them during AF startup, and set short values in the E2E deployment overlay. we don't use env variables"),
    ("convention_violation", "kubernaut", "we don't use helm charts, we use programmatic go to install the services and dependencies. For istio, I expect us to disable the mesh network part to reduce memory consumption"),
    ("convention_violation", "kubernaut", "we don't use env variables"),
    ("convention_violation", "kubernaut", "> ...why is the db migration being triggered again? >The fix is to set the env var to the actual credentials file path. Let me check the operator deployment code. no, we don't use env variables. Read the code in ../kubernaut and reassess"),
    ("convention_violation", "kubernaut", "we don't do that in kubernaut"),
    ("convention_violation", "kubernaut", "do not use tag"),
    ("convention_violation", "kubernaut", "> @kubernaut-agent.yaml (125-128) these are no longer supported as far as I know, we don't use env variables. Check ../kubernaut-v1.3/ and reassess why do we need placeholders?"),
    ("convention_violation", "kubernaut", "did you build the remediation image for amd64 already and push to quay.io? use the go cross platform to build the image, do not use emulation"),
    ("convention_violation", "kubernaut", "do not use HAPI as service name, we deprecated it in favor of KA (Kubernaut Agent)"),
    ("convention_violation", "dcm", "fix the (2) document and add a comment so that reviewers are aware of what we found. As for 1 we don't use the catalog from OSAC, DCM will have its own. How does that impact? post 3 as a follow up suggestion in a comment. Make sure you add direct links (perma links) to code and jira tickets"),
    ("technical_misstatement", "kubernaut", ">Phase 1 (GW signal ingestion): The MCP Gateway is not involved at all. Alerts come from Alertmanager/Thanos directly to the GW webhook. The cluster label arrives in commonLabels. No MCP needed — just a POST with the right labels. no, that's not true. GW needs to filter if the resource is managed by kubernaut, so it will first check the MCP Gateway"),
    ("technical_misstatement", "kubernaut", "no, that's not FMC job: FMC needs to cache the metadata from the remote clusters via MCP K8s and expose them via REST API to the kubernaut services"),
    ("scope_correction", "kubernaut", "it's just not worth the time and effort: we don't use these images in cicd, we're just building them to ensure they work fine"),
    ("scope_correction", "kubernaut", ">Wait — but earlier the user said \"left-align horizontally for the status next to the divider\". That was about not floating right. Now they want it anchored to the same position always. These are contradicting unless I misunderstood. no"),
    ("repeated_mistake", "kubernaut", ">we just can't run it without the Kind cluster) yes, but you keep forgetting our E2E infra every time you excuse yourself of not writing the E2E because of kind dependency"),
    ("repeated_mistake", "kubernaut", "you keep forgetting"),
    ("undo_revert", "kubernaut", "revert that change, open an issue to kubernaut and wait for the fix"),
    ("unwanted_action", "kubernaut", "why did you have to guess? isn't it stored in a secret? do we pass the valkey password as a knob in the CRD?"),
    ("unwanted_action", "kubernaut", "why did you remove the sizeLimit?"),
    ("unwanted_action", "kubernaut", "why did you merge the PR without permission?"),
    ("unwanted_action", "kubernaut", "why did you extract the text and removed the images?"),
]

# --- Negative examples (benign; look correction-adjacent but are not) ------

_NEGATIVE: list[tuple[str, str, str]] = [
    # category, project, text
    ("confused_question", "kubernaut", "203, what's the old value and new value and what should be? I'm confused"),
    ("mistake_non_correction", "kubernaut", "confidence score that we did not overwrite by mistake"),
    ("mistake_non_correction", "kubernaut", "worried that another team switched branches by mistake"),
    ("clarifying_question", "kubernaut", "I'm still not clear on 1578 and how does it impact the RCA. Can you explain?"),
    ("quoted_self_reflection", "kubernaut", "the core mistake: I was treating validation (linting, confirming tests stay green) as the improvement itself"),
    ("observation_not_correction", "kubernaut", "I'm surprised that we don't have to change the logic now that we don't use the yaml output format"),
    ("dismissal", "kubernaut", "nevermind"),
    ("dismissal", "kubernaut", "nevermind, open a new issue and I'll have upstream team look into it. Because 1452 image is the one we used in dev"),
    ("dismissal", "kubernaut", "nevermind, we'll have the demo scenarios team handle it"),
    ("dismissal", "dcm", "nevermind, if OSAC only listens to pods in the endpoints then that's as safe as it gets. Let's not overdo ourselves. The v2 health enhancement is enough"),
    ("dismissal", "kubernaut", "nevermind, it's fixed, this is another issue"),
    ("dismissal", "kubernaut", "this is a problem for the kubernaut operator, nevermind"),
    ("dismissal", "kubernaut", "https://github.com/jordigilh/kubernaut/actions/runs/27032549968/job/79788290364 nevermind, you already fixed it"),
    ("dismissal", "kubernaut", "nevermind, carry on"),
    # This one is the highest-value hard negative in the set: it contains
    # "misunderstood", a word that appears in several real corrections above,
    # but here the USER is admitting THEIR OWN misunderstanding -- not
    # correcting the assistant. A naive keyword or even a naive embedding
    # match on "misunderstood" alone would misclassify this as a correction.
    ("user_self_correction", "kubernaut", "let's bundle thE A and B as you suggested, I misunderstood C as something else"),
]


def build_examples() -> list[Example]:
    examples = [
        Example(text=text, is_correction=True, category=cat, project=proj)
        for cat, proj, text in _POSITIVE
    ]
    examples += [
        Example(text=text, is_correction=False, category=cat, project=proj)
        for cat, proj, text in _NEGATIVE
    ]
    return examples


def split_dataset(examples: list[Example], seed_fraction: float = 0.6, seed: int = 42) -> list[Example]:
    """Stratify by (is_correction, category) so both splits cover every
    category seen, then randomly assign within each stratum. Returns new
    Example instances with `split` populated ("seed" or "eval").
    """
    rng = random.Random(seed)
    by_stratum: dict[tuple[bool, str], list[Example]] = {}
    for ex in examples:
        by_stratum.setdefault((ex.is_correction, ex.category), []).append(ex)

    out: list[Example] = []
    for _, group in by_stratum.items():
        group = group[:]
        rng.shuffle(group)
        n_seed = max(1, round(len(group) * seed_fraction)) if len(group) > 1 else 1
        # Guarantee at least one example in eval when the stratum has >1 member.
        if len(group) > 1 and n_seed == len(group):
            n_seed -= 1
        for i, ex in enumerate(group):
            split = "seed" if i < n_seed else "eval"
            out.append(Example(text=ex.text, is_correction=ex.is_correction, category=ex.category, project=ex.project, split=split))
    return out


DATASET = split_dataset(build_examples())


def seed_examples() -> list[Example]:
    return [e for e in DATASET if e.split == "seed"]


def eval_examples() -> list[Example]:
    return [e for e in DATASET if e.split == "eval"]


if __name__ == "__main__":
    seed = seed_examples()
    ev = eval_examples()
    print(f"Total: {len(DATASET)}  Seed: {len(seed)}  Eval: {len(ev)}")
    print(f"Seed positives: {sum(e.is_correction for e in seed)}  Seed negatives: {sum(not e.is_correction for e in seed)}")
    print(f"Eval positives: {sum(e.is_correction for e in ev)}  Eval negatives: {sum(not e.is_correction for e in ev)}")
    cats = sorted({e.category for e in DATASET})
    print(f"\nCategories ({len(cats)}): {cats}")
    for cat in cats:
        n_seed = sum(1 for e in seed if e.category == cat)
        n_eval = sum(1 for e in ev if e.category == cat)
        print(f"  {cat:28s} seed={n_seed:2d} eval={n_eval:2d}")
