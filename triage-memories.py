#!/usr/bin/env python3
"""Triage Hindsight memories for cleanup.

Scans all memories in a bank, identifies cleanup candidates (duplicates,
ephemeral content, stale snapshots, low-value entries), and removes them
by rearranging documents: each mixed document is rebuilt with only the
valuable memories re-retained via the 'exact' strategy.

Usage:
  # Dry-run (default): report what would be cleaned
  python3 triage-memories.py

  # Apply: rearrange documents, removing flagged memories
  python3 triage-memories.py --apply

  # Target a specific bank
  python3 triage-memories.py --bank kubernaut-docs

  # Adjust age threshold for stale content
  python3 triage-memories.py --stale-days 7
"""

import argparse
import json
import logging
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HINDSIGHT_URL = "http://localhost:8888"
DEFAULT_BANK = "cursor-memory"
LOG_DIR = Path.home() / ".hindsight" / "logs"
REARRANGE_BATCH_SIZE = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# --- Patterns for identifying ephemeral/low-value content ---

EPHEMERAL_PATTERNS = [
    re.compile(
        r"assistant (?:is |was )"
        r"(?:creating|implementing|investigating|executing|analyzing|building|"
        r"running|writing|checking|reviewing|debugging|fixing|updating|"
        r"generating|pushing|working|deploying|refactoring|processing)",
        re.I,
    ),
    re.compile(
        r"assistant (?:committed|pushed|created|completed|finished|deployed|"
        r"started|moved|extracted|fixed|updated|added|applied|removed|replaced|"
        r"refactored|identified|examined|verified|confirmed|addressed|resolved|"
        r"rebased|cherry-picked|squashed|amended)",
        re.I,
    ),
    re.compile(r"user requested (?!.*(?:always|never|mandatory|convention))", re.I),
    re.compile(r"user is (?:working on|fixing|reviewing|investigating|performing|checking)", re.I),
    re.compile(r"user instructed (?!.*(?:always|never|mandatory|convention))", re.I),
    re.compile(r"all \d+ (?:test|spec|job|resource)s? (?:pass|succeed|complet)", re.I),
    re.compile(r"ci (?:pipeline|build|image|job|status)", re.I),
    re.compile(r"build (?:passed|failed|succeeded|completed|in progress)", re.I),
    re.compile(r"pipeline (?:status|build|jobs?|completed|passed|failed)", re.I),
    re.compile(
        r"(?:lint|unit|integration|e2e) (?:test|job|check)s? (?:passed|failed|succeeded|completed)",
        re.I,
    ),
    re.compile(r"image (?:built|pushed|deployed|published|tagged|is in)", re.I),
    re.compile(r"pod (?:entered|crashed|restarted|is in|has status)", re.I),
    re.compile(r"deployed to|deploying to|rollout", re.I),
    re.compile(r"commit(?:ted|s?) (?:changes|work|fix|update)", re.I),
]

SNAPSHOT_PATTERNS = [
    re.compile(
        r"(?:running|using|deployed) (?:the |)(?:pr-\d+|sha256|v\d+|commit|version|build|image)",
        re.I,
    ),
    re.compile(r"currently (?:running|deployed|using|in)", re.I),
    re.compile(r"(?:ready|waiting) for ", re.I),
    re.compile(r"(?:in progress|not started|not yet (?:started|complete))", re.I),
]

VALUABLE_PATTERNS = [
    re.compile(r"\b(?:always|never|must|mandatory|convention|standard)\b", re.I),
    re.compile(r"\b(?:pattern|anti-?pattern|best practice|lesson)\b", re.I),
    re.compile(r"\b(?:architecture|design decision|trade-?off)\b", re.I),
    re.compile(r"\b(?:bug|root cause|fix|regression)\b.*\b(?:because|due to|caused by)\b", re.I),
    re.compile(r"\b(?:fedramp|nist|compliance|control)\b.*\b(?:require|mandate|verify)\b", re.I),
    re.compile(r"\b(?:adr|dd)-\d+\b", re.I),
]


# --- HTTP helpers ---

def api_get(path: str) -> dict:
    url = f"{HINDSIGHT_URL}{path}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except (HTTPError, URLError) as e:
        log.warning("GET %s failed: %s", url, e)
        return {}


def api_post(path: str, payload: dict) -> dict:
    url = f"{HINDSIGHT_URL}{path}"
    data = json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log.error("API error %s %s: %s", e.code, url, body[:500])
        raise
    except URLError as e:
        log.error("Connection error %s: %s", url, e.reason)
        raise


