//! Tree-level shadow parity with the Python docs flow.

use std::path::{Path, PathBuf};
use std::process::Command;

use engram_ingest::shadow::{plan_tree, to_jsonl, ShadowOptions};

fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/shadow/tree")
}

fn expected() -> &'static str {
    include_str!("../fixtures/shadow/expected.jsonl")
}

fn options() -> ShadowOptions {
    ShadowOptions {
        source_tag: "repo".to_string(),
        timestamps: false,
    }
}

#[test]
fn rust_plan_matches_python_expected_operations() {
    let operations = plan_tree(&fixture_root(), &options()).unwrap();
    assert_eq!(operations.len(), 11);
    assert_eq!(to_jsonl(&operations).unwrap(), expected());
}

#[test]
fn binary_emits_the_same_operations() {
    let output = Command::new(env!("CARGO_BIN_EXE_engram-ingest-shadow"))
        .arg("--root")
        .arg(fixture_root())
        .arg("--source-tag")
        .arg("repo")
        .arg("--no-timestamps")
        .output()
        .expect("run shadow binary");
    assert!(output.status.success(), "{output:?}");
    assert_eq!(String::from_utf8(output.stdout).unwrap(), expected());
}

#[test]
fn fixture_provenance_records_the_python_walker() {
    let meta: serde_json::Value =
        serde_json::from_str(include_str!("../fixtures/shadow/expected.meta.json")).unwrap();
    assert_eq!(meta["timestamps"], false);
    assert_eq!(meta["operations"], 11);
    assert_eq!(meta["source_tag"], "repo");
    assert_eq!(meta["tree"], "tree");
}
