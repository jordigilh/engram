//! Catalog aggregation, tool filtering, and zvec compatibility helpers.
//!
//! Ported from `engram_gateway.py` with the same observable names and order.

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};

use crate::shaping::{coerce_int_or, json_truthy};

pub type Route = (String, String);

#[derive(Debug, Clone, Default)]
pub struct Catalog {
    pub routes: BTreeMap<String, Route>,
    pub tools: Vec<Value>,
}

impl Catalog {
    pub fn route(&self, tool_name: &str) -> Option<&Route> {
        self.routes.get(tool_name)
    }
}

pub const PREFIXED_BACKENDS: [&str; 6] = [
    "docs",
    "issues",
    "docs_manual",
    "issues_manual",
    "kuadrant_docs",
    "kuadrant_issues",
];

pub fn prefixed_tool_name(backend_key: &str, raw_name: &str) -> String {
    if matches!(backend_key, "docs_manual" | "issues_manual") && raw_name == "manual_mental_model" {
        return format!("{backend_key}_mental_model");
    }
    if PREFIXED_BACKENDS.contains(&backend_key) {
        return format!("{backend_key}_{raw_name}");
    }
    raw_name.to_string()
}

/// Merge each backend's raw tool list into one catalog, preserving backend
/// order and qualifying a duplicate unprefixed name with its backend key.
pub fn build_catalog(per_backend_tools: Vec<(String, Vec<Value>)>) -> Catalog {
    build_catalog_reserved(per_backend_tools, &BTreeMap::new()).0
}

/// Build a catalog while preserving public-name reservations across refreshes.
/// A name stays bound to its original `(backend, raw_tool)` even when that
/// backend is temporarily absent, so a surviving backend cannot silently take
/// over a name a client already cached.
pub fn build_catalog_reserved(
    per_backend_tools: Vec<(String, Vec<Value>)>,
    reservations: &BTreeMap<String, Route>,
) -> (Catalog, BTreeMap<String, Route>) {
    let mut catalog = Catalog::default();
    let mut next_reservations = reservations.clone();
    for (backend_key, tools) in per_backend_tools {
        for tool in tools {
            let Some(raw_name) = tool.get("name").and_then(Value::as_str) else {
                continue;
            };
            let identity = (backend_key.clone(), raw_name.to_string());
            let existing = reservations
                .iter()
                .find(|(_, owner)| **owner == identity)
                .map(|(name, _)| name.clone());
            let mut final_name =
                existing.unwrap_or_else(|| prefixed_tool_name(&backend_key, raw_name));
            if catalog.routes.contains_key(&final_name)
                || next_reservations
                    .get(&final_name)
                    .is_some_and(|owner| owner != &identity)
            {
                let base_name = format!("{backend_key}_{raw_name}");
                final_name = base_name.clone();
                let mut duplicate = 2;
                while catalog.routes.contains_key(&final_name)
                    || next_reservations
                        .get(&final_name)
                        .is_some_and(|owner| owner != &identity)
                {
                    final_name = format!("{base_name}_{duplicate}");
                    duplicate += 1;
                }
            }
            next_reservations.insert(final_name.clone(), identity);
            catalog.routes.insert(
                final_name.clone(),
                (backend_key.clone(), raw_name.to_string()),
            );
            let mut definition = tool;
            if let Some(object) = definition.as_object_mut() {
                object.insert("name".into(), Value::String(final_name));
            }
            catalog.tools.push(definition);
        }
    }
    (catalog, next_reservations)
}

