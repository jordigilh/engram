//! GitHub discussion serialization parity (issue #244).
//!
//! Mirrors `process_discussion` and `_discussion_comments` in
//! `src/engram/flows/configured.py`. Discussions differ from issues in three
//! ways: the id is `[{prefix}-]{short_repo}-discussion-{number}`, comments are
//! selected from the first ten raw GraphQL nodes before filtering, and there is
//! no `state` metadata field.

use std::collections::BTreeMap;

use serde_json::{json, Value};

use crate::document::RetainCall;
use crate::hashing::ContentHash;
use crate::issues::{
    issue_document_id, short_repo, split_issue_sections, truncate_comment_body, IssueError,
    IssuePlan, MAX_ISSUE_COMMENTS, MIN_COMMENT_CHARS, MIN_HEADER_CHARS, SOURCE_METADATA_VALUE,
};
use crate::python_text;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiscussionComment {
    pub author: String,
    pub body: String,
    pub author_association: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Discussion {
    pub number: u64,
    pub title: String,
    pub body: String,
    pub category: String,
    pub author: String,
    pub created_at: String,
    pub updated_at: String,
    pub comments: Vec<DiscussionComment>,
}

impl Discussion {
    pub fn from_graphql(value: &Value) -> Result<Self, IssueError> {
        let number = value
            .get("number")
            .and_then(Value::as_u64)
            .ok_or(IssueError::Missing("number"))?;
        let comments = value
            .pointer("/comments/nodes")
            .and_then(Value::as_array)
            .map(|comments| {
                comments
                    .iter()
                    .map(|comment| DiscussionComment {
                        author: comment
                            .pointer("/author/login")
                            .and_then(Value::as_str)
                            .unwrap_or("?")
                            .to_string(),
                        body: comment
                            .get("body")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                        author_association: comment
                            .get("authorAssociation")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                    })
                    .collect()
            })
            .unwrap_or_default();
        Ok(Self {
            number,
            title: string(value, "title"),
            body: string(value, "body"),
            category: value
                .pointer("/category/name")
                .and_then(Value::as_str)
                .unwrap_or("general")
                .to_string(),
            author: value
                .pointer("/author/login")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            created_at: string(value, "createdAt"),
            updated_at: string(value, "updatedAt"),
            comments,
        })
    }
}

pub fn plan_discussion(
    item: &Discussion,
    repo: &str,
    document_prefix: Option<&str>,
    chunk_size: usize,
    chunk_overlap: usize,
) -> Result<IssuePlan, IssueError> {
    let repo_name = short_repo(repo);
    let prefix = document_prefix.filter(|prefix| !prefix.is_empty());
    let base_id = match prefix {
        Some(prefix) => format!("{prefix}-{repo_name}-discussion-{}", item.number),
        None => format!("{repo_name}-discussion-{}", item.number),
    };
    let header = discussion_header(item, repo, &repo_name);
    let snapshot = discussion_snapshot_value(item, repo, prefix);
    let snapshot_sha256 = ContentHash::of(
        &serde_json::to_vec(&snapshot).map_err(|error| IssueError::Serialize(error.to_string()))?,
    );
    let size_bytes = serde_json::to_vec(&snapshot)
        .map_err(|error| IssueError::Serialize(error.to_string()))?
        .len() as i64;
    let mut plan = IssuePlan {
        relative_path: format!("discussion/{}", item.number),
        snapshot_sha256,
        size_bytes,
        calls: Vec::new(),
    };
    if python_text::char_len(python_text::strip(&header)) < MIN_HEADER_CHARS {
        return Ok(plan);
    }

    // Python takes the first ten raw comments, then filters. Do not reuse the
    // issue filter: discussions ignore authorAssociation and only test the
    // literal bot suffix and body length.
    let comments: Vec<String> = item
        .comments
        .iter()
        .take(MAX_ISSUE_COMMENTS)
        .filter(|comment| {
            let body = python_text::strip(&comment.body);
            python_text::char_len(body) > MIN_COMMENT_CHARS && !comment.author.ends_with("[bot]")
        })
        .map(|comment| {
            format!(
                "**@{}:**\n{}",
                comment.author,
                truncate_comment_body(&comment.body)
            )
        })
        .collect();
    let mut metadata = BTreeMap::new();
    metadata.insert("source".to_string(), SOURCE_METADATA_VALUE.to_string());
    metadata.insert("repo".to_string(), repo.to_string());
    metadata.insert("kind".to_string(), "discussion".to_string());
    metadata.insert("number".to_string(), item.number.to_string());
    metadata.insert("category".to_string(), item.category.clone());
    let tags = vec![
        "discussion".to_string(),
        repo_name,
        item.category.to_lowercase().replace(' ', "-"),
    ];
    let timestamp = (!item.updated_at.is_empty()).then(|| item.updated_at.clone());
    plan.calls = split_issue_sections(&header, &comments, chunk_size, chunk_overlap)
        .into_iter()
        .map(|(key, content)| RetainCall {
            document_id: issue_document_id(&base_id, &key),
            chunk_key: key,
            content_sha256: ContentHash::of(content.as_bytes()),
            content,
            metadata: metadata.clone(),
            tags: tags.clone(),
            timestamp: timestamp.clone(),
        })
        .collect();
    Ok(plan)
}

pub fn discussion_header(item: &Discussion, repo: &str, repo_name: &str) -> String {
    [
        format!(
            "# Discussion #{} ({}): {}",
            item.number, repo_name, item.title
        ),
        format!(
            "Repo: {} | Category: {} | Author: {} | Created: {}",
            repo,
            item.category,
            item.author,
            take_chars(&item.created_at, 10)
        ),
        String::new(),
        python_text::strip(&item.body).to_string(),
    ]
    .join("\n")
    .trim_end_matches(python_text::is_space)
    .to_string()
}

fn discussion_snapshot_value(item: &Discussion, repo: &str, prefix: Option<&str>) -> Value {
    json!({
        "provider": "github_discussion",
        "repo": repo,
        "prefix": prefix,
        "number": item.number,
        "title": item.title,
        "body": item.body,
        "category": item.category,
        "author": item.author,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "comments": item.comments.iter().map(|comment| json!({
            "author": comment.author,
            "body": comment.body,
            "author_association": comment.author_association,
        })).collect::<Vec<_>>(),
    })
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

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Discussion {
        Discussion {
            number: 1562,
            title: "Hello from Hive Commons (fellow CNCF Sandbox project)".to_string(),
            body: "We have read the Praxis AI and policy docs with interest and have a few questions, if you have time.".to_string(),
            category: "General".to_string(),
            author: "clubanderson".to_string(),
            created_at: "2026-11-18T14:02:50Z".to_string(),
            updated_at: "2026-11-18T14:02:50Z".to_string(),
            comments: Vec::new(),
        }
    }

    #[test]
    fn discussion_ids_and_metadata_match_the_python_contract() {
        let plan = plan_discussion(&fixture(), "praxis-proxy/praxis", None, 1200, 300).unwrap();
        assert_eq!(plan.calls[0].document_id, "praxis-discussion-1562");
        assert_eq!(plan.calls[0].metadata["kind"], "discussion");
        assert_eq!(plan.calls[0].metadata["number"], "1562");
        assert_eq!(plan.calls[0].metadata["category"], "General");
        assert!(!plan.calls[0].metadata.contains_key("state"));
        assert_eq!(plan.calls[0].tags, vec!["discussion", "praxis", "general"]);
        assert_eq!(
            plan.calls[0].timestamp.as_deref(),
            Some("2026-11-18T14:02:50Z")
        );
        assert!(plan.calls[0]
            .content
            .starts_with("# Discussion #1562 (praxis): Hello from Hive Commons"));
    }

    #[test]
    fn discussion_prefix_is_prepended() {
        let plan =
            plan_discussion(&fixture(), "praxis-proxy/praxis", Some("gh"), 1200, 300).unwrap();
        assert_eq!(plan.calls[0].document_id, "gh-praxis-discussion-1562");
    }

    #[test]
    fn discussion_comments_take_ten_raw_then_filter() {
        let mut item = fixture();
        item.comments = (0..12)
            .map(|index| DiscussionComment {
                author: if index == 2 {
                    "praxis-bot".to_string()
                } else if index == 3 {
                    "dependabot[bot]".to_string()
                } else {
                    format!("user{index}")
                },
                body: if index == 5 {
                    "short".to_string()
                } else {
                    "a long enough discussion comment".to_string()
                },
                author_association: "MEMBER".to_string(),
            })
            .collect();
        let plan = plan_discussion(&item, "praxis-proxy/praxis", None, 1200, 300).unwrap();
        // Header plus raw comments 0,1,2,4,6,7,8,9. Raw 3 is a literal [bot]
        // login, raw 5 is short, and raw 10/11 are never inspected. Surviving
        // comments are renumbered 0..7, matching Python.
        assert_eq!(plan.calls.len(), 9);
        assert_eq!(plan.calls[8].document_id, "praxis-discussion-1562-comment7");
        assert!(plan.calls[3].content.starts_with("**@praxis-bot:**"));
        assert!(plan.calls[4].content.starts_with("**@user4:**"));
        assert!(!plan
            .calls
            .iter()
            .any(|call| call.content.contains("dependabot[bot]")));
        assert!(!plan
            .calls
            .iter()
            .any(|call| call.content == "**@user5:**\nshort"));
    }

    #[test]
    fn discussion_comment_length_uses_the_stripped_body() {
        let mut item = fixture();
        item.comments = vec![DiscussionComment {
            author: "alice".to_string(),
            body: "                    hello".to_string(),
            author_association: "MEMBER".to_string(),
        }];
        let plan = plan_discussion(&item, "org/repo", None, 1200, 300).unwrap();
        assert_eq!(plan.calls.len(), 1);
        assert_eq!(plan.calls[0].document_id, "repo-discussion-1562");
    }

    #[test]
    fn discussion_long_comment_is_truncated_before_splitting() {
        let mut item = fixture();
        item.comments = vec![DiscussionComment {
            author: "alice".to_string(),
            body: "x".repeat(2_500),
            author_association: "MEMBER".to_string(),
        }];
        let plan = plan_discussion(&item, "org/repo", None, 1200, 300).unwrap();
        assert!(plan.calls.last().unwrap().content.ends_with("\n[...]"));
    }
}
