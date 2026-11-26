//! Frozen API fixtures for the #243 parity contract.

use engram_ingest::{plan_github_issue, plan_jira_issue, GitHubIssue, GitHubKind, JiraIssue};
use serde_json::Value;

#[test]
fn github_pr_fixture_matches_the_documented_contract() {
    let fixture: Value =
        serde_json::from_str(include_str!("../fixtures/issues/github_pr.json")).unwrap();
    let kind = GitHubKind::parse(fixture["kind"].as_str().unwrap()).unwrap();
    let comments = fixture["comments"].as_array().unwrap();
    let issue = GitHubIssue::from_rest(&fixture["issue"], kind, comments).unwrap();
    let plan =
        plan_github_issue(&issue, fixture["repo"].as_str().unwrap(), None, 1200, 300).unwrap();
    let expected = &fixture["expected"];
    assert_eq!(plan.calls[0].document_id, expected["base_id"]);
    assert_eq!(plan.calls[1].document_id, expected["comment_id"]);
    assert_eq!(issue.state, expected["state"]);
    for (key, value) in expected["metadata"].as_object().unwrap() {
        assert_eq!(
            plan.calls[0].metadata.get(key).map(String::as_str),
            Some(value.as_str().unwrap()),
            "metadata {key}"
        );
    }
    let mut actual_tags = plan.calls[0].tags.clone();
    assert_eq!(actual_tags, vec!["merged", "pr", "ai", "ai"]);
    let mut expected_tags: Vec<String> = expected["tags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tag| tag.as_str().unwrap().to_string())
        .collect();
    actual_tags.sort();
    actual_tags.dedup();
    expected_tags.sort();
    expected_tags.dedup();
    assert_eq!(actual_tags, expected_tags);
    assert!(plan.calls[1].content.starts_with("**@praxis-bot-app:**"));
}

#[test]
fn jira_story_fixture_matches_the_documented_contract() {
    let fixture: Value =
        serde_json::from_str(include_str!("../fixtures/issues/jira_story.json")).unwrap();
    let issue = JiraIssue::from_search(&fixture["issue"]).unwrap();
    let plan = plan_jira_issue(
        &issue,
        fixture["project"].as_str().unwrap(),
        None,
        1200,
        300,
    )
    .unwrap();
    let expected = &fixture["expected"];
    assert_eq!(plan.calls[0].document_id, expected["base_id"]);
    assert_eq!(plan.calls[1].document_id, expected["comment_id"]);
    for (key, value) in expected["metadata"].as_object().unwrap() {
        assert_eq!(
            plan.calls[0].metadata.get(key).map(String::as_str),
            Some(value.as_str().unwrap()),
            "metadata {key}"
        );
    }
    let mut actual_tags = plan.calls[0].tags.clone();
    let mut expected_tags: Vec<String> = expected["tags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tag| tag.as_str().unwrap().to_string())
        .collect();
    actual_tags.sort();
    actual_tags.dedup();
    expected_tags.sort();
    expected_tags.dedup();
    assert_eq!(actual_tags, expected_tags);
    assert!(plan.calls[0].content.contains("Parent: RHIDP-18441"));
}
