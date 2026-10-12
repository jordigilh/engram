# Proposal 3: four-language paired qeval

This run compares the Rust baseline and gated-relationship candidate on each
language's own locked fixture and qrels. It used 8 queries per language,
`local/potion-code-16m-v2` snapshot `e9d2a44ca6a05ac6685f3b23709ea57eb7352d5b`,
CPU, and the matrix runner's normalized source-unit `@10` protocol. The
unchanged TypeScript binary was used for both TypeScript arms as a control.

## Rust metrics

| Fixture language | nDCG@10 baseline → candidate | MRR@10 baseline → candidate | Recall@10 baseline → candidate | Precision@10 baseline → candidate |
| --- | ---: | ---: | ---: | ---: |
| Go | 0.471013 → 0.471013 (0) | 0.667857 → 0.667857 (0) | 0.620833 → 0.620833 (0) | 0.225000 → 0.225000 (0) |
| Python | 0.530362 → 0.530362 (0) | 0.543750 → 0.543750 (0) | 0.639583 → 0.639583 (0) | 0.225000 → 0.225000 (0) |
| Rust | 0.430053 → 0.430053 (0) | 0.435714 → 0.435714 (0) | 0.585417 → 0.585417 (0) | 0.212500 → 0.212500 (0) |
| TypeScript | 0.320709 → 0.320709 (0) | 0.323958 → 0.323958 (0) | 0.570833 → 0.570833 (0) | 0.200000 → 0.200000 (0) |

Every Rust per-query delta is also zero for all four metrics in all four lanes;
`comparison.json` contains the per-query nDCG, MRR, recall, and precision
breakdown. The unchanged TypeScript control also has zero baseline/candidate
deltas throughout.

## Relationship opportunities and order changes

An extra `--trace` replay was run against the same candidate indexes. Its raw
result order matched each corresponding matrix candidate response. Counts below
are unique top-10 candidate-pair relationships that passed the resolved,
unambiguous, endpoint-query, exact-source-range, and indexed-content-hash gates.

| Fixture language | Verified query-aligned pairs | Pairs within 5% | Applied swaps | Queries with an applied swap |
| --- | ---: | ---: | ---: | ---: |
| Go | 9 | 4 | 1 | `copy-selected-workflow-details` |
| Python | 13 | 5 | 1 | `empty-discovery-fails-closed` |
| Rust | 13 | 4 | 2 | `reject-undiscovered-workflow` |
| TypeScript | 5 | 4 | 0 | — |

The applied raw-rank changes were:

- **Go — `copy-selected-workflow-details`:** `HandleSelectWorkflow` 3→2 and
  `authorizeSelectionDriver` 2→3. The corresponding normalized unit swap is
  present; all four query metrics remain unchanged.
- **Python — `empty-discovery-fails-closed`:** `self_correct_selection` 9→8 and
  `Validator.validate` 8→9. The normalized qrels-unit order is unchanged, and
  all four query metrics remain unchanged.
- **Rust — `reject-undiscovered-workflow`:**
  `is_workflow_in_discovery_result` 3→2, `handle_select_workflow` 4→3, and
  `authorize_selection_driver` 2→4. The normalized qrels-unit order changes,
  but the query's four metrics remain unchanged.
- **TypeScript:** no pair received an applied adjustment and no result order
  changed.

The complete 32-query trace audit is in
[`relationship-tiebreak-audit.json`](./relationship-tiebreak-audit.json).
The candidate reranks only its already-selected top 10; the candidate set is
fixed, with no graph-driven retrieval expansion.

## Gate outcome

- **Behavioral constraints:** pass. No graph evidence means baseline order; the
  observed changes were confined to verified, query-aligned near-tie pairs and
  the adjustment is capped at 2%.
- **No-regression score gate:** pass. All four aggregate metrics are unchanged
  in every language lane, and every per-query metric delta is zero.
- **Improvement/promotion gate:** fail. No metric improves, so this remains a
  neutral experiment rather than a demonstrated retrieval improvement.
- **Opportunity question:** verified near-tie evidence occurred in every lane;
  applied swaps occurred in Go, Python, and Rust, but not TypeScript.

## Graph and provenance notes

Candidate graph sidecars were generated independently from each staged fixture:

| Language | Files | Nodes | Edges |
| --- | ---: | ---: | ---: |
| Go | 10 | 53 | 96 |
| Python | 8 | 46 | 87 |
| Rust | 14 | 52 | 140 |
| TypeScript | 8 | 44 | 70 |

The matrix runner initially hardcoded Rust candidate `index_version: 3` and
omitted indexed-search paths from its diff digest. Final per-arm manifests were
reconciled against both source worktrees: each uses index v2, and the candidate
diff digest includes the indexed-search files. `--graph` writes a sidecar after
indexing and does not change workspace index format. See
[`run-provenance.json`](./run-provenance.json) for the complete four-file source
bundle hash and individual file hashes.
