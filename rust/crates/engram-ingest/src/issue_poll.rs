//! GitHub/Jira issue polling and ledger application (issue #243).
//!
//! The pollers fetch a **full snapshot** on every pass, matching the Python
//! `issues_main` flow. That is what makes out-of-scope deletion exact: an issue
//! missing from a complete snapshot really did leave scope. A delta would make
//! `reconcile` delete every unchanged issue, so no delta path is exposed here.
//!
//! ETags are still honoured as an optimisation: a stored ETag is only replayed
//! when the previous snapshot fit in one page. A 304 on page 1 of a multi-page
//! response cannot prove later pages are unchanged.
//!
//! All HTTP access goes through [`HttpTransport`] so tests can script
//! pagination, 304s, rate limits, and failures without a network.

use std::collections::{BTreeMap, BTreeSet};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, Utc};
use postgres::Client;
use reqwest::blocking::Client as HttpClient;
use serde_json::{json, Value};

use crate::hashing::ContentHash;
use crate::issues::{
    plan_github_issue, plan_jira_issue, GitHubIssue, GitHubKind, IssueError, IssuePlan, JiraIssue,
    ISSUE_CHUNK_OVERLAP, ISSUE_CHUNK_SIZE,
};
use crate::ledger::{SourceRecord, ISSUE_LAYOUT_VERSION};
use crate::reconcile::{reconcile_with_removed_reason, DeleteReason, Reconciliation, ScannedFile};
use crate::store::{
    apply_reconciliation, load_issue_cursor, load_manifest, load_source_revision,
    save_issue_cursor, ApplySummary, IssueCursor, StoreError,
};

pub const DEFAULT_GITHUB_API: &str = "https://api.github.com";
pub const GITHUB_PAGE_SIZE: usize = 100;
pub const JIRA_PAGE_SIZE: usize = 100;
const MAX_PAGES: usize = 1_000;

#[derive(Debug, thiserror::Error)]
pub enum PollError {
    #[error("HTTP transport failed: {0}")]
    Transport(String),
    #[error("HTTP {status}: {body}")]
    Http { status: u16, body: String },
    #[error("rate limited; retry after {retry_after:?}")]
    RateLimited { retry_after: Option<Duration> },
    #[error("issue source returned more pages than the configured limit {0}")]
    Truncated(usize),
    #[error("invalid HTTP response: {0}")]
    Invalid(String),
    #[error(transparent)]
    Issue(#[from] IssueError),
}

impl PollError {
    pub fn is_transient(&self) -> bool {
        match self {
            Self::Transport(_) | Self::RateLimited { .. } => true,
            Self::Http { status, .. } => *status >= 500,
            _ => false,
        }
    }

    pub fn retry_after(&self) -> Option<Duration> {
        match self {
            Self::RateLimited { retry_after } => *retry_after,
            _ => None,
        }
    }

