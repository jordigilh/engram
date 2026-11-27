//! Frozen GraphQL fixtures for the #244 parity contract.

use engram_ingest::{plan_board, plan_discussion, Board, BoardItem, Discussion};
use serde_json::Value;

#[test]
fn discussion_fixture_matches_the_documented_contract() {
    let fixture: Value =
        serde_json::from_str(include_str!("../fixtures/issues/discussion.json")).unwrap();
    let item = Discussion::from_graphql(&fixture["discussion"]).unwrap();
    let plan = plan_discussion(&item, fixture["repo"].as_str().unwrap(), None, 1200, 300).unwrap();
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
    actual_tags.sort();
    let mut expected_tags: Vec<String> = expected["tags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tag| tag.as_str().unwrap().to_string())
        .collect();
    expected_tags.sort();
    assert_eq!(actual_tags, expected_tags);
}

#[test]
fn board_fixture_matches_the_documented_contract() {
    let fixture: Value =
        serde_json::from_str(include_str!("../fixtures/issues/board.json")).unwrap();
    let board_value = &fixture["board"];
    let items = board_value["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(BoardItem::from_graphql)
        .collect();
    let board = Board {
        number: fixture["number"].as_u64().unwrap(),
        organization: fixture["organization"].as_str().unwrap().to_string(),
        title: board_value["title"].as_str().unwrap().to_string(),
        description: board_value["shortDescription"]
            .as_str()
            .unwrap()
            .to_string(),
        items,
    };
    let plan = plan_board(&board, None, Some("2026-11-26T12:00:00".to_string())).unwrap();
    let expected = &fixture["expected"];
    assert_eq!(plan.calls[0].document_id, expected["document_id"]);
    assert_eq!(plan.calls.len(), 1);
    for (key, value) in expected["metadata"].as_object().unwrap() {
        assert_eq!(
            plan.calls[0].metadata.get(key).map(String::as_str),
            Some(value.as_str().unwrap()),
            "metadata {key}"
        );
    }
    let expected_tags: Vec<String> = expected["tags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tag| tag.as_str().unwrap().to_string())
        .collect();
    assert_eq!(plan.calls[0].tags, expected_tags);
    assert!(plan.calls[0].content.contains("Total items: 3"));
    assert!(plan.calls[0].content.contains("## Status: Review (1)"));
}
