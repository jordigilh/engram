//! Dev-only docs shadow gate (issue #214 slice 3).
//!
//! Compares two oracle captures of the real CocoIndex docs flow with the native
//! planner on the same frozen trees. The before/after pair also exercises the
//! native-only delete path against exactly the document ids the oracle stopped
//! emitting.
//!
//! This is not a service binary; see `fixtures/docs_shadow_run.py` for the
//! one-command driver that freezes the tree, captures both oracle snapshots,
//! and invokes this example.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use engram_ingest::{
    compare, compare_deletes, compare_ledger, expected_deletes, manifest_from_scan,
    merge_conflicts, merge_deletes, parse_oracle, reconcile, scan_source, validate_oracle,
    DocsSource, OracleProvenance, ShadowReport,
};

const EXIT_UNEXPECTED: i32 = 2;

struct Options {
    root_before: PathBuf,
    root_after: PathBuf,
    oracle_before: PathBuf,
    oracle_after: PathBuf,
    report_json: PathBuf,
    report_markdown: PathBuf,
    revision: String,
    mutation: String,
    source: SourceOptions,
}

struct SourceOptions {
    project: String,
    tag: String,
    bank: String,
    include: Vec<String>,
    exclude: Vec<String>,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("docs_shadow: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let options = parse_args()?;
    let before_json = std::fs::read_to_string(&options.oracle_before)?;
    let after_json = std::fs::read_to_string(&options.oracle_after)?;
    let oracle_before = parse_oracle(&before_json)?;
    let oracle_after = parse_oracle(&after_json)?;
    validate_oracle(&oracle_before).map_err(plain)?;
    validate_oracle(&oracle_after).map_err(plain)?;

    let source_before = options.source.docs_source(&options.root_before);
    let source_after = options.source.docs_source(&options.root_after);

    let (scan_before, report_before_scan) = scan_source(&source_before)?;
    let (scan_after, report_after_scan) = scan_source(&source_after)?;

    let mut before = compare(&source_before, &oracle_before, &scan_before);
    let ledger_before = reconcile(
        &engram_ingest::ledger::Manifest::default(),
        &scan_before,
        engram_ingest::ledger::LAYOUT_VERSION,
    );
    let ledger_report_before = compare_ledger(&oracle_before, &ledger_before);
    merge_report(&mut before, &ledger_report_before);
    merge_conflicts(&mut before, &ledger_before);

    let mut after = compare(&source_after, &oracle_after, &scan_after);
    let ledger_after = reconcile(
        &engram_ingest::ledger::Manifest::default(),
        &scan_after,
        engram_ingest::ledger::LAYOUT_VERSION,
    );
    let ledger_report_after = compare_ledger(&oracle_after, &ledger_after);
    merge_report(&mut after, &ledger_report_after);
    merge_conflicts(&mut after, &ledger_after);

    let manifest = manifest_from_scan(&scan_before, 1);
    let reconciliation = reconcile(
        &manifest,
        &scan_after,
        engram_ingest::ledger::LAYOUT_VERSION,
    );
    let expected = expected_deletes(&oracle_before, &oracle_after);
    let (delete_parity, delete_divergences) = compare_deletes(&expected, &reconciliation);
    let mut deletes = ShadowReport::default();
    merge_deletes(&mut deletes, delete_parity, delete_divergences);

    let total_unexpected = before.unexpected + after.unexpected + deletes.unexpected;

    let document = serde_json::json!({
        "gate": "docs-shadow",
        "issue": 214,
        "source": {
            "project": options.source.project,
            "tag": options.source.tag,
            "bank": options.source.bank,
            "docs_include": options.source.include,
            "docs_exclude": options.source.exclude,
            "revision": options.revision,
            "mutation": options.mutation,
        },
        "scope": {
            "cocoindex": oracle_before.provenance.cocoindex.clone(),
            "statement": "Measured against the deployed CocoIndex version above; pyproject.toml pins cocoindex==1.2.0, so this report does not claim 1.2.0 parity.",
        },
        "provenance_before": report_provenance(&oracle_before.provenance),
        "provenance_after": report_provenance(&oracle_after.provenance),
        "scan_before": {
            "matched_files": report_before_scan.matched_files,
            "skipped_symlinks": report_before_scan.skipped_symlinks,
            "pruned_directories": report_before_scan.pruned_directories,
        },
        "scan_after": {
            "matched_files": report_after_scan.matched_files,
            "skipped_symlinks": report_after_scan.skipped_symlinks,
            "pruned_directories": report_after_scan.pruned_directories,
        },
        "before": &before,
        "after": &after,
        "deletes": &deletes,
        "expected_deletes": &expected,
        "unexpected_total": total_unexpected,
    });
    std::fs::write(&options.report_json, serde_json::to_vec_pretty(&document)?)?;
    std::fs::write(
        &options.report_markdown,
        render_markdown(&options, &document, total_unexpected),
    )?;

    println!(
        "docs shadow: before {} retains / {} files, after {} retains / {} files, deletes {} expected / {} emitted, unexpected {}",
        before.retains.oracle_retains,
        before.files.oracle_files,
        after.retains.oracle_retains,
        after.files.oracle_files,
        deletes.deletes.expected,
        deletes.deletes.emitted,
        total_unexpected
    );
    if total_unexpected > 0 {
        std::process::exit(EXIT_UNEXPECTED);
    }
    Ok(())
}

fn merge_report(into: &mut ShadowReport, other: &ShadowReport) {
    into.unexpected += other.unexpected;
    into.divergences.extend(other.divergences.iter().cloned());
}

/// Report the captured provenance without the ephemeral frozen-tree path; the
/// tree sha256 and source revision identify the snapshot.
fn report_provenance(provenance: &OracleProvenance) -> serde_json::Value {
    serde_json::json!({
        "python": provenance.python,
        "cocoindex": provenance.cocoindex,
        "sklearn": provenance.sklearn,
        "networkx": provenance.networkx,
        "flow_source_commit": provenance.flow_source_commit,
        "flow_source_dirty": provenance.flow_source_dirty,
        "flow_source_sha256": provenance.flow_source_sha256,
        "generated_at": provenance.generated_at,
        "tree": {
            "root": "<frozen>/docs",
            "source_tag": provenance.tree.source_tag,
            "bank_id": provenance.tree.bank_id,
            "docs_include": provenance.tree.docs_include,
            "docs_exclude": provenance.tree.docs_exclude,
            "files": provenance.tree.files,
            "bytes": provenance.tree.bytes,
            "sha256": provenance.tree.sha256,
        },
    })
}

fn plain(error: String) -> Box<dyn std::error::Error> {
    error.into()
}

impl SourceOptions {
    fn docs_source(&self, root: &Path) -> DocsSource {
        DocsSource {
            project: self.project.clone(),
            tag: self.tag.clone(),
            root: root.to_path_buf(),
            bank_id: self.bank.clone(),
            docs_include: self.include.clone(),
            docs_exclude: self.exclude.clone(),
        }
    }
}

fn next_value(
    args: &mut impl Iterator<Item = String>,
    flag: &str,
) -> Result<String, Box<dyn std::error::Error>> {
    args.next()
        .ok_or_else(|| format!("{flag} requires a value").into())
}

fn parse_args() -> Result<Options, Box<dyn std::error::Error>> {
    let mut root_before = None;
    let mut root_after = None;
    let mut oracle_before = None;
    let mut oracle_after = None;
    let mut report_json = None;
    let mut report_markdown = None;
    let mut revision = "unknown".to_string();
    let mut mutation = "not recorded".to_string();
    let mut project = "engram".to_string();
    let mut tag = "engram-docs".to_string();
    let mut bank = "engram-docs".to_string();
    let mut include = Vec::new();
    let mut exclude = Vec::new();

    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--root-before" => root_before = Some(PathBuf::from(next_value(&mut args, &arg)?)),
            "--root-after" => root_after = Some(PathBuf::from(next_value(&mut args, &arg)?)),
            "--oracle-before" => oracle_before = Some(PathBuf::from(next_value(&mut args, &arg)?)),
            "--oracle-after" => oracle_after = Some(PathBuf::from(next_value(&mut args, &arg)?)),
            "--report-json" => report_json = Some(PathBuf::from(next_value(&mut args, &arg)?)),
            "--report-markdown" => {
                report_markdown = Some(PathBuf::from(next_value(&mut args, &arg)?))
            }
            "--revision" => revision = next_value(&mut args, &arg)?,
            "--mutation" => mutation = next_value(&mut args, &arg)?,
            "--project" => project = next_value(&mut args, &arg)?,
            "--tag" => tag = next_value(&mut args, &arg)?,
            "--bank" => bank = next_value(&mut args, &arg)?,
            "--include" => include.push(next_value(&mut args, &arg)?),
            "--exclude" => exclude.push(next_value(&mut args, &arg)?),
            "-h" | "--help" => {
                println!(
                    "usage: docs_shadow --root-before DIR --root-after DIR \\
                     --oracle-before FILE --oracle-after FILE \\
                     --report-json FILE --report-markdown FILE \\
                     [--revision REV] [--mutation TEXT] [--project NAME] [--tag TAG] [--bank BANK] \\
                     [--include GLOB]... [--exclude GLOB]..."
                );
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument {other}").into()),
        }
    }
    if include.is_empty() {
        include.push("**/*.md".to_string());
    }
    Ok(Options {
        root_before: root_before.ok_or("--root-before is required")?,
        root_after: root_after.ok_or("--root-after is required")?,
        oracle_before: oracle_before.ok_or("--oracle-before is required")?,
        oracle_after: oracle_after.ok_or("--oracle-after is required")?,
        report_json: report_json.ok_or("--report-json is required")?,
        report_markdown: report_markdown.ok_or("--report-markdown is required")?,
        revision,
        mutation,
        source: SourceOptions {
            project,
            tag,
            bank,
            include,
            exclude,
        },
    })
}

