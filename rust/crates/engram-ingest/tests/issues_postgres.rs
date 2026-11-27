//! Postgres + HTTP fixture harness for native issue ingestion (issue #243).

use std::collections::BTreeMap;
use std::sync::Mutex;

use chrono::Utc;
use engram_ingest::{
    apply_snapshot, connect, ingest_issue_stream, load_source_revision, migrate, save_issue_cursor,
    stream_fingerprint, upsert_source, GitHubKind, HttpResponse, HttpTransport, IssueCursor,
    IssueIngestError, IssueIngestOutcome, IssueSnapshot, IssueStream, PollError, StoreError,
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

fn response(status: u16, body: Value) -> HttpResponse {
    HttpResponse {
        status,
        headers: BTreeMap::new(),
        body: serde_json::to_vec(&body).unwrap(),
    }
}

fn response_with_headers(status: u16, body: Value, headers: &[(&str, &str)]) -> HttpResponse {
    let mut response = response(status, body);
    for (name, value) in headers {
        response
            .headers
            .insert(name.to_ascii_lowercase(), value.to_string());
    }
    response
}

#[derive(Default)]
struct Routes {
    gets: Mutex<BTreeMap<String, HttpResponse>>,
    posts: Mutex<BTreeMap<String, HttpResponse>>,
    requests: Mutex<Vec<String>>,
    request_headers: Mutex<Vec<BTreeMap<String, String>>>,
    etag_304: Mutex<Option<String>>,
}

impl Routes {
    fn get(mut self, url: &str, response: HttpResponse) -> Self {
        self.gets
            .get_mut()
            .unwrap()
            .insert(url.to_string(), response);
        self
    }

    fn post(mut self, url: &str, response: HttpResponse) -> Self {
        self.posts
            .get_mut()
            .unwrap()
            .insert(url.to_string(), response);
        self
    }

    fn etag_304(mut self, etag: &str) -> Self {
        *self.etag_304.get_mut().unwrap() = Some(etag.to_string());
        self
    }

    fn saw_if_none_match(&self) -> bool {
        self.request_headers
            .lock()
            .unwrap()
            .iter()
            .any(|headers| headers.contains_key("if-none-match"))
    }

    fn requests(&self) -> Vec<String> {
        self.requests.lock().unwrap().clone()
    }
}

impl HttpTransport for Routes {
    fn get(&self, url: &str, headers: &[(&str, String)]) -> Result<HttpResponse, PollError> {
        self.requests.lock().unwrap().push(url.to_string());
        self.request_headers.lock().unwrap().push(
            headers
                .iter()
                .map(|(name, value)| (name.to_ascii_lowercase(), value.clone()))
                .collect(),
        );
        if let Some(etag) = self.etag_304.lock().unwrap().as_ref() {
            let matches = headers
                .iter()
                .any(|(name, value)| name.eq_ignore_ascii_case("if-none-match") && value == etag);
            if matches {
                return Ok(HttpResponse {
                    status: 304,
                    headers: BTreeMap::new(),
                    body: Vec::new(),
                });
            }
        }
        self.gets
            .lock()
            .unwrap()
            .get(url)
            .cloned()
            .ok_or_else(|| PollError::Invalid(format!("no route for {url}")))
    }

    fn post_json(
        &self,
        url: &str,
        _headers: &[(&str, String)],
        _body: &Value,
    ) -> Result<HttpResponse, PollError> {
        self.requests.lock().unwrap().push(url.to_string());
        self.posts
            .lock()
            .unwrap()
            .get(url)
            .cloned()
            .ok_or_else(|| PollError::Invalid(format!("no POST route for {url}")))
    }
}

fn github_list_url() -> String {
    "https://api.github.com/repos/org/repo/issues?state=all&per_page=100&sort=created&direction=desc"
        .to_string()
}

fn github_comments_url(number: u64) -> String {
    format!("https://api.github.com/repos/org/repo/issues/{number}/comments?per_page=100")
}

fn github_item(number: u64, state: &str, labels: &[&str], comments: u64) -> Value {
    json!({
        "number": number,
        "title": format!("Issue {number}"),
        "body": "A sufficiently long issue body for parity.",
        "state": state,
        "labels": labels.iter().map(|label| json!({"name": label})).collect::<Vec<_>>(),
        "user": {"login": "alice"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "comments": comments
    })
}

fn seed_source(client: &mut Client, stream: &IssueStream) -> i64 {
    upsert_source(client, &stream.source_record()).expect("source")
}

fn job_rows(client: &mut Client) -> Vec<(String, String, Value)> {
    client
        .query(
            "SELECT kind, hindsight_document_id, payload
               FROM engram_ingest.jobs ORDER BY id",
            &[],
        )
        .expect("jobs")
        .into_iter()
        .map(|row| (row.get(0), row.get(1), row.get(2)))
        .collect()
}

#[test]
fn github_snapshot_replay_edit_delete_and_etag() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
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

    let stream = IssueStream::github("p", "p-issues", "org/repo", GitHubKind::Issue, None, 100);
    let source_id = seed_source(&mut client, &stream);
    let routes = Routes::default()
        .get(
            &github_list_url(),
            response_with_headers(
                200,
                json!([
                    github_item(1, "open", &["bug"], 1),
                    github_item(2, "open", &[], 0)
                ]),
                &[("etag", "\"v1\"")],
            ),
        )
        .get(
            &github_comments_url(1),
            response(
                200,
                json!([{
                    "id": 9,
                    "body": "A long enough human comment.",
                    "author_association": "MEMBER",
                    "user": {"login": "alice[bot]"}
                }]),
            ),
        );

    let first = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("first ingest");
    let IssueIngestOutcome::Applied { summary, cursor } = first else {
        panic!("expected an applied snapshot");
    };
    assert!(summary.jobs_created >= 3);
    assert_eq!(cursor.status, "ok");
    assert_eq!(cursor.etag.as_deref(), Some("\"v1\""));
    assert_eq!(cursor.page_count, 1);
    let rows = job_rows(&mut client);
    let docs: Vec<&str> = rows
        .iter()
        .map(|(_, document, _)| document.as_str())
        .collect();
    assert!(docs.contains(&"repo-issue-1"));
    assert!(docs.contains(&"repo-issue-1-comment0"));
    assert!(docs.contains(&"repo-issue-2"));
    let comment = rows
        .iter()
        .find(|(_, document, _)| document == "repo-issue-1-comment0")
        .unwrap();
    assert_eq!(comment.2["metadata"]["kind"], "issue");
    assert_eq!(comment.2["metadata"]["state"], "open");
    assert_eq!(comment.2["tags"], json!(["open", "issue", "repo", "bug"]));

    // Replay with the same snapshot and a matching ETag is a 304 and creates no
    // jobs.
    let routes = routes.etag_304("\"v1\"");
    let replay = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("replay");
    let IssueIngestOutcome::NotModified(cursor) = replay else {
        panic!("expected not modified");
    };
    assert_eq!(cursor.status, "not_modified");
    assert_eq!(job_rows(&mut client).len(), rows.len());

    // A state/label edit re-retains the same ids with new metadata.
    let routes = Routes::default()
        .get(
            &github_list_url(),
            response(
                200,
                json!([
                    github_item(1, "closed", &["bug", "verified"], 1),
                    github_item(2, "open", &[], 0)
                ]),
            ),
        )
        .get(
            &github_comments_url(1),
            response(
                200,
                json!([{
                    "id": 9,
                    "body": "A long enough human comment.",
                    "author_association": "MEMBER",
                    "user": {"login": "alice[bot]"}
                }]),
            ),
        );
    let edited = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("edited ingest");
    let IssueIngestOutcome::Applied { summary, .. } = edited else {
        panic!("expected an applied snapshot");
    };
    assert!(summary.jobs_created >= 2);
    let rows = job_rows(&mut client);
    let latest = rows
        .iter()
        .rev()
        .find(|(_, document, _)| document == "repo-issue-1")
        .unwrap();
    assert_eq!(latest.2["metadata"]["state"], "closed");
    assert_eq!(
        latest.2["tags"],
        json!(["closed", "issue", "repo", "bug", "verified"])
    );

    // Removing issue 2 tombstones it and enqueues a delete with the issue reason.
    let routes = Routes::default()
        .get(
            &github_list_url(),
            response(
                200,
                json!([github_item(1, "closed", &["bug", "verified"], 1)]),
            ),
        )
        .get(
            &github_comments_url(1),
            response(
                200,
                json!([{
                    "id": 9,
                    "body": "A long enough human comment.",
                    "author_association": "MEMBER",
                    "user": {"login": "alice[bot]"}
                }]),
            ),
        );
    let removed = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("delete ingest");
    let IssueIngestOutcome::Applied { summary, .. } = removed else {
        panic!("expected an applied snapshot");
    };
    assert!(summary.deleted_chunks >= 1);
    let outcome = client
        .query_one(
            "SELECT kind FROM engram_ingest.jobs
              WHERE hindsight_document_id = 'repo-issue-2' AND kind = 'delete'",
            &[],
        )
        .expect("delete job");
    assert_eq!(outcome.get::<_, String>(0), "delete");
    let reason: String = client
        .query_one(
            "SELECT reason FROM engram_ingest.tombstones
              WHERE hindsight_document_id = 'repo-issue-2'",
            &[],
        )
        .expect("tombstone")
        .get(0);
    assert_eq!(reason, "issue_removed");
}

#[test]
fn github_failure_does_not_delete_and_records_the_cursor_error() {
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
    let stream = IssueStream::github("p", "p-issues", "org/repo", GitHubKind::Issue, None, 100);
    let source_id = seed_source(&mut client, &stream);
    let routes = Routes::default().get(
        &github_list_url(),
        response(200, json!([github_item(1, "open", &[], 0)])),
    );
    ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("seed snapshot");
    let before = job_rows(&mut client).len();
    let routes = Routes::default().get(
        &github_list_url(),
        response(500, json!({"message": "boom"})),
    );
    let error = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .unwrap_err();
    assert!(matches!(error, IssueIngestError::Poll(_)));
    assert_eq!(job_rows(&mut client).len(), before);
    let cursor: IssueCursor = {
        let row = client
            .query_one(
                "SELECT status, last_error FROM engram_ingest.issue_cursors WHERE source_id = $1",
                &[&source_id],
            )
            .expect("cursor");
        IssueCursor {
            status: row.get(0),
            last_error: row.get(1),
            ..IssueCursor::default()
        }
    };
    assert_eq!(cursor.status, "error");
    assert!(cursor.last_error.unwrap().contains("500"));
}

#[test]
fn jira_snapshot_runs_through_the_same_ledger() {
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
    let stream = IssueStream::jira(
        "p",
        "p-issues",
        "https://jira.example",
        "a@example.com",
        "project = EX",
        "EX",
        100,
    );
    let source_id = seed_source(&mut client, &stream);
    let routes = Routes::default().post(
        "https://jira.example/rest/api/3/search/jql",
        response(
            200,
            json!({
                "issues": [{
                    "key": "EX-1",
                    "fields": {
                        "summary": "A Jira story",
                        "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Description"}]}]},
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Story"},
                        "labels": ["backend"],
                        "reporter": {"displayName": "Bob"},
                        "created": "2026-01-01T00:00:00.000+0000",
                        "updated": "2026-01-02T00:00:00.000+0000",
                        "parent": {"key": "EX-0"},
                        "comment": {"total": 1, "comments": [{"id": "1", "author": {"displayName": "Alice"}, "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "A long enough Jira comment."}]}]}}]}
                    }
                }],
                "isLast": true
            }),
        ),
    );
    let outcome = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("jira ingest");
    let IssueIngestOutcome::Applied { summary, .. } = outcome else {
        panic!("expected an applied snapshot");
    };
    assert!(summary.jobs_created >= 2);
    let rows = job_rows(&mut client);
    let docs: Vec<&str> = rows
        .iter()
        .map(|(_, document, _)| document.as_str())
        .collect();
    assert!(docs.contains(&"jira-EX-1"));
    assert!(docs.contains(&"jira-EX-1-comment0"));
    let header = rows
        .iter()
        .find(|(_, document, _)| document == "jira-EX-1")
        .unwrap();
    assert_eq!(header.2["metadata"]["tracker"], "jira");
    assert_eq!(header.2["metadata"]["project"], "EX");
    assert_eq!(
        header.2["tags"],
        json!(["open", "story", "jira", "backend", "EX-0"])
    );
}

