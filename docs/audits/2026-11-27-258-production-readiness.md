# #258 PDF ingestion — production-readiness audit (FedRAMP / OWASP ASVS)

Date: 2026-11-27
Scope: PR #260 / issue #258, Rust `engram-ingest` PDF path, fixtures, CI, dependencies, docs.
Audit basis: independent read-only review at commit `43111ab`, with the remediation commits applied afterwards in this branch.

## Control basis and authoritative references

Authoritative control excerpts were retrieved from the NIST SP 800-53 Rev.5
OSCAL catalog (`usnistgov/oscal-content`, `NIST_SP-800-53_rev5_catalog.json`)
and the OWASP ASVS 5.0.0 English chapters on 2026-11-27, retained in Engram
(tags `project:engram`, `audit`, `fedramp`, `nist-800-53-rev5`, `asvs-5.0`,
`authoritative-reference`; memory `83e34534-8dfa-4165-8ab0-1c150f8fd8db`,
recall document `dacb3560-d5b6-40e9-9d9a-09c73f68edf4`) and recalled for this
audit. FedRAMP Rev.5 baselines adopt the NIST controls cited below. No control
ID is cited here without its text having been recalled from that record.

## Verdict

**Conditional.** The three blockers found at `43111ab` (unbounded Form XObject
recursion, no input/page amplification caps, and empty-password PDF rejection)
are fixed and regression-tested in this branch. The PDF slice is credible for
merge, but production authorization still requires the open evidence items in
§4 (audit-event persistence, SBOM/audit CI, DB format constraint, deployment
smoke). Layout parity beyond the committed corpus remains a documented
limitation.

## 1. Control matrix

| Area | NIST SP 800-53 Rev.5 | OWASP ASVS 5.0 | Evidence | Status |
|---|---|---|---|---|
| File type / size / unpacked-size limits | SI-10, SC-5 | 5.1.1, 5.2.1, 5.2.2, 5.2.3, 15.2.2 | Format comes from trusted config, not content (`config.rs`, `scan.rs`); 256 MiB input cap before read; 64 MiB/stream, 256 MiB/document, page/form caps (`pdf.rs`); `oversized_pdf_is_rejected_before_reading`, `decompression_bomb_is_rejected_before_extraction`, `stream_and_document_expansion_caps_are_enforced`, `page_content_budget_is_enforced` | Satisfied |
| Parser consistency / memory safety | SI-10, SI-7 | 1.4.1, 1.4.2, 1.4.3, 1.5.3 | Strict lopdf decode for page/Form content; xref object and `/Length` checks; panic boundary; `corrupt_page_content…`, `form_xobject_content_is_strictly_validated`, `object_table_rejects_unloaded_xref_entries` | Satisfied |
| Recursion / page / form exhaustion | SC-5, SI-10 | 15.2.2 | Depth 64, 10k forms, 10k pages, 1,024 streams/page; `deeply_nested_form_xobjects_are_rejected_without_stack_overflow` | Satisfied |
| Path traversal / symlink handling | SI-10 | 5.3.1, 5.3.2 | `openat` + `O_NOFOLLOW`, fd-relative walk; `skips_symlinked_files_and_directories`, `configured_root_symlink_is_opened_by_its_canonical_target` | Satisfied |
| Error handling / fail-secure | SI-11 | 16.5.1, 16.5.3 | Rejected PDF skips snapshot without partial apply; `corrupt_pdf_skips_the_source_and_leaves_the_manifest_untouched` | Partially satisfied (see C4/C6) |
| Audit record content / retention / integrity | AU-2, AU-3, AU-9, AU-11, AU-12 | 16.1.1, 16.2.1, 16.2.2, 16.4.2, 14.1.2, 14.2.7 | Ledger records source/tag/root/format/hash/size/state/timestamps; `document_format='pdf'` and delete flow tested | Partially satisfied (C5) |
| Integrity: hashes, stable IDs, replay | SI-7 | 11.4.1, 11.4.3, 2.3.3 | SHA-256 over rendered text; stable IDs; replay/delete test; transactional revision/fingerprint | Satisfied |
| Configuration baseline / change control / least functionality | CM-2, CM-3, CM-5, CM-6, CM-7 | 2.3.2, 15.2.3 | Lockfile + PR review; format validated and persisted; no subprocess/service/OCR | Partially satisfied (C8/C9) |
| Supply chain / provenance / SBOM / remediation | SR-3, SR-4, SR-11, RA-5, SI-2 | 15.1.1, 15.1.2, 15.2.1, 15.2.4 | Locked checksums, fixture/source hashes; `cargo audit`: 0 vulnerabilities, 2 unmaintained warnings (`chrono` pre-existing, `ttf-parser` new via pdf-extract) | Gap (E1) |
| Cryptographic protection / hash choice | SC-13 | 11.2.1, 11.4.1, 11.4.3 | SHA-256 integrity; PDF decryption delegated to lopdf/RustCrypto (empty user password only) | Partially satisfied (not FIPS-validated; spec-bound) |
| Concurrency / atomicity | SI-7 | 2.3.3, 15.4.1, 15.4.2 | Single transaction, `FOR UPDATE`, revision + fingerprint replay; `ledger_postgres` stale-plan tests | Satisfied |
| Test pyramid / developer testing | SA-11 | — | 256 unit, 11 PDF parity, 3 corpus, 21 Postgres (2 PDF), workspace suites; aggregate coverage >93% | Satisfied for #258 modules; see E2 |
| Malicious-code scanning of untrusted files | SI-3 | 5.4.3 | No AV scan; local operator-owned docs roots; parser hardening is the compensating control | Not applicable |
| Authentication / session / network boundary | AC/IA families; ASVS V6/V7 | — | Local process, no ingest endpoint or sessions | Not applicable |

