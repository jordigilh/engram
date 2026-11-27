//! Postgres + GraphQL fixture harness for discussions and boards (issue #244).

use std::collections::BTreeMap;
use std::sync::Mutex;

use chrono::Utc;
use engram_ingest::{
    connect, ingest_issue_stream, migrate, upsert_source, HttpResponse, HttpTransport,
    IssueIngestOutcome, IssueStream, PollError, StoreError,
};
use postgres::Client;
use serde_json::{json, Value};

fn test_url() -> Option<String> {
    std::env::var("ENGRAM_INGEST_TEST_DATABASE_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
}

fn truncate(client: &mut Client) {
    client
        .batch_execute(
            "TRUNCATE engram_ingest.leases, engram_ingest.jobs,
                      engram_ingest.tombstones, engram_ingest.chunks,
                      engram_ingest.documents, engram_ingest.issue_cursors,
                      engram_ingest.sources
             RESTART IDENTITY CASCADE",
        )
        .expect("truncate");
}

fn response(body: Value) -> HttpResponse {
    HttpResponse {
        status: 200,
        headers: BTreeMap::new(),
        body: serde_json::to_vec(&body).unwrap(),
    }
}

#[derive(Default)]
struct Scripted {
    responses: Mutex<Vec<HttpResponse>>,
    requests: Mutex<Vec<String>>,
}

impl Scripted {
    fn new(responses: Vec<HttpResponse>) -> Self {
        Self {
            responses: Mutex::new(responses),
            requests: Mutex::new(Vec::new()),
        }
    }
}

impl HttpTransport for Scripted {
    fn get(&self, url: &str, _headers: &[(&str, String)]) -> Result<HttpResponse, PollError> {
        Err(PollError::Invalid(format!("unexpected GET {url}")))
    }

    fn post_json(
        &self,
        url: &str,
        _headers: &[(&str, String)],
        _body: &Value,
    ) -> Result<HttpResponse, PollError> {
        self.requests.lock().unwrap().push(url.to_string());
        let mut responses = self.responses.lock().unwrap();
        if responses.is_empty() {
            return Err(PollError::Invalid("no scripted response".into()));
        }
        Ok(responses.remove(0))
    }
}

fn discussion_node(number: u64, body: &str, comments: Vec<Value>) -> Value {
    json!({
        "number": number,
        "title": format!("Discussion {number}"),
        "body": body,
        "category": {"name": "General"},
        "author": {"login": "alice"},
        "createdAt": "2026-11-18T14:02:50Z",
        "updatedAt": "2026-11-18T14:02:50Z",
        "comments": {"nodes": comments}
    })
}

fn discussions_page(nodes: Vec<Value>) -> Value {
    json!({
        "data": {"repository": {"discussions": {
            "pageInfo": {"hasNextPage": false, "endCursor": null},
            "nodes": nodes
        }}}
    })
}

fn board_item(number: u64, status: &str) -> Value {
    json!({
        "content": {"number": number, "title": format!("Item {number}"), "repository": {"name": "policy"}},
        "fieldValues": {"nodes": [{"name": status, "field": {"name": "Status"}}]}
    })
}

fn board_page(nodes: Vec<Value>, has_next: bool, cursor: Option<&str>) -> Value {
    json!({
        "data": {"organization": {"projectV2": {
            "title": "Praxis Policy Engine (PPE)",
            "shortDescription": "Policy engine roadmap",
            "items": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes
            }
        }}}
    })
}

fn job_docs(client: &mut Client) -> Vec<String> {
    client
        .query(
            "SELECT hindsight_document_id FROM engram_ingest.jobs ORDER BY id",
            &[],
        )
        .expect("jobs")
        .into_iter()
        .map(|row| row.get(0))
        .collect()
}

#[test]
fn discussion_snapshot_replay_edit_and_delete() {
    let Some(url) = test_url() else {
        return;
    };
    let mut client = connect(&url).expect("connect");
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database name")
        .get(0);
    assert_eq!(database, "engram_ingest_test");
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);

    let stream = IssueStream::github_discussions("p", "p-issues", "praxis-proxy/praxis", None, 100);
    let source_id = upsert_source(&mut client, &stream.source_record()).expect("source");
    let comment = json!({
        "author": {"login": "shaneutt"},
        "body": "A long enough discussion comment for parity.",
        "authorAssociation": "MEMBER"
    });
    let routes = Scripted::new(vec![response(discussions_page(vec![discussion_node(
        1562,
        "We have read the Praxis AI and policy docs with interest and have questions.",
        vec![comment.clone()],
    )]))]);
    let outcome = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("ingest discussion");
    let IssueIngestOutcome::Applied { summary, .. } = outcome else {
        panic!("expected applied");
    };
    assert!(summary.jobs_created >= 2);
    let docs = job_docs(&mut client);
    assert!(docs.contains(&"praxis-discussion-1562".to_string()));
    assert!(docs.contains(&"praxis-discussion-1562-comment0".to_string()));

    // Replay is a no-op.
    let routes = Scripted::new(vec![response(discussions_page(vec![discussion_node(
        1562,
        "We have read the Praxis AI and policy docs with interest and have questions.",
        vec![comment],
    )]))]);
    let replay = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("replay");
    let IssueIngestOutcome::Applied { summary, .. } = replay else {
        panic!("expected applied");
    };
    assert_eq!(summary.jobs_created, 0);

    // An empty valid result deletes the discussion document.
    let routes = Scripted::new(vec![response(discussions_page(Vec::new()))]);
    let removed = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("delete discussion");
    let IssueIngestOutcome::Applied { summary, .. } = removed else {
        panic!("expected applied");
    };
    assert!(summary.deleted_chunks >= 1);
    let reason: String = client
        .query_one(
            "SELECT reason FROM engram_ingest.tombstones
              WHERE hindsight_document_id = 'praxis-discussion-1562'",
            &[],
        )
        .expect("tombstone")
        .get(0);
    assert_eq!(reason, "issue_removed");
}