#[test]
fn migration_v5_is_rerunnable_against_populated_rows() {
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
    let stream = IssueStream::github("p", "p-issues", "org/repo", GitHubKind::Issue, None, 100);
    let source_id = seed_source(&mut client, &stream);
    save_issue_cursor(
        &mut client,
        source_id,
        &IssueCursor {
            etag: Some("\"v1\"".to_string()),
            fingerprint: Some("fingerprint".to_string()),
            status: "ok".to_string(),
            ..IssueCursor::default()
        },
        Utc::now(),
    )
    .expect("cursor");
    client
        .execute(
            "DELETE FROM engram_ingest.schema_migrations WHERE version >= 5",
            &[],
        )
        .expect("drop migration marker");
    migrate(&mut client).expect("reapply v5-v7");
    let kind: String = client
        .query_one(
            "SELECT source_kind FROM engram_ingest.sources WHERE id = $1",
            &[&source_id],
        )
        .expect("source kind")
        .get(0);
    assert_eq!(kind, "github_issue");
    let row = client
        .query_one(
            "SELECT etag, fingerprint FROM engram_ingest.issue_cursors WHERE source_id = $1",
            &[&source_id],
        )
        .expect("cursor survived");
    assert_eq!(row.get::<_, Option<String>>(0).as_deref(), Some("\"v1\""));
    assert_eq!(
        row.get::<_, Option<String>>(1).as_deref(),
        Some("fingerprint")
    );
}