## 2. Pyramid invariant and coverage

Invariant applied (from `docs/TESTING.md`): deterministic business logic is
unit-tested without network/Postgres; database and HTTP wiring is integration
tested; deployment smoke is separate and must not be faked by mocks. For #258,
the owning modules are unit-tested and the full ingest path is integration
tested against real Postgres and a mock Hindsight sink.

| Tier | Count | Notes |
|---|---:|---|
| Unit (lib) | 265 | Includes 24+ PDF-path unit tests |
| Integration | 93+ | PDF parity 11, corpus 3, Postgres 21 (2 PDF), other crate suites |
| Deployment smoke | 0 | **Gap E5**: no packaged-binary PDF smoke |

Coverage (real Postgres DSN, 2026-11-27):

| Gate | Result |
|---|---|
| Unit business gate (`--lib`, CI exclusion regex) | **93.41% lines** |
| All-targets gate | **93.25% lines** |
| `pdf.rs` | **93.61%** |
| `document.rs` | **95.38%** |
| `config.rs` | **95.90%** |
| `scan.rs` | **90.96%** |

Every module owning #258 business logic is above 90% lines. Shared business
support was raised in this branch: `ledger.rs` 100%, `synthesis.rs` 95.25%,
`hashing.rs` 95.65%, `jobs.rs` 93.30% (all-targets). Remaining all-targets
modules below 90% are adapters or developer tooling: `http` 78.48%, `sink`
85.24%, `runtime` 86.65%, `shadow` 88.95% (developer shadow harness), `store`
89.78%; their behavior is covered by the HTTP/Postgres/runtime integration
tiers, not claimed as unit coverage. CI gates are aggregate-only (E2).

## 3. Differential evidence

- Primary fixture: rendered content and planner calls byte-for-byte against the
  pdfplumber/`_retain_document` oracle.
- Corpus: 8 differential cases (two-column, table, two fonts, long page,
  paragraph gaps, WinAnsi unicode, delimiters, multi-page with blank middle) all
  match pdfplumber byte-for-byte.