const RELEVANT_HINDSIGHT: [&str; 5] = [
    "retain",
    "sync_retain",
    "recall",
    "list_mental_models",
    "get_mental_model",
];
const RECALL_ONLY_HINDSIGHT: [&str; 1] = ["recall"];
const METHODOLOGY_HINDSIGHT: [&str; 2] = ["recall", "get_mental_model"];
const MANUAL_TOOLS: [&str; 1] = ["manual_mental_model"];
const RECALL_ONLY_CODE: [&str; 1] = ["kuadrant_code_search"];
const ZVEC_MANAGEMENT: [&str; 4] = [
    "zvec_grep_index",
    "zvec_grep_index_drop",
    "zvec_grep_index_status",
    "zvec_grep_server_status",
];
const ZVEC_AGENT: [&str; 13] = [
    "zvec_grep_search",
    "zvec_search_code",
    "zvec_grep_rg",
    "zvec_grep_callgraph_blast_radius",
    "zvec_grep_callgraph_shortest_path",
    "zvec_grep_callgraph_cluster",
    "zvec_grep_callgraph_communities",
    "zvec_grep_codegraph_affected",
    "zvec_grep_codegraph_node",
    "zvec_grep_codegraph_neighbors",
    "zvec_grep_codegraph_relation_path",
    "zvec_grep_codegraph_explain",
    "zvec_grep_codegraph_capabilities",
];
const SERENA_TOOLS: [&str; 16] = [
    "replace_content",
    "replace_in_files",
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "get_symbols_overview",
    "find_symbol",
    "find_referencing_symbols",
    "find_implementations",
    "find_declaration",
    "get_diagnostics_for_file",
    "rename_symbol",
    "safe_delete_symbol",
    "activate_project",
    "list_queryable_projects",
    "query_project",
];
const RCA_TOOLS: [&str; 8] = [
    "ingest_test_run",
    "triage_test_failure",
    "generate_rca",
    "get_evidence",
    "get_related_events",
    "promote_incident",
    "get_failure_history",
    "get_incident_timeline",
];

fn allowed_for(backend_key: &str) -> Option<&'static [&'static str]> {
    match backend_key {
        "docs" | "issues" => Some(&RELEVANT_HINDSIGHT),
        "hindsight" => Some(&METHODOLOGY_HINDSIGHT),
        "serena" => Some(&SERENA_TOOLS),
        "zg" => Some(&ZVEC_AGENT),
        "docs_manual" | "issues_manual" => Some(&MANUAL_TOOLS),
        "kuadrant_docs" | "kuadrant_issues" => Some(&RECALL_ONLY_HINDSIGHT),
        "kuadrant_code" => Some(&RECALL_ONLY_CODE),
        "rca" => Some(&RCA_TOOLS),
        _ => None,
    }
}

/// Trim a backend catalog before aggregation. `code` only hides lifecycle
/// management tools; keys without a filter pass through.
pub fn filter_relevant_tools(backend_key: &str, tools: Vec<Value>) -> Vec<Value> {
    if backend_key == "code" {
        return tools
            .into_iter()
            .filter(|tool| {
                tool.get("name")
                    .and_then(Value::as_str)
                    .map(|name| !ZVEC_MANAGEMENT.contains(&name))
                    .unwrap_or(false)
            })
            .collect();
    }
    let Some(allowed) = allowed_for(backend_key) else {
        return tools;
    };
    tools
        .into_iter()
        .filter(|tool| {
            tool.get("name")
                .and_then(Value::as_str)
                .map(|name| allowed.contains(&name))
                .unwrap_or(false)
        })
        .collect()
}

pub fn search_for_pattern_tool() -> Value {
    json!({
        "name": "search_for_pattern",
        "description": "Search project files with a regular expression across the workspace. Backed by zvec-grep's managed ripgrep (no LSP process). Accepts the same arguments as the historical Serena tool; returns file-grouped matches with line numbers. Prefer `find_symbol` for symbol definitions and `find_referencing_symbols` for callers/references. Use this tool for literal or regex text searches. `substring_pattern` is a regex; with multiline matching, `.` can cross lines and `|` needs grouping. Prefer anchored patterns and narrow path/context filters; split broad alternatives into focused searches and reduce scope or context before raising `max_answer_chars`. If the answer limit is exceeded, Engram returns the available matches grouped by file and line.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "substring_pattern": {"type": "string", "description": "Regular expression to search for."},
                "context_lines_before": {"type": "integer", "default": 0},
                "context_lines_after": {"type": "integer", "default": 0},
                "paths_include_glob": {"type": "string", "default": ""},
                "paths_exclude_glob": {"type": "string", "default": ""},
                "relative_path": {"type": "string", "default": ""},
                "restrict_search_to_code_files": {"type": "boolean", "default": false},
                "skip_ignored_files": {"type": "boolean", "default": true},
                "multiline": {"type": "boolean", "default": true},
                "max_answer_chars": {"type": "integer", "default": -1}
            },
            "required": ["substring_pattern"]
        },
        "annotations": {
            "title": "Search with managed ripgrep",
            "readOnlyHint": true,
            "destructiveHint": false,
            "idempotentHint": true,
            "openWorldHint": false
        }
    })
}

const CODE_FILE_GLOBS: [&str; 16] = [
    "*.go", "*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.rs", "*.java", "*.kt", "*.c", "*.cc",
    "*.cpp", "*.h", "*.hpp", "*.rb", "*.php",
];