def api_delete(path: str) -> bool:
    url = f"{HINDSIGHT_URL}{path}"
    req = Request(url, method="DELETE")
    try:
        urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.warning("DELETE %s failed: %s", url, e)
        return False


# --- Data fetching ---

def fetch_all_memories(bank_id: str) -> list[dict]:
    """Fetch all memories from a bank via pagination."""
    all_items = []
    offset = 0
    while True:
        resp = api_get(
            f"/v1/default/banks/{bank_id}/memories/list?limit=100&offset={offset}"
        )
        items = resp.get("items", [])
        if not items:
            break
        all_items.extend(items)
        offset += len(items)
        total = resp.get("total", 0)
        if offset >= total or len(items) < 100:
            break
    return all_items


def fetch_all_documents(bank_id: str) -> list[dict]:
    """Fetch all documents from a bank via pagination."""
    all_docs = []
    offset = 0
    while True:
        resp = api_get(
            f"/v1/default/banks/{bank_id}/documents?limit=100&offset={offset}"
        )
        docs = resp.get("documents", resp.get("items", []))
        if not docs:
            break
        all_docs.extend(docs)
        offset += len(docs)
        if len(docs) < 100:
            break
    return all_docs


# --- Classification ---

def classify_memory(memory: dict, stale_cutoff: datetime) -> list[str]:
    """Classify a memory into zero or more cleanup categories.

    Returns list of reason strings. Empty list means the memory is clean.
    """
    text = memory.get("text", "")
    reasons = []

    if not text.strip():
        return ["empty"]

    for pat in VALUABLE_PATTERNS:
        if pat.search(text):
            return []

    for pat in EPHEMERAL_PATTERNS:
        if pat.search(text):
            reasons.append("ephemeral")
            break

    if not reasons:
        for pat in SNAPSHOT_PATTERNS:
            if pat.search(text):
                reasons.append("snapshot")
                break

    if len(text) < 80:
        reasons.append("short")

    mem_date = memory.get("date", "")[:10]
    if mem_date:
        try:
            dt = datetime.strptime(mem_date, "%Y-%m-%d")
            if dt < stale_cutoff and reasons:
                reasons.append("stale")
        except ValueError:
            pass

    return reasons