#[test]
fn pr_stream_uses_pulls_and_deletes_on_removal() {
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
    let stream = IssueStream::github("p", "p-issues", "org/repo", GitHubKind::Pr, None, 100);
    let source_id = seed_source(&mut client, &stream);
    let pulls_url = "https://api.github.com/repos/org/repo/pulls?state=all&per_page=100&sort=created&direction=desc";
    let comments_url = "https://api.github.com/repos/org/repo/issues/9/comments?per_page=100";
    let mut pr = github_item(9, "closed", &[], 0);
    pr["state"] = json!("closed");
    pr["merged_at"] = json!("2026-01-03T00:00:00Z");
    let routes = Routes::default()
        .get(pulls_url, response(200, json!([pr])))
        .get(
            comments_url,
            response(
                200,
                json!([{
                    "id": 1,
                    "body": "A long enough PR comment.",
                    "author_association": "MEMBER",
                    "user": {"login": "alice"}
                }]),
            ),
        );
    let outcome = ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("pr ingest");
    let IssueIngestOutcome::Applied { summary, cursor } = outcome else {
        panic!("expected applied");
    };
    assert!(summary.jobs_created >= 2);
    assert_eq!(cursor.item_count, 1);
    let rows = job_rows(&mut client);
    let header = rows
        .iter()
        .find(|(_, document, _)| document == "repo-pr-9")
        .unwrap();
    assert_eq!(header.2["metadata"]["state"], "merged");
    assert!(routes.requests()[0].contains("/pulls"));

    let routes = Routes::default().get(pulls_url, response(200, json!([])));
    ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("delete pr");
    let reason: String = client
        .query_one(
            "SELECT reason FROM engram_ingest.tombstones WHERE hindsight_document_id = 'repo-pr-9'",
            &[],
        )
        .expect("tombstone")
        .get(0);
    assert_eq!(reason, "issue_removed");
}