- Encrypted: empty user password matches pdfplumber; non-empty password is
  rejected.
- Mtime: four fixed nanosecond mtimes match Python `datetime.fromtimestamp` in
  UTC.
- Oracle pins: pdfplumber `0.11.10`, fixture SHA-256, Python source SHA-256
  (`configured.py`, `chunking.py`, `synthesis.py`).

## 4. Findings and disposition

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| C1 | High | Unbounded Form XObject recursion overflowed the stack (SIGABRT) | **Fixed**: depth 64 + 10k form cap; regression test |
| C2 | High | No input size cap; page-content/filename amplification; clone before limit | **Fixed**: 256 MiB input cap, page/stream/form caps, limit check before clone; tests |
| C3 | Medium-High | Empty-password encrypted PDFs rejected (xref check after decrypt) | **Fixed**: xref check before decrypt, stream checks after; encrypted fixtures/tests |
| C4 | Medium | One bad PDF skips the whole source; only stderr, no metric | **Accepted policy** (matches unreadable-file fail-closed behavior); operator metric deferred |
| C5 | Medium | Parse rejections are not persisted audit events | **Open**: add append-only skip/event record or structured JSON log with retention |
| C6 | Low-Medium | Log injection via raw path/Debug PDF object text | **Open accepted**: local-only error channel; encode/truncate in a follow-up |
| C7 | Low | O(n²) xref validation | **Fixed**: loaded-object HashSet |
| C8 | Low | PDF sources may include non-PDF globs and amplify C4 | **Open**: startup warning/rejection for mixed patterns |
| C9 | Low | `sources.document_format` has no DB CHECK | **Open**: forward migration to constrain values |
| E1 | High evidence | No cargo audit/deny/SBOM gate or cargo Dependabot | **Open**: add RustSec audit + CycloneDX SBOM + cargo ecosystem |
| E2 | Medium evidence | CI gates are aggregate-only; legacy modules below 90% | **Addressed for shared business support** (ledger 100%, synthesis 95.25%, hashing 95.65%, jobs 93.30%); adapter-only exceptions disclosed; per-module gate follow-up |
| E3 | Medium evidence | Oracle provenance not regenerated in CI; pdfplumber pin asserted only in Rust | **Partially fixed**: goldens regenerated at branch HEAD and pin asserted; CI freshness job deferred |
| E4 | Medium evidence | Encryption path untested and doc claim false | **Fixed**: fixtures + positive/negative tests; audit corrected |
| E5 | Low evidence | No deployment smoke for packaged binary | **Open** |
| E6 | Low evidence | Control-basis statement stale | **Fixed**: authoritative references retained in Engram and cited |

## 5. Open items required before production authorization

1. C5: persist parse-rejection events (AU-2/AU-3/AU-12, ASVS 16.3.3/16.3.4).
2. E1: RustSec audit + SBOM + cargo Dependabot (RA-5, SR-3/SR-4, ASVS 15.1–15.2).
3. C9: DB CHECK for `document_format` (CM-6, SI-7).
4. C8: guard PDF include patterns (SI-10, ASVS 2.1.1).
5. E5: one packaged-binary PDF smoke (SA-11).
6. E2: add per-module coverage enforcement or document an approved exception list.

## 6. Verification commands

```bash
cd rust
cargo fmt --all --check
cargo clippy --all-targets --locked -- -D warnings
rustup run 1.88.0 cargo check --all-targets --locked
ENGRAM_INGEST_TEST_DATABASE_URL=... cargo test --all --locked
cargo llvm-cov -p engram-ingest --lib --ignore-filename-regex ... --fail-under-lines 90
cargo llvm-cov -p engram-ingest --all-targets --fail-under-lines 90
```

Results: all pass; 93.25% all-targets, 93.41% unit business gate; `cargo audit`
0 vulnerabilities, 2 unmaintained warnings (E1).