fn render_markdown(
    options: &Options,
    document: &serde_json::Value,
    total_unexpected: usize,
) -> String {
    let source = &document["source"];
    let before = &document["before"];
    let after = &document["after"];
    let deletes = &document["deletes"];
    let provenance_before = &document["provenance_before"];
    let provenance_after = &document["provenance_after"];

    let mut lines = Vec::new();
    lines.push("# Docs shadow gate (#214 slice 3)".to_string());
    lines.push(String::new());
    lines.push(format!(
        "Measured comparison of the real CocoIndex docs flow with the native docs planner on frozen trees. Unexpected divergences: **{total_unexpected}**."
    ));
    lines.push(String::new());
    lines.push("## Provenance".to_string());
    lines.push(String::new());
    lines.push("| | Before | After |".to_string());
    lines.push("|---|---|---|".to_string());
    lines.push(format!(
        "| Source revision | `{}` | same |",
        options.revision
    ));
    lines.push(format!(
        "| Flow source commit | `{}` | `{}` |",
        text(&provenance_before["flow_source_commit"]),
        text(&provenance_after["flow_source_commit"])
    ));
    lines.push(format!(
        "| Flow source dirty | {} | {} |",
        text(&provenance_before["flow_source_dirty"]),
        text(&provenance_after["flow_source_dirty"])
    ));
    lines.push(format!(
        "| Python | {} | {} |",
        text(&provenance_before["python"]),
        text(&provenance_after["python"])
    ));
    lines.push(format!(
        "| CocoIndex | {} | {} |",
        text(&provenance_before["cocoindex"]),
        text(&provenance_after["cocoindex"])
    ));
    lines.push(format!(
        "| Tree files | {} | {} |",
        text(&provenance_before["tree"]["files"]),
        text(&provenance_after["tree"]["files"])
    ));
    lines.push(format!(
        "| Tree bytes | {} | {} |",
        text(&provenance_before["tree"]["bytes"]),
        text(&provenance_after["tree"]["bytes"])
    ));
    lines.push(format!(
        "| Tree sha256 | `{}` | `{}` |",
        text(&provenance_before["tree"]["sha256"]),
        text(&provenance_after["tree"]["sha256"])
    ));
    lines.push(format!(
        "| Source tag | `{}` | `{}` |",
        text(&provenance_before["tree"]["source_tag"]),
        text(&provenance_after["tree"]["source_tag"])
    ));
    lines.push(String::new());
    lines.push(format!(
        "Rust source identity: project `{}`, tag `{}`, bank `{}`, include `{}`, exclude `{}`.",
        text(&source["project"]),
        text(&source["tag"]),
        text(&source["bank"]),
        text(&source["docs_include"]),
        text(&source["docs_exclude"])
    ));
    lines.push(String::new());
    lines.push(format!(
        "Scope: this measurement covers CocoIndex `{}` from the deployed capture venv. `pyproject.toml` pins `cocoindex==1.2.0`, so the report does not claim 1.2.0 parity.",
        text(&document["scope"]["cocoindex"])
    ));
    lines.push(String::new());
    lines.push("## Measured parity".to_string());
    lines.push(String::new());
    lines.push("| Scenario | Files (oracle/Rust) | Retains (oracle/Rust) | Matched | Duplicate ids | Deletes expected/emitted | Unexpected |".to_string());
    lines.push("|---|---|---|---|---|---|---|".to_string());
    for (label, report) in [("Before", before), ("After", after)] {
        lines.push(format!(
            "| {label} | {}/{} | {}/{} | {} | {} | - | {} |",
            report["files"]["oracle_files"],
            report["files"]["rust_files"],
            report["retains"]["oracle_retains"],
            report["retains"]["rust_retains"],
            report["retains"]["matched"],
            report["retains"]["duplicate_ids"],
            report["unexpected"]
        ));
    }
    lines.push(format!(
        "| Before → after deletes | - | - | {} | - | {}/{} | {} |",
        deletes["deletes"]["matched"],
        deletes["deletes"]["expected"],
        deletes["deletes"]["emitted"],
        deletes["unexpected"]
    ));
    lines.push(String::new());
    lines.push(format!(
        "The Python flow has no source-document delete. The expectation is derived from the two oracle retain snapshots: {} ids disappeared. The native ledger emitted {} deletes ({} matched).",
        deletes["deletes"]["expected"], deletes["deletes"]["emitted"], deletes["deletes"]["matched"]
    ));
    lines.push(String::new());
    lines.push(format!(
        "Mutation scenario: {}. Each oracle run used a fresh CocoIndex LMDB, so both snapshots are full-flow emissions rather than incremental deltas.",
        options.mutation
    ));
    lines.push(String::new());
    lines.push("## Divergences".to_string());
    lines.push(String::new());
    let mut by_kind: BTreeMap<String, usize> = BTreeMap::new();
    for (label, report) in [("before", before), ("after", after), ("deletes", deletes)] {
        for divergence in report["divergences"].as_array().into_iter().flatten() {
            let kind = divergence["kind"].as_str().unwrap_or("?");
            *by_kind.entry(format!("{label} / {kind}")).or_default() += 1;
        }
    }
    if by_kind.is_empty() {
        lines.push("None. All compared operations match.".to_string());
    } else {
        lines.push("| Scenario / kind | Count |".to_string());
        lines.push("|---|---|".to_string());
        for (kind, count) in &by_kind {
            lines.push(format!("| `{kind}` | {count} |"));
        }
        lines.push(String::new());
        for (label, report) in [("before", before), ("after", after), ("deletes", deletes)] {
            for divergence in report["divergences"].as_array().into_iter().flatten() {
                lines.push(format!(
                    "- **{}** ({label}){}: {}",
                    divergence["kind"].as_str().unwrap_or("?"),
                    divergence["relative_path"]
                        .as_str()
                        .map(|path| format!(" `{path}`"))
                        .unwrap_or_default(),
                    divergence["detail"].as_str().unwrap_or("")
                ));
            }
        }
    }
    lines.push(String::new());
    lines.push("## Reproduce".to_string());
    lines.push(String::new());
    lines.push("See `rust/crates/engram-ingest/fixtures/docs_shadow_run.py`.".to_string());
    lines.push(String::new());
    lines.push("```bash".to_string());
    lines.push(format!(
        "~/.engram/venv/bin/python rust/crates/engram-ingest/fixtures/docs_shadow_run.py --rev {}",
        options.revision
    ));
    lines.push("```".to_string());
    lines.push(String::new());
    lines.push(
        "Both sides read the same frozen snapshots; the tree sha256 and source revision above identify them."
            .to_string(),
    );
    lines.push(String::new());
    lines.join("\n")
}

fn text(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(value) => value.clone(),
        other => other.to_string(),
    }
}
