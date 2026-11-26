//! GitHub project-board serialization parity (issue #244).
//!
//! Mirrors `process_board`, `_fetch_project_items`, and `_board_status` in
//! `src/engram/flows/configured.py`. A board is one Hindsight document, never
//! chunked. The content is grouped by the project's `Status` field in a fixed
//! order, followed by first-seen custom statuses. `Total items` counts every
//! GraphQL node, including draft items with no content.

use std::collections::BTreeMap;

use chrono::Timelike;
use serde_json::{json, Value};

use crate::document::RetainCall;
use crate::hashing::ContentHash;
use crate::issues::{IssueError, IssuePlan, SOURCE_METADATA_VALUE};

pub const BOARD_STATUS_ORDER: [&str; 7] = [
    "In Progress",
    "Next",
    "Review",
    "Backlog",
    "Epics",
    "Done",
    "(no status)",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BoardItem {
    pub status: Option<String>,
    pub repo: String,
    pub number: u64,
    pub title: String,
    pub has_content: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Board {
    pub number: u64,
    pub organization: String,
    pub title: String,
    pub description: String,
    pub items: Vec<BoardItem>,
}

impl BoardItem {
    pub fn from_graphql(value: &Value) -> Self {
        let content = value.get("content").cloned().unwrap_or(Value::Null);
        let has_content = content.as_object().is_some_and(|object| !object.is_empty());
        let status = value
            .pointer("/fieldValues/nodes")
            .and_then(Value::as_array)
            .and_then(|nodes| {
                nodes.iter().find_map(|node| {
                    (node.pointer("/field/name").and_then(Value::as_str) == Some("Status")).then(
                        || {
                            node.get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string()
                        },
                    )
                })
            });
        Self {
            status,
            repo: content
                .pointer("/repository/name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            number: content.get("number").and_then(Value::as_u64).unwrap_or(0),
            title: content
                .get("title")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            has_content,
        }
    }
}

pub fn plan_board(
    board: &Board,
    document_prefix: Option<&str>,
    timestamp: Option<String>,
) -> Result<IssuePlan, IssueError> {
    let slug = board_slug(&board.title);
    let prefix = document_prefix
        .filter(|prefix| !prefix.is_empty())
        .unwrap_or("board");
    let document_id = format!("{prefix}-{}-{slug}", board.number);
    let content = board_content(board);
    let snapshot = board_snapshot_value(board, prefix);
    let snapshot_sha256 = ContentHash::of(
        &serde_json::to_vec(&snapshot).map_err(|error| IssueError::Serialize(error.to_string()))?,
    );
    let size_bytes = serde_json::to_vec(&snapshot)
        .map_err(|error| IssueError::Serialize(error.to_string()))?
        .len() as i64;
    let mut metadata = BTreeMap::new();
    metadata.insert("source".to_string(), SOURCE_METADATA_VALUE.to_string());
    metadata.insert("kind".to_string(), "board".to_string());
    metadata.insert("board_number".to_string(), board.number.to_string());
    metadata.insert("board_title".to_string(), board.title.clone());
    let tags = vec!["board".to_string(), "roadmap".to_string(), slug];
    Ok(IssuePlan {
        relative_path: format!("board/{}", board.number),
        snapshot_sha256,
        size_bytes,
        calls: vec![RetainCall {
            document_id,
            chunk_key: String::new(),
            content_sha256: ContentHash::of(content.as_bytes()),
            content,
            metadata,
            tags,
            timestamp,
        }],
    })
}

pub fn board_content(board: &Board) -> String {
    let mut grouped: Vec<(String, Vec<String>)> = Vec::new();
    for item in &board.items {
        if !item.has_content {
            continue;
        }
        let status = item
            .status
            .clone()
            .unwrap_or_else(|| "(no status)".to_string());
        let line = format!("- #{} ({}): {}", item.number, item.repo, item.title);
        match grouped.iter_mut().find(|(name, _)| name == &status) {
            Some((_, lines)) => lines.push(line),
            None => grouped.push((status, vec![line])),
        }
    }
    let mut ordered = Vec::new();
    for status in BOARD_STATUS_ORDER {
        if let Some(index) = grouped.iter().position(|(name, _)| name == status) {
            ordered.push(grouped.remove(index));
        }
    }
    ordered.extend(grouped);

    let mut parts = vec![format!(
        "# Project Board: {} (#{})",
        board.title, board.number
    )];
    if !board.description.is_empty() {
        parts.push(board.description.clone());
    }
    parts.push(format!("Total items: {}", board.items.len()));
    parts.push(String::new());
    for (status, lines) in ordered {
        parts.push(format!("## Status: {status} ({})", lines.len()));
        parts.extend(lines);
        parts.push(String::new());
    }
    parts.join("\n")
}

pub fn board_slug(title: &str) -> String {
    title.to_lowercase().replace([' ', '/'], "-")
}

/// `datetime.now().isoformat()` for the board retain timestamp.
pub fn python_now_local_timestamp() -> String {
    let now = chrono::Local::now().naive_local();
    if now.nanosecond() / 1_000 == 0 {
        now.format("%Y-%m-%dT%H:%M:%S").to_string()
    } else {
        now.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    }
}

fn board_snapshot_value(board: &Board, prefix: &str) -> Value {
    json!({
        "provider": "github_board",
        "organization": board.organization,
        "number": board.number,
        "prefix": prefix,
        "title": board.title,
        "description": board.description,
        "items": board.items.iter().map(|item| json!({
            "status": item.status,
            "repo": item.repo,
            "number": item.number,
            "title": item.title,
            "has_content": item.has_content,
        })).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn board() -> Board {
        Board {
            number: 9,
            organization: "praxis-proxy".to_string(),
            title: "Praxis Policy Engine (PPE)".to_string(),
            description: "Policy engine roadmap".to_string(),
            items: vec![
                BoardItem {
                    status: Some("Done".to_string()),
                    repo: "policy".to_string(),
                    number: 130,
                    title: "Epic: AuthPolicy compatibility".to_string(),
                    has_content: true,
                },
                BoardItem {
                    status: Some("Review".to_string()),
                    repo: "policy".to_string(),
                    number: 10,
                    title: "Epic: PPE ODIS / AIMS Compliance".to_string(),
                    has_content: true,
                },
                BoardItem {
                    status: None,
                    repo: "".to_string(),
                    number: 0,
                    title: "".to_string(),
                    has_content: false,
                },
            ],
        }
    }

    #[test]
    fn board_id_metadata_and_tags_match_the_preflight_record() {
        let plan = plan_board(&board(), None, Some("2026-11-26T12:00:00".to_string())).unwrap();
        let call = &plan.calls[0];
        assert_eq!(call.document_id, "board-9-praxis-policy-engine-(ppe)");
        assert_eq!(call.metadata["kind"], "board");
        assert_eq!(call.metadata["board_number"], "9");
        assert_eq!(call.metadata["board_title"], "Praxis Policy Engine (PPE)");
        assert_eq!(
            call.tags,
            vec!["board", "roadmap", "praxis-policy-engine-(ppe)"]
        );
        assert_eq!(call.timestamp.as_deref(), Some("2026-11-26T12:00:00"));
        assert_eq!(plan.calls.len(), 1);
    }

    #[test]
    fn board_content_matches_python_order_and_total_count() {
        let content = board_content(&board());
        assert!(content.starts_with("# Project Board: Praxis Policy Engine (PPE) (#9)"));
        assert!(content.contains("Policy engine roadmap"));
        assert!(content.contains("Total items: 3"));
        let review = content.find("## Status: Review (1)").unwrap();
        let done = content.find("## Status: Done (1)").unwrap();
        assert!(review < done);
        assert!(!content.contains("(no status)"));
        assert!(content.contains("- #10 (policy): Epic: PPE ODIS / AIMS Compliance"));
        assert!(content.contains("- #130 (policy): Epic: AuthPolicy compatibility"));
    }

    #[test]
    fn board_prefix_is_prepended() {
        let plan = plan_board(&board(), Some("roadmap"), None).unwrap();
        assert_eq!(
            plan.calls[0].document_id,
            "roadmap-9-praxis-policy-engine-(ppe)"
        );
    }

    #[test]
    fn items_without_a_status_render_the_no_status_group() {
        let mut board = board();
        board.items.push(BoardItem {
            status: None,
            repo: "policy".to_string(),
            number: 7,
            title: "Unset".to_string(),
            has_content: true,
        });
        let content = board_content(&board);
        assert!(content.contains("## Status: (no status) (1)"));
        assert!(content.contains("- #7 (policy): Unset"));
    }

    #[test]
    fn custom_statuses_follow_first_seen_order() {
        let mut board = board();
        board.items.push(BoardItem {
            status: Some("Blocked".to_string()),
            repo: "policy".to_string(),
            number: 1,
            title: "Blocked one".to_string(),
            has_content: true,
        });
        board.items.push(BoardItem {
            status: Some("Triage".to_string()),
            repo: "policy".to_string(),
            number: 2,
            title: "Triage one".to_string(),
            has_content: true,
        });
        let content = board_content(&board);
        let blocked = content.find("## Status: Blocked").unwrap();
        let triage = content.find("## Status: Triage").unwrap();
        assert!(blocked < triage);
    }

    #[test]
    fn board_snapshot_hash_ignores_the_timestamp() {
        let first = plan_board(&board(), None, Some("a".to_string())).unwrap();
        let second = plan_board(&board(), None, Some("b".to_string())).unwrap();
        assert_eq!(first.snapshot_sha256, second.snapshot_sha256);
    }
}