/// Translate Serena-shaped `search_for_pattern` args to managed rg args.
pub fn zvec_rg_arguments(
    arguments: &Map<String, Value>,
    workspace: Option<&str>,
) -> Result<Value, String> {
    let workspace = workspace
        .filter(|workspace| !workspace.is_empty())
        .ok_or_else(|| {
            "search_for_pattern requires the request workspace (absolute root)".to_string()
        })?;
    let pattern = arguments
        .get("substring_pattern")
        .and_then(Value::as_str)
        .filter(|pattern| !pattern.is_empty())
        .ok_or_else(|| "substring_pattern is required".to_string())?;

    let mut parts = vec!["rg".to_string()];
    if truthy_or(arguments, "multiline", true) {
        parts.push("-U".into());
    }
    let before = coerce_int_or(arguments.get("context_lines_before"), 0);
    let after = coerce_int_or(arguments.get("context_lines_after"), 0);
    if before != 0 {
        parts.push("-B".into());
        parts.push(before.to_string());
    }
    if after != 0 {
        parts.push("-A".into());
        parts.push(after.to_string());
    }
    if !truthy_or(arguments, "skip_ignored_files", true) {
        parts.push("--no-ignore".into());
    }

    let include = arguments
        .get("paths_include_glob")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let exclude = arguments
        .get("paths_exclude_glob")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !include.is_empty() {
        parts.extend(["-g".into(), include.to_string()]);
    }
    if !exclude.is_empty() {
        parts.extend(["-g".into(), format!("!{exclude}")]);
    }
    let relative = arguments
        .get("relative_path")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !relative.is_empty() {
        parts.extend([
            "-g".into(),
            relative.to_string(),
            "-g".into(),
            format!("{relative}/**"),
        ]);
    }
    if truthy_or(arguments, "restrict_search_to_code_files", false) {
        for glob in CODE_FILE_GLOBS {
            parts.extend(["-g".into(), glob.to_string()]);
        }
    }
    parts.extend(["-e".into(), pattern.to_string(), ".".into()]);

    Ok(json!({
        "root": workspace,
        "command": parts.iter().map(|part| shell_quote(part)).collect::<Vec<_>>().join(" ")
    }))
}

fn truthy_or(arguments: &Map<String, Value>, key: &str, default: bool) -> bool {
    match arguments.get(key) {
        None => default,
        Some(value) => json_truthy(Some(value)),
    }
}

fn shell_quote(value: &str) -> String {
    if value.is_empty() {
        return "''".into();
    }
    if value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || "-_./*=@%+:,!".contains(character))
    {
        return value.to_string();
    }
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

const ZVEC_FRESHNESS_TOOLS: [&str; 12] = [
    "zvec_grep_search",
    "zvec_search_code",
    "zvec_grep_callgraph_blast_radius",
    "zvec_grep_callgraph_shortest_path",
    "zvec_grep_callgraph_cluster",
    "zvec_grep_callgraph_communities",
    "zvec_grep_codegraph_affected",
    "zvec_grep_codegraph_node",
    "zvec_grep_codegraph_neighbors",
    "zvec_grep_codegraph_relation_path",
    "zvec_grep_codegraph_explain",
    "zvec_grep_codegraph_capabilities",
];

/// Require managed search reads to wait for a fresh index. Applied by routed
/// raw name, so the `search_for_pattern` alias (raw `zvec_grep_rg`) is exempt.
pub fn managed_zvec_arguments(raw_name: &str, arguments: Value) -> Value {
    if !ZVEC_FRESHNESS_TOOLS.contains(&raw_name) {
        return arguments;
    }
    let mut object = match arguments {
        Value::Object(object) => object,
        other => {
            let mut object = Map::new();
            object.insert("arguments".into(), other);
            object
        }
    };
    object.insert("autoUpdate".into(), Value::Bool(true));
    object.insert("freshness".into(), Value::String("wait_for_fresh".into()));
    Value::Object(object)
}

const HTTP_READ_ONLY_RETRYABLE: [&str; 8] = [
    "recall",
    "docs_recall",
    "issues_recall",
    "kuadrant_docs_recall",
    "kuadrant_issues_recall",
    "kuadrant_code_search",
    "list_mental_models",
    "get_mental_model",
];

pub fn is_retryable_raw_tool(name: &str) -> bool {
    HTTP_READ_ONLY_RETRYABLE.contains(&name)
}