def find_near_duplicates(memories: list[dict]) -> list[tuple[str, str, float]]:
    """Find near-duplicate memory pairs (>85% similarity)."""

    def normalize(text: str) -> str:
        t = text.lower().strip()
        t = re.sub(r"\s+", " ", t)
        t = re.sub(r"\| when:.*$", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\| involving:.*$", "", t, flags=re.IGNORECASE)
        return t[:200]

    norms = [(m, normalize(m["text"])) for m in memories if m.get("text", "").strip()]

    prefix_groups: dict[str, list[int]] = defaultdict(list)
    for i, (_, n) in enumerate(norms):
        prefix_groups[n[:50]].append(i)

    pairs = []
    for indices in prefix_groups.values():
        if len(indices) < 2:
            continue
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                idx_a, idx_b = indices[i], indices[j]
                ratio = SequenceMatcher(
                    None, norms[idx_a][1], norms[idx_b][1]
                ).ratio()
                if ratio > 0.85:
                    pairs.append(
                        (memories[idx_a]["id"], memories[idx_b]["id"], ratio)
                    )
    return pairs


def find_repeated_facts(memories: list[dict]) -> dict[str, list[str]]:
    """Find factual claims restated 3+ times across memories."""
    fact_groups: dict[str, list[str]] = defaultdict(list)

    for m in memories:
        text = m.get("text", "").lower()
        if "itpc-gcp-eco-eng-claude" in text:
            fact_groups["GCP project = itpc-gcp-eco-eng-claude"].append(m["id"])
        if "kubernaut-system" in text and "namespace" in text and len(text) < 200:
            fact_groups["kubernaut-system namespace"].append(m["id"])
        if "hapi" in text and ("deprecated" in text or "replaced" in text):
            fact_groups["HAPI deprecated"].append(m["id"])

    return {k: v for k, v in fact_groups.items() if len(v) >= 3}


# --- Rearrange: rebuild documents with only valuable memories ---

def rearrange_document(
    bank_id: str,
    doc_id: str,
    keep_memories: list[dict],
) -> dict:
    """Delete a document and re-retain only its valuable memories.

    Uses strategy='exact' to store each memory text verbatim (no LLM
    re-extraction), preserving timestamps and tags.

    Returns dict with counts of kept/removed memories.
    """
    doc_prefix = f"triage-{doc_id[:8]}"

    items = []
    for idx, m in enumerate(keep_memories):
        item: dict = {
            "content": m["text"],
            "strategy": "exact",
            "document_id": f"{doc_prefix}-{uuid.uuid4().hex[:8]}",
        }
        if m.get("date"):
            item["timestamp"] = m["date"]
        if m.get("context"):
            item["context"] = m["context"]
        if m.get("tags"):
            item["tags"] = m["tags"]
        item["metadata"] = {
            "source": "triage-rearrange",
            "original_doc": doc_id,
        }
        items.append(item)

    # Step 1: delete original document (removes all its memories)
    if not api_delete(f"/v1/default/banks/{bank_id}/documents/{doc_id}"):
        return {"error": f"failed to delete document {doc_id}"}

    if not items:
        return {"kept": 0, "doc_deleted": True}

    # Step 2: re-retain valuable memories in batches
    retained = 0
    for i in range(0, len(items), REARRANGE_BATCH_SIZE):
        batch = items[i : i + REARRANGE_BATCH_SIZE]
        try:
            result = api_post(
                f"/v1/default/banks/{bank_id}/memories",
                {"items": batch},
            )
            retained += result.get("items_count", 0)
        except Exception as e:
            log.warning("Re-retain batch failed for %s: %s", doc_id, e)

    return {"kept": retained, "doc_deleted": True}


def triage(
    bank_id: str,
    stale_days: int = 14,
    apply: bool = False,
) -> dict:
    """Run triage on a memory bank.

    When apply=True, documents are rearranged: each document containing
    flagged memories is deleted and rebuilt with only the valuable memories.
    """
    stale_cutoff = datetime.now() - timedelta(days=stale_days)

    log.info("Fetching memories from bank '%s'...", bank_id)
    raw_items = fetch_all_memories(bank_id)

    memories = [m for m in raw_items if m.get("fact_type") and m.get("text", "").strip()]
    log.info("Loaded %d memories", len(memories))

    if not memories:
        return {"bank": bank_id, "total_memories": 0, "actions": []}

    # --- Phase 1: Classify each memory ---
    flagged: dict[str, list[str]] = {}
    for m in memories:
        reasons = classify_memory(m, stale_cutoff)
        if reasons:
            flagged[m["id"]] = reasons

    # --- Phase 2: Near-duplicate detection ---
    dup_pairs = find_near_duplicates(memories)
    mem_by_id = {m["id"]: m for m in memories}
    for id_a, id_b, ratio in dup_pairs:
        date_a = mem_by_id[id_a].get("date", "")
        date_b = mem_by_id[id_b].get("date", "")
        remove = id_a if date_a <= date_b else id_b
        if remove not in flagged:
            flagged[remove] = []
        flagged[remove].append(f"near-duplicate ({ratio:.0%})")

    # --- Phase 3: Repeated facts ---
    repeated = find_repeated_facts(memories)
    for claim, ids in repeated.items():
        dated = sorted(ids, key=lambda i: mem_by_id[i].get("date", ""), reverse=True)
        for mid in dated[1:]:
            if mid not in flagged:
                flagged[mid] = []
            flagged[mid].append(f"repeated-fact: {claim}")

    # --- Phase 4: Group by document ---
    doc_to_memories: dict[str, list[dict]] = defaultdict(list)
    doc_to_flagged: dict[str, set] = defaultdict(set)

    for m in memories:
        chunk_id = m.get("chunk_id", "")
        match = re.match(r"cursor-memory_([a-f0-9-]+)_", chunk_id)
        if match:
            doc_id = match.group(1)
            doc_to_memories[doc_id].append(m)
            if m["id"] in flagged:
                doc_to_flagged[doc_id].add(m["id"])

    # Classify documents
    fully_flagged = []
    mixed_docs = []
    clean_docs = 0
    for doc_id, mems in doc_to_memories.items():
        n_flagged = len(doc_to_flagged.get(doc_id, set()))
        if n_flagged == 0:
            clean_docs += 1
        elif n_flagged == len(mems):
            fully_flagged.append(doc_id)
        else:
            mixed_docs.append(doc_id)

    # Count by reason
    reason_counts: dict[str, int] = defaultdict(int)
    for reasons in flagged.values():
        for r in reasons:
            category = r.split(":")[0].split("(")[0].strip()
            reason_counts[category] += 1

    memories_removable = len(flagged)
    memories_in_fully = sum(len(doc_to_memories[d]) for d in fully_flagged)
    memories_in_mixed_flagged = sum(len(doc_to_flagged[d]) for d in mixed_docs)
    memories_in_mixed_kept = sum(
        len(doc_to_memories[d]) - len(doc_to_flagged[d]) for d in mixed_docs
    )

    summary = {
        "bank": bank_id,
        "total_memories": len(memories),
        "total_flagged": memories_removable,
        "flagged_pct": round(memories_removable / len(memories) * 100, 1),
        "by_reason": dict(reason_counts),
        "near_duplicate_pairs": len(dup_pairs),
        "repeated_fact_groups": len(repeated),
        "documents_clean": clean_docs,
        "documents_fully_flagged": len(fully_flagged),
        "documents_mixed": len(mixed_docs),
        "memories_in_fully_flagged_docs": memories_in_fully,
        "memories_removed_from_mixed": memories_in_mixed_flagged,
        "memories_kept_in_mixed": memories_in_mixed_kept,
    }

    log.info("--- Triage Results ---")
    log.info("  Total memories: %d", summary["total_memories"])
    log.info("  Flagged: %d (%.1f%%)", summary["total_flagged"], summary["flagged_pct"])
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        log.info("    %s: %d", reason, count)
    log.info("  Near-duplicate pairs: %d", summary["near_duplicate_pairs"])
    log.info("  Repeated fact groups: %d", summary["repeated_fact_groups"])
    log.info("  Documents clean (untouched): %d", clean_docs)
    log.info(
        "  Documents fully flagged (delete all %d memories): %d",
        memories_in_fully, len(fully_flagged),
    )
    log.info(
        "  Documents mixed (rearrange — remove %d, keep %d): %d",
        memories_in_mixed_flagged, memories_in_mixed_kept, len(mixed_docs),
    )

    if not apply:
        summary["applied"] = False
        if fully_flagged or mixed_docs:
            log.info("Dry-run mode. Use --apply to rearrange.")
        return summary

    # --- Apply: rearrange all affected documents ---
    log.info("Applying rearrange...")
    docs_deleted = 0
    docs_rearranged = 0
    memories_removed = 0
    memories_re_retained = 0

    # Fully flagged: just delete (no re-retain needed)
    for doc_id in fully_flagged:
        if api_delete(f"/v1/default/banks/{bank_id}/documents/{doc_id}"):
            docs_deleted += 1
            memories_removed += len(doc_to_memories[doc_id])

    # Mixed: rearrange (delete + re-retain valuable)
    for i, doc_id in enumerate(mixed_docs):
        flagged_ids = doc_to_flagged[doc_id]
        keep = [m for m in doc_to_memories[doc_id] if m["id"] not in flagged_ids]
        remove_count = len(doc_to_memories[doc_id]) - len(keep)

        result = rearrange_document(bank_id, doc_id, keep)

        if result.get("doc_deleted"):
            docs_rearranged += 1
            memories_removed += remove_count
            memories_re_retained += result.get("kept", 0)

        if (i + 1) % 20 == 0:
            log.info("  Progress: %d / %d mixed docs rearranged", i + 1, len(mixed_docs))

    # Clean up empty documents
    docs = fetch_all_documents(bank_id)
    empty_docs = [d for d in docs if d.get("memory_unit_count", 0) == 0]
    empty_deleted = 0
    for d in empty_docs:
        if api_delete(f"/v1/default/banks/{bank_id}/documents/{d['id']}"):
            empty_deleted += 1

    summary["applied"] = True
    summary["docs_deleted"] = docs_deleted
    summary["docs_rearranged"] = docs_rearranged
    summary["memories_removed"] = memories_removed
    summary["memories_re_retained"] = memories_re_retained
    summary["empty_docs_cleaned"] = empty_deleted

    log.info("--- Applied ---")
    log.info("  Docs deleted (fully flagged): %d", docs_deleted)
    log.info("  Docs rearranged (mixed): %d", docs_rearranged)
    log.info("  Memories removed: %d", memories_removed)
    log.info("  Memories re-retained: %d", memories_re_retained)
    log.info("  Empty docs cleaned: %d", empty_deleted)

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Triage Hindsight memories for cleanup"
    )
    parser.add_argument(
        "--bank",
        default=DEFAULT_BANK,
        help="Bank ID to triage (default: cursor-memory)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rearrange documents, removing flagged memories (default: dry-run)",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=14,
        help="Days after which flagged date-bound facts are considered stale (default: 14)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    summary = triage(
        bank_id=args.bank,
        stale_days=args.stale_days,
        apply=args.apply,
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "triage-report.jsonl"
    summary["timestamp"] = datetime.now().isoformat()
    with open(log_path, "a") as f:
        f.write(json.dumps(summary) + "\n")

    if args.json:
        print(json.dumps(summary, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