#[test]
fn board_snapshot_paginates_and_rejects_missing_projects() {
    let Some(url) = test_url() else {
        return;
    };
    let mut client = connect(&url).expect("connect");
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database name")
        .get(0);
    assert_eq!(database, "engram_ingest_test");
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);

    let stream = IssueStream::github_project("p", "p-issues", "praxis-proxy", 9, None);
    let source_id = upsert_source(&mut client, &stream.source_record()).expect("source");
    let routes = Scripted::new(vec![
        response(board_page(
            vec![board_item(130, "Done"), board_item(10, "Review")],
            true,
            Some("c1"),
        )),
        response(board_page(vec![board_item(131, "Next")], false, None)),
        response(board_page(
            vec![board_item(130, "Done"), board_item(10, "Review")],
            true,
            Some("c1"),
        )),
        response(board_page(vec![board_item(131, "Next")], false, None)),
    ]);
    let outcome = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("ingest board");
    let IssueIngestOutcome::Applied { summary, cursor } = outcome else {
        panic!("expected applied");
    };
    assert_eq!(summary.jobs_created, 1);
    assert_eq!(cursor.item_count, 3);
    let docs = job_docs(&mut client);
    assert_eq!(docs, vec!["board-9-praxis-policy-engine-(ppe)"]);
    let payload: Value = client
        .query_one(
            "SELECT payload FROM engram_ingest.jobs
              WHERE hindsight_document_id = 'board-9-praxis-policy-engine-(ppe)'",
            &[],
        )
        .expect("job")
        .get(0);
    assert_eq!(payload["metadata"]["kind"], "board");
    assert!(payload["content"]
        .as_str()
        .unwrap()
        .contains("Total items: 3"));

    // A missing project is an error, not a deletion.
    let routes = Scripted::new(vec![response(json!({
        "data": {"organization": {"projectV2": null}}
    }))]);
    let error = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .unwrap_err();
    assert!(matches!(error, engram_ingest::IssueIngestError::Poll(_)));
    let state: String = client
        .query_one(
            "SELECT state FROM engram_ingest.documents
              WHERE source_id = $1 AND relative_path = 'board/9'",
            &[&source_id],
        )
        .expect("board document")
        .get(0);
    assert_eq!(state, "active");
}

#[test]
fn migration_v6_is_rerunnable_and_accepts_new_source_kinds() {
    let Some(url) = test_url() else {
        return;
    };
    let mut client = connect(&url).expect("connect");
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database name")
        .get(0);
    assert_eq!(database, "engram_ingest_test");
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);
    let board = IssueStream::github_project("p", "p-issues", "praxis-proxy", 9, None);
    upsert_source(&mut client, &board.source_record()).expect("board source");
    client
        .execute(
            "DELETE FROM engram_ingest.schema_migrations WHERE version >= 6",
            &[],
        )
        .expect("drop migration marker");
    migrate(&mut client).expect("reapply v6 and v7");
    let discussion =
        IssueStream::github_discussions("p", "p-issues", "praxis-proxy/praxis", None, 100);
    upsert_source(&mut client, &discussion.source_record()).expect("discussion source after v6");
    let kinds: Vec<String> = client
        .query(
            "SELECT source_kind FROM engram_ingest.sources ORDER BY id",
            &[],
        )
        .expect("kinds")
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(kinds, vec!["github_board", "github_discussion"]);
}

#[test]
fn migration_guard_detects_a_v5_constraint_recorded_as_v6() {
    let Some(url) = test_url() else {
        return;
    };
    let mut client = connect(&url).expect("connect");
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database name")
        .get(0);
    assert_eq!(database, "engram_ingest_test");
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);
    client
        .batch_execute(
            "ALTER TABLE engram_ingest.sources DROP CONSTRAINT sources_source_kind_check;
             ALTER TABLE engram_ingest.sources ADD CONSTRAINT sources_source_kind_check
                 CHECK (source_kind IN ('docs','github_issue','github_pr','jira_issue'));
             DELETE FROM engram_ingest.schema_migrations WHERE version = 6;
             INSERT INTO engram_ingest.schema_migrations (version) VALUES (6);",
        )
        .expect("simulate drifted v6");
    let error = migrate(&mut client).expect_err("drifted constraint must fail");
    assert!(matches!(error, StoreError::MigrationMismatch(_)));
    client
        .execute(
            "DELETE FROM engram_ingest.schema_migrations WHERE version >= 6",
            &[],
        )
        .expect("drop drifted marker");
    migrate(&mut client).expect("restore v6/v7 after drift test");
}
