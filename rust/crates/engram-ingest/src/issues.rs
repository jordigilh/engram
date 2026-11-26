//! GitHub/Jira issue serialization parity (issue #243).
//!
//! This module is the issue half of the CocoIndex replacement. It mirrors the
//! Python serializers in `src/engram/flows/configured.py` and
//! `src/engram/chunking.py`:
//!
//! - GitHub ids are `[{prefix}-]{short_repo}-{kind}-{number}` and Jira ids are
//!   `{prefix}-{key}` (default prefix `jira`);
//! - the header key is `""`, comments are `comment{i}`, oversized sections use
//!   `part{n}` / `comment{i}-part{n}` with 1200/300 windows;
//! - headers exclude volatile fields (state, labels, milestone) so a state or
//!   label edit re-retains tags/metadata without changing the header text.
//!
//! Comment keys stay ordinal to preserve existing Hindsight document ids. The
//! deprecation contract notes that a stable comment id would be better, but
//! changing it now would orphan every existing comment document; that rekey is
//! a separate migration.

use std::collections::BTreeMap;

use serde_json::{json, Value};

use crate::chunking::split_fixed_window;
use crate::document::RetainCall;
use crate::hashing::ContentHash;
use crate::python_text;

pub const ISSUE_CHUNK_SIZE: usize = 1200;
pub const ISSUE_CHUNK_OVERLAP: usize = 300;
pub const MAX_ISSUE_COMMENTS: usize = 10;
pub const MAX_COMMENT_CHARS: usize = 2_000;
pub const MIN_COMMENT_CHARS: usize = 20;
pub const MIN_HEADER_CHARS: usize = 50;
pub const SOURCE_METADATA_VALUE: &str = "cocoindex";