    fn from_status(status: u16, headers: &BTreeMap<String, String>, body: Vec<u8>) -> Self {
        if status == 429 || status == 403 {
            let retry_after = headers
                .get("retry-after")
                .and_then(|value| value.parse::<u64>().ok())
                .map(Duration::from_secs);
            let reset_after = headers
                .get("x-ratelimit-reset")
                .and_then(|value| value.parse::<u64>().ok())
                .and_then(|reset| {
                    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
                    reset.checked_sub(now).map(Duration::from_secs)
                });
            let exhausted = headers
                .get("x-ratelimit-remaining")
                .is_some_and(|value| value == "0");
            let retry_after = match (retry_after, reset_after) {
                (Some(retry), Some(reset)) => Some(retry.max(reset)),
                (Some(retry), None) => Some(retry),
                (None, Some(reset)) => Some(reset),
                (None, None) => None,
            };
            if status == 429 || exhausted || retry_after.is_some() {
                return Self::RateLimited { retry_after };
            }
        }
        Self::Http {
            status,
            body: String::from_utf8_lossy(&body).chars().take(300).collect(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    /// Header names are lower-cased.
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .get(&name.to_ascii_lowercase())
            .map(String::as_str)
    }
}

pub trait HttpTransport: Send + Sync {
    fn get(&self, url: &str, headers: &[(&str, String)]) -> Result<HttpResponse, PollError>;
    fn post_json(
        &self,
        url: &str,
        headers: &[(&str, String)],
        body: &Value,
    ) -> Result<HttpResponse, PollError>;
}

/// Blocking reqwest transport. Redirects are disabled: an API token must not
/// be forwarded to a redirect target.
pub struct ReqwestTransport {
    client: HttpClient,
}

impl ReqwestTransport {
    pub fn new(timeout: Duration) -> Result<Self, PollError> {
        let client = HttpClient::builder()
            .timeout(timeout)
            .redirect(reqwest::redirect::Policy::custom(|attempt| {
                let Some(original) = attempt.previous().first() else {
                    return attempt.follow();
                };
                if urls_same_origin(original, attempt.url()) {
                    attempt.follow()
                } else {
                    attempt.error("cross-origin redirect blocked")
                }
            }))
            .build()
            .map_err(|error| PollError::Transport(error.to_string()))?;
        Ok(Self { client })
    }
}

impl HttpTransport for ReqwestTransport {
    fn get(&self, url: &str, headers: &[(&str, String)]) -> Result<HttpResponse, PollError> {
        let mut request = self.client.get(url);
        for (name, value) in headers {
            request = request.header(*name, value);
        }
        let response = request
            .send()
            .map_err(|error| PollError::Transport(error.to_string()))?;
        response_parts(response)
    }

    fn post_json(
        &self,
        url: &str,
        headers: &[(&str, String)],
        body: &Value,
    ) -> Result<HttpResponse, PollError> {
        let mut request = self.client.post(url).json(body);
        for (name, value) in headers {
            request = request.header(*name, value);
        }
        let response = request
            .send()
            .map_err(|error| PollError::Transport(error.to_string()))?;
        response_parts(response)
    }
}

fn response_parts(response: reqwest::blocking::Response) -> Result<HttpResponse, PollError> {
    let status = response.status().as_u16();
    let mut headers = BTreeMap::new();
    for (name, value) in response.headers() {
        if let Ok(value) = value.to_str() {
            headers
                .entry(name.as_str().to_ascii_lowercase())
                .and_modify(|existing: &mut String| {
                    existing.push_str(", ");
                    existing.push_str(value);
                })
                .or_insert_with(|| value.to_string());
        }
    }
    let body = response
        .bytes()
        .map_err(|error| PollError::Transport(error.to_string()))?
        .to_vec();
    Ok(HttpResponse {
        status,
        headers,
        body,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IssueProvider {
    GitHub,
    Jira,
}

impl IssueProvider {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::GitHub => "github",
            Self::Jira => "jira",
        }
    }
}

/// One independently polled issue stream: a GitHub `(repo, item kind)` pair or a
/// Jira JQL stream. One stream maps to one `engram_ingest.sources` row so a
/// failed repo cannot delete another repo's documents.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssueStream {
    pub project: String,
    pub bank_id: String,
    pub provider: IssueProvider,
    pub source_kind: String,
    pub repo: Option<String>,
    pub github_kind: Option<GitHubKind>,
    pub jira_server: Option<String>,
    pub jira_email: Option<String>,
    pub jira_jql: Option<String>,
    pub jira_project: Option<String>,
    pub document_prefix: Option<String>,
    pub limit: usize,
    pub chunk_size: usize,
    pub chunk_overlap: usize,
}

impl IssueStream {
    pub fn github(
        project: &str,
        bank_id: &str,
        repo: &str,
        kind: GitHubKind,
        document_prefix: Option<&str>,
        limit: usize,
    ) -> Self {
        Self {
            project: project.to_string(),
            bank_id: bank_id.to_string(),
            provider: IssueProvider::GitHub,
            source_kind: match kind {
                GitHubKind::Issue => "github_issue",
                GitHubKind::Pr => "github_pr",
            }
            .to_string(),
            repo: Some(repo.to_string()),
            github_kind: Some(kind),
            jira_server: None,
            jira_email: None,
            jira_jql: None,
            jira_project: None,
            document_prefix: document_prefix.map(str::to_string),
            limit,
            chunk_size: ISSUE_CHUNK_SIZE,
            chunk_overlap: ISSUE_CHUNK_OVERLAP,
        }
    }

    pub fn jira(
        project: &str,
        bank_id: &str,
        server: &str,
        email: &str,
        jql: &str,
        jira_project: &str,
        limit: usize,
    ) -> Self {
        Self {
            project: project.to_string(),
            bank_id: bank_id.to_string(),
            provider: IssueProvider::Jira,
            source_kind: "jira_issue".to_string(),
            repo: None,
            github_kind: None,
            jira_server: Some(server.to_string()),
            jira_email: Some(email.to_string()),
            jira_jql: Some(jql.to_string()),
            jira_project: Some(jira_project.to_string()),
            document_prefix: None,
            limit,
            chunk_size: ISSUE_CHUNK_SIZE,
            chunk_overlap: ISSUE_CHUNK_OVERLAP,
        }
    }

    pub fn with_document_prefix(mut self, prefix: Option<&str>) -> Self {
        self.document_prefix = prefix.map(str::to_string);
        self
    }

    fn effective_prefix(&self) -> &str {
        match self.provider {
            IssueProvider::Jira => self
                .document_prefix
                .as_deref()
                .filter(|prefix| !prefix.is_empty())
                .unwrap_or("jira"),
            IssueProvider::GitHub => self
                .document_prefix
                .as_deref()
                .filter(|prefix| !prefix.is_empty())
                .unwrap_or(""),
        }
    }

    pub fn identity(&self) -> String {
        let config = match self.provider {
            IssueProvider::GitHub => json!({
                "provider": "github",
                "repo": self.repo.as_deref().unwrap_or("").to_lowercase(),
                "kind": self.github_kind.map(GitHubKind::as_str).unwrap_or("issue"),
                "prefix": self.effective_prefix(),
            }),
            IssueProvider::Jira => json!({
                "provider": "jira",
                "server": self
                    .jira_server
                    .as_deref()
                    .unwrap_or("")
                    .trim_end_matches('/')
                    .to_lowercase(),
                "jql": self.jira_jql.as_deref().unwrap_or(""),
                "project": self.jira_project.as_deref().unwrap_or(""),
                "prefix": self.effective_prefix(),
            }),
        };
        let digest = ContentHash::of(&serde_json::to_vec(&config).unwrap_or_default());
        let short = digest.to_string();
        let short = short.get(..16).unwrap_or(&short);
        match self.provider {
            IssueProvider::GitHub => format!(
                "github:{}:{}:{short}",
                self.repo.as_deref().unwrap_or("").to_lowercase(),
                self.github_kind.map(GitHubKind::as_str).unwrap_or("issue")
            ),
            IssueProvider::Jira => format!(
                "jira:{}:{}:{short}",
                self.jira_server
                    .as_deref()
                    .unwrap_or("")
                    .trim_end_matches('/')
                    .to_lowercase(),
                self.jira_project.as_deref().unwrap_or("")
            ),
        }
    }

    pub fn source_record(&self) -> SourceRecord {
        let identity = self.identity();
        SourceRecord::issue(
            &self.project,
            &identity,
            &identity,
            &self.bank_id,
            &self.source_kind,
        )
    }

    fn validate(&self) -> Result<(), PollError> {
        if self.limit == 0 {
            return Err(PollError::Invalid("issue limit must be positive".into()));
        }
        match self.provider {
            IssueProvider::GitHub => {
                if self.repo.as_deref().unwrap_or("").is_empty() {
                    return Err(PollError::Invalid("GitHub stream needs a repo".into()));
                }
                if self.github_kind.is_none() {
                    return Err(PollError::Invalid("GitHub stream needs a kind".into()));
                }
            }
            IssueProvider::Jira => {
                for (name, value) in [
                    ("server", self.jira_server.as_deref()),
                    ("email", self.jira_email.as_deref()),
                    ("jql", self.jira_jql.as_deref()),
                    ("project", self.jira_project.as_deref()),
                ] {
                    if value.unwrap_or("").is_empty() {
                        return Err(PollError::Invalid(format!("Jira stream needs {name}")));
                    }
                }
            }
        }
        Ok(())
    }
}

/// Reject stream sets that would share Hindsight document ids in one bank.
///
/// GitHub ids include the short repo and kind, so one prefix can serve several
/// repositories. Jira ids are `{prefix}-{key}` and a JQL cannot be proven
/// disjoint, so two Jira streams in the same bank must use different prefixes.
pub fn validate_bank_streams(streams: &[IssueStream]) -> Result<(), PollError> {
    let mut identities: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut jira_prefixes: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for stream in streams {
        stream.validate()?;
        if !identities
            .entry(stream.bank_id.clone())
            .or_default()
            .insert(stream.identity())
        {
            return Err(PollError::Invalid(format!(
                "duplicate issue stream identity in bank {}",
                stream.bank_id
            )));
        }
        if stream.provider == IssueProvider::Jira
            && !jira_prefixes
                .entry(stream.bank_id.clone())
                .or_default()
                .insert(stream.effective_prefix().to_string())
        {
            return Err(PollError::Invalid(format!(
                "two Jira streams in bank {} share the document prefix {:?}",
                stream.bank_id,
                stream.effective_prefix()
            )));
        }
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssueSnapshot {
    pub plans: Vec<IssuePlan>,
    pub page_count: usize,
    pub item_count: usize,
    pub etag: Option<String>,
    pub last_updated: Option<String>,
    /// False when a multi-page page sequence could not be proven stable; the
    /// snapshot may still retain changed issues but must not delete absent ones.
    pub deletions_allowed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IssuePollOutcome {
    NotModified,
    Snapshot(IssueSnapshot),
}

impl IssueSnapshot {
    pub fn scan(&self) -> Vec<ScannedFile> {
        self.plans
            .iter()
            .map(|plan| ScannedFile {
                relative_path: plan.relative_path.clone(),
                content_sha256: plan.snapshot_sha256,
                size_bytes: plan.size_bytes,
                calls: plan.calls.clone(),
            })
            .collect()
    }

    pub fn reconciliation(&self, manifest: &crate::ledger::Manifest) -> Reconciliation {
        let mut reconciliation = reconcile_with_removed_reason(
            manifest,
            &self.scan(),
            ISSUE_LAYOUT_VERSION,
            DeleteReason::IssueRemoved,
        );
        if !self.deletions_allowed {
            reconciliation
                .deletes
                .retain(|delete| delete.reason != DeleteReason::IssueRemoved);
            reconciliation.removed_files.clear();
            reconciliation.stats.deleted_chunks = reconciliation.deletes.len();
            reconciliation.stats.tombstoned_files = 0;
        }
        reconciliation
    }
}

pub fn poll_issue_stream(
    transport: &dyn HttpTransport,
    token: &str,
    stream: &IssueStream,
    cursor: Option<&IssueCursor>,
) -> Result<IssuePollOutcome, PollError> {
    stream.validate()?;
    match stream.provider {
        IssueProvider::GitHub => poll_github(transport, token, DEFAULT_GITHUB_API, stream, cursor),
        IssueProvider::Jira => poll_jira(transport, token, stream, cursor),
    }
}

pub fn poll_github(
    transport: &dyn HttpTransport,
    token: &str,
    api_base: &str,
    stream: &IssueStream,
    cursor: Option<&IssueCursor>,
) -> Result<IssuePollOutcome, PollError> {
    let repo = stream
        .repo
        .as_deref()
        .ok_or_else(|| PollError::Invalid("GitHub stream needs a repo".into()))?;
    let kind = stream
        .github_kind
        .ok_or_else(|| PollError::Invalid("GitHub stream needs a kind".into()))?;
    let headers = github_headers(token);
    let is_pr_stream = kind == GitHubKind::Pr;
    let fingerprint = stream_fingerprint(stream);
    let conditional = cursor
        .filter(|cursor| {
            cursor.page_count <= 1 && cursor.fingerprint.as_deref() == Some(fingerprint.as_str())
        })
        .and_then(|cursor| cursor.etag.clone());
    let per_page = GITHUB_PAGE_SIZE.min(stream.limit.max(1));
    let endpoint = if is_pr_stream { "pulls" } else { "issues" };
    let mut url = format!(
        "{}/repos/{repo}/{endpoint}?state=all&per_page={per_page}&sort=created&direction=desc",
        api_base.trim_end_matches('/')
    );
    let mut items: Vec<Value> = Vec::new();
    let mut page_count = 0usize;
    let mut matched_count = 0usize;
    let mut first_etag: Option<String> = None;
    let mut first_page_hash: Option<ContentHash> = None;
    let mut last_updated: Option<String> = None;
    let initial_url = url.clone();

    loop {
        page_count += 1;
        if page_count > MAX_PAGES {
            return Err(PollError::Truncated(stream.limit));
        }
        let mut request_headers = headers.clone();
        if page_count == 1 {
            if let Some(etag) = &conditional {
                request_headers.push(("If-None-Match", etag.clone()));
            }
        }
        let response = transport.get(&url, &request_headers)?;
        if page_count == 1 && response.status == 304 {
            return Ok(IssuePollOutcome::NotModified);
        }
        if response.status != 200 {
            return Err(PollError::from_status(
                response.status,
                &response.headers,
                response.body,
            ));
        }
        if page_count == 1 {
            first_etag = response.header("etag").map(str::to_string);
            first_page_hash = Some(ContentHash::of(&response.body));
        }
        let page: Vec<Value> = serde_json::from_slice(&response.body)
            .map_err(|error| PollError::Invalid(error.to_string()))?;
        for item in &page {
            if let Some(updated) = item.get("updated_at").and_then(Value::as_str) {
                last_updated = Some(match last_updated {
                    Some(current) if current.as_str() >= updated => current,
                    _ => updated.to_string(),
                });
            }
            if is_pr_stream || item.get("pull_request").is_none() {
                matched_count += 1;
                if matched_count > stream.limit {
                    return Err(PollError::Truncated(stream.limit));
                }
            }
        }
        items.extend(page);
        let next = response.header("link").and_then(next_link);
        let Some(next) = next else {
            break;
        };
        if !same_origin(&next, api_base) {
            return Err(PollError::Invalid(format!(
                "refusing to follow a GitHub page on another origin: {next}"
            )));
        }
        url = next;
    }

    // Offset-based pages have no snapshot token. Re-read page 1 after the last
    // page; only a byte-identical first page proves the list did not shift
    // underneath the scan, which is required before deleting absent issues.
    let deletions_allowed = if page_count <= 1 {
        true
    } else {
        let response = transport.get(&initial_url, &headers)?;
        if response.status != 200 {
            return Err(PollError::from_status(
                response.status,
                &response.headers,
                response.body,
            ));
        }
        first_page_hash == Some(ContentHash::of(&response.body))
    };

    let mut plans = Vec::new();
    for item in items.iter() {
        let is_pr = is_pr_stream || item.get("pull_request").is_some();
        if (kind == GitHubKind::Pr) != is_pr {
            continue;
        }
        let number = item
            .get("number")
            .and_then(Value::as_u64)
            .ok_or_else(|| PollError::Invalid("issue without number".into()))?;
        // `/pulls` has no comment count; `/issues` exposes it for issues.
        let comments = if is_pr || item.get("comments").and_then(Value::as_u64).unwrap_or(0) > 0 {
            github_comments(transport, api_base, &headers, repo, number)?
        } else {
            Vec::new()
        };
        let issue = GitHubIssue::from_rest(item, kind, &comments)?;
        plans.push(plan_github_issue(
            &issue,
            repo,
            stream.document_prefix.as_deref(),
            stream.chunk_size,
            stream.chunk_overlap,
        )?);
    }

    Ok(IssuePollOutcome::Snapshot(IssueSnapshot {
        plans,
        page_count,
        item_count: matched_count,
        etag: if page_count == 1 { first_etag } else { None },
        last_updated,
        deletions_allowed,
    }))
}

pub fn stream_fingerprint(stream: &IssueStream) -> String {
    let value = json!({
        "provider": stream.provider.as_str(),
        "prefix": stream.effective_prefix(),
        "chunk_size": stream.chunk_size,
        "chunk_overlap": stream.chunk_overlap,
        "issue_layout_version": ISSUE_LAYOUT_VERSION,
    });
    ContentHash::of(&serde_json::to_vec(&value).unwrap_or_default()).to_string()
}

fn same_origin(candidate: &str, base: &str) -> bool {
    let (Ok(candidate), Ok(base)) = (reqwest::Url::parse(candidate), reqwest::Url::parse(base))
    else {
        return false;
    };
    urls_same_origin(&candidate, &base)
}

fn urls_same_origin(candidate: &reqwest::Url, base: &reqwest::Url) -> bool {
    candidate.scheme() == base.scheme()
        && candidate.host_str() == base.host_str()
        && candidate.port_or_known_default() == base.port_or_known_default()
}

fn github_comments(
    transport: &dyn HttpTransport,
    api_base: &str,
    headers: &[(&str, String)],
    repo: &str,
    number: u64,
) -> Result<Vec<Value>, PollError> {
    let mut url = format!(
        "{}/repos/{repo}/issues/{number}/comments?per_page={GITHUB_PAGE_SIZE}",
        api_base.trim_end_matches('/')
    );
    let mut comments = Vec::new();
    for _ in 0..MAX_PAGES {
        let response = transport.get(&url, headers)?;
        if response.status != 200 {
            return Err(PollError::from_status(
                response.status,
                &response.headers,
                response.body,
            ));
        }
        let page: Vec<Value> = serde_json::from_slice(&response.body)
            .map_err(|error| PollError::Invalid(error.to_string()))?;
        comments.extend(page);
        match response.header("link").and_then(next_link) {
            Some(next) => {
                if !same_origin(&next, api_base) {
                    return Err(PollError::Invalid(format!(
                        "refusing to follow a GitHub comment page on another origin: {next}"
                    )));
                }
                url = next;
            }
            None => return Ok(comments),
        }
    }
    Err(PollError::Truncated(GITHUB_PAGE_SIZE))
}

fn github_headers(token: &str) -> Vec<(&'static str, String)> {
    vec![
        ("Accept", "application/vnd.github+json".to_string()),
        ("X-GitHub-Api-Version", "2022-11-28".to_string()),
        ("User-Agent", "engram-ingest".to_string()),
        ("Authorization", format!("Bearer {token}")),
    ]
}

fn next_link(header: &str) -> Option<String> {
    header.split(',').find_map(|part| {
        let part = part.trim();
        if !part.ends_with("rel=\"next\"") {
            return None;
        }
        let start = part.find('<')? + 1;
        let end = part.find('>')?;
        (end > start).then(|| part[start..end].to_string())
    })
}

pub fn poll_jira(
    transport: &dyn HttpTransport,
    token: &str,
    stream: &IssueStream,
    _cursor: Option<&IssueCursor>,
) -> Result<IssuePollOutcome, PollError> {
    let server = stream
        .jira_server
        .as_deref()
        .ok_or_else(|| PollError::Invalid("Jira stream needs a server".into()))?;
    let email = stream
        .jira_email
        .as_deref()
        .ok_or_else(|| PollError::Invalid("Jira stream needs an email".into()))?;
    let jql = stream
        .jira_jql
        .as_deref()
        .ok_or_else(|| PollError::Invalid("Jira stream needs a JQL".into()))?;
    let endpoint = format!("{}/rest/api/3/search/jql", server.trim_end_matches('/'));
    let auth = base64_basic(email, token);
    let headers = vec![
        ("Content-Type", "application/json".to_string()),
        ("Accept", "application/json".to_string()),
        ("Authorization", format!("Basic {auth}")),
    ];
    let fields = [
        "summary",
        "description",
        "status",
        "issuetype",
        "priority",
        "labels",
        "reporter",
        "created",
        "updated",
        "comment",
        "parent",
    ];
    let mut items: Vec<Value> = Vec::new();
    let mut next_page_token: Option<String> = None;
    let mut requested_tokens = BTreeSet::new();
    let mut page_count = 0usize;
    let mut last_updated: Option<String> = None;

    loop {
        page_count += 1;
        if page_count > MAX_PAGES {
            return Err(PollError::Truncated(stream.limit));
        }
        let remaining = stream.limit.saturating_sub(items.len());
        let mut payload = json!({
            "jql": jql,
            "maxResults": remaining.min(JIRA_PAGE_SIZE),
            "fields": fields,
        });
        if let Some(token) = &next_page_token {
            payload["nextPageToken"] = Value::String(token.clone());
        }
        let response = transport.post_json(&endpoint, &headers, &payload)?;
        if response.status != 200 {
            return Err(PollError::from_status(
                response.status,
                &response.headers,
                response.body,
            ));
        }
        let document: Value = serde_json::from_slice(&response.body)
            .map_err(|error| PollError::Invalid(error.to_string()))?;
        let Some(page) = document.get("issues").and_then(Value::as_array) else {
            return Err(PollError::Invalid(
                "Jira response is missing the issues array".into(),
            ));
        };
        let page = page.clone();
        let is_last = match document.get("isLast") {
            Some(value) => Some(
                value
                    .as_bool()
                    .ok_or_else(|| PollError::Invalid("Jira isLast must be a boolean".into()))?,
            ),
            None => None,
        };
        let token = document
            .get("nextPageToken")
            .and_then(Value::as_str)
            .filter(|token| !token.is_empty())
            .map(str::to_string);
        if page.is_empty() {
            if is_last == Some(true) {
                break;
            }
            if let Some(token) = token {
                if !requested_tokens.insert(token.clone()) {
                    return Err(PollError::Invalid(
                        "Jira repeated a nextPageToken that was already used".into(),
                    ));
                }
                next_page_token = Some(token);
                continue;
            }
            return Err(PollError::Invalid(
                "Jira returned an empty page without a next token".into(),
            ));
        }
        for item in &page {
            if let Some(updated) = item.pointer("/fields/updated").and_then(Value::as_str) {
                last_updated = Some(match last_updated {
                    Some(current) if current.as_str() >= updated => current,
                    _ => updated.to_string(),
                });
            }
        }
        items.extend(page);
        if is_last == Some(true) {
            break;
        }
        if is_last == Some(false) && token.is_none() {
            return Err(PollError::Invalid(
                "Jira pagination reported more pages without nextPageToken".into(),
            ));
        }
        let Some(token) = token else {
            break;
        };
        if items.len() >= stream.limit {
            return Err(PollError::Truncated(stream.limit));
        }
        if !requested_tokens.insert(token.clone()) {
            return Err(PollError::Invalid(
                "Jira repeated a nextPageToken that was already used".into(),
            ));
        }
        next_page_token = Some(token);
    }

    if items.len() > stream.limit {
        return Err(PollError::Truncated(stream.limit));
    }
    let project = stream
        .jira_project
        .as_deref()
        .ok_or_else(|| PollError::Invalid("Jira stream needs a project".into()))?;
    let mut plans = Vec::new();
    for item in &items {
        let issue = JiraIssue::from_search(item)?;
        plans.push(plan_jira_issue(
            &issue,
            project,
            stream.document_prefix.as_deref(),
            stream.chunk_size,
            stream.chunk_overlap,
        )?);
    }
    Ok(IssuePollOutcome::Snapshot(IssueSnapshot {
        plans,
        page_count,
        item_count: items.len(),
        etag: None,
        last_updated,
        deletions_allowed: true,
    }))
}

fn base64_basic(email: &str, token: &str) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let input = format!("{email}:{token}");
    let bytes = input.as_bytes();
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        output.push(TABLE[(triple >> 18) as usize & 63] as char);
        output.push(TABLE[(triple >> 12) as usize & 63] as char);
        output.push(if chunk.len() > 1 {
            TABLE[(triple >> 6) as usize & 63] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            TABLE[triple as usize & 63] as char
        } else {
            '='
        });
    }
    output
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IssueIngestOutcome {
    NotModified(IssueCursor),
    Applied {
        summary: ApplySummary,
        cursor: IssueCursor,
    },
}

#[derive(Debug, thiserror::Error)]
pub enum IssueIngestError {
    #[error(transparent)]
    Poll(#[from] PollError),
    #[error(transparent)]
    Store(#[from] StoreError),
}

/// Poll one stream, reconcile its full snapshot, enqueue ledger jobs, and only
/// then advance the cursor. A crash after apply is safe: replay is a no-op
/// because the ledger already converged. A crash before the cursor save simply
/// re-polls the same snapshot.
pub fn ingest_issue_stream(
    client: &mut Client,
    source_id: i64,
    stream: &IssueStream,
    transport: &dyn HttpTransport,
    token: &str,
    now: DateTime<Utc>,
) -> Result<IssueIngestOutcome, IssueIngestError> {
    let previous = load_issue_cursor(client, source_id)?;
    let base_revision = load_source_revision(client, source_id)?;
    let fingerprint = stream_fingerprint(stream);
    let outcome = match poll_issue_stream(transport, token, stream, previous.as_ref()) {
        Ok(outcome) => outcome,
        Err(error) => {
            let mut cursor = previous.unwrap_or_default();
            cursor.last_polled_at = Some(now);
            cursor.status = "error".to_string();
            cursor.last_error = Some(error.to_string());
            save_issue_cursor(client, source_id, &cursor, now)?;
            return Err(IssueIngestError::Poll(error));
        }
    };
    match outcome {
        IssuePollOutcome::NotModified => {
            let mut cursor = previous.unwrap_or_default();
            cursor.last_polled_at = Some(now);
            cursor.fingerprint = Some(fingerprint);
            cursor.status = "not_modified".to_string();
            cursor.last_error = None;
            save_issue_cursor(client, source_id, &cursor, now)?;
            Ok(IssueIngestOutcome::NotModified(cursor))
        }
        IssuePollOutcome::Snapshot(snapshot) => {
            // Drop the old ETag before applying so a crash between the ledger
            // commit and the new cursor cannot replay a 304 for the old list.
            let mut invalidated = previous.clone().unwrap_or_default();
            invalidated.etag = None;
            invalidated.status = "applying".to_string();
            invalidated.last_polled_at = Some(now);
            save_issue_cursor(client, source_id, &invalidated, now)?;
            let summary = match apply_snapshot(client, source_id, &snapshot, base_revision, now) {
                Ok(summary) => summary,
                Err(error) => {
                    let mut cursor = invalidated;
                    cursor.status = "error".to_string();
                    cursor.last_error = Some(error.to_string());
                    let _ = save_issue_cursor(client, source_id, &cursor, now);
                    return Err(IssueIngestError::Store(error));
                }
            };
            let cursor = IssueCursor {
                etag: snapshot.etag,
                fingerprint: Some(fingerprint),
                last_updated: snapshot.last_updated,
                last_polled_at: Some(now),
                item_count: snapshot.item_count as i32,
                page_count: snapshot.page_count as i32,
                last_error: None,
                status: "ok".to_string(),
            };
            save_issue_cursor(client, source_id, &cursor, now)?;
            Ok(IssueIngestOutcome::Applied { summary, cursor })
        }
    }
}

pub fn apply_snapshot(
    client: &mut Client,
    source_id: i64,
    snapshot: &IssueSnapshot,
    base_revision: i64,
    now: DateTime<Utc>,
) -> Result<ApplySummary, StoreError> {
    let manifest = load_manifest(client, source_id)?;
    if manifest.revision != base_revision {
        return Err(StoreError::StaleSnapshot);
    }
    let reconciliation = snapshot.reconciliation(&manifest);
    match apply_reconciliation(client, source_id, &reconciliation, now) {
        Ok(summary) => Ok(summary),
        Err(StoreError::StaleReconciliation) => Err(StoreError::StaleSnapshot),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[derive(Default)]
    struct ScriptedTransport {
        responses: Mutex<Vec<Result<HttpResponse, PollError>>>,
        requests: Mutex<Vec<(String, BTreeMap<String, String>)>>,
    }

    impl ScriptedTransport {
        fn new(responses: Vec<HttpResponse>) -> Self {
            Self {
                responses: Mutex::new(responses.into_iter().map(Ok).collect()),
                requests: Mutex::new(Vec::new()),
            }
        }

        fn with_errors(responses: Vec<Result<HttpResponse, PollError>>) -> Self {
            Self {
                responses: Mutex::new(responses),
                requests: Mutex::new(Vec::new()),
            }
        }

        fn requests(&self) -> Vec<(String, BTreeMap<String, String>)> {
            self.requests.lock().unwrap().clone()
        }
    }

    impl HttpTransport for ScriptedTransport {
        fn get(&self, url: &str, headers: &[(&str, String)]) -> Result<HttpResponse, PollError> {
            self.requests.lock().unwrap().push((
                url.to_string(),
                headers
                    .iter()
                    .map(|(name, value)| (name.to_ascii_lowercase(), value.clone()))
                    .collect(),
            ));
            self.responses.lock().unwrap().remove(0)
        }

        fn post_json(
            &self,
            url: &str,
            headers: &[(&str, String)],
            _body: &Value,
        ) -> Result<HttpResponse, PollError> {
            self.get(url, headers)
        }
    }

    fn response(status: u16, body: Value) -> HttpResponse {
        let body = serde_json::to_vec(&body).unwrap();
        HttpResponse {
            status,
            headers: BTreeMap::new(),
            body,
        }
    }

    fn github_item(number: u64, updated: &str) -> Value {
        json!({
            "number": number,
            "title": format!("Issue {number}"),
            "body": "A sufficiently long body for parity.",
            "state": "open",
            "labels": [],
            "user": {"login": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": updated,
            "comments": 0
        })
    }

    fn github_item_with_comments(number: u64, updated: &str, comments: u64) -> Value {
        let mut item = github_item(number, updated);
        item["comments"] = json!(comments);
        item
    }

    fn stream() -> IssueStream {
        IssueStream::github("p", "p-issues", "org/repo", GitHubKind::Issue, None, 100)
    }

    fn jira_stream() -> IssueStream {
        IssueStream::jira(
            "p",
            "p-issues",
            "https://jira.example",
            "a@example.com",
            "project = EX",
            "EX",
            100,
        )
    }

    #[test]
    fn github_paginates_and_records_the_page_count() {
        let mut first = response(200, json!([github_item(1, "2026-01-02T00:00:00Z")]));
        first.headers.insert(
            "link".to_string(),
            "<https://api.github.com/next>; rel=\"next\"".to_string(),
        );
        let transport = ScriptedTransport::new(vec![
            first,
            response(200, json!([github_item(2, "2026-01-03T00:00:00Z")])),
            response(200, json!([github_item(1, "2026-01-02T00:00:00Z")])),
        ]);
        let outcome = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap();
        let IssuePollOutcome::Snapshot(snapshot) = outcome else {
            panic!("expected snapshot");
        };
        assert_eq!(snapshot.page_count, 2);
        assert_eq!(snapshot.item_count, 2);
        assert_eq!(snapshot.etag, None);
        assert!(snapshot.deletions_allowed);
        assert_eq!(
            snapshot.last_updated.as_deref(),
            Some("2026-01-03T00:00:00Z")
        );
        let requests = transport.requests();
        assert!(requests[1].0.ends_with("/next"));
    }

    #[test]
    fn github_etag_304_is_not_modified_for_a_single_page() {
        let mut not_modified = HttpResponse {
            status: 304,
            headers: BTreeMap::new(),
            body: Vec::new(),
        };
        not_modified
            .headers
            .insert("etag".to_string(), "\"abc\"".to_string());
        let transport = ScriptedTransport::new(vec![not_modified]);
        let cursor = IssueCursor {
            etag: Some("\"abc\"".to_string()),
            fingerprint: Some(stream_fingerprint(&stream())),
            page_count: 1,
            ..IssueCursor::default()
        };
        let outcome = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            Some(&cursor),
        )
        .unwrap();
        assert_eq!(outcome, IssuePollOutcome::NotModified);
        let requests = transport.requests();
        assert_eq!(
            requests[0].1.get("if-none-match").map(String::as_str),
            Some("\"abc\"")
        );
    }

    #[test]
    fn github_multi_page_snapshots_do_not_store_an_etag() {
        let mut first = response(200, json!([github_item(1, "2026-01-02T00:00:00Z")]));
        first
            .headers
            .insert("etag".to_string(), "\"abc\"".to_string());
        first.headers.insert(
            "link".to_string(),
            "<https://api.github.com/next>; rel=\"next\"".to_string(),
        );
        let transport = ScriptedTransport::new(vec![
            first,
            response(200, json!([github_item(2, "2026-01-03T00:00:00Z")])),
            response(200, json!([github_item(1, "2026-01-02T00:00:00Z")])),
        ]);
        let IssuePollOutcome::Snapshot(snapshot) = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap() else {
            panic!("expected snapshot");
        };
        assert_eq!(snapshot.etag, None);
    }

    #[test]
    fn github_limit_truncation_is_an_error_not_a_delete() {
        let mut first = response(200, json!([github_item(1, "2026-01-02T00:00:00Z")]));
        first.headers.insert(
            "link".to_string(),
            "<https://api.github.com/next>; rel=\"next\"".to_string(),
        );
        let transport = ScriptedTransport::new(vec![
            first,
            response(200, json!([github_item(2, "2026-01-03T00:00:00Z")])),
        ]);
        let mut stream = stream();
        stream.limit = 1;
        let error =
            poll_github(&transport, "token", "https://api.github.com", &stream, None).unwrap_err();
        assert!(matches!(error, PollError::Truncated(1)));
    }

    #[test]
    fn rate_limit_is_transient_and_carries_retry_after() {
        let mut response = HttpResponse {
            status: 429,
            headers: BTreeMap::new(),
            body: b"{}".to_vec(),
        };
        response
            .headers
            .insert("retry-after".to_string(), "7".to_string());
        let transport = ScriptedTransport::new(vec![response]);
        let error = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap_err();
        assert!(error.is_transient());
        assert_eq!(error.retry_after(), Some(Duration::from_secs(7)));
    }

    #[test]
    fn provider_failure_aborts_before_a_snapshot_is_returned() {
        let transport =
            ScriptedTransport::with_errors(vec![Err(PollError::Transport("timeout".to_string()))]);
        let error = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap_err();
        assert!(error.is_transient());
    }

    #[test]
    fn github_filters_prs_out_of_issue_streams() {
        let issue = github_item(1, "2026-01-02T00:00:00Z");
        let mut pr = github_item(2, "2026-01-03T00:00:00Z");
        pr["pull_request"] = json!({"merged_at": null});
        let transport = ScriptedTransport::new(vec![response(200, json!([issue, pr]))]);
        let IssuePollOutcome::Snapshot(snapshot) = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap() else {
            panic!("expected snapshot");
        };
        assert_eq!(snapshot.item_count, 1);
        assert_eq!(snapshot.plans.len(), 1);
        assert_eq!(snapshot.plans[0].relative_path, "issue/1");
    }

    #[test]
    fn jira_paginates_with_next_page_token() {
        let transport = ScriptedTransport::new(vec![
            response(
                200,
                json!({
                    "issues": [{"key": "EX-1", "fields": {"summary": "S", "description": {"type": "doc", "content": []}, "status": {"name": "Open"}, "issuetype": {"name": "Story"}, "created": "2026-01-01T00:00:00.000+0000", "updated": "2026-01-02T00:00:00.000+0000"}}],
                    "isLast": false,
                    "nextPageToken": "next"
                }),
            ),
            response(
                200,
                json!({
                    "issues": [{"key": "EX-2", "fields": {"summary": "S2", "description": {"type": "doc", "content": []}, "status": {"name": "Done"}, "issuetype": {"name": "Task"}, "created": "2026-01-01T00:00:00.000+0000", "updated": "2026-01-03T00:00:00.000+0000"}}],
                    "isLast": true
                }),
            ),
        ]);
        let IssuePollOutcome::Snapshot(snapshot) =
            poll_jira(&transport, "token", &jira_stream(), None).unwrap()
        else {
            panic!("expected snapshot");
        };
        assert_eq!(snapshot.page_count, 2);
        assert_eq!(snapshot.item_count, 2);
        assert_eq!(
            snapshot.last_updated.as_deref(),
            Some("2026-01-03T00:00:00.000+0000")
        );
    }

    #[test]
    fn jira_limit_truncation_is_an_error() {
        let transport = ScriptedTransport::new(vec![response(
            200,
            json!({
                "issues": [{"key": "EX-1", "fields": {"summary": "S", "description": {"type": "doc", "content": []}, "status": {"name": "Open"}, "issuetype": {"name": "Story"}}}],
                "isLast": false,
                "nextPageToken": "next"
            }),
        )]);
        let mut stream = jira_stream();
        stream.limit = 1;
        let error = poll_jira(&transport, "token", &stream, None).unwrap_err();
        assert!(matches!(error, PollError::Truncated(1)));
    }

    #[test]
    fn next_link_parser_handles_multiple_relations() {
        let header =
            "<https://api.github.com/a>; rel=\"prev\", <https://api.github.com/b>; rel=\"next\"";
        assert_eq!(
            next_link(header).as_deref(),
            Some("https://api.github.com/b")
        );
        assert_eq!(next_link("<https://x>; rel=\"last\""), None);
    }

    #[test]
    fn source_identity_is_stable_per_stream() {
        let github = stream();
        assert!(github.identity().starts_with("github:org/repo:issue:"));
        let record = github.source_record();
        assert_eq!(record.source_kind, "github_issue");
        assert_eq!(record.document_format, "issue");
        let jira = jira_stream();
        assert!(jira.identity().starts_with("jira:https://jira.example:EX:"));
        let mut other = jira.clone();
        other.jira_jql = Some("project = OTHER".to_string());
        assert_ne!(jira.identity(), other.identity());
    }

    #[test]
    fn streams_validate_required_fields() {
        let mut github = stream();
        github.repo = None;
        assert!(matches!(github.validate(), Err(PollError::Invalid(_))));
        let mut jira = jira_stream();
        jira.jira_jql = None;
        assert!(matches!(jira.validate(), Err(PollError::Invalid(_))));
    }

    #[test]
    fn base64_matches_standard_encoding() {
        assert_eq!(base64_basic("a@b.c", "token"), "YUBiLmM6dG9rZW4=");
        assert_eq!(base64_basic("ab", "c"), "YWI6Yw==");
    }

    #[test]
    fn transient_classification_and_retry_after_cover_all_variants() {
        assert!(PollError::Transport("x".into()).is_transient());
        assert!(PollError::RateLimited { retry_after: None }.is_transient());
        assert!(PollError::Http {
            status: 500,
            body: String::new()
        }
        .is_transient());
        assert!(!PollError::Http {
            status: 404,
            body: String::new()
        }
        .is_transient());
        assert!(!PollError::Invalid("x".into()).is_transient());
        assert_eq!(
            PollError::Http {
                status: 404,
                body: String::new()
            }
            .retry_after(),
            None
        );
    }

    #[test]
    fn status_classification_handles_rate_limit_variants() {
        let mut exhausted = BTreeMap::new();
        exhausted.insert("x-ratelimit-remaining".to_string(), "0".to_string());
        assert!(matches!(
            PollError::from_status(403, &exhausted, Vec::new()),
            PollError::RateLimited { .. }
        ));
        let mut retry = BTreeMap::new();
        retry.insert("retry-after".to_string(), "9".to_string());
        assert!(matches!(
            PollError::from_status(403, &retry, Vec::new()),
            PollError::RateLimited {
                retry_after: Some(_)
            }
        ));
        assert!(matches!(
            PollError::from_status(403, &BTreeMap::new(), Vec::new()),
            PollError::Http { status: 403, .. }
        ));
        let body = "x".repeat(400);
        let error = PollError::from_status(500, &BTreeMap::new(), body.into_bytes());
        match error {
            PollError::Http { body, .. } => assert_eq!(body.chars().count(), 300),
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn github_comments_paginate() {
        let mut comments_page = response(
            200,
            json!([{
                "id": 1,
                "body": "A long enough first comment.",
                "author_association": "MEMBER",
                "user": {"login": "alice"}
            }]),
        );
        comments_page.headers.insert(
            "link".to_string(),
            "<https://api.github.com/comments-next>; rel=\"next\"".to_string(),
        );
        let transport = ScriptedTransport::new(vec![
            response(
                200,
                json!([github_item_with_comments(1, "2026-01-02T00:00:00Z", 1)]),
            ),
            comments_page,
            response(
                200,
                json!([{
                    "id": 2,
                    "body": "A long enough second comment.",
                    "author_association": "OWNER",
                    "user": {"login": "bob"}
                }]),
            ),
        ]);
        let IssuePollOutcome::Snapshot(snapshot) = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap() else {
            panic!("expected snapshot");
        };
        assert_eq!(snapshot.plans.len(), 1);
        assert_eq!(snapshot.plans[0].calls.len(), 3);
        assert_eq!(
            snapshot.plans[0].calls[2].document_id,
            "repo-issue-1-comment1"
        );
    }

    #[test]
    fn github_comment_fetch_failure_aborts_the_snapshot() {
        let transport = ScriptedTransport::new(vec![
            response(
                200,
                json!([github_item_with_comments(1, "2026-01-02T00:00:00Z", 1)]),
            ),
            response(500, json!({"message": "comments"})),
        ]);
        let error = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap_err();
        assert!(matches!(error, PollError::Http { status: 500, .. }));
    }

    #[test]
    fn jira_http_and_parse_failures_are_poll_errors() {
        let transport = ScriptedTransport::new(vec![response(500, json!({"message": "boom"}))]);
        let error = poll_jira(&transport, "token", &jira_stream(), None).unwrap_err();
        assert!(matches!(error, PollError::Http { status: 500, .. }));

        let transport = ScriptedTransport::new(vec![HttpResponse {
            status: 200,
            headers: BTreeMap::new(),
            body: b"not json".to_vec(),
        }]);
        let error = poll_jira(&transport, "token", &jira_stream(), None).unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn provider_names_and_pr_streams() {
        assert_eq!(IssueProvider::GitHub.as_str(), "github");
        assert_eq!(IssueProvider::Jira.as_str(), "jira");
        let pr = IssueStream::github("p", "b", "o/r", GitHubKind::Pr, Some("gh"), 10)
            .with_document_prefix(Some("gh"));
        assert_eq!(pr.source_kind, "github_pr");
        assert_eq!(pr.document_prefix.as_deref(), Some("gh"));
    }

    #[test]
    fn validation_rejects_zero_limit_and_missing_kind() {
        let mut zero = stream();
        zero.limit = 0;
        assert!(matches!(zero.validate(), Err(PollError::Invalid(_))));
        let mut missing_kind = stream();
        missing_kind.github_kind = None;
        assert!(matches!(
            missing_kind.validate(),
            Err(PollError::Invalid(_))
        ));
    }

    #[test]
    fn invalid_json_list_is_a_poll_error() {
        let transport = ScriptedTransport::new(vec![HttpResponse {
            status: 200,
            headers: BTreeMap::new(),
            body: b"not json".to_vec(),
        }]);
        let error = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn pr_streams_use_pulls_and_read_top_level_merged_at() {
        let mut pr = github_item(1, "2026-01-02T00:00:00Z");
        pr["merged_at"] = json!("2026-01-03T00:00:00Z");
        pr["state"] = json!("closed");
        let transport =
            ScriptedTransport::new(vec![response(200, json!([pr])), response(200, json!([]))]);
        let mut pr_stream = stream();
        pr_stream.github_kind = Some(GitHubKind::Pr);
        pr_stream.source_kind = "github_pr".to_string();
        let IssuePollOutcome::Snapshot(snapshot) = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &pr_stream,
            None,
        )
        .unwrap() else {
            panic!("expected snapshot");
        };
        assert!(transport.requests()[0].0.contains("/pulls"));
        assert_eq!(snapshot.item_count, 1);
        assert_eq!(snapshot.plans[0].calls[0].metadata["state"], "merged");
    }

    #[test]
    fn issue_limit_counts_only_issues_not_prs() {
        let mut pr = github_item(2, "2026-01-03T00:00:00Z");
        pr["pull_request"] = json!({"merged_at": null});
        let mut issue_stream = stream();
        issue_stream.limit = 1;
        let transport = ScriptedTransport::new(vec![response(
            200,
            json!([github_item(1, "2026-01-02T00:00:00Z"), pr]),
        )]);
        let IssuePollOutcome::Snapshot(snapshot) = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &issue_stream,
            None,
        )
        .unwrap() else {
            panic!("expected snapshot");
        };
        assert_eq!(snapshot.item_count, 1);
        assert_eq!(snapshot.plans.len(), 1);
    }

    #[test]
    fn jira_missing_issues_is_invalid_not_an_empty_snapshot() {
        let transport = ScriptedTransport::new(vec![response(200, json!({"isLast": true}))]);
        let error = poll_jira(&transport, "token", &jira_stream(), None).unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn jira_false_is_last_without_a_token_is_invalid() {
        let transport = ScriptedTransport::new(vec![response(
            200,
            json!({
                "issues": [{"key": "EX-1", "fields": {"summary": "S", "description": {"type": "doc", "content": []}, "status": {"name": "Open"}, "issuetype": {"name": "Story"}}}],
                "isLast": false
            }),
        )]);
        let error = poll_jira(&transport, "token", &jira_stream(), None).unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn cross_origin_next_link_is_rejected() {
        let mut first = response(200, json!([github_item(1, "2026-01-02T00:00:00Z")]));
        first.headers.insert(
            "link".to_string(),
            "<https://evil.example/next>; rel=\"next\"".to_string(),
        );
        let transport = ScriptedTransport::new(vec![first]);
        let error = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn cross_origin_comment_link_is_rejected() {
        let mut comments_page = response(
            200,
            json!([{
                "id": 1,
                "body": "A long enough first comment.",
                "author_association": "MEMBER",
                "user": {"login": "alice"}
            }]),
        );
        comments_page.headers.insert(
            "link".to_string(),
            "<https://evil.example/comments-next>; rel=\"next\"".to_string(),
        );
        let transport = ScriptedTransport::new(vec![
            response(
                200,
                json!([github_item_with_comments(1, "2026-01-02T00:00:00Z", 1)]),
            ),
            comments_page,
        ]);
        let error = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn jira_empty_page_with_a_token_continues() {
        let transport = ScriptedTransport::new(vec![
            response(
                200,
                json!({"issues": [], "isLast": false, "nextPageToken": "next"}),
            ),
            response(
                200,
                json!({
                    "issues": [{"key": "EX-1", "fields": {"summary": "S", "description": {"type": "doc", "content": []}, "status": {"name": "Open"}, "issuetype": {"name": "Story"}, "updated": "2026-01-02T00:00:00.000+0000"}}],
                    "isLast": true
                }),
            ),
        ]);
        let IssuePollOutcome::Snapshot(snapshot) =
            poll_jira(&transport, "token", &jira_stream(), None).unwrap()
        else {
            panic!("expected snapshot");
        };
        assert_eq!(snapshot.item_count, 1);
        assert_eq!(snapshot.page_count, 2);
    }

    #[test]
    fn jira_empty_page_without_a_token_is_invalid() {
        let transport =
            ScriptedTransport::new(vec![response(200, json!({"issues": [], "isLast": false}))]);
        let error = poll_jira(&transport, "token", &jira_stream(), None).unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn jira_non_boolean_is_last_is_invalid() {
        let transport =
            ScriptedTransport::new(vec![response(200, json!({"issues": [], "isLast": "yes"}))]);
        let error = poll_jira(&transport, "token", &jira_stream(), None).unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn jira_default_and_explicit_prefix_share_identity() {
        let default = jira_stream();
        let explicit = jira_stream().with_document_prefix(Some("jira"));
        assert_eq!(default.identity(), explicit.identity());
        assert_eq!(stream_fingerprint(&default), stream_fingerprint(&explicit));
    }

    #[test]
    fn unstable_multi_page_snapshot_disables_deletions() {
        let mut first = response(200, json!([github_item(1, "2026-01-02T00:00:00Z")]));
        first.headers.insert(
            "link".to_string(),
            "<https://api.github.com/next>; rel=\"next\"".to_string(),
        );
        let transport = ScriptedTransport::new(vec![
            first,
            response(200, json!([github_item(2, "2026-01-03T00:00:00Z")])),
            response(200, json!([github_item(99, "2026-01-04T00:00:00Z")])),
        ]);
        let IssuePollOutcome::Snapshot(snapshot) = poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream(),
            None,
        )
        .unwrap() else {
            panic!("expected snapshot");
        };
        assert!(!snapshot.deletions_allowed);
    }

    #[test]
    fn repeated_jira_continuation_token_is_rejected() {
        let transport = ScriptedTransport::new(vec![
            response(
                200,
                json!({"issues": [], "isLast": false, "nextPageToken": "same"}),
            ),
            response(
                200,
                json!({"issues": [], "isLast": false, "nextPageToken": "same"}),
            ),
        ]);
        let error = poll_jira(&transport, "token", &jira_stream(), None).unwrap_err();
        assert!(matches!(error, PollError::Invalid(_)));
    }

    #[test]
    fn bank_streams_reject_shared_jira_prefixes_and_duplicate_identities() {
        let a = jira_stream();
        let b = jira_stream();
        assert!(validate_bank_streams(&[a.clone(), b]).is_err());
        let b = jira_stream().with_document_prefix(Some("other"));
        assert!(validate_bank_streams(&[a, b]).is_ok());
        let first = stream();
        let second = stream();
        assert!(validate_bank_streams(&[first, second]).is_err());
    }

    #[test]
    fn rate_limit_reset_is_used_when_retry_after_is_absent() {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let mut headers = BTreeMap::new();
        headers.insert("x-ratelimit-remaining".to_string(), "0".to_string());
        headers.insert("x-ratelimit-reset".to_string(), (now + 60).to_string());
        let error = PollError::from_status(403, &headers, Vec::new());
        let retry = error.retry_after().expect("reset deadline");
        assert!(retry >= Duration::from_secs(55) && retry <= Duration::from_secs(60));
    }

    #[test]
    fn fingerprint_change_disables_the_etag_fast_path() {
        let response = response(200, json!([github_item(1, "2026-01-02T00:00:00Z")]));
        let transport = ScriptedTransport::new(vec![response]);
        let cursor = IssueCursor {
            etag: Some("\"abc\"".to_string()),
            fingerprint: Some("different".to_string()),
            page_count: 1,
            ..IssueCursor::default()
        };
        let mut stream = stream();
        stream.document_prefix = Some("changed".to_string());
        poll_github(
            &transport,
            "token",
            "https://api.github.com",
            &stream,
            Some(&cursor),
        )
        .unwrap();
        assert_eq!(transport.requests()[0].1.get("if-none-match"), None);
    }
}