/// Add the public `search_for_pattern` alias when a zg/code catalog exposes
/// `zvec_grep_rg`. Returns the backend key owning the alias.
pub fn inject_search_for_pattern(catalog: &mut Catalog) -> Option<String> {
    if catalog.routes.contains_key("search_for_pattern") {
        return None;
    }
    let owner = ["zg", "code"]
        .into_iter()
        .find(|backend| {
            catalog
                .routes
                .values()
                .any(|(owner, raw)| owner == backend && raw == "zvec_grep_rg")
        })?
        .to_string();
    if !matches!(owner.as_str(), "zg" | "code") {
        return None;
    }
    catalog.routes.insert(
        "search_for_pattern".into(),
        (owner.clone(), "zvec_grep_rg".into()),
    );
    catalog.tools.push(search_for_pattern_tool());
    Some(owner)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tool(name: &str) -> Value {
        json!({"name": name, "description": name, "inputSchema": {"type": "object"}})
    }

    #[test]
    fn docs_and_issues_are_prefixed_and_manual_alias_is_compact() {
        assert_eq!(prefixed_tool_name("docs", "recall"), "docs_recall");
        assert_eq!(
            prefixed_tool_name("kuadrant_docs", "recall"),
            "kuadrant_docs_recall"
        );
        assert_eq!(
            prefixed_tool_name("docs_manual", "manual_mental_model"),
            "docs_manual_mental_model"
        );
        assert_eq!(prefixed_tool_name("serena", "find_symbol"), "find_symbol");
    }

    #[test]
    fn duplicate_unprefixed_names_are_qualified() {
        let catalog = build_catalog(vec![
            ("code".into(), vec![tool("shared_name")]),
            ("serena".into(), vec![tool("shared_name")]),
        ]);
        assert_eq!(
            catalog.routes["shared_name"],
            ("code".into(), "shared_name".into())
        );
        assert_eq!(
            catalog.routes["serena_shared_name"],
            ("serena".into(), "shared_name".into())
        );
        assert_eq!(
            catalog
                .tools
                .iter()
                .map(|tool| tool["name"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec!["shared_name", "serena_shared_name"]
        );
    }

    #[test]
    fn duplicate_raw_names_inside_one_backend_are_qualified() {
        let catalog = build_catalog(vec![("docs".into(), vec![tool("recall"), tool("recall")])]);
        let names: Vec<&str> = catalog
            .tools
            .iter()
            .map(|tool| tool["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, vec!["docs_recall", "docs_recall_2"]);
        assert_eq!(
            catalog.routes["docs_recall_2"],
            ("docs".into(), "recall".into())
        );
    }

    #[test]
    fn rg_arguments_follow_python_truthiness_and_int_coercion() {
        let arguments = json!({
            "substring_pattern": "x",
            "multiline": null,
            "skip_ignored_files": null,
            "restrict_search_to_code_files": 1,
            "context_lines_before": "2",
            "context_lines_after": 2.9
        });
        let translated =
            zvec_rg_arguments(arguments.as_object().unwrap(), Some("/w")).expect("translation");
        let command = translated["command"].as_str().unwrap();
        assert!(!command.contains("-U"), "{command}");
        assert!(command.contains("--no-ignore"), "{command}");
        assert!(command.contains("-B 2"), "{command}");
        assert!(command.contains("-A 2"), "{command}");
        assert!(command.contains("*.go"), "{command}");
    }

    #[test]
    fn filters_match_python_sets() {
        assert_eq!(
            filter_relevant_tools(
                "docs",
                vec![tool("recall"), tool("reflect"), tool("retain")]
            )
            .iter()
            .map(|tool| tool["name"].as_str().unwrap())
            .collect::<Vec<_>>(),
            vec!["recall", "retain"]
        );
        assert_eq!(
            filter_relevant_tools("hindsight", vec![tool("recall"), tool("retain")]).len(),
            1
        );
        assert_eq!(
            filter_relevant_tools(
                "kuadrant_code",
                vec![tool("kuadrant_code_search"), tool("other")]
            )
            .len(),
            1
        );
        assert!(filter_relevant_tools(
            "code",
            vec![tool("zvec_grep_index"), tool("zvec_grep_search")]
        )
        .iter()
        .all(|tool| tool["name"] == "zvec_grep_search"));
        assert_eq!(
            filter_relevant_tools("unknown", vec![tool("anything")]).len(),
            1
        );
    }

    #[test]
    fn rg_arguments_match_the_serena_shape() {
        let arguments = json!({
            "substring_pattern": "WorkflowName",
            "context_lines_before": 2,
            "context_lines_after": 3,
            "paths_include_glob": "**/*.go",
            "paths_exclude_glob": "vendor/**",
            "relative_path": "pkg",
            "skip_ignored_files": false,
            "multiline": true
        });
        let translated = zvec_rg_arguments(arguments.as_object().unwrap(), Some("/workspace"))
            .expect("valid translation");
        assert_eq!(translated["root"], "/workspace");
        let command = translated["command"].as_str().unwrap();
        assert!(command.starts_with("rg "));
        for fragment in [
            "-U",
            "-B 2",
            "-A 3",
            "**/*.go",
            "!vendor/**",
            "pkg/**",
            "--no-ignore",
            "WorkflowName",
        ] {
            assert!(
                command.contains(fragment),
                "missing {fragment} in {command}"
            );
        }
        assert!(zvec_rg_arguments(arguments.as_object().unwrap(), None).is_err());
    }

    #[test]
    fn alias_injection_is_conditional_and_keeps_rg() {
        let mut catalog = build_catalog(vec![(
            "zg".into(),
            vec![tool("zvec_grep_search"), tool("zvec_grep_rg")],
        )]);
        assert_eq!(
            inject_search_for_pattern(&mut catalog).as_deref(),
            Some("zg")
        );
        assert_eq!(
            catalog.routes["search_for_pattern"],
            ("zg".into(), "zvec_grep_rg".into())
        );
        assert!(catalog.routes.contains_key("zvec_grep_rg"));
        assert!(catalog
            .tools
            .iter()
            .any(|tool| tool["name"] == "search_for_pattern"));

        let mut absent = build_catalog(vec![("zg".into(), vec![tool("zvec_grep_search")])]);
        assert!(inject_search_for_pattern(&mut absent).is_none());
    }

    #[test]
    fn managed_policy_targets_raw_names_only() {
        let updated = managed_zvec_arguments("zvec_grep_search", json!({"root": "/w"}));
        assert_eq!(updated["autoUpdate"], true);
        assert_eq!(updated["freshness"], "wait_for_fresh");

        let rg = managed_zvec_arguments("zvec_grep_rg", json!({"root": "/w"}));
        assert!(rg.get("freshness").is_none());
        assert!(is_retryable_raw_tool("docs_recall"));
        assert!(is_retryable_raw_tool("kuadrant_code_search"));
        assert!(!is_retryable_raw_tool("retain"));
    }

    #[test]
    fn alias_prefers_zg_over_code_regardless_of_public_name_order() {
        let mut catalog = build_catalog(vec![
            ("code".into(), vec![tool("zvec_grep_rg")]),
            ("zg".into(), vec![tool("zvec_grep_rg")]),
        ]);
        // `code` owns the unqualified name and `zg` is qualified, so a sorted
        // public-name scan would pick code first.
        assert_eq!(
            catalog.routes["zvec_grep_rg"],
            ("code".into(), "zvec_grep_rg".into())
        );
        assert_eq!(
            inject_search_for_pattern(&mut catalog).as_deref(),
            Some("zg")
        );
        assert_eq!(
            catalog.routes["search_for_pattern"],
            ("zg".into(), "zvec_grep_rg".into())
        );
    }

    #[test]
    fn reservations_keep_public_names_bound_across_refresh() {
        let first = build_catalog_reserved(
            vec![
                ("code".into(), vec![tool("shared_name")]),
                ("serena".into(), vec![tool("shared_name")]),
            ],
            &BTreeMap::new(),
        );
        assert_eq!(
            first.0.routes["shared_name"],
            ("code".into(), "shared_name".into())
        );
        assert_eq!(
            first.0.routes["serena_shared_name"],
            ("serena".into(), "shared_name".into())
        );

        let second =
            build_catalog_reserved(vec![("serena".into(), vec![tool("shared_name")])], &first.1);
        assert!(!second.0.routes.contains_key("shared_name"));
        assert_eq!(
            second.0.routes["serena_shared_name"],
            ("serena".into(), "shared_name".into())
        );
    }

    #[test]
    fn alias_schema_exposes_max_answer_chars() {
        let alias = search_for_pattern_tool();
        assert_eq!(
            alias["inputSchema"]["properties"]["max_answer_chars"]["default"],
            -1
        );
        assert!(alias["description"]
            .as_str()
            .unwrap()
            .contains("max_answer_chars"));
    }
}