pub const TRUSTED_ASSOCIATIONS: [&str; 4] = ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"];

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum IssueError {
    #[error("issue payload is missing {0}")]
    Missing(&'static str),
    #[error("issue payload has an invalid {0}")]
    Invalid(String),
    #[error("issue snapshot could not be serialized: {0}")]
    Serialize(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GitHubKind {
    Issue,
    Pr,
}

impl GitHubKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Issue => "issue",
            Self::Pr => "pr",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Issue => "Issue",
            Self::Pr => "PR",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "issue" => Some(Self::Issue),
            "pr" | "pull_request" | "pull-request" => Some(Self::Pr),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitHubComment {
    pub id: Option<String>,
    pub author: String,
    pub author_association: String,
    pub body: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitHubIssue {
    pub number: u64,
    pub kind: GitHubKind,
    pub title: String,
    pub body: String,
    /// `open`, `closed`, or `merged` (a merged PR, matching `gh pr list`).
    pub state: String,
    pub labels: Vec<String>,
    pub milestone_title: Option<String>,
    pub milestone_due: Option<String>,
    pub author: String,
    pub created_at: String,
    pub updated_at: String,
    pub comments: Vec<GitHubComment>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JiraComment {
    pub id: Option<String>,
    pub author: String,
    pub body: Value,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JiraIssue {
    pub key: String,
    pub summary: String,
    pub description: Value,
    pub status: String,
    pub issue_type: String,
    pub labels: Vec<String>,
    pub reporter: String,
    pub created: String,
    pub updated: String,
    pub parent: Option<String>,
    /// Embedded comments in Jira search order. Python takes the first ten before
    /// applying the length filter, so the raw list is kept here.
    pub comments: Vec<JiraComment>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssuePlan {
    /// Synthetic relative path used by the shared ledger/reconcile layer.
    pub relative_path: String,
    /// Hash of the provider snapshot, including metadata that never appears in
    /// the header (state, labels, milestone, updated timestamp, comments).
    pub snapshot_sha256: ContentHash,
    pub size_bytes: i64,
    pub calls: Vec<RetainCall>,
}

impl GitHubComment {
    pub fn from_rest(value: &Value) -> Self {
        let author = normalize_github_login(&login(value, "user"));
        Self {
            id: value
                .get("id")
                .and_then(Value::as_u64)
                .map(|id| id.to_string()),
            author: if author.is_empty() {
                "?".to_string()
            } else {
                author
            },
            author_association: string(value, "author_association"),
            body: string(value, "body"),
        }
    }
}

impl GitHubIssue {
    /// Build a normalized issue from GitHub REST issue/PR payload plus its
    /// comments. `gh` exposes bot logins without the `[bot]` suffix; REST adds
    /// it, so the login is normalized here to preserve the Python filter and the
    /// existing Hindsight comment documents.
    pub fn from_rest(
        value: &Value,
        kind: GitHubKind,
        comments: &[Value],
    ) -> Result<Self, IssueError> {
        let number = value
            .get("number")
            .and_then(Value::as_u64)
            .ok_or(IssueError::Missing("number"))?;
        let state = if kind == GitHubKind::Pr
            && (value
                .pointer("/pull_request/merged_at")
                .and_then(Value::as_str)
                .is_some()
                || value.get("merged_at").and_then(Value::as_str).is_some())
        {
            "merged".to_string()
        } else {
            string(value, "state").to_lowercase()
        };
        let labels = value
            .get("labels")
            .and_then(Value::as_array)
            .map(|labels| {
                labels
                    .iter()
                    .filter(|label| label.is_object())
                    .map(|label| {
                        label
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string()
                    })
                    .collect()
            })
            .unwrap_or_default();
        let milestone = value.get("milestone").filter(|value| value.is_object());
        let author = normalize_github_login(&login(value, "user"));
        Ok(Self {
            number,
            kind,
            title: string(value, "title"),
            body: string(value, "body"),
            state,
            labels,
            milestone_title: milestone.and_then(|value| {
                value
                    .get("title")
                    .and_then(Value::as_str)
                    .filter(|title| !title.is_empty())
                    .map(str::to_string)
            }),
            milestone_due: milestone.and_then(|value| {
                value
                    .get("due_on")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            }),
            author: if author.is_empty() {
                "unknown".to_string()
            } else {
                author
            },
            created_at: string(value, "created_at"),
            updated_at: string(value, "updated_at"),
            comments: comments.iter().map(GitHubComment::from_rest).collect(),
        })
    }
}

impl JiraIssue {
    /// Build a normalized issue from a Jira `/rest/api/3/search/jql` issue.
    pub fn from_search(value: &Value) -> Result<Self, IssueError> {
        let key = value
            .get("key")
            .and_then(Value::as_str)
            .ok_or(IssueError::Missing("key"))?;
        let fields = value.get("fields").unwrap_or(&Value::Null);
        let labels = fields
            .get("labels")
            .and_then(Value::as_array)
            .map(|labels| {
                labels
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        let comments = fields
            .pointer("/comment/comments")
            .and_then(Value::as_array)
            .map(|comments| {
                comments
                    .iter()
                    .map(|comment| JiraComment {
                        id: comment
                            .get("id")
                            .and_then(Value::as_str)
                            .map(str::to_string),
                        author: comment
                            .pointer("/author/displayName")
                            .and_then(Value::as_str)
                            .unwrap_or("?")
                            .to_string(),
                        body: comment.get("body").cloned().unwrap_or(Value::Null),
                    })
                    .collect()
            })
            .unwrap_or_default();
        Ok(Self {
            key: key.to_string(),
            summary: string(fields, "summary"),
            description: fields.get("description").cloned().unwrap_or(Value::Null),
            status: {
                let status = string(fields.pointer("/status").unwrap_or(&Value::Null), "name");
                if status.is_empty() {
                    "unknown".to_string()
                } else {
                    status
                }
            },
            issue_type: {
                let issue_type =
                    string(fields.pointer("/issuetype").unwrap_or(&Value::Null), "name");
                if issue_type.is_empty() {
                    "Issue".to_string()
                } else {
                    issue_type
                }
            },
            labels,
            reporter: fields
                .pointer("/reporter/displayName")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            created: string(fields, "created"),
            updated: string(fields, "updated"),
            parent: fields
                .pointer("/parent/key")
                .and_then(Value::as_str)
                .filter(|key| !key.is_empty())
                .map(str::to_string),
            comments,
        })
    }
}

pub fn plan_github_issue(
    issue: &GitHubIssue,
    repo: &str,
    document_prefix: Option<&str>,
    chunk_size: usize,
    chunk_overlap: usize,
) -> Result<IssuePlan, IssueError> {
    let short_repo = short_repo(repo);
    let prefix = document_prefix.filter(|prefix| !prefix.is_empty());
    let base_id = match prefix {
        Some(prefix) => format!(
            "{prefix}-{short_repo}-{}-{}",
            issue.kind.as_str(),
            issue.number
        ),
        None => format!("{short_repo}-{}-{}", issue.kind.as_str(), issue.number),
    };
    let header = github_header(issue, repo, &short_repo);
    let snapshot_sha256 = github_snapshot_hash(issue, repo, prefix)?;
    let size_bytes = canonical_size(&github_snapshot_value(issue, repo, prefix))?;
    let relative_path = format!("{}/{}", issue.kind.as_str(), issue.number);
    let mut plan = IssuePlan {
        relative_path,
        snapshot_sha256,
        size_bytes,
        calls: Vec::new(),
    };
    if python_text::char_len(python_text::strip(&header)) < MIN_HEADER_CHARS {
        return Ok(plan);
    }

    let comments: Vec<String> = issue
        .comments
        .iter()
        .filter(|comment| is_human_comment(comment))
        .take(MAX_ISSUE_COMMENTS)
        .map(format_github_comment)
        .collect();
    let mut metadata = github_metadata(issue, repo);
    if let Some(title) = &issue.milestone_title {
        metadata.insert("milestone".to_string(), title.clone());
        metadata.insert(
            "milestone_due".to_string(),
            issue.milestone_due.clone().unwrap_or_default(),
        );
    }
    let mut tags = vec![
        issue.state.clone(),
        issue.kind.as_str().to_string(),
        short_repo,
    ];
    tags.extend(issue.labels.iter().take(5).cloned());
    if let Some(title) = &issue.milestone_title {
        tags.push(format!("milestone:{title}"));
    }

    plan.calls = section_calls(
        &base_id,
        &header,
        &comments,
        chunk_size,
        chunk_overlap,
        CallContext {
            metadata,
            tags,
            timestamp: Some(issue.updated_at.clone()),
        },
    );
    Ok(plan)
}

pub fn plan_jira_issue(
    issue: &JiraIssue,
    project_name: &str,
    document_prefix: Option<&str>,
    chunk_size: usize,
    chunk_overlap: usize,
) -> Result<IssuePlan, IssueError> {
    let prefix = document_prefix
        .filter(|prefix| !prefix.is_empty())
        .unwrap_or("jira");
    let base_id = format!("{prefix}-{}", issue.key);
    let header = jira_header(issue, project_name);
    let snapshot_sha256 = jira_snapshot_hash(issue, project_name, prefix)?;
    let size_bytes = canonical_size(&jira_snapshot_value(issue, project_name, prefix))?;
    let mut plan = IssuePlan {
        relative_path: issue.key.clone(),
        snapshot_sha256,
        size_bytes,
        calls: Vec::new(),
    };
    if python_text::char_len(python_text::strip(&header)) < MIN_HEADER_CHARS {
        return Ok(plan);
    }

    // Python caps at the first ten embedded comments *before* filtering.
    let comments: Vec<String> = issue
        .comments
        .iter()
        .take(MAX_ISSUE_COMMENTS)
        .filter_map(format_jira_comment)
        .collect();
    let status = issue.status.to_lowercase();
    let issue_type = issue.issue_type.to_lowercase();
    let mut tags = vec![status.clone(), issue_type, "jira".to_string()];
    tags.extend(issue.labels.iter().take(5).cloned());
    if let Some(parent) = &issue.parent {
        tags.push(parent.clone());
    }
    let mut metadata = BTreeMap::new();
    metadata.insert("source".to_string(), SOURCE_METADATA_VALUE.to_string());
    metadata.insert("tracker".to_string(), "jira".to_string());
    metadata.insert("key".to_string(), issue.key.clone());
    metadata.insert("status".to_string(), status);
    metadata.insert("project".to_string(), project_name.to_string());

    plan.calls = section_calls(
        &base_id,
        &header,
        &comments,
        chunk_size,
        chunk_overlap,
        CallContext {
            metadata,
            tags,
            timestamp: Some(issue.updated.clone()),
        },
    );
    Ok(plan)
}

/// Python `split_issue_sections`: header `""` then `comment{i}`, with
/// `part{n}` / `comment{i}-part{n}` for oversized sections.
pub fn split_issue_sections(
    header: &str,
    comments: &[String],
    chunk_size: usize,
    chunk_overlap: usize,
) -> Vec<(String, String)> {
    let mut result = Vec::new();
    if python_text::char_len(header) <= chunk_size {
        result.push((String::new(), header.to_string()));
    } else {
        for (index, part) in split_fixed_window(header, chunk_size, chunk_overlap)
            .into_iter()
            .enumerate()
        {
            result.push((
                if index == 0 {
                    String::new()
                } else {
                    format!("part{index}")
                },
                part,
            ));
        }
    }
    for (index, comment) in comments.iter().enumerate() {
        let key = format!("comment{index}");
        if python_text::char_len(comment) <= chunk_size {
            result.push((key, comment.clone()));
        } else {
            for (part_index, part) in split_fixed_window(comment, chunk_size, chunk_overlap)
                .into_iter()
                .enumerate()
            {
                result.push((
                    if part_index == 0 {
                        key.clone()
                    } else {
                        format!("{key}-part{part_index}")
                    },
                    part,
                ));
            }
        }
    }
    result
}

/// Issue document ids join with a single dash, unlike docs chunk ids (`--`).
pub fn issue_document_id(base_id: &str, chunk_key: &str) -> String {
    if chunk_key.is_empty() {
        base_id.to_string()
    } else {
        format!("{base_id}-{chunk_key}")
    }
}

/// Port of `_adf_to_text` from `configured.py`.
pub fn adf_to_text(node: &Value) -> String {
    match node {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        Value::Array(items) => items.iter().map(adf_to_text).collect(),
        Value::Object(map) => {
            let node_type = map.get("type").and_then(Value::as_str).unwrap_or("");
            match node_type {
                "text" => map
                    .get("text")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                "mention" => map
                    .get("attrs")
                    .and_then(|attrs| attrs.get("text"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                "hardBreak" => "\n".to_string(),
                "rule" => "\n---\n".to_string(),
                _ => {
                    let mut inner = map.get("content").map(adf_to_text).unwrap_or_default();
                    if matches!(
                        node_type,
                        "paragraph" | "heading" | "listItem" | "codeBlock"
                    ) {
                        inner.push('\n');
                    }
                    inner
                }
            }
        }
        _ => String::new(),
    }
}

pub fn short_repo(repo: &str) -> String {
    repo.rsplit('/').next().unwrap_or(repo).to_string()
}

/// `gh` strips the `[bot]` suffix from bot logins; normalize REST to match.
pub fn normalize_github_login(login: &str) -> String {
    login.strip_suffix("[bot]").unwrap_or(login).to_string()
}

struct CallContext {
    metadata: BTreeMap<String, String>,
    tags: Vec<String>,
    timestamp: Option<String>,
}

fn section_calls(
    base_id: &str,
    header: &str,
    comments: &[String],
    chunk_size: usize,
    chunk_overlap: usize,
    context: CallContext,
) -> Vec<RetainCall> {
    split_issue_sections(header, comments, chunk_size, chunk_overlap)
        .into_iter()
        .map(|(key, content)| RetainCall {
            document_id: issue_document_id(base_id, &key),
            chunk_key: key,
            content_sha256: ContentHash::of(content.as_bytes()),
            content,
            metadata: context.metadata.clone(),
            tags: context.tags.clone(),
            timestamp: context.timestamp.clone(),
        })
        .collect()
}

fn github_header(issue: &GitHubIssue, repo: &str, short_repo: &str) -> String {
    let mut parts = vec![
        format!(
            "# {} #{} ({}): {}",
            issue.kind.label(),
            issue.number,
            short_repo,
            issue.title
        ),
        format!(
            "Repo: {} | Author: {} | Created: {}",
            repo,
            issue.author,
            take_chars(&issue.created_at, 10)
        ),
        String::new(),
    ];
    let body = python_text::strip(&issue.body);
    if !body.is_empty() {
        parts.push(body.to_string());
    }
    parts.join("\n")
}

fn jira_header(issue: &JiraIssue, project_name: &str) -> String {
    let mut line = format!(
        "Project: {} | Reporter: {} | Created: {}",
        project_name,
        issue.reporter,
        take_chars(&issue.created, 10)
    );
    if let Some(parent) = &issue.parent {
        line.push_str(&format!(" | Parent: {parent}"));
    }
    let description_text = adf_to_text(&issue.description);
    let description = python_text::strip(&description_text);
    [
        format!("# {} {}: {}", issue.issue_type, issue.key, issue.summary),
        line,
        String::new(),
        description.to_string(),
    ]
    .join("\n")
    .trim_end_matches(python_text::is_space)
    .to_string()
}

fn github_metadata(issue: &GitHubIssue, repo: &str) -> BTreeMap<String, String> {
    let mut metadata = BTreeMap::new();
    metadata.insert("source".to_string(), SOURCE_METADATA_VALUE.to_string());
    metadata.insert("repo".to_string(), repo.to_string());
    metadata.insert("kind".to_string(), issue.kind.as_str().to_string());
    metadata.insert("number".to_string(), issue.number.to_string());
    metadata.insert("state".to_string(), issue.state.clone());
    metadata
}

fn is_human_comment(comment: &GitHubComment) -> bool {
    TRUSTED_ASSOCIATIONS.contains(&comment.author_association.as_str())
        && !comment.author.ends_with("[bot]")
        && python_text::char_len(&comment.body) > MIN_COMMENT_CHARS
}

fn format_github_comment(comment: &GitHubComment) -> String {
    format!(
        "**@{}:**\n{}",
        comment.author,
        truncate_comment_body(&comment.body)
    )
}

fn format_jira_comment(comment: &JiraComment) -> Option<String> {
    let body_text = adf_to_text(&comment.body);
    let body = python_text::strip(&body_text);
    if python_text::char_len(body) <= MIN_COMMENT_CHARS {
        return None;
    }
    Some(format!(
        "**{}:**\n{}",
        comment.author,
        truncate_comment_body(body)
    ))
}

fn truncate_comment_body(body: &str) -> String {
    let body = python_text::strip(body);
    if python_text::char_len(body) > MAX_COMMENT_CHARS {
        format!(
            "{}\n[...]",
            body.chars().take(MAX_COMMENT_CHARS).collect::<String>()
        )
    } else {
        body.to_string()
    }
}

fn github_snapshot_value(issue: &GitHubIssue, repo: &str, prefix: Option<&str>) -> Value {
    json!({
        "provider": "github",
        "repo": repo,
        "prefix": prefix,
        "kind": issue.kind.as_str(),
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": issue.labels,
        "milestone_title": issue.milestone_title,
        "milestone_due": issue.milestone_due,
        "author": issue.author,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "comments": issue.comments.iter().map(|comment| json!({
            "id": comment.id,
            "author": comment.author,
            "author_association": comment.author_association,
            "body": comment.body,
        })).collect::<Vec<_>>(),
    })
}

fn jira_snapshot_value(issue: &JiraIssue, project_name: &str, prefix: &str) -> Value {
    json!({
        "provider": "jira",
        "prefix": prefix,
        "project": project_name,
        "key": issue.key,
        "summary": issue.summary,
        "description": issue.description,
        "status": issue.status,
        "issue_type": issue.issue_type,
        "labels": issue.labels,
        "reporter": issue.reporter,
        "created": issue.created,
        "updated": issue.updated,
        "parent": issue.parent,
        "comments": issue.comments.iter().map(|comment| json!({
            "id": comment.id,
            "author": comment.author,
            "body": comment.body,
        })).collect::<Vec<_>>(),
    })
}

fn github_snapshot_hash(
    issue: &GitHubIssue,
    repo: &str,
    prefix: Option<&str>,
) -> Result<ContentHash, IssueError> {
    canonical_hash(&github_snapshot_value(issue, repo, prefix))
}

fn jira_snapshot_hash(
    issue: &JiraIssue,
    project_name: &str,
    prefix: &str,
) -> Result<ContentHash, IssueError> {
    canonical_hash(&jira_snapshot_value(issue, project_name, prefix))
}

fn canonical_hash(value: &Value) -> Result<ContentHash, IssueError> {
    let bytes =
        serde_json::to_vec(value).map_err(|error| IssueError::Serialize(error.to_string()))?;
    Ok(ContentHash::of(&bytes))
}

fn canonical_size(value: &Value) -> Result<i64, IssueError> {
    let bytes =
        serde_json::to_vec(value).map_err(|error| IssueError::Serialize(error.to_string()))?;
    Ok(bytes.len() as i64)
}

fn take_chars(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}

fn string(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn login(value: &Value, key: &str) -> String {
    value
        .pointer(&format!("/{key}/login"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn github_issue(kind: GitHubKind) -> GitHubIssue {
        GitHubIssue {
            number: 4,
            kind,
            title: "A useful issue".to_string(),
            body: "A sufficiently long issue body for the generic connector.".to_string(),
            state: "open".to_string(),
            labels: vec!["bug".to_string(), "p1".to_string()],
            milestone_title: None,
            milestone_due: None,
            author: "alice".to_string(),
            created_at: "2026-01-01T00:00:00Z".to_string(),
            updated_at: "2026-01-02T00:00:00Z".to_string(),
            comments: Vec::new(),
        }
    }

    fn jira_issue() -> JiraIssue {
        JiraIssue {
            key: "EX-4".to_string(),
            summary: "Jira item".to_string(),
            description: json!({"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Plain text description"}]}]}),
            status: "Open".to_string(),
            issue_type: "Story".to_string(),
            labels: vec!["backend".to_string()],
            reporter: "bob".to_string(),
            created: "2026-01-01T00:00:00.000+0000".to_string(),
            updated: "2026-01-02T00:00:00.000+0000".to_string(),
            parent: Some("EX-1".to_string()),
            comments: Vec::new(),
        }
    }

    #[test]
    fn issue_ids_use_one_dash() {
        assert_eq!(
            issue_document_id("github-repo-pr-4", ""),
            "github-repo-pr-4"
        );
        assert_eq!(
            issue_document_id("github-repo-pr-4", "comment0"),
            "github-repo-pr-4-comment0"
        );
        assert_eq!(issue_document_id("jira-EX-4", "part1"), "jira-EX-4-part1");
    }

    #[test]
    fn github_serializer_matches_python_contract() {
        let plan = plan_github_issue(
            &github_issue(GitHubKind::Pr),
            "org/repo",
            Some("github"),
            ISSUE_CHUNK_SIZE,
            ISSUE_CHUNK_OVERLAP,
        )
        .unwrap();
        assert_eq!(plan.relative_path, "pr/4");
        let call = &plan.calls[0];
        assert_eq!(call.document_id, "github-repo-pr-4");
        assert_eq!(
            call.metadata.get("repo").map(String::as_str),
            Some("org/repo")
        );
        assert_eq!(call.metadata.get("kind").map(String::as_str), Some("pr"));
        assert_eq!(call.tags, vec!["open", "pr", "repo", "bug", "p1"]);
        assert_eq!(call.timestamp.as_deref(), Some("2026-01-02T00:00:00Z"));
        assert!(call.content.starts_with("# PR #4 (repo): A useful issue"));
        assert!(!call.content.contains("State:"));
        assert!(!call.content.contains("Labels:"));
    }

    #[test]
    fn github_comments_filter_and_truncate() {
        let mut issue = github_issue(GitHubKind::Issue);
        issue.comments = vec![
            GitHubComment {
                id: Some("1".to_string()),
                author: "alice".to_string(),
                author_association: "MEMBER".to_string(),
                body: "x".repeat(25),
            },
            GitHubComment {
                id: Some("3".to_string()),
                author: "mallory".to_string(),
                author_association: "NONE".to_string(),
                body: "this is a long enough outsider comment".to_string(),
            },
            GitHubComment {
                id: Some("4".to_string()),
                author: "bob".to_string(),
                author_association: "OWNER".to_string(),
                body: "short".to_string(),
            },
            GitHubComment {
                id: Some("5".to_string()),
                author: "carol".to_string(),
                author_association: "COLLABORATOR".to_string(),
                body: "y".repeat(2_050),
            },
        ];
        let plan = plan_github_issue(
            &issue,
            "org/repo",
            None,
            ISSUE_CHUNK_SIZE,
            ISSUE_CHUNK_OVERLAP,
        )
        .unwrap();
        assert_eq!(plan.calls.len(), 4);
        assert_eq!(plan.calls[1].document_id, "repo-issue-4-comment0");
        assert!(plan.calls[1].content.starts_with("**@alice:**"));
        assert!(plan.calls[2].content.starts_with("**@carol:**"));
        assert_eq!(plan.calls[2].document_id, "repo-issue-4-comment1");
        assert_eq!(plan.calls[3].document_id, "repo-issue-4-comment1-part1");
        assert!(plan.calls[3].content.ends_with("\n[...]"));
    }

    #[test]
    fn rest_bot_login_is_normalized_and_included_like_gh() {
        let payload = json!({
            "number": 1,
            "title": "Bot parity",
            "body": "A sufficiently long body for the bot parity fixture.",
            "state": "open",
            "labels": [],
            "user": {"login": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z"
        });
        let comments = vec![json!({
            "id": 1,
            "body": "A long enough trusted bot comment.",
            "author_association": "CONTRIBUTOR",
            "user": {"login": "praxis-bot-app[bot]"}
        })];
        let issue = GitHubIssue::from_rest(&payload, GitHubKind::Issue, &comments).unwrap();
        assert_eq!(issue.comments[0].author, "praxis-bot-app");
        let plan = plan_github_issue(
            &issue,
            "org/repo",
            None,
            ISSUE_CHUNK_SIZE,
            ISSUE_CHUNK_OVERLAP,
        )
        .unwrap();
        assert_eq!(plan.calls.len(), 2);
        assert!(plan.calls[1].content.starts_with("**@praxis-bot-app:**"));
    }

    #[test]
    fn github_comment_cap_is_ten() {
        let mut issue = github_issue(GitHubKind::Issue);
        issue.comments = (0..12)
            .map(|index| GitHubComment {
                id: Some(index.to_string()),
                author: format!("user{index}"),
                author_association: "MEMBER".to_string(),
                body: "a long enough comment body".to_string(),
            })
            .collect();
        let plan = plan_github_issue(
            &issue,
            "org/repo",
            None,
            ISSUE_CHUNK_SIZE,
            ISSUE_CHUNK_OVERLAP,
        )
        .unwrap();
        assert_eq!(plan.calls.len(), 11);
        assert_eq!(plan.calls[10].document_id, "repo-issue-4-comment9");
    }

    #[test]
    fn github_milestone_and_merged_state_are_in_metadata() {
        let mut issue = github_issue(GitHubKind::Pr);
        issue.state = "merged".to_string();
        issue.milestone_title = Some("v1".to_string());
        issue.milestone_due = Some("2026-02-01T00:00:00Z".to_string());
        let plan = plan_github_issue(
            &issue,
            "org/repo",
            None,
            ISSUE_CHUNK_SIZE,
            ISSUE_CHUNK_OVERLAP,
        )
        .unwrap();
        let call = &plan.calls[0];
        assert_eq!(
            call.metadata.get("state").map(String::as_str),
            Some("merged")
        );
        assert_eq!(
            call.metadata.get("milestone").map(String::as_str),
            Some("v1")
        );
        assert_eq!(
            call.metadata.get("milestone_due").map(String::as_str),
            Some("2026-02-01T00:00:00Z")
        );
        assert!(call.tags.contains(&"milestone:v1".to_string()));
    }

    #[test]
    fn short_headers_emit_no_calls_but_keep_the_snapshot() {
        let mut issue = github_issue(GitHubKind::Issue);
        issue.title = String::new();
        issue.body = String::new();
        issue.author = String::new();
        issue.created_at = String::new();
        let plan =
            plan_github_issue(&issue, "r", None, ISSUE_CHUNK_SIZE, ISSUE_CHUNK_OVERLAP).unwrap();
        assert!(plan.calls.is_empty());
        assert_ne!(plan.snapshot_sha256, ContentHash::of(b""));
    }

    #[test]
    fn state_and_label_edits_change_the_snapshot_hash() {
        let base = github_issue(GitHubKind::Issue);
        let first = plan_github_issue(&base, "org/repo", None, 1200, 300).unwrap();
        let mut closed = base.clone();
        closed.state = "closed".to_string();
        let second = plan_github_issue(&closed, "org/repo", None, 1200, 300).unwrap();
        assert_ne!(first.snapshot_sha256, second.snapshot_sha256);
        let mut relabeled = base.clone();
        relabeled.labels.push("new".to_string());
        let third = plan_github_issue(&relabeled, "org/repo", None, 1200, 300).unwrap();
        assert_ne!(first.snapshot_sha256, third.snapshot_sha256);
    }

    #[test]
    fn jira_serializer_matches_python_contract() {
        let plan = plan_jira_issue(
            &jira_issue(),
            "EX",
            None,
            ISSUE_CHUNK_SIZE,
            ISSUE_CHUNK_OVERLAP,
        )
        .unwrap();
        let call = &plan.calls[0];
        assert_eq!(call.document_id, "jira-EX-4");
        assert_eq!(
            call.metadata.get("tracker").map(String::as_str),
            Some("jira")
        );
        assert_eq!(call.tags, vec!["open", "story", "jira", "backend", "EX-1"]);
        assert_eq!(
            call.timestamp.as_deref(),
            Some("2026-01-02T00:00:00.000+0000")
        );
        assert!(call.content.starts_with("# Story EX-4: Jira item"));
        assert!(call.content.contains("Parent: EX-1"));
        assert!(call.content.contains("Plain text description"));
    }

    #[test]
    fn jira_caps_before_filtering_comments() {
        let mut issue = jira_issue();
        issue.comments = (0..12)
            .map(|index| JiraComment {
                id: Some(index.to_string()),
                author: format!("user{index}"),
                body: if index < 9 {
                    json!("a long enough Jira comment body")
                } else {
                    json!("short")
                },
            })
            .collect();
        let plan =
            plan_jira_issue(&issue, "EX", None, ISSUE_CHUNK_SIZE, ISSUE_CHUNK_OVERLAP).unwrap();
        // comments 9 and 10 are dropped by the ten-item cap; 11 is never seen.
        assert_eq!(plan.calls.len(), 10);
        assert_eq!(plan.calls[9].document_id, "jira-EX-4-comment8");
    }

    #[test]
    fn adf_to_text_matches_python_rules() {
        let node = json!({
            "type": "doc",
            "content": [
                {"type": "heading", "content": [{"type": "text", "text": "Title"}]},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "mention", "attrs": {"text": "@bob"}},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "world"}
                ]},
                {"type": "rule"},
                {"type": "codeBlock", "content": [{"type": "text", "text": "code"}]}
            ]
        });
        assert_eq!(
            adf_to_text(&node),
            "Title\nHello @bob\nworld\n\n---\ncode\n"
        );
    }

    #[test]
    fn splits_use_python_character_offsets() {
        let header = "é".repeat(5);
        let sections = split_issue_sections(&header, &[], 3, 1);
        assert_eq!(sections[0].0, "");
        assert_eq!(sections[0].1.chars().count(), 3);
        assert_eq!(sections[1].0, "part1");
    }

    #[test]
    fn rest_parsing_normalizes_bot_logins_and_merged_prs() {
        let payload = json!({
            "number": 7,
            "title": "Merged",
            "body": "body",
            "state": "closed",
            "labels": [{"name": "bug"}],
            "milestone": {"title": "v2", "due_on": "2026-03-01T00:00:00Z"},
            "user": {"login": "alice[bot]"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "pull_request": {"merged_at": "2026-01-02T00:00:00Z"}
        });
        let comments = vec![json!({
            "id": 9,
            "body": "a long enough comment",
            "author_association": "MEMBER",
            "user": {"login": "reviewer[bot]"}
        })];
        let issue = GitHubIssue::from_rest(&payload, GitHubKind::Pr, &comments).unwrap();
        assert_eq!(issue.state, "merged");
        assert_eq!(issue.author, "alice");
        assert_eq!(issue.comments[0].author, "reviewer");
        assert_eq!(issue.milestone_due.as_deref(), Some("2026-03-01T00:00:00Z"));
    }

    #[test]
    fn jira_rest_parsing_reads_embedded_comments() {
        let payload = json!({
            "key": "EX-1",
            "fields": {
                "summary": "S",
                "description": {"type": "doc", "content": []},
                "status": {"name": "Open"},
                "issuetype": {"name": "Story"},
                "labels": ["a"],
                "reporter": {"displayName": "Bob"},
                "created": "2026-01-01T00:00:00.000+0000",
                "updated": "2026-01-02T00:00:00.000+0000",
                "parent": {"key": "EX-0"},
                "comment": {"total": 1, "comments": [{"id": "1", "author": {"displayName": "Alice"}, "body": {"type": "doc"}}]}
            }
        });
        let issue = JiraIssue::from_search(&payload).unwrap();
        assert_eq!(issue.key, "EX-1");
        assert_eq!(issue.parent.as_deref(), Some("EX-0"));
        assert_eq!(issue.comments.len(), 1);
        assert_eq!(issue.comments[0].author, "Alice");
    }

    #[test]
    fn jira_missing_fields_use_python_defaults() {
        let payload = json!({
            "key": "EX-2",
            "fields": {
                "summary": "S",
                "parent": {"key": ""}
            }
        });
        let issue = JiraIssue::from_search(&payload).unwrap();
        assert_eq!(issue.status, "unknown");
        assert_eq!(issue.issue_type, "Issue");
        assert_eq!(issue.reporter, "unknown");
        assert_eq!(issue.parent, None);
        let plan =
            plan_jira_issue(&issue, "EX", None, ISSUE_CHUNK_SIZE, ISSUE_CHUNK_OVERLAP).unwrap();
        assert!(plan.calls[0].content.contains("# Issue EX-2: S"));
        assert!(plan.calls[0].tags.contains(&"unknown".to_string()));
        assert!(plan.calls[0].tags.contains(&"issue".to_string()));
    }
}