#[test]
fn fingerprint_change_after_a_failed_poll_does_not_reuse_the_old_etag() {
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
    let stream = IssueStream::github("p", "p-issues", "org/repo", GitHubKind::Issue, None, 100);
    let source_id = seed_source(&mut client, &stream);
    let routes = Routes::default().get(
        &github_list_url(),
        response_with_headers(
            200,
            json!([github_item(1, "open", &[], 0)]),
            &[("etag", "\"v1\"")],
        ),
    );
    ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("seed");

    let mut changed = stream.clone();
    changed.chunk_size = 900;
    let routes = Routes::default().get(
        &github_list_url(),
        response(500, json!({"message": "boom"})),
    );
    let error = ingest_issue_stream(
        &mut client,
        source_id,
        &changed,
        &routes,
        "token",
        Utc::now(),
    )
    .unwrap_err();
    assert!(matches!(error, IssueIngestError::Poll(_)));
    let stored: Option<String> = client
        .query_one(
            "SELECT fingerprint FROM engram_ingest.issue_cursors WHERE source_id = $1",
            &[&source_id],
        )
        .expect("cursor")
        .get(0);
    assert_eq!(stored, Some(stream_fingerprint(&stream)));

    let routes = Routes::default().get(
        &github_list_url(),
        response(200, json!([github_item(1, "open", &[], 0)])),
    );
    ingest_issue_stream(
        &mut client,
        source_id,
        &changed,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("changed config ingest");
    assert!(!routes.saw_if_none_match());
}

#[test]
fn an_older_snapshot_cannot_delete_a_newer_issue() {
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
    let stream = IssueStream::github("p", "p-issues", "org/repo", GitHubKind::Issue, None, 100);
    let source_id = seed_source(&mut client, &stream);
    let base_revision = load_source_revision(&mut client, source_id).expect("revision");

    let routes = Routes::default().get(
        &github_list_url(),
        response(200, json!([github_item(1, "open", &[], 0)])),
    );
    ingest_issue_stream(
        &mut client,
        source_id,
        &stream,
        &routes,
        "token",
        Utc::now(),
    )
    .expect("newer poll");

    let empty = IssueSnapshot {
        plans: Vec::new(),
        page_count: 1,
        item_count: 0,
        etag: None,
        last_updated: None,
        deletions_allowed: true,
    };
    let error = apply_snapshot(&mut client, source_id, &empty, base_revision, Utc::now())
        .expect_err("old snapshot must be rejected");
    assert!(matches!(error, StoreError::StaleSnapshot));
    let state: String = client
        .query_one(
            "SELECT state FROM engram_ingest.documents
              WHERE source_id = $1 AND relative_path = 'issue/1'",
            &[&source_id],
        )
        .expect("newer issue")
        .get(0);
    assert_eq!(state, "active");
}
